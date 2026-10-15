"""Real fixture-owner caller transactions with SQL-only synthetic provider metadata.

Preparation/plan construction use the real BigQuery adapter. Its provider poll is
an in-process recording transport: no HTTP, credentials, Node or cloud query.
"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from psycopg import sql
from sqlalchemy import create_engine, text

from gateway.adapters.base import StaticBindingResolver
from gateway.adapters.bigquery_serving import BigQueryServingAdapter
from gateway.adapters.bundles import BundleCatalog, CubeEndpoint
from gateway.auth.principals import PrincipalStore
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import Completed
from gateway.policy.audit import AuditWriter
from gateway.policy.service import PolicyService
from gateway.publishing.test_runner import FixtureRunner
from gateway.source_access import SourceAccessService
from gateway.source_lifecycle import SourceLifecycle, provision_sources
from tests.integration.conftest import connection_kwargs
from tests.integration.test_source_lifecycle import call, intent
from tests.unit.test_source_approval_contracts import principal

pytestmark = pytest.mark.postgres


@pytest.fixture
def reviewed_fixture_database(registry_database, valid_manifest, request):
    """Explicit owner test review and ephemeral, narrowly mapped provider roles."""
    import secrets
    from tests.integration.test_source_fixture_owners import fixture_setup
    db = registry_database
    raw = deepcopy(valid_manifest)
    options = getattr(request, 'param', {})
    if options.get('budget'):
        raw.setdefault('limits', {})['execution_timeout_seconds'] = options['budget']
        raw['limits']['sync_wait_seconds'] = min(options['budget'],raw['limits'].get('sync_wait_seconds',5))
    document, manifest, catalog, approval = fixture_setup(db, raw, omit_case=options.get('omit_case', False))
    created = []
    with psycopg.connect(**connection_kwargs(db.admin_url), autocommit=True) as admin:
        for kind in ('fixture_runtime', 'fixture_observer'):
            name = 'gws_' + secrets.token_hex(8)
            password = secrets.token_urlsafe(32)
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}')
                .format(sql.Identifier(name), sql.Literal(password)))
            admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}')
                .format(sql.Identifier(db.urls['owner'].database), sql.Identifier(name)))
            db.roles[kind] = name
            db.urls[kind] = db.urls['app'].set(username=name, password=password)
            created.append(name)
    engine = create_engine(db.urls['owner'])
    with engine.begin() as connection:
        provision_sources(connection, [catalog.consumer_source, catalog.fixture_source], [
            dict(login_name=db.roles[role], source_id=catalog.fixture_source.source_id, revision=1,
                context='fixture_test', role_kind=kind)
            for role, kind in [('fixture_runtime','runtime'),('fixture_observer','observer')]],
            deployment_epoch='reviewed-fixture-test')
    engine.dispose()
    try:
        yield SimpleNamespace(db=db, document=document, manifest=manifest, catalog=catalog, approval=approval)
    finally:
        with psycopg.connect(**connection_kwargs(db.admin_url.set(database=db.urls['owner'].database)), autocommit=True) as admin:
            for name in created:
                admin.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(name)))
        with psycopg.connect(**connection_kwargs(db.admin_url), autocommit=True) as admin:
            for name in created:
                admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))
        for kind in ('fixture_runtime','fixture_observer'):
            db.roles.pop(kind)
            db.urls.pop(kind)


def fixture_runner(data):
    db, catalog = data.db, data.catalog
    audit = AuditWriter(b'synthetic-fixture-runner-audit-key-32')
    access = SourceAccessService(db.registry, audit)
    policy = PolicyService(db.registry, PrincipalStore(db.registry.sessions), audit, source_access=access)
    bindings = StaticBindingResolver([catalog.consumer_binding, catalog.fixture_binding])
    sources = SimpleNamespace(resolve=lambda ref: next(source for source in
        (catalog.consumer_source, catalog.fixture_source)
        if source.source_id == ref['source_id'] and source.context == ref['context']))
    bundles = BundleCatalog({source.context:CubeEndpoint(endpoint_id=source.context+'-recording',
        adapter_revision='cube-bigquery-v1',url=source.cube_endpoint,bundle_digest=source.model_bundle_digest,
        secret_ref='/run/secrets/synthetic-never-read',context=source.context)
        for source in (catalog.consumer_source, catalog.fixture_source)})
    lifecycle = SourceLifecycle(db.registry, policy, sources, deployment_epoch='reviewed-fixture-test')
    trace = []
    hooks = SimpleNamespace(after_terminal=None, corrupt_answer=False)
    def no_http(request):
        raise AssertionError('Fixture SQL tests must not send HTTP')
    class RecordingAdapter(BigQueryServingAdapter):
        async def prepare(self, *args, **kwargs):
            trace.append('prepare')
            return await super().prepare(*args, **kwargs)
        async def poll(self, prepared, decision, *, source_context, **kwargs):
            trace.append('poll')
            context = source_context.wire_context()
            job = call(db, 'fixture_runtime', 'SELECT gateway.source_bq_admit(%s,%s,%s)',
                context, 'e'*64, intent(timeout=100))['job']
            call(db, 'fixture_runtime', 'SELECT gateway.source_bq_observe(%s,%s)', job['job_id'],
                dict(intent_digest=job['intent_digest'], state='done', terminal_error=False))
            if hooks.after_terminal is not None:
                hooks.after_terminal()
            case = next(item for item in catalog.cases if item.kind == 'known_answer')
            rows = [{catalog.fixture_binding.fields[name].response_key:
                (value+'T00:00:00.000' if name == 'day' else value)
                for name, value in row.items()} for row in case.fixture.expected_rows]
            if hooks.corrupt_answer:
                numeric = next(name for name, field in data.manifest.outputs.items() if field.type == 'decimal')
                rows[0][catalog.fixture_binding.fields[numeric].response_key] = 'invalid-decimal'
            return Completed(raw_rows=rows, source_watermark=None)
    runner = FixtureRunner(registry=db.registry, policy=policy, bindings=bindings, bundles=bundles,
        catalog_provider=SimpleNamespace(resolve=lambda *args:catalog), source_lifecycle=lifecycle,
        adapter_factory=lambda:RecordingAdapter(sources=sources,catalog=bundles,bindings=bindings,
            secret_resolver=lambda ref:(_ for _ in ()).throw(AssertionError('No secret reads')),
            transport=httpx.MockTransport(no_http)))
    return SimpleNamespace(runner=runner, access=access, policy=policy, trace=trace, hooks=hooks)


def revoke(data):
    data.db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',
        (data.approval['id'], data.approval['content_digest'], 'synthetic-fixture-ended'))


def test_real_fixture_run_retains_invoker_and_separate_fixed_positive_owners(reviewed_fixture_database):
    data = reviewed_fixture_database
    service = fixture_runner(data)
    result = asyncio.run(service.runner.run(principal(), 'fixture-draft'))
    assert result.status == 'passed' and len(result.evidence['checks']) == 2
    assert service.trace == ['prepare','poll']
    db = data.db
    assert db.scalar('SELECT count(*) FROM gateway.fixture_source_invocations') == 1
    assert db.scalar('SELECT count(*) FROM gateway.fixture_attempt_source_authorities') == 1
    assert db.scalar('SELECT count(*) FROM gateway.fixture_test_source_authorities') == 1
    assert db.scalar('SELECT count(*) FROM gateway.executions') == 0
    assert db.scalar('SELECT count(*) FROM gateway.results') == 0
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity') == 0
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='result' AND actor_id='fixed-positive' AND source_approval_id=%s", (data.approval['id'],)) >= 1
    assert all(check['passed'] for check in result.evidence['checks'])


@pytest.mark.parametrize('reviewed_fixture_database', [{'omit_case':True}], indirect=True)
def test_missing_negative_structural_review_precedes_group_or_adapter_creation(reviewed_fixture_database):
    data = reviewed_fixture_database
    service = fixture_runner(data)
    with pytest.raises(GatewayError):
        asyncio.run(service.runner.run(principal(), 'fixture-draft'))
    assert service.trace == []
    for table in ('source_operation_groups','fixture_attempts','test_runs','bigquery_source_jobs'):
        assert data.db.scalar('SELECT count(*) FROM gateway.'+table) == 0


def test_wrong_invoking_client_cannot_create_fixture_invocation_or_group(reviewed_fixture_database):
    data = reviewed_fixture_database
    service = fixture_runner(data)
    with pytest.raises(GatewayError):
        asyncio.run(service.runner.run(principal().model_copy(update={'service_client_id':'another-client'}), 'fixture-draft'))
    assert service.trace == []
    assert data.db.scalar('SELECT count(*) FROM gateway.fixture_source_invocations') == 0
    assert data.db.scalar('SELECT count(*) FROM gateway.source_operation_groups') == 0


def test_revoked_completion_preserves_exact_terminal_metadata_without_evidence_rows(reviewed_fixture_database):
    data = reviewed_fixture_database
    service = fixture_runner(data)
    service.hooks.after_terminal = lambda:revoke(data)
    with pytest.raises(GatewayError):
        asyncio.run(service.runner.run(principal(), 'fixture-draft'))
    assert service.trace == ['prepare','poll']
    assert data.db.scalar('SELECT count(*) FROM gateway.test_runs') == 0
    assert data.db.scalar("SELECT count(*) FROM gateway.bigquery_source_jobs WHERE state='done'") == 1
    assert data.db.scalar('SELECT count(*) FROM gateway.held_source_capacity') == 0
    state=data.db.scalar('SELECT row_to_json(s) FROM gateway.fixture_attempt_states s')
    assert (state['state'],state['source_completed'],state['error_code'])==('failed',False,'NOT_FOUND_OR_FORBIDDEN'), state
    assert state['check_digest'] is not None


@pytest.mark.parametrize('reviewed_fixture_database', [{'budget':10}], indirect=True)
@pytest.mark.parametrize('stage', ['admission','completion'])
def test_fixture_execution_budget_cannot_outlive_final_allow_audit(reviewed_fixture_database, stage):
    data = reviewed_fixture_database
    service = fixture_runner(data)
    original = service.access.finalize
    delayed = []
    async def slow_gate(uow, requests):
        result = await original(uow, requests)
        action = 'execute' if stage == 'admission' else 'result'
        request = next((item for item in requests if item.owner is not None
            and item.owner.kind == 'fixture_attempt' and item.action == action), None)
        if request is not None and not delayed:
            if stage=='completion':
                # Delay the successful completion boundary, never a later read
                # of an already failed case caused by an invalid provider intent.
                if not await uow.session.scalar(text("SELECT state='succeeded' AND source_completed FROM gateway.fixture_attempt_states WHERE attempt_id=:id"),
                    {'id':request.owner.id}):
                    # Sealing the provider session also checks result authority;
                    # this test delays only the later state/answer commit.
                    return result
                assert data.db.scalar("SELECT count(*) FROM gateway.bigquery_source_jobs WHERE state='done'") == 1
            delayed.extend(item.request_id for item in requests)
            await uow.session.scalar(text("SELECT pg_sleep(greatest(0,extract(epoch FROM (CAST(:expiry AS timestamptz)-clock_timestamp()))+0.02))"),
                {'expiry':request.accepted.effective_deadline_at})
        return result
    service.access.finalize = slow_gate
    result = asyncio.run(service.runner.run(principal(), 'fixture-draft'))
    assert delayed and result.status == 'failed', [(item['kind'],item['actual_error']) for item in result.evidence['checks']]
    assert all(not check['actual'] for check in result.evidence['checks'])
    assert data.db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow' AND correlation_id=ANY(%s)", (delayed,)) == 0
    if stage == 'admission':
        assert service.trace == []
        assert data.db.scalar('SELECT count(*) FROM gateway.fixture_attempts') == 0
        assert data.db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs') == 0
    else:
        assert service.trace == ['prepare','poll']
        state=data.db.scalar('SELECT row_to_json(s) FROM gateway.fixture_attempt_states s')
        assert (state['state'],state['error_code'],state['source_completed'])==('timed_out','EXECUTION_TIMEOUT',False)
        from gateway.contracts.canonical import canonical_digest
        check=next(item for item in result.evidence['checks'] if item['kind']=='known_answer')
        assert check['actual_error']=='EXECUTION_TIMEOUT' and state['check_digest']==canonical_digest(check)
        assert data.db.scalar('SELECT count(*) FROM gateway.held_source_capacity') == 0


def test_normalization_rejection_retains_exact_empty_failure_check(reviewed_fixture_database):
    from gateway.contracts.canonical import canonical_digest
    data = reviewed_fixture_database
    service = fixture_runner(data)
    service.hooks.corrupt_answer = True
    result = asyncio.run(service.runner.run(principal(), 'fixture-draft'))
    assert result.status == 'failed' and service.trace == ['prepare','poll']
    check = next(item for item in result.evidence['checks'] if item['kind'] == 'known_answer')
    assert not check['passed'] and not check['actual'] and check['actual_error'] == 'BINDING_DRIFT'
    assert data.db.scalar("SELECT check_digest FROM gateway.fixture_attempt_states WHERE attempt_id=%s", (check['attempt_id'],)) == canonical_digest(check)
    assert data.db.scalar("SELECT count(*) FROM gateway.fixture_attempt_states WHERE state='failed' AND source_completed AND error_code='BINDING_DRIFT'") == 1
    assert data.db.scalar('SELECT count(*) FROM gateway.held_source_capacity') == 0
