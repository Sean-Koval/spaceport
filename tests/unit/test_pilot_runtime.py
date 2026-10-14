"""Pilot composition contracts. All source/identity network and DB I/O are synthetic."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.auth.settings import Settings
from gateway.runtime_config import load_assets

pytestmark = pytest.mark.unit


def module():
    return importlib.import_module('gateway.pilot_runtime')


class Config:
    public_origin = 'https://gateway.example.test'
    rest_allowed_hosts = ('gateway.example.test',)
    allowed_origins = (public_origin,)
    mcp_allowed_hosts = ('gateway.example.test',)
    mcp_resource_url = public_origin + '/mcp'
    probe_principal_id = 'pilot-reader'

    def __init__(self):
        self.events = []

    def validate_files(self, role='app'):
        self.events.append(('files', role))

    def identity_settings(self):
        return Settings(mode='production', source='synthetic_postgres', bind_host='0.0.0.0',
            public_ingress=True, trust_forwarded_headers=False,
            oidc_issuer='https://issuer.example.test', oidc_audience=self.mcp_resource_url,
            oidc_jwks_url='https://issuer.example.test/keys',
            oidc_access_token_profile='rfc9068', oidc_client_ids=('studio-client',))

    def database_url(self, role):
        self.events.append(('url', role))
        return f'postgresql+psycopg://gateway_{role}:synthetic@registry.example.test/db?sslmode=verify-full'

    def load_assets(self):
        from gateway.adapters.bundles import BundleCatalog
        assets = load_assets()
        entries = {}
        for binding in assets.bindings:
            endpoint = assets.bundles.resolve(binding.model_bundle_digest)
            entries[binding.context] = {**endpoint.model_dump(),
                'url': f'https://{binding.context.replace("_", "-")}.example.test'}
        return replace(assets, bundles=BundleCatalog(entries))


@pytest.fixture
def built(monkeypatch, clock):
    api = module()
    config = Config()
    resources = []

    class Registry:
        def __init__(self, url):
            config.events.append(('registry', url))
            self.url = url
            self.sessions = object()
            self.close = AsyncMock()
            resources.append(self)

    class Adapter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.close = AsyncMock()
            resources.append(self)

    class Auth:
        def __init__(self, settings, store, **kwargs):
            self.settings = settings
            self.store = store
            self.close = AsyncMock()
            resources.append(self)

    monkeypatch.setattr(api, 'Registry', Registry)
    monkeypatch.setattr(api, 'CubeAdapter', Adapter)
    monkeypatch.setattr(api, 'Authenticator', Auth)
    monkeypatch.setattr(api, 'secret_key', lambda name: b'k' * 32)
    monkeypatch.setattr(api, 'SystemClock', lambda: clock)
    return SimpleNamespace(api=api, config=config, resources=resources)


def test_build_uses_production_settings_shared_services_and_https_assets(built):
    async def check():
        runtime = await built.api.build_runtime(built.config)
        try:
            svc = runtime.services
            assert svc.authenticator.settings == built.config.identity_settings()
            assert svc.authenticator.settings.source == 'synthetic_postgres'
            assert svc.readiness.probe_principal_id == 'pilot-reader'
            assert svc.readiness.probe_product_id == 'revenue'
            assert svc.execution.registry is svc.registry
            assert svc.publishing.registry is svc.registry
            assert svc.catalog.registry is svc.registry
            assert svc.drafts.registry is svc.registry
            assert built.config.events[0] == ('files', 'app')
            assert 'sslmode=verify-full' in svc.registry.url
            assert all(svc.bundles.resolve(binding.model_bundle_digest).url.startswith('https://')
                for binding in built.config.load_assets().bindings)
        finally:
            await runtime.close()
        for resource in built.resources:
            resource.close.assert_awaited_once()
    asyncio.run(check())


def test_invalid_files_fail_before_any_resource_is_created(built, monkeypatch):
    def fail(role='app'):
        raise ValueError('PRIVATE_FILE_CANARY')
    monkeypatch.setattr(built.config, 'validate_files', fail)
    with pytest.raises(ValueError, match='Pilot runtime configuration is unavailable') as caught:
        asyncio.run(built.api.build_runtime(built.config))
    assert 'PRIVATE_FILE_CANARY' not in str(caught.value)
    assert built.resources == []


@pytest.mark.parametrize('stage', ['PrincipalStore', 'CubeAdapter', 'Authenticator', 'PublishingService', 'ReadinessService'])
def test_partial_construction_closes_every_created_resource_once(built, monkeypatch, stage):
    def fail(*args, **kwargs):
        raise ValueError('PRIVATE_STARTUP_CANARY')
    monkeypatch.setattr(built.api, stage, fail)
    with pytest.raises(ValueError, match='Pilot runtime configuration is unavailable') as caught:
        asyncio.run(built.api.build_runtime(built.config))
    assert 'PRIVATE_STARTUP_CANARY' not in str(caught.value)
    for resource in built.resources:
        resource.close.assert_awaited_once()


def test_partial_cleanup_failure_does_not_skip_other_resources(built, monkeypatch):
    def fail(*args, **kwargs):
        built.resources[0].close.side_effect = RuntimeError('PRIVATE_CLOSE_CANARY')
        raise ValueError('PRIVATE_STARTUP_CANARY')
    monkeypatch.setattr(built.api, 'ReadinessService', fail)
    with pytest.raises(ValueError, match='Pilot runtime configuration is unavailable'):
        asyncio.run(built.api.build_runtime(built.config))
    for resource in built.resources:
        resource.close.assert_awaited_once()


def test_factory_redacts_configuration_error_without_building_runtime(monkeypatch):
    api = module()
    def fail():
        raise ValueError('PRIVATE_CONFIG_CANARY')
    monkeypatch.setattr(api, 'load_pilot_config', fail)
    build = AsyncMock()
    monkeypatch.setattr(api, 'build_runtime', build)
    with pytest.raises(ValueError, match='Pilot runtime configuration is unavailable') as caught:
        api.app_factory()
    assert 'PRIVATE_CONFIG_CANARY' not in str(caught.value)
    build.assert_not_called()


@asynccontextmanager
async def lifespan(app):
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    task = asyncio.create_task(app({'type': 'lifespan', 'asgi': {'version': '3.0'}},
        incoming.get, outgoing.put))
    await incoming.put({'type': 'lifespan.startup'})
    response = await asyncio.wait_for(outgoing.get(), 5)
    assert response['type'] == 'lifespan.startup.complete', response
    try:
        yield app
    finally:
        await incoming.put({'type': 'lifespan.shutdown'})
        response = await asyncio.wait_for(outgoing.get(), 5)
        assert response['type'] == 'lifespan.shutdown.complete', response
        await asyncio.wait_for(task, 5)


def test_wrapper_does_not_serve_before_startup_or_after_shutdown(monkeypatch):
    api = module()
    config = Config()
    monkeypatch.setattr(api, 'load_pilot_config', lambda: config)
    app = api.app_factory()
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config.public_origin) as client:
            response = await client.get('/healthz')
            assert response.status_code == 503
            assert response.json()['code'] == 'UPSTREAM_UNAVAILABLE'
    asyncio.run(check())


def service_runtime(config, clock):
    from tests.unit.test_identity import SubjectStore, authenticator
    auth, requests, client = authenticator(clock, settings=config.identity_settings(), store=SubjectStore())
    original_close = auth.close
    async def close_auth():
        await original_close()
        await client.aclose()
    auth.close = close_auth
    svc = SimpleNamespace(authenticator=auth, clock=clock,
        catalog=SimpleNamespace(search=AsyncMock(return_value={'items': [], 'next_cursor': None})),
        readiness=SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(ready=True))))
    close = AsyncMock(side_effect=auth.close)
    return SimpleNamespace(services=svc, close=close), requests


def test_initialized_middleware_oidc_and_public_canonical_metadata(monkeypatch, clock):
    api, config = module(), Config()
    runtime, requests = service_runtime(config, clock)
    monkeypatch.setattr(api, 'load_pilot_config', lambda: config)
    build = AsyncMock(return_value=runtime)
    monkeypatch.setattr(api, 'build_runtime', build)
    from tests.unit.test_identity import signed_token
    token = signed_token(clock, aud=config.mcp_resource_url)
    async def check():
        app = api.app_factory()
        async with lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config.public_origin) as client:
            metadata = await client.get('/.well-known/oauth-protected-resource/mcp')
            assert metadata.status_code == 200, metadata.text
            assert metadata.json()['resource'] == config.mcp_resource_url
            assert metadata.json()['authorization_servers'] == ['https://issuer.example.test']
            assert requests == []
            unauthenticated = await client.post('/mcp', json={})
            assert unauthenticated.status_code == 401
            assert unauthenticated.headers['www-authenticate'] == 'Bearer resource_metadata="https://gateway.example.test/.well-known/oauth-protected-resource/mcp"'
            response = await client.get('/v1/products', headers={'Authorization': 'Bearer ' + token})
            assert response.status_code == 200, response.text
            principal = runtime.services.catalog.search.await_args.args[0]
            assert (principal.id, principal.kind, principal.service_client_id) == ('db-user', 'user', 'studio-client')
            assert len(requests) == 1
            from tests.contract.test_mcp import body, PROTOCOL
            mcp_headers = {'Authorization':'Bearer '+token, 'MCP-Protocol-Version':PROTOCOL,
                'MCP-Method':'tools/call', 'MCP-Name':'search_products',
                'Accept':'application/json, text/event-stream'}
            result = await client.post('/mcp', json=body(), headers=mcp_headers)
            assert result.status_code == 200, result.text
            assert result.json()['result']['structuredContent'] == {'items':[], 'next_cursor':None}
            assert runtime.services.catalog.search.await_count == 2
            for headers in ({'Host': 'localhost'}, {'Host': 'gateway.example.test:444'}, {'Host': 'evil.test', 'X-Forwarded-Host': 'gateway.example.test'},
                            {'Origin': 'https://evil.test'}, {'Authorization': 'Bearer dpg_demo_' + 'a'*43}):
                denied = await client.get('/v1/products', headers={'Authorization':'Bearer '+token, **headers})
                assert denied.status_code in {401, 422}
            wrong = signed_token(clock, aud='wrong')
            assert (await client.get('/v1/products', headers={'Authorization': 'Bearer '+wrong})).status_code == 401
            mcp_metadata = await client.get('/.well-known/oauth-protected-resource/mcp', headers={'Host': 'localhost'})
            assert mcp_metadata.status_code == 422
            runtime.services.authenticator.principal_store.active = False
            assert (await client.get('/v1/products', headers={'Authorization':'Bearer '+token})).status_code == 401
            assert (await client.post('/mcp', json=body(), headers=mcp_headers)).status_code == 401
            assert runtime.services.catalog.search.await_count == 2
        runtime.close.assert_awaited_once()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config.public_origin) as client:
            assert (await client.get('/healthz')).status_code == 503
    asyncio.run(check())


@pytest.mark.parametrize('stage', ['build', 'server', 'mount'])
def test_lifespan_startup_failure_is_redacted_and_cleans_runtime(monkeypatch, clock, stage):
    api, config = module(), Config()
    runtime, _ = service_runtime(config, clock)
    monkeypatch.setattr(api, 'load_pilot_config', lambda: config)
    build = AsyncMock(return_value=runtime)
    if stage == 'build':
        build.side_effect = ValueError('PRIVATE_LIFESPAN_CANARY')
    monkeypatch.setattr(api, 'build_runtime', build)
    def fail(*args, **kwargs):
        raise ValueError('PRIVATE_LIFESPAN_CANARY')
    if stage == 'server': monkeypatch.setattr(api, 'create_mcp_server', fail)
    if stage == 'mount': monkeypatch.setattr(api, 'mount_mcp', fail)
    async def check():
        messages = []
        async def receive(): return {'type':'lifespan.startup'}
        async def send(value): messages.append(value)
        await api.app_factory()({'type':'lifespan'}, receive, send)
        assert messages == [{'type':'lifespan.startup.failed', 'message':'Pilot runtime startup failed.'}]
        assert 'PRIVATE_LIFESPAN_CANARY' not in str(messages)
        if stage != 'build': runtime.close.assert_awaited_once()
        else: await runtime.close()
    asyncio.run(check())


def test_close_is_idempotent_and_one_close_failure_does_not_leak(built):
    async def check():
        runtime = await built.api.build_runtime(built.config)
        runtime.adapter.close.side_effect = RuntimeError('PRIVATE_CLOSE_CANARY')
        with pytest.raises(RuntimeError, match='Pilot runtime shutdown failed'):
            await runtime.close()
        await runtime.close()
        for resource in built.resources:
            resource.close.assert_awaited_once()
    asyncio.run(check())


def test_cancellation_during_construction_closes_resources_then_propagates(built, monkeypatch):
    def cancel(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(built.api, 'ReadinessService', cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(built.api.build_runtime(built.config))
    for resource in built.resources:
        resource.close.assert_awaited_once()


@pytest.mark.parametrize('mode', ['demo', 'bigquery', 'forwarded', 'audience'])
def test_wrong_identity_profile_fails_before_resource_allocation(built, monkeypatch, mode):
    original = built.config.identity_settings()
    if mode == 'demo': changes = {'mode':'demo'}
    elif mode == 'bigquery': changes = {'source':'bigquery'}
    elif mode == 'forwarded': changes = {'trust_forwarded_headers':True}
    else: changes = {'oidc_audience':'other-resource'}
    monkeypatch.setattr(built.config, 'identity_settings', lambda: original.model_copy(update=changes))
    with pytest.raises(ValueError, match='Pilot runtime configuration is unavailable'):
        asyncio.run(built.api.build_runtime(built.config))
    assert built.resources == []


def signal_stub(api, monkeypatch, events):
    holder = {}
    loop = SimpleNamespace(remove_signal_handler=lambda value: events.append(('remove', value)))
    def install(stop):
        holder['stop'] = stop
        return loop, ['synthetic-signal']
    monkeypatch.setattr(api, '_stop_signals', install)
    return holder


def test_worker_drains_inflight_work_uses_unique_id_and_closes_runtime(monkeypatch, clock):
    api = module()
    events, ids = [], []
    holder = signal_stub(api, monkeypatch, events)
    runtime = SimpleNamespace(services=SimpleNamespace(registry=object(), policy=object(), bindings=object(), clock=clock),
        adapter_factory=object(), close=AsyncMock(side_effect=lambda: events.append('closed')))
    monkeypatch.setattr(api, 'build_runtime', AsyncMock(return_value=runtime))
    monkeypatch.setattr(api, 'ExecutionQueue', lambda *args, **kwargs: 'queue')
    class Worker:
        def __init__(self, queue, **kwargs):
            assert queue == 'queue' and kwargs['adapter_factory'] is runtime.adapter_factory
            ids.append(kwargs['worker_id'])
        async def run_once(self):
            events.append('started')
            holder['stop'].set()
            await asyncio.sleep(0)
            events.append('finished')
            return True
    monkeypatch.setattr(api, 'Worker', Worker)
    asyncio.run(api.worker_main())
    asyncio.run(api.worker_main())
    assert ids[0] != ids[1]
    from pydantic import TypeAdapter
    from gateway.contracts.canonical import Identifier
    for value in ids: assert TypeAdapter(Identifier).validate_python(value) == value
    assert events == ['started', 'finished', ('remove','synthetic-signal'), 'closed'] * 2
    assert runtime.close.await_count == 2


def test_worker_error_has_bounded_idle_and_no_exception_text(monkeypatch, clock, capsys):
    api = module()
    events = []
    holder = signal_stub(api, monkeypatch, events)
    runtime = SimpleNamespace(services=SimpleNamespace(registry=object(), policy=object(), bindings=object(), clock=clock),
        adapter_factory=object(), close=AsyncMock())
    monkeypatch.setattr(api, 'build_runtime', AsyncMock(return_value=runtime))
    monkeypatch.setattr(api, 'ExecutionQueue', lambda *args, **kwargs: object())
    class Worker:
        def __init__(self, *args, **kwargs): pass
        async def run_once(self): raise RuntimeError('PRIVATE_WORKER_CANARY')
    monkeypatch.setattr(api, 'Worker', Worker)
    async def idle(stop, seconds):
        events.append(('idle', seconds))
        stop.set()
    monkeypatch.setattr(api, '_idle', idle)
    asyncio.run(api.worker_main())
    assert ('idle', 1) in events
    runtime.close.assert_awaited_once()
    assert 'PRIVATE_WORKER_CANARY' not in capsys.readouterr().err


@pytest.mark.parametrize('boundary', ['signals', 'work'])
def test_worker_failure_or_cancellation_closes_without_releasing_source(monkeypatch, clock, boundary):
    api = module()
    events = []
    signal_stub(api, monkeypatch, events)
    runtime = SimpleNamespace(services=SimpleNamespace(registry=object(), policy=object(), bindings=object(), clock=clock),
        adapter_factory=object(), close=AsyncMock())
    monkeypatch.setattr(api, 'build_runtime', AsyncMock(return_value=runtime))
    queue = SimpleNamespace(fail=AsyncMock(), complete=AsyncMock())
    monkeypatch.setattr(api, 'ExecutionQueue', lambda *args, **kwargs: queue)
    def fail(*args): raise RuntimeError('PRIVATE_SIGNAL_CANARY')
    class Worker:
        def __init__(self, *args, **kwargs): pass
        async def run_once(self): raise asyncio.CancelledError()
    monkeypatch.setattr(api, 'Worker', Worker)
    if boundary == 'signals': monkeypatch.setattr(api, '_stop_signals', fail)
    with pytest.raises(RuntimeError if boundary == 'signals' else asyncio.CancelledError):
        asyncio.run(api.worker_main())
    runtime.close.assert_awaited_once()
    queue.fail.assert_not_awaited()
    queue.complete.assert_not_awaited()


def test_retention_validates_both_roles_closes_once_and_never_builds_source(built, monkeypatch):
    api, config = built.api, built.config
    monkeypatch.setattr(api, 'load_pilot_config', lambda: config)
    build = AsyncMock()
    monkeypatch.setattr(api, 'build_runtime', build)
    events = []
    holder = signal_stub(api, monkeypatch, events)
    class Sweep:
        def __init__(self, app, audit):
            assert app is not audit
            assert 'gateway_app:' in app.url and 'gateway_audit_retention:' in audit.url
        async def run_once(self, now):
            holder['stop'].set()
            events.append('swept')
    monkeypatch.setattr(api, 'RetentionSweep', Sweep)
    asyncio.run(api.retention_main())
    assert config.events[0] == ('files', 'audit_retention')
    assert ('url', 'app') in config.events and ('url','audit_retention') in config.events
    assert events == ['swept', ('remove','synthetic-signal')]
    assert len(built.resources) == 2
    for resource in built.resources: resource.close.assert_awaited_once()
    build.assert_not_awaited()


def test_retention_second_registry_failure_closes_first(built, monkeypatch):
    api, config = built.api, built.config
    monkeypatch.setattr(api, 'load_pilot_config', lambda: config)
    original = api.Registry
    def registry(url):
        if 'gateway_audit_retention:' in url: raise ValueError('PRIVATE_REGISTRY_CANARY')
        return original(url)
    monkeypatch.setattr(api, 'Registry', registry)
    with pytest.raises(ValueError, match='Pilot runtime configuration is unavailable'):
        asyncio.run(api.retention_main())
    assert len(built.resources) == 1
    built.resources[0].close.assert_awaited_once()


def test_lifespan_shutdown_failure_is_redacted(monkeypatch, clock):
    api, config = module(), Config()
    runtime, _ = service_runtime(config, clock)
    original_close = runtime.close.side_effect
    async def failing_close():
        await original_close()
        raise RuntimeError('PRIVATE_SHUTDOWN_CANARY')
    runtime.close.side_effect = failing_close
    monkeypatch.setattr(api, 'load_pilot_config', lambda: config)
    monkeypatch.setattr(api, 'build_runtime', AsyncMock(return_value=runtime))
    async def check():
        messages = []
        incoming = iter([{'type':'lifespan.startup'}, {'type':'lifespan.shutdown'}])
        async def receive(): return next(incoming)
        async def send(message): messages.append(message)
        await api.app_factory()({'type':'lifespan'}, receive, send)
        assert messages == [{'type':'lifespan.startup.complete'},
            {'type':'lifespan.shutdown.failed', 'message':'Pilot runtime shutdown failed.'}]
        runtime.close.assert_awaited_once()
    asyncio.run(check())


def test_cancellation_from_one_close_does_not_skip_remaining_resources(built):
    async def check():
        runtime = await built.api.build_runtime(built.config)
        runtime.adapter.close.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await runtime.close()
        for resource in built.resources: resource.close.assert_awaited_once()
    asyncio.run(check())


@pytest.mark.parametrize('stage', ['enter', 'exit'])
def test_sdk_lifecycle_failure_is_redacted_and_runtime_is_closed(monkeypatch, clock, stage):
    api, config = module(), Config()
    runtime, _ = service_runtime(config, clock)
    events = []
    @asynccontextmanager
    async def run():
        events.append('enter')
        if stage == 'enter': raise RuntimeError('PRIVATE_SDK_CANARY')
        yield
        events.append('exit')
        raise RuntimeError('PRIVATE_SDK_CANARY')
    server = SimpleNamespace(session_manager=SimpleNamespace(run=run))
    monkeypatch.setattr(api, 'load_pilot_config', lambda: config)
    monkeypatch.setattr(api, 'build_runtime', AsyncMock(return_value=runtime))
    monkeypatch.setattr(api, 'create_mcp_server', lambda *args, **kwargs: server)
    monkeypatch.setattr(api, 'mount_mcp', lambda *args: None)
    async def check():
        messages = []
        incoming = iter([{'type':'lifespan.startup'}, {'type':'lifespan.shutdown'}])
        async def receive(): return next(incoming)
        async def send(message): messages.append(message)
        await api.app_factory()({'type':'lifespan'}, receive, send)
        expected = [{'type':'lifespan.startup.failed', 'message':'Pilot runtime startup failed.'}]
        if stage == 'exit': expected = [{'type':'lifespan.startup.complete'},
            {'type':'lifespan.shutdown.failed', 'message':'Pilot runtime shutdown failed.'}]
        assert messages == expected
        runtime.close.assert_awaited_once()
    asyncio.run(check())


def test_http_waits_for_sdk_startup_and_cancelled_lifespan_closes(monkeypatch, clock):
    api, config = module(), Config()
    runtime, _ = service_runtime(config, clock)
    monkeypatch.setattr(api, 'load_pilot_config', lambda: config)
    monkeypatch.setattr(api, 'build_runtime', AsyncMock(return_value=runtime))
    async def check():
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        app = api.app_factory()
        task = asyncio.create_task(app({'type':'lifespan'}, incoming.get, outgoing.put))
        await incoming.put({'type':'lifespan.startup'})
        assert (await outgoing.get())['type'] == 'lifespan.startup.complete'
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        runtime.close.assert_awaited_once()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config.public_origin) as client:
            assert (await client.get('/healthz')).status_code == 503
    asyncio.run(check())


def test_resource_close_timeout_is_bounded_and_later_resources_close(built, monkeypatch):
    monkeypatch.setattr(built.api, '_CLOSE_SECONDS', .01)
    async def check():
        runtime = await built.api.build_runtime(built.config)
        async def stall(): await asyncio.Event().wait()
        runtime.adapter.close.side_effect = stall
        with pytest.raises(RuntimeError, match='Pilot runtime shutdown failed'):
            await asyncio.wait_for(runtime.close(), .5)
        for resource in built.resources: resource.close.assert_awaited_once()
    asyncio.run(check())


def test_retention_invalid_files_prevent_every_registry(built, monkeypatch):
    def fail(role='app'): raise ValueError('PRIVATE_RETENTION_CANARY')
    monkeypatch.setattr(built.config, 'validate_files', fail)
    monkeypatch.setattr(built.api, 'load_pilot_config', lambda: built.config)
    with pytest.raises(ValueError, match='Pilot runtime configuration is unavailable'):
        asyncio.run(built.api.retention_main())
    assert built.resources == []


def test_main_reports_fixed_redacted_failure(monkeypatch, capsys):
    api = module()
    monkeypatch.setattr('sys.argv', ['pilot_runtime', 'worker'])
    monkeypatch.setattr(api, 'worker_main', AsyncMock(side_effect=ValueError('PRIVATE_MAIN_CANARY')))
    with pytest.raises(SystemExit, match='Pilot runtime process failed') as caught:
        api.main()
    assert 'PRIVATE_MAIN_CANARY' not in str(caught.value)
    assert 'PRIVATE_MAIN_CANARY' not in capsys.readouterr().err


@pytest.mark.parametrize('explicit_default_port', [False, True])
def test_actual_config_file_composes_runtime_oidc_and_both_transports(built, monkeypatch, tmp_path, clock, explicit_default_port):
    """Real parser/config/assets/runtime/SDK, with only file/DB/HTTP I/O replaced."""
    import json
    from gateway import pilot_config
    from tests.unit.test_pilot_config import config_document
    from tests.unit.test_identity import SubjectStore, authenticator, signed_token
    from tests.contract.test_mcp import body, PROTOCOL
    from sqlalchemy.engine import make_url
    document = config_document()
    if explicit_default_port: document['public_origin'] += ':443'
    path = tmp_path / 'pilot.json'
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    monkeypatch.setenv('GATEWAY_PILOT_CONFIG_FILE', str(path))
    monkeypatch.setattr(pilot_config, 'read_secret', lambda path: 'a' * 43)
    monkeypatch.setattr(pilot_config, '_validate_ca', lambda path: None)
    monkeypatch.setattr(built.api, 'PrincipalStore', lambda sessions: SubjectStore())
    clients, authenticators = [], []
    def auth_factory(settings, store, **kwargs):
        auth, _, client = authenticator(clock, settings=settings, store=store)
        original_close = auth.close
        async def close():
            await original_close()
            await client.aclose()
        auth.close = AsyncMock(side_effect=close)
        clients.append(client)
        authenticators.append(auth)
        return auth
    monkeypatch.setattr(built.api, 'Authenticator', auth_factory)
    catalog = SimpleNamespace(search=AsyncMock(return_value={'items':[], 'next_cursor':None}))
    monkeypatch.setattr(built.api, 'CatalogService', lambda **kwargs: catalog)
    async def check():
        app = built.api.app_factory()
        assert isinstance(app.config, pilot_config.PilotRuntimeConfig)
        assert app.config.public_origin == 'https://spaceport.example.test'
        token = signed_token(clock, aud=app.config.mcp_resource_url)
        headers = {'Authorization':'Bearer '+token, 'MCP-Protocol-Version':PROTOCOL,
            'MCP-Method':'tools/call', 'MCP-Name':'search_products',
            'Accept':'application/json, text/event-stream'}
        async with lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=app.config.public_origin) as client:
            assert authenticators[0].settings == app.config.identity_settings()
            registry = next(resource for resource in built.resources if hasattr(resource, 'url'))
            url = make_url(registry.url)
            assert url.host == 'registry.example.test'
            assert dict(url.query) == {'sslmode':'verify-full', 'sslrootcert':'/run/secrets/registry_ca.pem',
                'options':'-c statement_timeout=4000 -c lock_timeout=2000'}
            assert (await client.get('/v1/products', headers=headers)).status_code == 200
            reply = await client.post('/mcp', json=body(), headers=headers)
            assert reply.status_code == 200, reply.text
            assert reply.json()['result']['structuredContent'] == {'items':[], 'next_cursor':None}
            metadata = await client.get('/.well-known/oauth-protected-resource/mcp')
            assert metadata.status_code == 200
            assert metadata.json()['resource'] == app.config.mcp_resource_url
            for target, method in [('/v1/products','GET'), ('/mcp','POST'),
                                   ('/.well-known/oauth-protected-resource/mcp','GET')]:
                rejected = await client.request(method, target, headers={**headers, 'Host':'spaceport.example.test:8443'})
                assert rejected.status_code == 422
        for resource in built.resources: resource.close.assert_awaited_once()
        authenticators[0].close.assert_awaited_once()
        assert all(client.is_closed for client in clients)
    asyncio.run(check())


def test_actual_nondefault_public_port_is_rejected_before_runtime(built, monkeypatch, tmp_path):
    import json
    from tests.unit.test_pilot_config import config_document
    document = config_document()
    document['public_origin'] += ':8443'
    document['oidc']['audience'] = document['public_origin'] + '/mcp'
    path = tmp_path / 'pilot.json'
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    monkeypatch.setenv('GATEWAY_PILOT_CONFIG_FILE', str(path))
    with pytest.raises(ValueError, match='Pilot runtime configuration is unavailable'):
        built.api.app_factory()
    assert built.resources == []


@pytest.mark.parametrize('operation', ['worker', 'retention'])
def test_signal_cleanup_failure_still_closes_all_resources(built, monkeypatch, operation):
    api, config = built.api, built.config
    monkeypatch.setattr(api, 'load_pilot_config', lambda: config)
    events = []
    def remove(name):
        events.append(name)
        raise RuntimeError('PRIVATE_SIGNAL_CLOSE_CANARY')
    def signals(stop):
        stop.set()
        return SimpleNamespace(remove_signal_handler=remove), ['first', 'second']
    monkeypatch.setattr(api, '_stop_signals', signals)
    if operation == 'retention':
        monkeypatch.setattr(api, 'RetentionSweep', lambda *args: object())
    with pytest.raises(RuntimeError, match='Pilot runtime shutdown failed'):
        asyncio.run(api.worker_main() if operation == 'worker' else api.retention_main())
    assert events == ['first', 'second']
    for resource in built.resources: resource.close.assert_awaited_once()
