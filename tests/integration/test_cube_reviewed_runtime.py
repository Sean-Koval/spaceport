"""Real reviewed-v2 lifecycle on the exact disposable PostgreSQL/Cube stack.

No mock transport, external URL, user database, or skip fallback is admitted.
The shared fixture verifies run-owned container identities and private topology.
"""
import asyncio
from datetime import timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import time

import httpx
import jwt
import pytest

from test_cube_compatibility import cube_stack
from gateway.adapters.base import StaticBindingResolver
from gateway.adapters.bundles import BundleCatalog
from gateway.adapters.cube import CubeAdapter
from gateway.adapters.normalize import NormalizationContext, normalize
from gateway.adapters.reviewed import ReviewedCubeBinding, security_contract
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.clock import SystemClock
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import Completed, ExecutionSpec, ExecutionView, Pending, PinnedProduct
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import validate_manifest
from gateway.contracts.query import QueryRequest, validate_query
from gateway.reviewed_runtime import load_assets

pytestmark = pytest.mark.cube
ROOT = Path(__file__).resolve().parents[2]
SERVICES = {'consumer': 'cube-reviewed', 'fixture_test': 'cube-reviewed-test'}
SCHEMAS = {'consumer': ('demo_reader', 'demo_source'), 'fixture_test': ('fixture_reader', 'test_source')}
EXPECTED = {
    'north': (
        {'day': '2024-03-09', 'segment': 'A', 'revenue': '10.10'},
        {'day': '2024-03-10', 'segment': None, 'revenue': '20.20'},
        {'day': '2024-03-11', 'segment': 'A', 'revenue': '9007199254740993.01'},
    ),
    'south': (
        {'day': '2024-03-09', 'segment': 'A', 'revenue': '100.00'},
        {'day': '2024-03-10', 'segment': 'B', 'revenue': '0.01'},
    ),
}


@pytest.fixture(scope='module')
def reviewed_stack(cube_stack):
    for service in SERVICES.values():
        try:
            response = cube_stack.request('/livez', endpoint=service)
            assert response.status_code == 200
        except (KeyError, AssertionError, httpx.HTTPError):
            pytest.fail('BLOCKED: both real pinned reviewed-v2 Cube services are required', pytrace=False)
    return cube_stack


def reviewed_binding(context='consumer'):
    records = json.loads((ROOT / 'cube/reviewed-postgres/bundles.json').read_text())
    assets = load_assets()
    binding = next(binding for binding in assets.bindings if binding.context == context)
    assert isinstance(binding, ReviewedCubeBinding)
    return binding, records


def reviewed_token(stack, *, context='consumer', tenant='north', groups=None, **patch):
    binding, _ = reviewed_binding(context)
    now = int(time.time())
    payload = {
        'iss': 'dpg-gateway', 'aud': 'dpg-cube', 'iat': now, 'exp': now + 60,
        'principal_id': 'demo-' + tenant, 'tenant_id': tenant, 'policy_revision': 1,
        'model_bundle_digest': binding.model_bundle_digest, 'context': context,
        'adapter_revision': binding.adapter_revision,
        'contract_digest': canonical_digest(security_contract(binding)),
        'row_constraints': [
            {'member': binding.scope.tenant_member, 'operator': 'eq', 'values': [tenant]},
            {'member': binding.scope.access_group_member, 'operator': 'in',
                'values': [tenant] if groups is None else groups},
        ], **patch,
    }
    secret = 'cube_test_signing_key' if context == 'fixture_test' else 'cube_signing_key'
    return jwt.encode(payload, (stack.secrets / secret).read_text().strip(), algorithm='HS256')


class ObservedHttpTransport(httpx.AsyncBaseTransport):
    """Real HTTP forwarding; record only selected synthetic query shapes."""
    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        self.loads = []

    async def handle_async_request(self, request):
        if request.url.path.endswith('/load'):
            self.loads.append(json.loads(request.content)['query'])
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


def configured(stack, *, context='consumer', tenant='north', groups=None, inputs=None):
    binding, records = reviewed_binding(context)
    if stack.host_mode:
        for name, record in records.items():
            record['url'] = f'http://{stack.hosts[SERVICES[name]]}:4000'
    raw = json.loads((ROOT / 'fixtures/reviewed-postgres/product.json').read_text())
    raw.update(binding_id=binding.binding_id, binding_revision=binding.revision,
        model_bundle_digest=binding.model_bundle_digest, content_digest=None)
    manifest = validate_manifest(raw)
    product = PinnedProduct(manifest=manifest, binding_digest=binding.digest, version_id='reviewed-runtime-1')
    decision = Decision(principal_id='demo-' + tenant, policy_revision=1, tenant_id=tenant,
        row_constraints=[
            {'member': binding.scope.tenant_member, 'operator': 'eq', 'values': [tenant]},
            {'member': binding.scope.access_group_member, 'operator': 'in',
                'values': [tenant] if groups is None else groups}],
        visible_fields=list(manifest.outputs), limits=manifest.limits)
    request = validate_query(manifest, QueryRequest(product_id=manifest.product_id,
        version=manifest.version, inputs=inputs or {'start': '2024-03-09', 'end': '2024-03-12'}))
    canonical = request.model_dump(mode='json', exclude={'idempotency_key'})
    spec = ExecutionSpec(principal_id=decision.principal_id, pinned_product=product,
        canonical_query=canonical, canonical_hash=canonical_digest(canonical), policy_revision=1,
        context=context, request_id='reviewed-runtime')
    clock = SystemClock()
    transport = ObservedHttpTransport()
    adapter = CubeAdapter(catalog=BundleCatalog(records), bindings=StaticBindingResolver([binding]),
        clock=clock, transport=transport,
        secret_resolver=lambda ref: (stack.secrets / Path(ref).name).read_text().strip())
    return adapter, product, binding, decision, request, spec, clock, transport


async def execute(stack, *, normalize_rows=True, **kwargs):
    adapter, product, binding, decision, request, spec, clock, transport = configured(stack, **kwargs)
    started = clock.now()
    deadline = started + timedelta(seconds=120)
    async with adapter:
        prepared = await adapter.prepare(product, request, decision, context=spec.context,
            deadline=deadline, request_id=spec.request_id)
        assert binding.checkpoint_member in prepared.cube_query['dimensions']
        while clock.now() < deadline:
            completed = await adapter.poll(prepared, decision, context=spec.context, spec=spec,
                executed_decision=decision, deadline=deadline, request_id=spec.request_id)
            if isinstance(completed, Completed):
                break
            assert isinstance(completed, Pending)
            await clock.sleep(completed.retry_after_seconds)
        else:
            pytest.fail('Actual reviewed-v2 polling exceeded its fixed deadline', pytrace=False)
    # There is no second watermark query capable of freshening an old cached row.
    assert transport.loads and all(query == transport.loads[0] for query in transport.loads)
    assert all(query['dimensions'][-1] == binding.checkpoint_member for query in transport.loads)
    assert all(binding.checkpoint_member not in row for row in completed.raw_rows)
    view = ExecutionView(execution_id='reviewed-runtime-1', state='succeeded',
        accepted_at=started, started_at=started, completed_at=clock.now())
    context = NormalizationContext(spec=spec, decision=decision, executed_decision=decision,
        binding=binding, clock=clock)
    if not normalize_rows:
        return product, completed, view, context
    return normalize(product, completed, view, context=context)


def assert_exact_rows(result, tenant):
    expected = EXPECTED[tenant]
    assert len(result.rows) == result.row_count == len(expected)
    assert {(row['day'], row['segment']): dict(row) for row in result.rows} == {
        (row['day'], row['segment']): row for row in expected}
    assert sum((Decimal(row['revenue']) for row in result.rows), Decimal(0)) == sum(
        (Decimal(row['revenue']) for row in expected), Decimal(0))
    assert result.cache_status == 'unknown' and result.truncated is False
    assert result.freshness.status == 'fresh'
    assert result.provenance.adapter_revision == 'cube-rest-v2'


@pytest.mark.parametrize('context', list(SERVICES))
@pytest.mark.parametrize('tenant', ['north', 'south'])
def test_actual_reviewed_prepare_poll_normalize_matches_same_statement_checkpoint(reviewed_stack, context, tenant):
    result = asyncio.run(execute(reviewed_stack, context=context, tenant=tenant))
    assert_exact_rows(result, tenant)
    role, schema = SCHEMAS[context]
    with reviewed_stack.connect(role) as source:
        checkpoint = source.execute(f'SELECT loaded_at FROM {schema}.watermark WHERE tenant_id = %s', (tenant,)).fetchone()[0]
    # The model emits a hidden string in explicit UTC with six microseconds;
    # the requested local daily timezone cannot shift or truncate freshness.
    assert result.source_watermark == checkpoint


@pytest.mark.parametrize('context', list(SERVICES))
def test_reviewed_rows_and_checkpoint_survive_restart(reviewed_stack, context):
    result = asyncio.run(execute(reviewed_stack, context=context))
    assert_exact_rows(result, 'north')
    current = {'rows': sorted(result.model_dump(mode='json')['rows'],
        key=lambda row: (row['day'], row['segment'] or '')),
        'source_watermark': result.model_dump(mode='json')['source_watermark']}
    filename = 'reviewed-' + context + '-before-restart.json'
    if os.environ.get('GATEWAY_CUBE_RESTART_PHASE') == 'after':
        assert json.loads((reviewed_stack.evidence / filename).read_text()) == current
    else:
        reviewed_stack.capture(filename, current)


def test_actual_reviewed_same_tenant_group_scopes_do_not_mix(reviewed_stack):
    async def scenario():
        assert_exact_rows(await execute(reviewed_stack, groups=['north']), 'north')
        product, completed, view, context = await execute(reviewed_stack, groups=['south'], normalize_rows=False)
        assert completed.raw_rows == () and completed.source_watermark is None
        with pytest.raises(GatewayError) as caught:
            normalize(product, completed, view, context=context)
        assert caught.value.code == 'FRESHNESS_UNKNOWN'
        assert_exact_rows(await execute(reviewed_stack, groups=['north']), 'north')
    asyncio.run(scenario())


def test_actual_reviewed_empty_result_cannot_claim_fresh_checkpoint(reviewed_stack):
    product, completed, view, context = asyncio.run(execute(reviewed_stack,
        inputs={'start': '2025-01-01', 'end': '2025-01-02'}, normalize_rows=False))
    assert completed.raw_rows == () and completed.source_watermark is None
    with pytest.raises(GatewayError) as caught:
        normalize(product, completed, view, context=context)
    assert caught.value.code == 'FRESHNESS_UNKNOWN'


@pytest.mark.parametrize('context', list(SERVICES))
def test_reviewed_fixture_and_legacy_tokens_cannot_cross_endpoints(reviewed_stack, context):
    service = SERVICES[context]
    token = reviewed_token(reviewed_stack, context=context)
    response = reviewed_stack.request('/cubejs-api/v1/meta', endpoint=service, token=token)
    assert response.status_code == 200
    assert {cube['name'] for cube in response.json()['cubes']} == {'Bookings'}
    targets = [SERVICES['fixture_test' if context == 'consumer' else 'consumer'], 'cube', 'cube-test']
    for target in targets:
        denied = reviewed_stack.request('/cubejs-api/v1/meta', endpoint=target, token=token)
        assert denied.status_code in {401, 403} and 'cubes' not in denied.json()
    legacy = reviewed_stack.token(endpoint='cube-test' if context == 'fixture_test' else 'cube')
    denied = reviewed_stack.request('/cubejs-api/v1/meta', endpoint=service, token=legacy)
    assert denied.status_code in {401, 403} and 'cubes' not in denied.json()


@pytest.mark.parametrize('context', list(SERVICES))
def test_reviewed_source_has_atomic_unique_checkpoint_and_separate_read_only_roles(reviewed_stack, context):
    role, schema = SCHEMAS[context]
    other = 'test_source' if schema == 'demo_source' else 'demo_source'
    with reviewed_stack.connect(role) as source:
        assert source.execute('SELECT current_user, current_setting(\'default_transaction_read_only\')').fetchone() == (role, 'on')
        assert source.execute('SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone() == (False,) * 5
        assert source.execute("SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname=%s AND t.relname='watermark' AND c.contype='p'", (schema,)).fetchall() == [('PRIMARY KEY (tenant_id)',)]
        assert source.execute(f'SELECT (SELECT count(*) FROM {schema}.orders), count(*) FROM {schema}.orders o JOIN {schema}.watermark w ON o.tenant_id=w.tenant_id').fetchone() == (5, 5)
        # Both row publication and per-tenant checkpoint inserts share one real
        # PostgreSQL transaction, rather than a separately refreshed timestamp.
        assert source.execute(f'SELECT count(DISTINCT tx) FROM (SELECT xmin::text tx FROM {schema}.orders UNION ALL SELECT xmin::text FROM {schema}.watermark) publication').fetchone()[0] == 1
        assert source.execute("SELECT bool_and(NOT has_table_privilege(current_user,c.oid,'SELECT')) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname IN ('orders','watermark')", (other,)).fetchone()[0] is True
