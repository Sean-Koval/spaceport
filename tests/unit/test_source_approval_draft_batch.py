"""All draft-page dependencies precede its single approval-lock suffix."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.api import drafts as module
from gateway.api.models import DraftSummary
from gateway.auth.principals import PrincipalGrants
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.manifest import validate_manifest
from gateway.registry.models import CapacityLock
from tests.unit.test_policy import grant
from tests.unit.test_source_approval_draft_projection import projection

pytestmark = pytest.mark.unit


def batch(valid_manifest, monkeypatch, *, deny_final=False, count=2, with_base=False):
    publishing, policy, actor, first, uow, audit, trace = projection(valid_manifest, deny_final=deny_final)
    policy.principals.get_current.return_value = PrincipalGrants(principal_id=actor.id,
        grants=[grant(capabilities=['author', 'publish'])])
    targets = [first]
    for index in range(1, count):
        raw = first.manifest.model_dump(mode='json', exclude={'content_digest'})
        raw['product_id'] = 'second-product' if index == 1 else f'synthetic-product-{index:03d}'
        targets.append(PinnedProduct(manifest=validate_manifest(raw), binding_digest=first.binding_digest,
            version_id=f'draft-{index + 1}'))
    bases = {}
    for target in targets:
        raw = target.manifest.model_dump(mode='json', exclude={'content_digest'})
        raw['version'] = '0.9.0'
        manifest = validate_manifest(raw)
        bases['base-' + target.version_id] = SimpleNamespace(id='base-' + target.version_id,
            manifest=manifest.model_dump(mode='json'), binding_digest=target.binding_digest)


    async def scalar(statement):
        if statement.column_descriptions[0].get('entity') is CapacityLock:
            trace.append(('capacity',))
            return SimpleNamespace(scope_id='deployment')
        return None

    async def lock_product(product_id):
        trace.append(('product', product_id))

    async def lock_draft(draft_id):
        trace.append(('draft', draft_id))

    async def load_draft(uow, draft_id):
        target = next(item for item in targets if item.version_id == draft_id)
        return (SimpleNamespace(latest_draft_id=draft_id, draft_revision=1,
                    current_version_id='base-' + draft_id if with_base else None),
                SimpleNamespace(id=draft_id, revision=1, content_digest=target.manifest.content_digest), target)

    async def policy_ids(*args):
        trace.append(('policy-ids',))
        return ('other-fingerprint-policy', first.manifest.policy_id)

    async def fingerprint(*args, **kwargs):
        assert kwargs.get('policy_ids') == ('other-fingerprint-policy', first.manifest.policy_id)
        trace.append(('fingerprint',))
        return 'f' * 64

    async def prelock(uow, products, *, draft_ids=(), version_ids=(), policy_ids=()):
        trace.append(('prelock', tuple(item.version_id for item in products), tuple(policy_ids)))

    uow.session.scalar = scalar
    uow.session.get = AsyncMock(side_effect=lambda model, identity: bases.get(identity))
    uow.lock_product, uow.lock_draft = lock_product, lock_draft
    publishing._draft = load_draft
    policy.source_access.prelock = prelock
    monkeypatch.setattr(module, 'scope_fingerprint', fingerprint)
    # The shared helper is supplied by the policy/catalog partition.
    monkeypatch.setattr(module, 'scope_policy_ids', policy_ids, raising=False)
    service = module.DraftDiscoveryService(registry=policy.registry, policy=policy, publishing=publishing, cursors=None)
    matches = [(DraftSummary(id=target.version_id, product_id=target.manifest.product_id,
        version=target.manifest.version, name=target.manifest.name, revision=1,
        editable_full_definition=True, can_test=True, can_publish=True),
        'base-' + target.version_id if with_base else None) for target in targets]
    return service, actor, uow, matches, trace


def test_final_draft_page_batches_all_reviews_and_publish_hints(valid_manifest, monkeypatch):
    async def scenario():
        service, actor, uow, matches, trace = batch(valid_manifest, monkeypatch)
        assert hasattr(service, '_final_page'), 'Draft discovery needs a single final batch boundary'
        checked, fingerprint = await service._final_page(actor, matches,
            {item.id: True for item, _ in matches}, 'f' * 64, uow)
        assert [item.product_id for item in checked] == ['revenue', 'second-product']
        assert all(item.can_publish for item in checked)
        assert fingerprint == 'f' * 64
        assert trace[0] == ('capacity',)
        assert [event[0] for event in trace].count('finalize') == 1
        assert trace[-1] == ('finalize', ('publish', 'author', 'publish', 'author'))
        prelock = next(event for event in trace if event[0] == 'prelock')
        assert set(prelock[1]) == {item.id for item, _ in matches}
        assert prelock[2] == ('other-fingerprint-policy', 'revenue-access')
        assert trace.index(prelock) < next(index for index, event in enumerate(trace) if event[0] == 'resolve')
    asyncio.run(scenario())


def test_one_final_approval_denial_prevents_the_entire_draft_page(valid_manifest, monkeypatch):
    async def scenario():
        service, actor, uow, matches, trace = batch(valid_manifest, monkeypatch, deny_final=True)
        assert hasattr(service, '_final_page'), 'Draft discovery needs a single final batch boundary'
        with pytest.raises(GatewayError) as caught:
            await service._final_page(actor, matches, {item.id: True for item, _ in matches}, 'f' * 64, uow)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert trace[-1][0] == 'finalize'
        assert [event[0] for event in trace].count('finalize') == 1
    asyncio.run(scenario())


def test_full_draft_page_with_bases_finalizes_each_action_once_within_bound(valid_manifest, monkeypatch):
    async def scenario():
        service, actor, uow, matches, trace = batch(valid_manifest, monkeypatch, count=51, with_base=True)
        finalized = []
        async def final(uow, requests):
            finalized.extend(requests)
            assert len(requests) <= 200, 'The protected finalizer has a fixed 200-request bound'
            return ()
        service.policy.source_access.finalize = final
        checked, _ = await service._final_page(actor, matches,
            {item.id: True for item, _ in matches}, 'f' * 64, uow)
        assert len(checked) == 51
        keys = [(request.product.version_id, request.action) for request in finalized]
        expected = {(item.id, action) for item, _ in matches for action in ('author', 'publish')}
        expected |= {(base, 'publish') for _, base in matches}
        assert len(keys) == len(set(keys)) == 153
        assert set(keys) == expected
    asyncio.run(scenario())
