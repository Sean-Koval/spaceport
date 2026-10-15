"""Explicit OIDC/synthetic pilot composition, separate from the local demo.

This profile opens no source or registry during configuration validation and never
provisions identities, migrates a database, or enables BigQuery/Domo. The host
must run ASGI lifespan; requests are unavailable until startup has succeeded.
"""
import asyncio
import logging
import signal
from uuid import uuid4

from gateway.adapters.base import StaticBindingResolver
from gateway.adapters.cube import CubeAdapter
from gateway.api.app import create_app
from gateway.api.audit import AuditReadService
from gateway.api.catalog import CatalogService
from gateway.api.choices import ChoiceService
from gateway.api.cursors import PageCursorCodec
from gateway.api.drafts import DraftDiscoveryService
from gateway.api.readiness import ReadinessService
from gateway.api.services import Services
from gateway.api.transport import TransportSettings, error_response
from gateway.auth.principals import PrincipalStore
from gateway.auth.settings import validate_mode
from gateway.auth.tokens import Authenticator
from gateway.contracts.clock import SystemClock
from gateway.execution.queue import ExecutionQueue
from gateway.execution.results import ResultService
from gateway.execution.retention import RetentionSweep
from gateway.execution.service import ExecutionService
from gateway.execution.worker import Worker
from gateway.logging import configure_logging
from gateway.mcp.server import MCPSettings, create_mcp_server, mount_mcp
from gateway.policy.audit import AuditWriter
from gateway.policy.service import PolicyService
from gateway.publishing.service import PublishingService
from gateway.publishing.test_runner import FixtureRunner
from gateway.registry.repository import Registry
from gateway.runtime import Runtime
from gateway.runtime_config import read_secret, secret_key

_CONFIG_ERROR = 'Pilot runtime configuration is unavailable.'
_STARTUP_ERROR = 'Pilot runtime startup failed.'
_SHUTDOWN_ERROR = 'Pilot runtime shutdown failed.'
_CLOSE_SECONDS = 2.0


def load_pilot_config():
    # Lazy import keeps the demo entrypoint independent of the pilot profile.
    from gateway.pilot_config import load_pilot_config as load
    return load()


async def _close_resources(*resources):
    """Attempt every owned close exactly once, with bounded redacted failures."""
    failed = False
    cancelled = None
    for resource in resources:
        try:
            async with asyncio.timeout(_CLOSE_SECONDS):
                await resource.close()
        except asyncio.CancelledError as error:
            cancelled = error
        except Exception:
            failed = True
    if cancelled is not None:
        raise cancelled
    if failed:
        raise RuntimeError(_SHUTDOWN_ERROR) from None


class PilotRuntime(Runtime):
    """The existing service/runtime interface with idempotent owned cleanup."""
    def __init__(self, services, adapter, adapter_factory):
        super().__init__(services, adapter, adapter_factory)
        self._closed = False

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await _close_resources(self.adapter, self.services.authenticator, self.services.registry)


def _identity(config):
    settings = config.identity_settings()
    validate_mode(settings)
    if (settings.mode != 'production' or settings.source != 'synthetic_postgres'
        or settings.trust_forwarded_headers or settings.oidc_audience != config.mcp_resource_url):
        raise ValueError(_CONFIG_ERROR)
    return settings


async def build_runtime(config=None) -> PilotRuntime:
    """Validate all local inputs before allocating resources; no bootstrap writes."""
    owned = []
    try:
        config = config if config is not None else load_pilot_config()
        config.validate_files(role='app')
        settings = _identity(config)
        assets = config.load_assets()
        url = config.database_url('app')
        audit_key, cursor_key = secret_key('audit_hmac'), secret_key('cursor_key')
        clock = SystemClock()
        registry = Registry(url)
        owned.append(registry)
        principals = PrincipalStore(registry.sessions)
        audit = AuditWriter(audit_key)
        policy = PolicyService(registry, principals, audit)
        bindings = StaticBindingResolver(list(assets.bindings))

        def adapter_factory():
            return CubeAdapter(catalog=assets.bundles, bindings=bindings,
                secret_resolver=read_secret, clock=clock)

        adapter = adapter_factory()
        owned.append(adapter)
        authenticator = Authenticator(settings, principals, clock=clock)
        owned.append(authenticator)
        cursors = PageCursorCodec(cursor_key, clock)
        publishing = PublishingService(registry=registry, policy=policy, bindings=bindings,
            bundles=assets.bundles, fixture_runner=FixtureRunner(registry=registry, policy=policy,
                bindings=bindings, bundles=assets.bundles, catalog_provider=lambda: assets.fixture_catalog,
                adapter_factory=adapter_factory, clock=clock))
        services = Services(authenticator=authenticator, registry=registry, policy=policy,
            bindings=bindings, bundles=assets.bundles,
            execution=ExecutionService(registry=registry, policy=policy, bindings=bindings, clock=clock),
            results=ResultService(registry, policy, bindings, clock), publishing=publishing, clock=clock,
            catalog=CatalogService(registry=registry, policy=policy, bindings=bindings, cursors=cursors),
            drafts=DraftDiscoveryService(registry=registry, policy=policy, publishing=publishing, cursors=cursors),
            choices=ChoiceService(registry=registry, policy=policy, bindings=bindings, bundles=assets.bundles,
                cursors=cursors, templates=(assets.manifest,)),
            audit_reader=AuditReadService(registry=registry, policy=policy, cursors=cursors, clock=clock),
            readiness=ReadinessService(registry=registry, policy=policy, adapter=adapter, bindings=bindings,
                bundles=assets.bundles, clock=clock, probe_principal_id=config.probe_principal_id,
                probe_product_id='revenue'))
        return PilotRuntime(services, adapter, adapter_factory)
    except BaseException as error:
        try:
            await _close_resources(*reversed(owned))
        except Exception:
            pass
        if isinstance(error, asyncio.CancelledError):
            raise
        raise ValueError(_CONFIG_ERROR) from None


class PilotApplication:
    """One ASGI lifespan owns the initialized application and all its resources."""
    def __init__(self, config):
        self.config = config
        self._application = None
        self._state = 'new'

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            return await self._lifespan(receive, send)
        if scope['type'] == 'http':
            if self._state != 'started' or self._application is None:
                return await error_response('UPSTREAM_UNAVAILABLE', 'pilot-runtime')(scope, receive, send)
            # The shared REST guard validates hostnames; this profile also pins
            # the authority/port, including the unauthenticated metadata route.
            hosts = [value for name, value in scope.get('headers', ()) if name.lower() == b'host']
            if len(hosts) != 1 or hosts[0] not in {
                host.encode('ascii') for host in self.config.mcp_allowed_hosts
            }:
                return await error_response('INVALID_INPUT', str(uuid4()))(scope, receive, send)
            return await self._application(scope, receive, send)
        if scope['type'] == 'websocket':
            return await send({'type':'websocket.close', 'code':1008})
        raise RuntimeError('Unsupported pilot ASGI scope.')

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
            runtime = await build_runtime(self.config)
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
    """Use with Uvicorn --factory --lifespan on --no-proxy-headers."""
    configure_logging()
    try:
        config = load_pilot_config()
        _identity(config)
        # Pure transport validation belongs before ASGI startup/resource creation.
        MCPSettings(resource_url=config.mcp_resource_url, allowed_hosts=config.mcp_allowed_hosts,
            allowed_origins=config.allowed_origins)
        TransportSettings(allowed_hosts=config.rest_allowed_hosts, allowed_origins=config.allowed_origins,
            authenticated_prefixes=('/v1', '/mcp'))
        return PilotApplication(config)
    except Exception:
        raise ValueError(_CONFIG_ERROR) from None


def _stop_signals(stop):
    loop = asyncio.get_running_loop()
    installed = []
    try:
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(name, stop.set)
            installed.append(name)
    except BaseException:
        _clear_stop_signals(loop, installed)
        raise
    return loop, installed


def _clear_stop_signals(loop, installed):
    failed = False
    for name in installed:
        try:
            loop.remove_signal_handler(name)
        except Exception:
            failed = True
    if failed:
        raise RuntimeError(_SHUTDOWN_ERROR) from None


async def _idle(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def worker_main():
    """Single worker; DB admission/fencing remains the cross-process authority."""
    configure_logging()
    runtime = await build_runtime()
    loop, installed = None, []
    try:
        stop = asyncio.Event()
        loop, installed = _stop_signals(stop)
        services = runtime.services
        queue = ExecutionQueue(services.registry, services.policy, services.bindings, clock=services.clock)
        worker = Worker(queue, adapter_factory=runtime.adapter_factory,
            worker_id='pilot-worker-' + uuid4().hex, clock=services.clock)
        while not stop.is_set():
            try:
                worked = await worker.run_once()
            except Exception:
                logging.error('', extra={'event':'operation_failed'})
                worked = False
            if not worked:
                await _idle(stop, 1)
    finally:
        try:
            _clear_stop_signals(loop, installed)
        finally:
            await runtime.close()


async def retention_main():
    """Explicit, bounded maintenance only; separate audit-retention DB role."""
    configure_logging()
    owned = []
    loop, installed = None, []
    try:
        try:
            config = load_pilot_config()
            config.validate_files(role='audit_retention')
            _identity(config)
            app_url, audit_url = config.database_url('app'), config.database_url('audit_retention')
            app = Registry(app_url)
            owned.append(app)
            audit = Registry(audit_url)
            owned.append(audit)
            sweep = RetentionSweep(app, audit)
        except Exception:
            raise ValueError(_CONFIG_ERROR) from None
        stop = asyncio.Event()
        loop, installed = _stop_signals(stop)
        while not stop.is_set():
            try:
                await sweep.run_once(SystemClock().now())
                logging.info('', extra={'event':'maintenance_complete'})
            except Exception:
                logging.error('', extra={'event':'operation_failed'})
            await _idle(stop, 60)
    finally:
        try:
            _clear_stop_signals(loop, installed)
        finally:
            await _close_resources(*reversed(owned))


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Explicit OIDC/synthetic pilot process')
    parser.add_argument('operation', choices=['worker', 'retention'])
    args = parser.parse_args()
    try:
        asyncio.run(worker_main() if args.operation == 'worker' else retention_main())
    except Exception:
        # No unredacted startup exception/traceback reaches process stderr.
        raise SystemExit('Pilot runtime process failed.') from None


if __name__ == '__main__':
    main()
