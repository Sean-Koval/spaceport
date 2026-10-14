"""Local demo composition. Shared domain services remain the only authority."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
import signal

from gateway.adapters.base import StaticBindingResolver
from gateway.adapters.cube import CubeAdapter
from gateway.api.app import create_app
from gateway.api.audit import AuditReadService
from gateway.api.catalog import CatalogService
from gateway.api.choices import ChoiceService
from gateway.api.cursors import PageCursorCodec
from gateway.api.readiness import ReadinessService
from gateway.api.services import Services
from gateway.api.transport import TransportSettings
from gateway.auth.principals import PrincipalStore
from gateway.auth.settings import Settings
from gateway.auth.tokens import Authenticator
from gateway.contracts.clock import SystemClock
from gateway.execution.queue import ExecutionQueue
from gateway.execution.service import ExecutionService
from gateway.execution.results import ResultService
from gateway.execution.retention import RetentionSweep
from gateway.execution.worker import Worker
from gateway.policy.audit import AuditWriter
from gateway.policy.service import PolicyService
from gateway.publishing.service import PublishingService
from gateway.publishing.test_runner import FixtureRunner
from gateway.registry.repository import Registry
from gateway.runtime_config import RuntimeConfig, load_assets, database_url, read_secret, secret_key
from gateway.logging import configure_logging


@dataclass
class Runtime:
    services: Services
    adapter: CubeAdapter
    adapter_factory: object

    async def close(self):
        try:
            await self.adapter.close()
        finally:
            try:
                await self.services.authenticator.close()
            finally:
                await self.services.registry.close()


def build_runtime() -> Runtime:
    config = RuntimeConfig.from_env()
    assets = load_assets()
    clock = SystemClock()
    registry = Registry(database_url('app'))
    principals = PrincipalStore(registry.sessions)
    audit = AuditWriter(secret_key('audit_hmac'))
    policy = PolicyService(registry,principals,audit)
    bindings = StaticBindingResolver(list(assets.bindings))
    def adapter_factory():
        return CubeAdapter(catalog=assets.bundles,bindings=bindings,secret_resolver=read_secret,clock=clock)
    adapter = adapter_factory()
    authenticator = Authenticator(Settings(credential_hash_file='/run/auth/credential-hashes.json'),principals,clock=clock)
    cursors = PageCursorCodec(secret_key('cursor_key'),clock)
    publishing = PublishingService(registry=registry,policy=policy,bindings=bindings,bundles=assets.bundles,
        fixture_runner=FixtureRunner(registry=registry,policy=policy,bindings=bindings,bundles=assets.bundles,
            catalog_provider=lambda:assets.fixture_catalog,adapter_factory=adapter_factory,clock=clock))
    services = Services(authenticator=authenticator,registry=registry,policy=policy,bindings=bindings,
        bundles=assets.bundles,execution=ExecutionService(registry=registry,policy=policy,bindings=bindings,clock=clock),
        results=ResultService(registry,policy,bindings,clock),publishing=publishing,clock=clock,
        catalog=CatalogService(registry=registry,policy=policy,bindings=bindings,cursors=cursors),
        choices=ChoiceService(registry=registry,policy=policy,bindings=bindings,bundles=assets.bundles,cursors=cursors,
            templates=(assets.manifest,)),audit_reader=AuditReadService(registry=registry,policy=policy,cursors=cursors,clock=clock),
        readiness=ReadinessService(registry=registry,policy=policy,adapter=adapter,bindings=bindings,bundles=assets.bundles,
            clock=clock,probe_principal_id=config.probe_principal_id,probe_product_id=config.probe_product_id))
    return Runtime(services,adapter,adapter_factory)


def app_factory():
    from gateway.mcp.server import MCPSettings,create_mcp_server,mount_mcp
    configure_logging()
    config = RuntimeConfig.from_env()
    runtime = build_runtime()
    origins=(f'http://127.0.0.1:{config.ingress_port}',f'http://localhost:{config.ingress_port}','http://edge:8080')
    server=create_mcp_server(runtime.services,settings=MCPSettings(
        resource_url=f'http://127.0.0.1:{config.ingress_port}/mcp',
        allowed_hosts=(f'localhost:{config.ingress_port}',f'127.0.0.1:{config.ingress_port}','edge:8080','gateway:8000'),
        allowed_origins=origins))
    @asynccontextmanager
    async def lifespan(app):
        try:
            async with server.session_manager.run():
                yield
        finally:
            await runtime.close()
    app = create_app(runtime.services,settings=TransportSettings(
        allowed_hosts=('localhost','127.0.0.1','edge','gateway'),
        allowed_origins=origins,authenticated_prefixes=('/v1','/mcp')),lifespan=lifespan)
    mount_mcp(app,server)
    return app


async def worker_main():
    configure_logging()
    runtime = build_runtime()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT,signal.SIGTERM): loop.add_signal_handler(name,stop.set)
    worker = Worker(ExecutionQueue(runtime.services.registry,runtime.services.policy,runtime.services.bindings,
        clock=runtime.services.clock),adapter_factory=runtime.adapter_factory,worker_id='demo-worker',clock=runtime.services.clock)
    try:
        while not stop.is_set():
            try:
                worked = await worker.run_once()
            except Exception:
                logging.error('',extra={'event':'operation_failed'})
                worked = False
            if not worked:
                try: await asyncio.wait_for(stop.wait(),timeout=1)
                except TimeoutError: pass
    finally:
        await runtime.close()


async def retention_main():
    configure_logging()
    app = Registry(database_url('app'))
    audit = Registry(database_url('audit_retention'))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT,signal.SIGTERM): loop.add_signal_handler(name,stop.set)
    sweep = RetentionSweep(app,audit)
    try:
        while not stop.is_set():
            try:
                await sweep.run_once(SystemClock().now())
                logging.info('',extra={'event':'maintenance_complete'})
            except Exception: logging.error('',extra={'event':'operation_failed'})
            try: await asyncio.wait_for(stop.wait(),timeout=60)
            except TimeoutError: pass
    finally:
        await app.close(); await audit.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Synthetic local gateway process')
    parser.add_argument('operation',choices=['worker','retention'])
    args = parser.parse_args()
    asyncio.run(worker_main() if args.operation == 'worker' else retention_main())


if __name__ == '__main__': main()
