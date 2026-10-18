"""Real reviewed publication callers and protected retained fixture evidence.

Provider terminal metadata is exercised through SQL without provider I/O.
"""
import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text

from gateway.auth.principals import PrincipalStore
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import PinnedProduct
from gateway.policy.audit import AuditWriter
from gateway.policy.service import PolicyService
from gateway.publishing.service import PublishingService
from gateway.source_access import SourceAccessService
from tests.integration.test_source_approval_registry import approval_assets, install
from tests.unit.test_source_approval_contracts import principal

pytestmark = pytest.mark.postgres


def composition(database, valid_manifest, actions):
    document, manifest, bindings, _ = approval_assets(database, valid_manifest)
    document['audiences'][0]['actions'] = list(actions)
    installed = install(database, document)
    audit = AuditWriter(b'publishing-source-actions-key-32!')
    source = SourceAccessService(database.registry, audit)
    policy = PolicyService(database.registry, PrincipalStore(database.registry.sessions), audit, source_access=source)
    publishing = PublishingService(registry=database.registry, policy=policy, bindings=None, bundles=None)
    target = PinnedProduct(manifest=manifest, binding_digest=bindings[0].digest, version_id=str(uuid4()))
    return publishing, target, installed


@pytest.mark.parametrize('actions', [('author',), ('publish',), ('author', 'publish')])
def test_prepare_review_retains_each_independent_owner_approved_action(registry_database, valid_manifest, actions):
    publishing, target, installed = composition(registry_database, valid_manifest, actions)
    async def scenario():
        async with registry_database.registry.transaction() as uow:
            await publishing._prelock_reviews((target,), uow)
            transaction = uow.session.get_transaction()
            decision, capabilities, requests = await publishing._prepare_review(principal(), target, uow)
            assert set(capabilities) == set(actions)
            assert {request.action for request in requests} == set(actions)
            assert all(request.decision == decision and request.principal == principal() for request in requests)
            assert all(request.projection.approval_id == installed['id'] for request in requests)
            assert len({request.request_id for request in requests}) == len(actions)
            assert uow.session.get_transaction() is transaction
            assert await uow.session.scalar(text('SELECT 1')) == 1
        assert registry_database.scalar('SELECT count(*) FROM gateway.audit_events') == 0
    asyncio.run(scenario())


def test_prepare_review_denies_other_authenticated_client_with_one_safe_audit(registry_database, valid_manifest):
    publishing, target, _ = composition(registry_database, valid_manifest, ('author', 'publish'))
    async def scenario():
        other = principal().model_copy(update={'service_client_id': 'unapproved-client'})
        with pytest.raises(GatewayError) as caught:
            async with registry_database.registry.transaction() as uow:
                await publishing._prelock_reviews((target,), uow)
                await publishing._prepare_review(other, target, uow)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert registry_database.scalar('SELECT count(*) FROM gateway.audit_events') == 1
        assert registry_database.scalar("SELECT bool_and(decision='deny' AND parameter_names='[]'::jsonb) FROM gateway.audit_events")
    asyncio.run(scenario())


async def draft_composition(database, valid_manifest, *, expires=None):
    """A real reviewed draft without any accepted fixture invocation/evidence."""
    from datetime import timezone
    from types import SimpleNamespace
    from gateway.adapters.base import StaticBindingResolver
    from copy import deepcopy
    manifest_data = deepcopy(valid_manifest)
    manifest_data['freshness']['required'] = False
    document, manifest, bindings, _ = approval_assets(database, manifest_data)
    document['audiences'][0]['actions'] = ['author', 'publish']
    if expires is not None:
        document['expires_at'] = expires.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    installed = install(database, document)
    audit = AuditWriter(b'publishing-source-actions-key-32!')
    access = SourceAccessService(database.registry, audit)
    policy = PolicyService(database.registry, PrincipalStore(database.registry.sessions), audit, source_access=access)
    publishing = PublishingService(registry=database.registry, policy=policy,
        bindings=StaticBindingResolver(bindings), bundles=None)
    draft = await publishing.save_draft(principal(), manifest, 0)
    return SimpleNamespace(service=publishing, draft=draft, audit=audit, installed=installed)


def test_diff_of_reviewed_draft_without_fixture_evidence_releases_only_finalized_metadata(registry_database, valid_manifest):
    async def scenario():
        db = registry_database
        seeded = await draft_composition(db, valid_manifest)
        before = db.scalar('SELECT count(*) FROM gateway.audit_events')
        result = await seeded.service.diff(principal(), seeded.draft.id)
        assert result.latest_test_run_id is None and result.stale_evidence == ('missing',)
        assert result.draft_id == seeded.draft.id and result.changes
        assert db.scalar('SELECT count(*) FROM gateway.audit_events') == before + 2
        assert db.scalar("SELECT bool_and(source_access_reason_code='APPROVED') FROM gateway.audit_events")
        assert db.scalar('SELECT count(*) FROM gateway.fixture_attempts') == 0
        assert db.scalar('SELECT count(*) FROM gateway.source_operation_groups') == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['revoked', 'expires_during_audit', 'allow_audit'])
def test_missing_evidence_diff_releases_nothing_after_final_gate_failure(registry_database, valid_manifest, failure):
    from gateway.policy.audit import audit_unavailable
    async def scenario():
        db = registry_database
        expires = db.scalar("SELECT clock_timestamp()+interval '2 seconds'") if failure == 'expires_during_audit' else None
        seeded = await draft_composition(db, valid_manifest, expires=expires)
        before = db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow'")
        allow_attempts = []
        if failure == 'revoked':
            db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',
                (seeded.installed['id'], seeded.installed['content_digest'], 'synthetic-review-ended'))
        else:
            append = seeded.audit.append
            async def gated_append(uow, event):
                if event.decision == 'allow':
                    allow_attempts.append(event.correlation_id)
                    if failure == 'allow_audit':
                        raise audit_unavailable()
                    await uow.session.execute(text('''SELECT pg_sleep(greatest(0,
                        extract(epoch FROM (CAST(:expiry AS timestamptz)-clock_timestamp())))+0.03)'''), {'expiry': expires})
                return await append(uow, event)
            seeded.audit.append = gated_append
        with pytest.raises(GatewayError) as caught:
            await seeded.service.diff(principal(), seeded.draft.id)
        assert caught.value.code in ('NOT_FOUND_OR_FORBIDDEN', 'UPSTREAM_UNAVAILABLE')
        if failure != 'revoked':
            assert allow_attempts
        assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow'") == before
        assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='deny'") == 1
        assert db.scalar("SELECT bool_and(parameter_names='[]'::jsonb) FROM gateway.audit_events WHERE decision='deny'")
        assert db.scalar('SELECT count(*) FROM gateway.test_runs') == 0
        assert db.scalar('SELECT count(*) FROM gateway.product_versions') == 0
    asyncio.run(scenario())


from tests.integration.test_source_fixture_owners import reviewed_fixture_database


def retained_evidence_composition(database):
    """Protected app-owned invocation/attempt/terminal evidence, no provider I/O."""
    from types import SimpleNamespace
    from tests.integration.test_source_fixture_owners import seed_fixture_session, complete_fixture_evidence
    from tests.integration.test_source_approval_fixture_runner import fixture_runner
    seeded = seed_fixture_session(database)
    complete_fixture_evidence(database, seeded)
    composition = fixture_runner(SimpleNamespace(db=database, catalog=database.fixture.catalog))
    runner = composition.runner
    publishing = PublishingService(registry=database.registry, policy=composition.policy,
        bindings=runner.bindings, bundles=runner.bundles, fixture_runner=runner)
    return publishing, seeded


@pytest.mark.parametrize('operation', ['get_test','diff'])
def test_retained_evidence_reads_use_real_original_fixture_owners(reviewed_fixture_database, operation):
    db = reviewed_fixture_database
    publishing, seeded = retained_evidence_composition(db)
    stored = db.scalar('SELECT evidence FROM gateway.test_runs WHERE id=%s', (seeded.invocation['invocation_id'],))
    async def scenario():
        if operation == 'get_test':
            result = await publishing.get_test(principal(), 'fixture-draft', seeded.invocation['invocation_id'])
            assert result.id == seeded.invocation['invocation_id'] and result.actor_id == 'north-user'
        else:
            result = await publishing.diff(principal(), 'fixture-draft')
            assert result.latest_test_run_id == seeded.invocation['invocation_id'] and not result.stale_evidence
        assert db.scalar('SELECT evidence FROM gateway.test_runs WHERE id=%s', (seeded.invocation['invocation_id'],)) == stored
        assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE actor_id='fixed-positive' AND action='result' AND source_access_reason_code='APPROVED'") >= 2
        assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations') == 0
    asyncio.run(scenario())


def test_reviewed_publish_consumes_one_exact_recorder_receipt(reviewed_fixture_database):
    db = reviewed_fixture_database
    publishing, seeded = retained_evidence_composition(db)
    async def forbidden_finalize(*args):
        raise AssertionError('Publication must not take an ordinary A phase before its recorder')
    publishing.policy.source_access.finalize = forbidden_finalize
    async def scenario():
        product = await publishing.publish(principal(),'fixture-draft',db.fixture.manifest.content_digest,1,'Reviewed release')
        row = db.scalar('''SELECT to_jsonb(v) FROM gateway.product_versions v WHERE id=%s''',(product.version_id,))
        receipt = db.scalar('SELECT to_jsonb(o) FROM gateway.source_publication_operations o WHERE id=%s',(row['source_operation_id'],))
        assert receipt['draft_id'] == 'fixture-draft' and receipt['test_run_id'] == seeded.invocation['invocation_id']
        assert receipt['actor_snapshot'] == principal().model_dump(mode='json')
        assert receipt['expected_product_revision'] == 0 and receipt['next_product_revision'] == 1
        assert db.scalar('SELECT source_operation_id::text FROM gateway.products WHERE id=%s',(product.manifest.product_id,)) == row['source_operation_id']
        assert db.scalar('SELECT source_operation_id::text FROM gateway.version_states WHERE version_id=%s',(product.version_id,)) == row['source_operation_id']
        assert db.scalar('SELECT request_id FROM gateway.publication_events WHERE source_operation_id=%s',(row['source_operation_id'],)) == receipt['request_id']
        assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations') == 1
    asyncio.run(scenario())


def test_reviewed_service_transitions_advance_each_protected_revision_once(reviewed_fixture_database):
    db = reviewed_fixture_database
    publishing, _ = retained_evidence_composition(db)
    async def scenario():
        product = await publishing.publish(principal(),'fixture-draft',db.fixture.manifest.content_digest,1,'Reviewed release')
        receipts = {db.scalar('SELECT source_operation_id FROM gateway.products')}
        for revision,action in enumerate(('deprecate','rollback','withdraw'),2):
            await publishing.transition(principal(),product.manifest.product_id,product.manifest.version,action,'Reviewed lifecycle')
            assert db.scalar('SELECT publication_revision FROM gateway.products') == revision
            operation = db.scalar('SELECT source_operation_id FROM gateway.products')
            assert operation not in receipts
            receipts.add(operation)
            assert db.scalar('SELECT source_operation_id FROM gateway.version_states') == operation
            assert db.scalar('SELECT action FROM gateway.publication_events WHERE source_operation_id=%s',(operation,)) == action
        assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations') == 4
        assert db.scalar('SELECT status FROM gateway.version_states') == 'withdrawn'
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['get_test','diff','publish'])
def test_replacement_publisher_approval_never_revives_original_fixture_evidence(reviewed_fixture_database, operation):
    from copy import deepcopy
    db = reviewed_fixture_database
    publishing, seeded = retained_evidence_composition(db)
    old = db.fixture.approval
    db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)', (old['id'],old['content_digest'],'fixture-review-ended'))
    replacement = deepcopy(db.fixture.document)
    replacement['review']['review_id'] = 'replacement-publisher-review'
    assert install(db,replacement)['id'] != old['id']
    async def scenario():
        with pytest.raises(GatewayError):
            if operation == 'get_test':
                await publishing.get_test(principal(),'fixture-draft',seeded.invocation['invocation_id'])
            elif operation == 'diff':
                await publishing.diff(principal(),'fixture-draft')
            else:
                await publishing.publish(principal(),'fixture-draft',db.fixture.manifest.content_digest,1,'Reviewed release')
        assert db.scalar('SELECT count(*) FROM gateway.product_versions') == 0
        assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations') == 0
        assert db.scalar('SELECT publication_revision FROM gateway.products') == 0
        assert str(db.scalar('SELECT approval_id FROM gateway.fixture_source_invocations')) == old['id']
    asyncio.run(scenario())


def test_concurrent_reviewed_publishers_commit_one_version_and_one_receipt(reviewed_fixture_database):
    db = reviewed_fixture_database
    publishing, _ = retained_evidence_composition(db)
    async def scenario():
        async def publish_once():
            try:
                return await publishing.publish(principal(),'fixture-draft',db.fixture.manifest.content_digest,1,'Reviewed release')
            except GatewayError as error:
                return error
        results = await asyncio.gather(publish_once(),publish_once())
        assert sum(isinstance(value,PinnedProduct) for value in results) == 1
        assert sum(isinstance(value,GatewayError) for value in results) == 1
        assert db.scalar('SELECT count(*) FROM gateway.product_versions') == 1
        assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations') == 1
        assert db.scalar('SELECT count(*) FROM gateway.publication_events') == 1
        assert db.scalar('SELECT publication_revision FROM gateway.products') == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['audit','after_recorder'])
def test_publication_gate_failure_rolls_back_allow_receipt_and_head_revision(reviewed_fixture_database, failure):
    from gateway.policy.audit import audit_unavailable
    from gateway.registry.repository import not_found
    db = reviewed_fixture_database
    publishing, _ = retained_evidence_composition(db)
    before = db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow'")
    reached = []
    if failure == 'audit':
        original = publishing.policy.audit.append
        async def fail_audit(uow,event):
            if event.action == 'publish' and event.decision == 'allow':
                reached.append('audit')
                raise audit_unavailable()
            return await original(uow,event)
        publishing.policy.audit.append = fail_audit
    else:
        original = publishing.policy.source_access.record_publication_operation
        async def fail_after_recorder(uow,**kwargs):
            operation = await original(uow,**kwargs)
            reached.append(operation)
            raise not_found()
        publishing.policy.source_access.record_publication_operation = fail_after_recorder
    async def scenario():
        with pytest.raises(GatewayError):
            await publishing.publish(principal(),'fixture-draft',db.fixture.manifest.content_digest,1,'Reviewed release')
        assert reached
        assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow'") == before
        assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations') == 0
        assert db.scalar('SELECT count(*) FROM gateway.publication_events') == 0
        assert db.scalar('SELECT count(*) FROM gateway.product_versions') == 0
        assert db.scalar('SELECT publication_revision FROM gateway.products') == 0
        assert db.scalar('SELECT count(*) FROM gateway.test_runs') == 1
    asyncio.run(scenario())


def test_later_publisher_keeps_original_fixture_approval_and_cannot_replace_it(reviewed_fixture_database):
    from copy import deepcopy
    from gateway.contracts.identity import Principal
    db = reviewed_fixture_database
    publishing, seeded = retained_evidence_composition(db)
    reviewer = Principal(id='later-publisher',subject='later-publisher',kind='user',service_client_id='review-client')
    db.execute_owner("INSERT INTO gateway.principals(id,subject,kind,service_client_id) VALUES('later-publisher','later-publisher','user','review-client')")
    db.execute_owner("INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) VALUES('later-publisher','revenue-access','north','[\"author\",\"publisher\"]','{\"groups\":[\"north\"]}')")
    document = deepcopy(db.fixture.document)
    document['review']['review_id'] = 'later-publisher-review'
    document['audiences'][0].update(id='later-publisher',principal_id=reviewer.id,client_ids=[reviewer.service_client_id])
    reviewer_approval = install(db,document)
    original = db.fixture.approval
    assert reviewer_approval['id'] != original['id']
    async def scenario():
        evidence = await publishing.get_test(reviewer,'fixture-draft',seeded.invocation['invocation_id'])
        assert evidence.actor_id == principal().id
        diff = await publishing.diff(reviewer,'fixture-draft')
        assert diff.latest_test_run_id == seeded.invocation['invocation_id'] and not diff.stale_evidence
        product = await publishing.publish(reviewer,'fixture-draft',db.fixture.manifest.content_digest,1,'Independent reviewer release')
        receipt = db.scalar('SELECT to_jsonb(o) FROM gateway.source_publication_operations o')
        assert receipt['publisher_approval_id'] == reviewer_approval['id']
        assert receipt['fixture_approval_id'] == original['id']
        assert receipt['actor_snapshot'] == reviewer.model_dump(mode='json')
        assert str(db.scalar('SELECT approval_id FROM gateway.fixture_source_invocations')) == original['id']
        db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(original['id'],original['content_digest'],'original-fixture-review-ended'))
        assert db.scalar('SELECT state FROM gateway.source_access_approval_states WHERE approval_id=%s',(reviewer_approval['id'],)) == 'active'
        for operation in ('get_test','diff','transition'):
            with pytest.raises(GatewayError):
                if operation == 'get_test':
                    await publishing.get_test(reviewer,'fixture-draft',seeded.invocation['invocation_id'])
                elif operation == 'diff':
                    await publishing.diff(reviewer,'fixture-draft')
                else:
                    await publishing.transition(reviewer,product.manifest.product_id,product.manifest.version,'deprecate','Original review revoked')
        assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations') == 1
        assert db.scalar('SELECT publication_revision FROM gateway.products') == 1
        assert db.scalar('SELECT status FROM gateway.version_states') == 'published'
    asyncio.run(scenario())


from tests.integration.test_source_approval_fixture_runner import reviewed_fixture_database as reviewed_runner_database


def test_publishing_test_runs_real_fixture_then_releases_original_owner_evidence(reviewed_runner_database):
    from tests.integration.test_source_approval_fixture_runner import fixture_runner
    data=reviewed_runner_database
    composition=fixture_runner(data)
    runner=composition.runner
    publishing=PublishingService(registry=data.db.registry,policy=composition.policy,
        bindings=runner.bindings,bundles=runner.bundles,fixture_runner=runner)
    async def scenario():
        tested=await publishing.test(principal(),'fixture-draft')
        assert tested.status=='passed' and tested.actor_id==principal().id
        assert tested.draft_id=='fixture-draft' and tested.draft_digest==data.manifest.content_digest
        assert all(check['passed'] for check in tested.evidence['checks'])
        assert composition.trace==['prepare','poll']
        reread=await publishing.get_test(principal(),'fixture-draft',tested.id)
        assert reread.id==tested.id and reread.evidence==tested.evidence
        assert data.db.scalar('SELECT count(*) FROM gateway.fixture_source_invocations')==1
        assert data.db.scalar('SELECT count(*) FROM gateway.fixture_attempt_source_authorities')==1
        assert data.db.scalar('SELECT count(*) FROM gateway.fixture_test_source_authorities')==1
        assert data.db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
        assert data.db.scalar('SELECT count(*) FROM gateway.source_publication_operations')==0
        assert data.db.scalar('SELECT count(*) FROM gateway.executions')==0
        assert data.db.scalar('SELECT count(*) FROM gateway.results')==0
    asyncio.run(scenario())
