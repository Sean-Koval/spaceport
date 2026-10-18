"""Real registry/owner-approved choice release; no provider credential or query I/O."""
import asyncio

import pytest

from gateway.adapters.base import StaticBindingResolver
from gateway.adapters.bundles import BundleCatalog, CubeEndpoint
from gateway.api.choices import ChoiceService
from gateway.api.cursors import PageCursorCodec
from gateway.auth.principals import PrincipalStore
from gateway.contracts.clock import SystemClock
from gateway.contracts.errors import GatewayError
from gateway.policy.audit import AuditWriter
from gateway.policy.service import PolicyService
from gateway.source_access import SourceAccessService
from tests.integration.test_source_approval_registry import approval_assets, install
from tests.unit.test_source_approval_contracts import principal

pytestmark = pytest.mark.postgres


def composition(database, valid_manifest, *, approved=True):
    from copy import deepcopy
    from gateway.adapters.base import check_binding
    # The registry contract seed deliberately declares unknown freshness. This
    # independently approved metadata package must agree with that declaration.
    valid_manifest = deepcopy(valid_manifest)
    valid_manifest['freshness']['required'] = False
    document, manifest, bindings, _ = approval_assets(database, valid_manifest)
    check_binding(bindings[0], manifest, digest=bindings[0].digest)
    document['audiences'][0]['actions'].append('author')
    receipt = install(database, document) if approved else None
    registry = database.registry
    audit = AuditWriter(b'synthetic-choice-release-key-32-bytes')
    source_access = SourceAccessService(registry, audit)
    policy = PolicyService(registry, PrincipalStore(registry.sessions), audit, source_access=source_access)
    endpoint = CubeEndpoint(endpoint_id='synthetic-consumer', adapter_revision='cube-bigquery-v1',
        url='https://consumer.synthetic.svc.cluster.local', bundle_digest=manifest.model_bundle_digest,
        secret_ref='/run/secrets/synthetic-never-read', context='consumer')
    service = ChoiceService(registry=registry, policy=policy, bindings=StaticBindingResolver(bindings),
        bundles=BundleCatalog({'consumer': endpoint}), cursors=PageCursorCodec(b'c'*32, SystemClock()),
        templates=(manifest,))
    return service, receipt, bindings[0]


def test_real_choice_release_requires_owner_installed_exact_approval(registry_database, valid_manifest):
    service, receipt, binding = composition(registry_database, valid_manifest)
    reached = []
    finalize = service.policy.source_access.finalize
    async def observed_finalizer(uow, requests):
        reached.append(tuple(request.projection.approval_id for request in requests))
        return await finalize(uow, requests)
    service.policy.source_access.finalize = observed_finalizer
    page = asyncio.run(service.bindings(principal()))
    assert reached and reached[0] == (receipt['id'],)
    assert len(page.items) == 1
    assert page.items[0].digest == binding.digest
    assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE source_approval_id=%s AND decision='allow'", (receipt['id'],)) >= 2


def test_real_choice_missing_approval_releases_no_binding_metadata(registry_database, valid_manifest):
    service, _, _ = composition(registry_database, valid_manifest, approved=False)
    page = asyncio.run(service.bindings(principal()))
    assert page.items == () and page.next_cursor is None
    assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow' AND source_approval_id IS NOT NULL") == 0


def test_real_choice_wrong_authenticated_client_cannot_borrow_author_scope(registry_database, valid_manifest):
    service, _, _ = composition(registry_database, valid_manifest)
    actor = principal().model_copy(update={'service_client_id': 'not-reviewed-client'})
    page = asyncio.run(service.bindings(actor))
    assert page.items == () and page.next_cursor is None
    assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow' AND source_approval_id IS NOT NULL") == 0


def test_real_choice_revocation_between_candidate_and_final_page_hides_all(registry_database, valid_manifest):
    service, receipt, _ = composition(registry_database, valid_manifest)
    original = service._binding_targets
    calls = 0
    async def revoke_before_final(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            registry_database.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',
                (receipt['id'], receipt['content_digest'], 'synthetic-choice-revoke'))
        return await original(*args, **kwargs)
    service._binding_targets = revoke_before_final
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.bindings(principal()))
    assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
    assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE source_approval_id=%s AND decision='allow'", (receipt['id'],)) == 1


def test_real_choice_audit_failure_releases_no_projection(registry_database, valid_manifest):
    service, _, _ = composition(registry_database, valid_manifest)
    registry_database.execute_owner("""CREATE FUNCTION gateway.synthetic_choice_audit_failure() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN IF NEW.decision='allow' THEN
        RAISE EXCEPTION 'synthetic-private-audit-detail'; END IF; RETURN NEW; END $$""")
    registry_database.execute_owner('CREATE TRIGGER synthetic_choice_audit_failure BEFORE INSERT ON gateway.audit_events FOR EACH ROW EXECUTE FUNCTION gateway.synthetic_choice_audit_failure()')
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.bindings(principal()))
    assert caught.value.code == 'UPSTREAM_UNAVAILABLE'
    assert 'synthetic-private-audit-detail' not in str(caught.value)
    assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow'") == 0


def retained_draft(database, manifest):
    """Owner fixture models metadata retained before the approval rollout."""
    import psycopg
    from psycopg.types.json import Jsonb
    from tests.integration.conftest import connection_kwargs
    with psycopg.connect(**connection_kwargs(database.urls['owner'])) as owner:
        owner.execute('INSERT INTO gateway.products(id) VALUES(%s)', (manifest.product_id,))
        owner.execute('''INSERT INTO gateway.draft_revisions(id,product_id,revision,manifest,content_digest,actor_id)
            VALUES('retained-review-draft',%s,1,%s,%s,'synthetic-operator')''',
            (manifest.product_id, Jsonb(manifest.model_dump(mode='json')), manifest.content_digest))
        owner.execute("UPDATE gateway.products SET latest_draft_id='retained-review-draft',draft_revision=1 WHERE id=%s", (manifest.product_id,))
    return 'retained-review-draft'


@pytest.mark.parametrize('missing', [True, False])
def test_real_contextual_policy_choices_cannot_borrow_scope_without_draft_approval(registry_database, valid_manifest, missing):
    service, _, _ = composition(registry_database, valid_manifest, approved=not missing)
    draft_id = retained_draft(registry_database, service.templates[0])
    actor = principal() if missing else principal().model_copy(update={'service_client_id': 'not-reviewed-client'})
    async def scenario():
        # The same administrative policy scope remains visible without a product.
        inventory = await service.policies(actor)
        assert [item.policy_id for item in inventory.items] == ['revenue-access']
        with pytest.raises(GatewayError) as caught:
            await service.policies(actor, draft_id=draft_id)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
    asyncio.run(scenario())
    assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow' AND source_approval_id IS NOT NULL") == 0


@pytest.mark.parametrize('missing', [True, False])
def test_real_rest_choices_apply_same_source_gate_and_hide_context(registry_database, valid_manifest, missing):
    from types import SimpleNamespace
    from tests.contract.test_rest import request, services
    service, _, _ = composition(registry_database, valid_manifest, approved=not missing)
    draft_id = retained_draft(registry_database, service.templates[0])
    actor = principal() if missing else principal().model_copy(update={'service_client_id': 'not-reviewed-client'})
    transport = services(actor, SystemClock())
    transport.authenticator.settings = SimpleNamespace(source='reviewed_sources')
    transport.choices = service
    async def scenario():
        bindings = await request(transport, 'GET', '/v1/bindings')
        assert bindings.status_code == 200 and bindings.json() == {'items': [], 'next_cursor': None}
        context = await request(transport, 'GET', '/v1/policies?draft_id=' + draft_id)
        missing_context = await request(transport, 'GET', '/v1/policies?draft_id=unknown-draft')
        assert context.status_code == missing_context.status_code == 404
        first, second = context.json(), missing_context.json()
        first.pop('request_id'); second.pop('request_id')
        assert first == second
        assert service.templates[0].name not in context.text
        assert 'source_approval' not in context.text
    asyncio.run(scenario())
