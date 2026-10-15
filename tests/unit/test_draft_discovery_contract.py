"""Pure sequencing/transport checks; database authorization is tested separately."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from gateway.api.cursors import PageCursorCodec
from gateway.contracts.errors import GatewayError
from tests.contract.test_rest import services, request

pytestmark = pytest.mark.unit


def test_draft_route_dispatch_is_read_only_and_no_store(north_principal, clock):
    svc = services(north_principal, clock)
    svc.drafts = SimpleNamespace(search=AsyncMock(return_value={'items': [], 'next_cursor': None}))
    response = asyncio.run(request(svc, 'GET', '/v1/drafts?q=daily&limit=3'))
    assert response.status_code == 200
    assert response.json() == {'items': [], 'next_cursor': None}
    assert response.headers['cache-control'] == 'no-store'
    svc.drafts.search.assert_awaited_once_with(north_principal, query='daily', cursor=None, limit=3)


@pytest.mark.parametrize('suffix', ['?limit=51', '?limit=0', '?limit=true', '?q=a&q=b', '?role=author', '?cursor=', '?q='+'x'*257])
def test_draft_route_rejects_unbounded_and_authority_arguments(north_principal, clock, suffix):
    svc = services(north_principal, clock)
    svc.drafts = SimpleNamespace(search=AsyncMock())
    response = asyncio.run(request(svc, 'GET', '/v1/drafts'+suffix))
    assert response.status_code == 422
    svc.drafts.search.assert_not_awaited()


def test_summary_has_only_review_metadata_and_capability_hints():
    from gateway.api.models import DraftSummary, DraftPage
    assert set(DraftSummary.model_fields) == {'id', 'product_id', 'version', 'name', 'revision',
        'editable_full_definition', 'can_test', 'can_publish'}
    assert set(DraftPage.model_fields) == {'items', 'next_cursor'}
    for name in ('can_test', 'can_publish'):
        assert 'hint' in DraftSummary.model_fields[name].description


@pytest.mark.parametrize('change', ['purpose', 'identity', 'scope', 'query', 'expiry'])
def test_draft_cursor_is_bound_and_short_lived(clock, change):
    codec = PageCursorCodec(b'd'*32, clock)
    fields = dict(purpose='drafts', identity='actor', scope='current-grants-policy', query='search-and-limit')
    token = codec.issue(**fields, last='revenue')
    assert 'revenue' not in token
    if change == 'expiry':
        clock.instant += timedelta(seconds=300)
    else:
        fields[change] = 'changed'
    with pytest.raises(GatewayError) as caught:
        codec.read(token, **fields)
    assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'


def discovery_stub(monkeypatch, clock):
    """Controlled service sequencing, deliberately not authority/DB evidence."""
    import gateway.api.drafts as module
    from gateway.api.models import DraftSummary
    transactions = []
    rows = [('alpha', 'a'), ('bravo', 'b'), ('charlie', 'c')]
    @asynccontextmanager
    async def transaction():
        uow = SimpleNamespace(session=SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)),
            scalar=AsyncMock(return_value=SimpleNamespace(scope_id='deployment'))),
            lock_product=AsyncMock(), lock_draft=AsyncMock())
        transactions.append(uow)
        yield uow
    registry = SimpleNamespace(transaction=transaction)
    publishing = SimpleNamespace(_publish_capability=AsyncMock(return_value=True),
        _draft=AsyncMock(return_value=(SimpleNamespace(), None, SimpleNamespace())),
        _publication_targets=AsyncMock(return_value=[]),
        _prelock_reviews=AsyncMock(), _prepare_scopes=AsyncMock(return_value=({}, ())),
        _finalize_reviews=AsyncMock())
    service = module.DraftDiscoveryService(registry=registry, policy=SimpleNamespace(), publishing=publishing,
        cursors=PageCursorCodec(b'd'*32, clock))
    fingerprint = AsyncMock(return_value='fresh-scope')
    monkeypatch.setattr(module, 'scope_fingerprint', fingerprint)
    monkeypatch.setattr(module, 'scope_policy_ids', AsyncMock(return_value=()))
    service._eligible_policies = AsyncMock(return_value=['policy'])
    summaries = {id: DraftSummary(id=id, product_id=product, version='1.0.0', name='Daily '+product,
        revision=1, editable_full_definition=True, can_test=True, can_publish=True) for product,id in rows}
    service._projection = AsyncMock(side_effect=lambda principal,id,uow: (summaries[id], None))
    async def prepare(principal, identity, uow, *, loaded=None):
        # Reuse this test's controlled projections at the separate final phase.
        # No fake source approval or real policy decision is introduced here.
        summary, base = await service._projection(principal, identity, uow)
        return summary, base, ()
    service._prepare_projection = AsyncMock(side_effect=prepare)
    return service, summaries, publishing, fingerprint, transactions


def test_discovery_filters_denials_before_paging_and_checks_lookahead(monkeypatch, north_principal, clock):
    from gateway.registry.repository import not_found
    service, summaries, publishing, fingerprint, transactions = discovery_stub(monkeypatch, clock)
    async def projection(principal, id, uow):
        if id == 'a':
            raise not_found()
        return summaries[id], None
    service._projection.side_effect = projection
    page = asyncio.run(service.search(north_principal, limit=1))
    assert [item.id for item in page.items] == ['b'] and page.next_cursor
    assert [call.args[1] for call in publishing._publish_capability.await_args_list] == ['b', 'c']
    assert publishing._prepare_scopes.await_count == 2
    assert fingerprint.await_count == 3
    assert [call.args[0] for call in transactions[-1].lock_product.await_args_list] == ['bravo', 'charlie']
    # Initial scope, three isolated candidates and final release are distinct UoWs.
    assert len(transactions) == 5 and len({id(value) for value in transactions}) == 5


@pytest.mark.parametrize('stage', ['before-release', 'after-rechecks'])
def test_discovery_never_releases_page_when_scope_changes_during_request(monkeypatch, north_principal, clock, stage):
    service, _, _, fingerprint, _ = discovery_stub(monkeypatch, clock)
    fingerprint.side_effect = ['fresh-scope', 'changed'] if stage == 'before-release' else ['fresh-scope', 'fresh-scope', 'changed']
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.search(north_principal, limit=1))
    assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'


def test_discovery_keeps_denied_base_publish_hint_false(monkeypatch, north_principal, clock):
    service, _, publishing, _, _ = discovery_stub(monkeypatch, clock)
    publishing._publish_capability.return_value = False
    page = asyncio.run(service.search(north_principal, limit=1))
    assert page.items[0].can_publish is False
    assert page.items[0].can_test is True
    publishing._prepare_scopes.assert_not_awaited()


@pytest.mark.parametrize('change', ['base', 'revision', 'capability'])
def test_discovery_rejects_head_base_or_capability_change_at_release(monkeypatch, north_principal, clock, change):
    service, summaries, _, _, _ = discovery_stub(monkeypatch, clock)
    calls = 0
    async def projection(principal, id, uow):
        nonlocal calls
        calls += 1
        item = summaries[id]
        if calls > 2:
            if change == 'base':
                return item, 'replacement-base'
            item = item.model_copy(update={'revision': 2} if change == 'revision' else {'can_test': False})
        return item, None
    service._projection.side_effect = projection
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.search(north_principal, limit=1))
    assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'


def test_discovery_searches_only_authorized_summary_text(monkeypatch, north_principal, clock):
    service, _, publishing, _, _ = discovery_stub(monkeypatch, clock)
    page = asyncio.run(service.search(north_principal, query='DAILY BRAVO'))
    assert [item.id for item in page.items] == ['b'] and page.next_cursor is None
    publishing._publish_capability.assert_awaited_once()


def test_no_eligible_review_scope_performs_no_global_candidate_scan(monkeypatch, north_principal, clock):
    service, _, publishing, _, transactions = discovery_stub(monkeypatch, clock)
    service._eligible_policies.return_value = []
    page = asyncio.run(service.search(north_principal))
    assert page.items == () and page.next_cursor is None
    assert len(transactions) == 2
    transactions[0].session.execute.assert_not_awaited()
    service._projection.assert_not_awaited()
    publishing._publish_capability.assert_not_awaited()
