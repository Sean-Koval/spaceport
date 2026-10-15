"""Real retained-owner ResultService release/audit; synthetic rows, no provider I/O."""
import asyncio
from copy import deepcopy

import psycopg
import pytest

from gateway.adapters.base import StaticBindingResolver
from gateway.adapters.bigquery_runtime import BigQueryBinding
from gateway.auth.principals import PrincipalStore
from gateway.contracts.clock import SystemClock
from gateway.contracts.errors import GatewayError
from gateway.execution.results import ResultService
from gateway.policy.audit import AuditWriter
from gateway.policy.service import PolicyService
from gateway.source_access import SourceAccessService
from tests.integration.conftest import connection_kwargs
from tests.integration.test_source_approval_registry import install
from tests.integration.test_source_approval_lifecycle import approved_source_database
from tests.unit.test_source_approval_contracts import principal

pytestmark = pytest.mark.postgres


def composition(db, valid_manifest, *, permit_result=True, expires=None):
    # This helper is provided by the real accepted-owner migration milestone.
    from tests.integration.test_source_approval_transactions import consumer_setup, insert_consumer
    manifest_value = deepcopy(valid_manifest)
    manifest_value['freshness']['required'] = False
    if hasattr(db, 'approved'):
        assert permit_result and expires is None
        data = db.approved
        document, manifest, receipt, product = data.document, data.manifest, data.approval, data.product
    else:
        document, manifest, receipt, product = consumer_setup(db, manifest_value, approve=permit_result, expires=expires)
    if not permit_result:
        document['audiences'][0]['actions'].remove('result')
        receipt = install(db, document)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as owner:
        insert_consumer(owner, manifest)
    binding = BigQueryBinding.model_validate(db.scalar(
        "SELECT content FROM gateway.binding_revisions WHERE id='consumer-binding-row'"))
    audit = AuditWriter(b'synthetic-real-result-release-key-32')
    access = SourceAccessService(db.registry, audit)
    policy = PolicyService(db.registry, PrincipalStore(db.registry.sessions), audit, source_access=access)
    service = ResultService(db.registry, policy, StaticBindingResolver([binding]), SystemClock())
    # Publication has genuine retained fixture-result allows and source history.
    # Keep that proof intact while identifying the separate consumer request.
    db.result_seed_audits = db.scalar("SELECT jsonb_agg(to_jsonb(e) ORDER BY e.id) FROM gateway.audit_events e WHERE action='result'")
    db.result_seed_sources = fixture_source_history(db)
    assert db.result_seed_audits and all(event['decision'] == 'allow' for event in db.result_seed_audits)
    assert all(event['actor_id'] != principal().id for event in db.result_seed_audits)
    assert db.result_seed_sources['sessions'] and db.result_seed_sources['jobs']
    assert consumer_result_audits(db) == []
    return service, receipt, document, product


def revoke(db, receipt):
    db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',
        (receipt['id'], receipt['content_digest'], 'synthetic-result-revocation'))


def fixture_source_history(db):
    return db.scalar("""SELECT jsonb_build_object(
        'sessions', (SELECT jsonb_agg(to_jsonb(s) ORDER BY s.id)
            FROM gateway.source_sessions s WHERE s.context='fixture_test'),
        'jobs', (SELECT jsonb_agg(to_jsonb(j) ORDER BY j.job_id)
            FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id
            WHERE s.context='fixture_test'))""")


def consumer_result_audits(db):
    """Exact actor/client result events, without erasing prior fixture proof."""
    preserved = db.scalar("SELECT jsonb_agg(to_jsonb(e) ORDER BY e.id) FROM gateway.audit_events e WHERE id=ANY(%s)",
        ([event['id'] for event in db.result_seed_audits],))
    assert preserved == db.result_seed_audits
    assert fixture_source_history(db) == db.result_seed_sources
    events = db.scalar("""SELECT coalesce(jsonb_agg(to_jsonb(e) ORDER BY e.id),'[]'::jsonb)
        FROM gateway.audit_events e WHERE action='result' AND id<>ALL(%s)""",
        ([event['id'] for event in db.result_seed_audits],))
    # Check every new result event, so an extra wrong-identity event cannot hide
    # outside an actor-filtered query or make the exact audit counts weaker.
    assert all(event['actor_id'] == principal().id
        and event['service_client_id'] == principal().service_client_id for event in events)
    return events


def assert_result_allows(db, approval_id, *, requests):
    from collections import Counter
    events = consumer_result_audits(db)
    assert len(events) == 2 * requests
    assert all(event['decision'] == 'allow' and event['source_approval_id'] == approval_id for event in events)
    correlations = Counter(event['correlation_id'] for event in events)
    assert len(correlations) == requests and set(correlations.values()) == {2}


def assert_one_safe_denial(db, request_id):
    events = consumer_result_audits(db)
    assert len(events) == 1
    denied = events[0]
    assert denied['decision'] == 'deny'
    assert denied['correlation_id'] == request_id
    assert denied['actor_id'] == principal().id
    assert denied['service_client_id'] == principal().service_client_id
    assert denied['product_id'] is None and denied['version_id'] is None
    assert denied['parameter_names'] == []


def test_real_result_status_release_uses_original_owner_and_current_approval(registry_database, valid_manifest):
    db = registry_database
    service, receipt, _, _ = composition(db, valid_manifest)
    view = asyncio.run(service.get(principal(), 'execution-1'))
    assert view.execution_id == 'execution-1' and view.state == 'accepted'
    assert_result_allows(db, receipt['id'], requests=1)
    assert db.scalar("SELECT count(*) FROM gateway.source_sessions WHERE context='consumer'") == 0
    assert db.scalar("SELECT count(*) FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context='consumer'") == 0


@pytest.mark.parametrize('stage', ['owner', 'finalize'])
def test_real_result_revocation_or_final_expiry_denies_once_without_releasing_status(registry_database, valid_manifest, stage):
    from datetime import timezone
    db = registry_database
    expiry = db.scalar("SELECT clock_timestamp()+interval '4 seconds'") if stage == 'finalize' else None
    service, receipt, _, _ = composition(db, valid_manifest,
        expires=expiry.astimezone(timezone.utc).isoformat().replace('+00:00','Z') if expiry else None)
    reached = []
    if stage == 'owner':
        revoke(db, receipt)
    else:
        original = service.policy.source_access.finalize
        async def expire_before_finalizer(uow, requests):
            # Revocation serializes on C; elapsed time still changes authority
            # after the private projection even while its lock prefix is held.
            reached.append(requests[0].projection.approval_id)
            db.scalar("SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))", (expiry,))
            return await original(uow, requests)
        service.policy.source_access.finalize = expire_before_finalizer
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.get(principal(), 'execution-1'))
    assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
    if stage == 'finalize': assert reached == [receipt['id']]
    assert_one_safe_denial(db, caught.value.request_id)


def test_real_result_action_cannot_borrow_retained_execute_permission(registry_database, valid_manifest):
    db = registry_database
    service, _, _, _ = composition(db, valid_manifest, permit_result=False)
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.get(principal(), 'execution-1'))
    assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
    assert_one_safe_denial(db, caught.value.request_id)


def test_real_result_failed_source_deny_audit_returns_unavailable(registry_database, valid_manifest):
    db = registry_database
    service, receipt, _, _ = composition(db, valid_manifest)
    revoke(db, receipt)
    db.execute_owner("""CREATE FUNCTION gateway.synthetic_result_deny_failure() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN IF NEW.decision='deny' THEN
        RAISE EXCEPTION 'synthetic-private-result-audit'; END IF; RETURN NEW; END $$""")
    db.execute_owner('CREATE TRIGGER synthetic_result_deny_failure BEFORE INSERT ON gateway.audit_events FOR EACH ROW EXECUTE FUNCTION gateway.synthetic_result_deny_failure()')
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.get(principal(), 'execution-1'))
    assert caught.value.code == 'UPSTREAM_UNAVAILABLE'
    assert 'synthetic-private-result-audit' not in str(caught.value)
    assert consumer_result_audits(db) == []


def test_real_result_expiry_after_outcome_audit_discards_all_allows(registry_database, valid_manifest):
    from datetime import timezone
    from sqlalchemy import text
    db = registry_database
    expiry = db.scalar("SELECT clock_timestamp()+interval '4 seconds'")
    service, _, _, _ = composition(db, valid_manifest,
        expires=expiry.astimezone(timezone.utc).isoformat().replace('+00:00','Z'))
    original = service.policy.audit.append
    provisional = []
    async def delayed_outcome(uow, event):
        result = await original(uow, event)
        if event.decision == 'allow':
            provisional.append(event.correlation_id)
            await uow.session.scalar(text("SELECT pg_sleep(greatest(0,extract(epoch FROM (CAST(:expiry AS timestamptz)-clock_timestamp()))+0.02))"),
                {'expiry': expiry})
        return result
    service.policy.audit.append = delayed_outcome
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.get(principal(), 'execution-1'))
    assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
    assert provisional == [caught.value.request_id]
    assert_one_safe_denial(db, caught.value.request_id)


def retained_result_rows(db, service, product, *, count=201, retention_seconds=3600):
    """Synthetic rows after actual approved session/terminal SQL; no provider I/O.

    The mapped synthetic provider records exact retained job metadata through its
    normal restricted function. No completion trigger is disabled. Row content is
    an app-role fixture for release tests, not worker/normalization qualification.
    """
    from datetime import timedelta
    from psycopg.types.json import Jsonb
    from sqlalchemy import text
    from gateway.adapters.normalize import NormalizationContext, normalize
    from gateway.contracts.execution import Completed, ExecutionSpec, ExecutionView
    from gateway.contracts.identity import Decision
    from gateway.execution.results import prepare_stored_result
    from gateway.registry.models import Execution, ExecutionAuthorization
    from tests.conftest import FakeClock
    from tests.integration.test_source_approval_lifecycle import seed_approved_source_session
    from tests.integration.test_source_lifecycle import call, intent
    seeded = seed_approved_source_session(db, existing=True)
    context = seeded.context.wire_context()
    job = call(db, 'bq_runtime', 'SELECT gateway.source_bq_admit(%s,%s,%s)', context, 'e'*64, intent())['job']
    call(db, 'bq_runtime', 'SELECT gateway.source_bq_observe(%s,%s)', job['job_id'],
        dict(intent_digest=job['intent_digest'], state='done', terminal_error=False))
    call(db, 'bq_runtime', 'SELECT gateway.source_seal_attempt(%s,%s)', context, 'completed')
    assert asyncio.run(seeded.lifecycle.close_group(seeded.group.group_id, 'completed')) == {'closed':True, 'settled':True}
    async def build():
        async with db.registry.transaction() as uow:
            execution = await uow.session.get(Execution,'execution-1')
            authority = await uow.session.get(ExecutionAuthorization,execution.id)
            decision = Decision.model_validate(authority.decision)
            now = await uow.session.scalar(text('SELECT clock_timestamp()'))
            spec = ExecutionSpec(principal_id=execution.principal_id,pinned_product=product,
                canonical_query=execution.canonical_query,canonical_hash=execution.canonical_hash,
                policy_revision=execution.policy_revision,context=execution.context,request_id=execution.request_id)
            binding = await service.bindings.resolve(product.manifest.binding_id,product.manifest.binding_revision)
            view = ExecutionView(execution_id=execution.id,state='succeeded',accepted_at=execution.created_at,
                started_at=now,completed_at=now)
            rows = [{'Orders.ordered_at.day':'2024-03-10T00:00:00.000',
                'Orders.segment':f'synthetic-segment-{index}','Orders.revenue':'10.10'} for index in range(count)]
            envelope = normalize(product,Completed(raw_rows=rows,source_watermark=None),view,
                context=NormalizationContext(spec=spec,decision=decision,executed_decision=decision,
                    binding=binding,clock=FakeClock(now)))
            return prepare_stored_result(execution,envelope,decision,now), now
    stored, now = asyncio.run(build())
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as app:
        app.execute("UPDATE gateway.execution_states SET state='succeeded',started_at=%s,completed_at=%s WHERE execution_id='execution-1'",(now,now))
        app.execute('''INSERT INTO gateway.results(id,execution_id,principal_id,version_id,policy_revision,
            canonical_hash,envelope,expires_at) VALUES('retained-result',%s,%s,%s,%s,%s,%s,%s)''',
            (stored.execution_id,stored.principal_id,stored.version_id,stored.policy_revision,
             stored.canonical_hash,Jsonb(stored.envelope),now+timedelta(seconds=retention_seconds)))
    return now+timedelta(seconds=retention_seconds)


def test_real_result_each_cursor_page_rechecks_original_approval(approved_source_database,valid_manifest):
    db=approved_source_database
    service,receipt,_,product=composition(db,valid_manifest)
    retained_result_rows(db,service,product)
    async def scenario():
        first=await service.get(principal(),'execution-1')
        second=await service.get(principal(),'execution-1',first.next_cursor)
        assert len(first.rows)==200 and len(second.rows)==1
        assert first.row_count==second.row_count==201 and second.next_cursor is None
    asyncio.run(scenario())
    assert_result_allows(db, receipt['id'], requests=2)


def test_real_result_revoked_cursor_cannot_release_retained_rows(approved_source_database,valid_manifest):
    db=approved_source_database
    service,receipt,_,product=composition(db,valid_manifest)
    retained_result_rows(db,service,product)
    first=asyncio.run(service.get(principal(),'execution-1'))
    assert first.next_cursor
    revoke(db,receipt)
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.get(principal(),'execution-1',first.next_cursor))
    assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
    events = consumer_result_audits(db)
    allowed = [event for event in events if event['decision'] == 'allow']
    denied = [event for event in events if event['decision'] == 'deny']
    assert len(events) == 3 and len(allowed) == 2 and len(denied) == 1
    assert len({event['correlation_id'] for event in allowed}) == 1
    assert all(event['correlation_id'] != caught.value.request_id for event in allowed)
    assert denied[0]['correlation_id'] == caught.value.request_id
    assert db.scalar('SELECT count(*) FROM gateway.results')==1
    assert db.scalar('SELECT count(*) FROM gateway.result_cursors')==1


def test_real_result_commit_audit_failure_rolls_back_rows_page_and_new_cursor(approved_source_database,valid_manifest):
    db=approved_source_database
    service,_,_,product=composition(db,valid_manifest)
    retained_result_rows(db,service,product)
    db.execute_owner("""CREATE FUNCTION gateway.synthetic_result_allow_commit_failure() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN IF NEW.decision='allow' THEN
        RAISE EXCEPTION 'synthetic-private-result-commit'; END IF; RETURN NEW; END $$""")
    db.execute_owner('CREATE CONSTRAINT TRIGGER synthetic_result_allow_commit_failure AFTER INSERT ON gateway.audit_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.synthetic_result_allow_commit_failure()')
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.get(principal(),'execution-1'))
    assert caught.value.code=='UPSTREAM_UNAVAILABLE'
    assert 'synthetic-private-result-commit' not in str(caught.value)
    assert consumer_result_audits(db) == []
    assert db.scalar('SELECT count(*) FROM gateway.result_cursors')==0
    assert db.scalar('SELECT count(*) FROM gateway.results')==1


@pytest.mark.parametrize('expires', ['result', 'cursor'])
def test_real_result_retention_expiring_during_audit_discards_rows_and_cursor(approved_source_database, valid_manifest, expires):
    from sqlalchemy import text
    from gateway.execution.cursors import cursor_digest, new_cursor_token
    db = approved_source_database
    service, _, _, product = composition(db, valid_manifest)
    expiry = retained_result_rows(db, service, product, count=401,
        retention_seconds=4 if expires == 'result' else 3600)
    cursor = None
    if expires == 'cursor':
        expiry = db.scalar("SELECT clock_timestamp()+interval '4 seconds'")
        cursor = new_cursor_token()
        db.execute_owner('''INSERT INTO gateway.result_cursors(id,token_digest,result_id,principal_id,
            version_id,policy_revision,position,expires_at)
            VALUES('short-retained-cursor',%s,'retained-result','north-user','version-1',1,200,%s)''',
            (cursor_digest(cursor), expiry))
    original = service.policy.audit.append
    delayed = []
    async def slow_outcome(uow, event):
        value = await original(uow, event)
        if event.decision == 'allow' and not delayed:
            delayed.append(event.correlation_id)
            await uow.session.scalar(text("SELECT pg_sleep(greatest(0,extract(epoch FROM (CAST(:expiry AS timestamptz)-clock_timestamp()))+0.02))"),
                {'expiry': expiry})
        return value
    service.policy.audit.append = slow_outcome
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.get(principal(), 'execution-1', cursor))
    assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
    assert delayed == [caught.value.request_id]
    assert_one_safe_denial(db, caught.value.request_id)
    assert db.scalar('SELECT count(*) FROM gateway.result_cursors') == int(expires == 'cursor')
    assert db.scalar('SELECT count(*) FROM gateway.results') == 1
