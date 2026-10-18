"""Explicit reviewed-source API/worker composition; startup never queries sources."""
import asyncio
import logging
import os
from pathlib import Path
from uuid import uuid4

from gateway.adapters.base import StaticBindingResolver
from gateway.api.app import create_app
from gateway.api.audit import AuditReadService
from gateway.api.catalog import CatalogService
from gateway.api.choices import ChoiceService
from gateway.api.cursors import PageCursorCodec
from gateway.api.drafts import DraftDiscoveryService
from gateway.api.models import ReadinessView
from gateway.api.readiness import ReadinessService
from gateway.api.services import Services
from gateway.api.transport import TransportSettings
from gateway.auth.principals import PrincipalStore
from gateway.auth.settings import validate_mode
from gateway.auth.tokens import Authenticator
from gateway.contracts.clock import SystemClock
from gateway.execution.results import ResultService
from gateway.execution.service import ExecutionService
from gateway.logging import configure_logging
from gateway.mcp.server import MCPSettings,create_mcp_server,mount_mcp
from gateway.pilot_config import _read_file
from gateway.pilot_runtime import PilotApplication,_CLOSE_SECONDS,_close_resources,_stop_signals,_clear_stop_signals,_idle
from gateway.policy.audit import AuditWriter
from gateway.policy.service import PolicyService
from gateway.publishing.service import PublishingService
from gateway.publishing.test_runner import FixtureRunner
from gateway.registry.repository import Registry
from gateway.runtime_config import read_secret,secret_key
from gateway.source_pilot_assets import load_source_assets
from gateway.source_pilot_config import load_source_pilot_config
from gateway.source_access_operator import validate_configured_approvals, validate_active_approvals, load_registry_approvals

_CONFIG_ERROR='Reviewed-source runtime configuration is unavailable.'
_STARTUP_ERROR='Reviewed-source startup failed.'
_SHUTDOWN_ERROR='Reviewed-source shutdown failed.'


def _identity(config):
    settings=config.identity_settings();validate_mode(settings)
    if (settings.mode!='production' or settings.source!='reviewed_sources' or settings.trust_forwarded_headers
        or settings.oidc_audience!=config.mcp_resource_url):raise ValueError(_CONFIG_ERROR)
    return settings


def source_secret_resolver(sources,context):
    if context not in {'consumer','fixture_test'}:raise ValueError(_CONFIG_ERROR)
    refs={}
    for source in sources.sources:
        if source.provider=='domo' and source.context==context:
            refs[source.auth.client_id_ref]=256;refs[source.auth.client_secret_ref]=8192
    def read(path):
        try:
            maximum=refs[path]
            value=_read_file(path,maximum+1).decode('ascii').removesuffix('\n')
            if not 1<=len(value)<=maximum or any(not 33<=ord(c)<=126 for c in value):raise ValueError
            return value
        except (KeyError,ValueError,TypeError,UnicodeError,OSError):
            raise ValueError('Required source credential is unavailable') from None
    return read


class SourceReadiness:
    """Migration/binding/epoch checks only. Ready never means source-qualified."""
    def __init__(self,registry,assets,config,approval_packages=()):
        self.registry,self.assets,self.config=registry,assets,config
        self.approval_packages=approval_packages
        self.lock=asyncio.Lock()

    async def check(self):
        from sqlalchemy import select,text
        from gateway.registry.models import BindingRevision,PolicyRow
        try:
            async with asyncio.timeout(3):
                async with self.lock:
                    await validate_active_approvals(self.registry,self.approval_packages)
                    async with self.registry.transaction() as uow:
                        heads=(await uow.session.execute(text('SELECT version_num FROM public.alembic_version'))).scalars().all()
                        if heads!=ReadinessService.expected_heads():return ReadinessView(ready=False)
                        if await uow.session.scalar(text('SELECT gateway.source_check_epoch(:epoch)'),{'epoch':self.config.deployment_epoch}) is not True:
                            return ReadinessView(ready=False)
                        for binding in self.assets.bindings:
                            row=await uow.session.scalar(select(BindingRevision).where(BindingRevision.binding_id==binding.binding_id,
                                BindingRevision.revision==binding.revision))
                            if row is None or row.content_digest!=binding.digest:return ReadinessView(ready=False)
                        for retained in self.assets.packages.packages:
                            if await uow.session.get(PolicyRow,retained.package.manifest.policy_id) is None:return ReadinessView(ready=False)
            return ReadinessView(ready=True)
        except Exception:return ReadinessView(ready=False)


class SourceRuntime:
    def __init__(self,services,adapter,adapter_factory,lifecycle,owned):
        self.services,self.adapter,self.adapter_factory,self.source_lifecycle=services,adapter,adapter_factory,lifecycle
        self._owned=tuple(owned);self._closed=False

    async def close(self):
        if self._closed:return
        self._closed=True
        await _close_resources(*reversed(self._owned))


def _configuration(config=None,root=None):
    if config is None:
        path=os.environ.get('GATEWAY_SOURCE_PILOT_CONFIG_FILE')
        config=load_source_pilot_config(path);root=Path(path).parent
    if root is None or not Path(root).is_absolute():raise ValueError(_CONFIG_ERROR)
    return config,Path(root)


async def build_runtime(config=None,*,root=None,role='api'):
    owned=[]
    try:
        if role not in {'api','worker'}:raise ValueError(_CONFIG_ERROR)
        config,root=_configuration(config,root)
        settings=_identity(config)
        assets=load_source_assets(config,root)  # Entire composition before credentials or DB engines.
        configured_approvals=validate_configured_approvals(config,assets,root)
        if role=='api':
            from gateway.source_pilot_bootstrap import validate_service_credentials
            validate_service_credentials(assets.identities,config)
        from gateway.source_lifecycle import SourceLifecycle
        from gateway.adapters.dispatch import SourceDispatchAdapter
        config.validate_files(role='app')
        clock=SystemClock();registry=Registry(config.database_url('app'));owned.append(registry)
        # Registry authentication is necessary; provider credentials/clients are not.
        approval_packages=await load_registry_approvals(registry,configured_approvals)
        principals=PrincipalStore(registry.sessions)
        from gateway.source_access import SourceAccessService
        audit=AuditWriter(secret_key('audit_hmac'))
        policy=PolicyService(registry,principals,audit,source_access=SourceAccessService(registry,audit))
        context='fixture_test' if role=='api' else 'consumer'
        provider_registries={}
        if any(s.provider=='domo' for s in assets.sources.sources):
            name='domo_fixture' if role=='api' else 'domo_consumer'
            config.validate_files(role=name)
            provider=Registry(config.database_url(name));owned.append(provider)
            provider_registries[('domo',context)]=provider
        lifecycle=SourceLifecycle(registry,policy,assets.sources,deployment_epoch=config.deployment_epoch,
            provider_registries=provider_registries)
        bindings=StaticBindingResolver(list(assets.bindings))
        providers={s.provider for s in assets.sources.sources}
        def adapter_factory():
            bq=domo=None
            if 'bigquery' in providers:
                from gateway.adapters.bigquery_runtime import BigQueryServingAdapter
                allowed={assets.bundles.resolve(b.model_bundle_digest).secret_ref for b in assets.bindings
                    if b.adapter_revision=='cube-bigquery-v1' and b.context==context}
                def signing(ref):
                    if ref not in allowed:raise ValueError('Source signing context is unavailable')
                    return read_secret(ref)
                bq=BigQueryServingAdapter(catalog=assets.bundles,bindings=bindings,sources=assets.sources,
                    secret_resolver=signing,clock=clock)
            if 'domo' in providers:
                from gateway.adapters.domo_serving import DomoRuntimeAdapter
                domo=DomoRuntimeAdapter(bindings=bindings,sources=assets.sources,statements=dict(assets.statements),
                    lifecycle=lifecycle,secret_resolver=source_secret_resolver(assets.sources,context),context=context,clock=clock)
            return SourceDispatchAdapter(bindings=bindings,bigquery_adapter=bq,domo_adapter=domo)
        adapter=adapter_factory();owned.append(adapter)
        authenticator=Authenticator(settings,principals,clock=clock);owned.append(authenticator)
        cursors=PageCursorCodec(secret_key('cursor_key'),clock)
        def fixture_adapter_factory():
            if role!='api':raise ValueError('Worker process has no fixture source authority')
            return adapter_factory()
        publishing=PublishingService(registry=registry,policy=policy,bindings=bindings,bundles=assets.bundles,
            fixture_runner=FixtureRunner(registry=registry,policy=policy,bindings=bindings,bundles=assets.bundles,
                catalog_provider=assets.fixtures,adapter_factory=fixture_adapter_factory,clock=clock,source_lifecycle=lifecycle))
        services=Services(authenticator=authenticator,registry=registry,policy=policy,bindings=bindings,bundles=assets.bundles,
            execution=ExecutionService(registry=registry,policy=policy,bindings=bindings,clock=clock,source_lifecycle=lifecycle),
            results=ResultService(registry,policy,bindings,clock),publishing=publishing,clock=clock,
            catalog=CatalogService(registry=registry,policy=policy,bindings=bindings,cursors=cursors),
            drafts=DraftDiscoveryService(registry=registry,policy=policy,publishing=publishing,cursors=cursors),
            choices=ChoiceService(registry=registry,policy=policy,bindings=bindings,bundles=assets.bundles,cursors=cursors,
                templates=tuple(p.package.manifest for p in assets.packages.packages)),
            audit_reader=AuditReadService(registry=registry,policy=policy,cursors=cursors,clock=clock),
            readiness=SourceReadiness(registry,assets,config,approval_packages))
        return SourceRuntime(services,adapter,adapter_factory,lifecycle,owned)
    except BaseException as error:
        try:await _close_resources(*reversed(owned))
        except Exception:pass
        if isinstance(error,asyncio.CancelledError):raise
        raise ValueError(_CONFIG_ERROR) from None


class SourceApplication(PilotApplication):
    def __init__(self,config,root):
        super().__init__(config);self.root=root

    async def _lifespan(self, receive, send):
        if self._state != 'new':
            await send({'type':'lifespan.startup.failed', 'message':_STARTUP_ERROR})
            return
        runtime = manager = None
        manager_entered = False
        startup_sent = False
        failure = None
        self._state = 'starting'
        try:
            message = await receive()
            if message['type'] != 'lifespan.startup':
                raise ValueError(_STARTUP_ERROR)
            runtime = await build_runtime(self.config,root=self.root)
            server = create_mcp_server(runtime.services, settings=MCPSettings(
                resource_url=self.config.mcp_resource_url, allowed_hosts=self.config.mcp_allowed_hosts,
                allowed_origins=self.config.allowed_origins))
            application = create_app(runtime.services, settings=TransportSettings(
                allowed_hosts=self.config.rest_allowed_hosts, allowed_origins=self.config.allowed_origins,
                authenticated_prefixes=('/v1', '/mcp')))
            mount_mcp(application, server)
            manager = server.session_manager.run()
            await manager.__aenter__()
            manager_entered = True
            self._application = application
            self._state = 'started'
            await send({'type':'lifespan.startup.complete'})
            startup_sent = True
            message = await receive()
            if message['type'] != 'lifespan.shutdown':
                raise ValueError(_SHUTDOWN_ERROR)
        except BaseException as error:
            failure = error
        finally:
            self._state = 'stopping'
            self._application = None
            if manager_entered:
                try:
                    # Exit in the same task that entered the SDK's task group.
                    async with asyncio.timeout(_CLOSE_SECONDS):
                        await manager.__aexit__(None, None, None)
                except BaseException as error:
                    failure = failure or error
            if runtime is not None:
                try:
                    await runtime.close()
                except BaseException as error:
                    failure = failure or error
            self._state = 'stopped'
        if isinstance(failure, asyncio.CancelledError):
            raise failure
        if failure is not None:
            await send({'type':'lifespan.shutdown.failed' if startup_sent else 'lifespan.startup.failed',
                'message':_SHUTDOWN_ERROR if startup_sent else _STARTUP_ERROR})
        elif startup_sent:
            await send({'type':'lifespan.shutdown.complete'})



def app_factory():
    configure_logging()
    try:
        config,root=_configuration();_identity(config)
        MCPSettings(resource_url=config.mcp_resource_url,allowed_hosts=config.mcp_allowed_hosts,allowed_origins=config.allowed_origins)
        TransportSettings(allowed_hosts=config.rest_allowed_hosts,allowed_origins=config.allowed_origins,authenticated_prefixes=('/v1','/mcp'))
        return SourceApplication(config,root)
    except Exception:raise ValueError(_CONFIG_ERROR) from None


async def worker_main():
    from gateway.execution.queue import ExecutionQueue
    from gateway.execution.worker import Worker
    runtime=await build_runtime(role='worker');loop,installed=None,[]
    try:
        stop=asyncio.Event();loop,installed=_stop_signals(stop);services=runtime.services
        queue=ExecutionQueue(services.registry,services.policy,services.bindings,clock=services.clock,source_lifecycle=runtime.source_lifecycle)
        worker=Worker(queue,adapter_factory=runtime.adapter_factory,worker_id='source-worker-'+uuid4().hex,clock=services.clock)
        while not stop.is_set():
            try:worked=await worker.run_once()
            except Exception:logging.error('',extra={'event':'operation_failed'});worked=False
            if not worked:await _idle(stop,1)
    finally:
        try:_clear_stop_signals(loop,installed)
        finally:await runtime.close()


async def retention_main():
    from gateway.execution.retention import RetentionSweep
    config,_=_configuration();_identity(config);owned=[];loop,installed=None,[]
    try:
        config.validate_files(role='app');config.validate_files(role='audit_retention')
        app=Registry(config.database_url('app'));owned.append(app)
        audit=Registry(config.database_url('audit_retention'));owned.append(audit)
        sweep=RetentionSweep(app,audit);stop=asyncio.Event();loop,installed=_stop_signals(stop)
        while not stop.is_set():
            try:await sweep.run_once(SystemClock().now())
            except Exception:logging.error('',extra={'event':'operation_failed'})
            await _idle(stop,60)
    finally:
        try:_clear_stop_signals(loop,installed)
        finally:await _close_resources(*reversed(owned))


def main(argv=None):
    from scripts.spaceport import Parser
    configure_logging()
    try:
        parser=Parser(description='Explicit reviewed-source process');parser.add_argument('operation',choices=['worker','retention'])
        args=parser.parse_args(argv)
        asyncio.run(worker_main() if args.operation=='worker' else retention_main())
    except Exception:
        logging.error('',extra={'event':'operation_failed'});return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
