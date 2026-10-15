"""Catalog phase ordering with fake storage/access; no database approval evidence."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.api.catalog import CatalogService
from gateway.api.cursors import PageCursorCodec
from gateway.auth.principals import PrincipalGrants, PolicyGrant
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.manifest import validate_manifest
from gateway.registry.repository import not_found
from tests.unit.test_api_services import CatalogRegistry
from tests.unit.test_source_approval_results import ReviewedResultProbe

pytestmark = pytest.mark.unit


def catalog(clock, *, deny_final_batch=False):
    result = ReviewedResultProbe(clock)
    first = result.product
    raw = first.manifest.model_dump(mode='json', exclude={'content_digest'})
    raw['product_id'] = 'second-product'
    second = PinnedProduct(manifest=validate_manifest(raw), binding_digest=first.binding_digest, version_id='second-version')
    registry = CatalogRegistry([first,second])
    trace = []
    grants = PrincipalGrants(principal_id=result.actor.id, grants=[PolicyGrant(policy_id=first.manifest.policy_id,
        tenant_id='north', capabilities=['discover','describe'],entitlements={'groups':['north']})])
    async def evaluate(principal, action, target, uow, **kwargs):
        trace.append((id(uow), 'policy', target.manifest.product_id))
        return result.context.decision
    async def prelock(uow, products, *, version_ids=(), draft_ids=(), policy_ids=()):
        trace.append((id(uow), 'prelock', tuple(item.manifest.product_id for item in products)))
        uow.policy_ids=policy_ids
    async def resolve(uow, target, principal, decision, *, action, request_id):
        trace.append((id(uow), 'resolve', target.manifest.product_id))
        return SimpleNamespace(product=target, action=action)
    async def finalize(uow, requests):
        trace.append((id(uow), 'finalize', tuple(item.product.manifest.product_id for item in requests)))
        if deny_final_batch and len(requests)>2:
            raise not_found()
        return ()
    access = SimpleNamespace(prelock=prelock, resolve_transient=resolve, finalize=finalize,
        scope_fingerprint=AsyncMock(return_value='b'*64))
    policy = SimpleNamespace(requires_source_approval=lambda target:True, source_access=access,
        evaluate=evaluate, authorize=AsyncMock(side_effect=AssertionError('Reviewed source used legacy authorize')),
        _identity=AsyncMock(), principals=SimpleNamespace(get_current=AsyncMock(return_value=grants)),
        _current=AsyncMock(return_value=(SimpleNamespace(revision=1,content_digest='a'*64),None)))
    service=CatalogService(registry=registry,policy=policy,bindings=result.bindings,
        cursors=PageCursorCodec(b'k'*32,clock))
    return service, registry, result.actor, trace


def test_describe_uses_preliminary_policy_then_exact_transient_final_gate(clock):
    service, registry, actor, trace = catalog(clock)
    response = asyncio.run(service.describe(actor,'revenue'))
    assert response.product_id == 'revenue' and registry.transactions[-1].committed
    assert [item[1] for item in trace] == ['prelock','policy','resolve','finalize']


def test_final_catalog_page_prelocks_all_targets_then_finalizes_once(clock):
    service, registry, actor, trace = catalog(clock)
    page = asyncio.run(service.search(actor,limit=1))
    assert len(page.items)==1 and page.next_cursor is not None
    final_trace = [item for item in trace if item[0] == id(registry.transactions[-1])]
    assert final_trace[0][1:] == ('prelock', ('revenue','second-product'))
    assert [item[1] for item in final_trace].count('finalize') == 1
    assert final_trace[-1][1:] == ('finalize', ('revenue','revenue','second-product','second-product'))


def test_one_denied_final_target_rolls_back_entire_catalog_page(clock):
    service, registry, actor, trace = catalog(clock,deny_final_batch=True)
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.search(actor,limit=1))
    assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
    assert not registry.transactions[-1].committed
    assert trace[-1][1] == 'finalize'


def test_catalog_cursor_invalidates_on_source_approval_generation_change(clock):
    service, registry, actor, trace = catalog(clock)
    page=asyncio.run(service.search(actor,limit=1))
    service.policy.source_access.scope_fingerprint.return_value='c'*64
    with pytest.raises(GatewayError):
        asyncio.run(service.search(actor,limit=1,cursor=page.next_cursor))


def test_final_catalog_prelocks_all_grant_policies_before_version_locks(clock):
    service, registry, actor, trace = catalog(clock)
    grants=service.policy.principals.get_current.return_value
    extra=PolicyGrant(policy_id='unrelated-policy',tenant_id='north',capabilities=['author'],entitlements={})
    service.policy.principals.get_current.return_value=grants.model_copy(update={'grants':(*grants.grants,extra)})
    asyncio.run(service.search(actor,limit=1))
    assert registry.transactions[-1].policy_ids==('revenue-access','unrelated-policy')


def test_fingerprint_new_grant_policy_denies_before_unheld_policy_lock(clock):
    from gateway.api.catalog import scope_fingerprint
    service, registry, actor, trace = catalog(clock)
    service.policy._current.reset_mock()
    async def scenario():
        async with registry.transaction() as uow:
            with pytest.raises(GatewayError):
                await scope_fingerprint(service.policy,actor,uow,policy_ids=('different-policy',))
    asyncio.run(scenario())
    service.policy._current.assert_not_awaited()
