"""Reviewed draft-save sequencing, without substituting for PostgreSQL approval tests."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.auth.principals import PrincipalGrants
from gateway.contracts.errors import GatewayError
from gateway.contracts.manifest import validate_manifest
from gateway.registry.models import BindingRevision, CapacityLock, DraftRevision
from tests.unit.test_policy import grant
from tests.unit.test_source_approval_draft_projection import projection

pytestmark = pytest.mark.unit


def saving(valid_manifest, *, previous=False, deny_final=False):
    publishing, policy, actor, target, uow, audit, trace = projection(valid_manifest, deny_final=deny_final)
    head = SimpleNamespace(latest_draft_id='previous-draft' if previous else None,
        current_version_id=None, draft_revision=int(previous))
    old = None
    if previous:
        raw = target.manifest.model_dump(mode='json', exclude={'content_digest'})
        raw['description'] = 'An independently reviewed previous definition'
        manifest = validate_manifest(raw)
        old = SimpleNamespace(id='previous-draft', manifest=manifest.model_dump(mode='json'), revision=1)
    binding = SimpleNamespace(content_digest=target.binding_digest,
        model_bundle_digest=target.manifest.model_bundle_digest, adapter_revision=target.manifest.adapter_revision)
    added = []
    original_identity = uow.session.execute

    async def execute(statement):
        if getattr(statement, 'is_insert', False):
            trace.append(('insert-product',))
            return None
        return await original_identity(statement)

    async def scalar(statement):
        entity = statement.column_descriptions[0].get('entity')
        if entity is CapacityLock:
            trace.append(('capacity',))
            return SimpleNamespace(scope_id='deployment')
        if entity is BindingRevision:
            trace.append(('binding-record',))
            return binding
        if entity is DraftRevision:
            return target.manifest.product_id
        return None

    async def locked(product_id):
        trace.append(('product', product_id))
        return head

    async def binding_check(manifest, uow):
        trace.append(('binding-check',))
        return binding, None

    def add(row):
        trace.append(('draft-insert',))
        added.append(row)

    async def flush():
        trace.append(('flush',))

    uow.session.scalar = scalar
    uow.session.execute = execute
    uow.session.get = AsyncMock(return_value=old)
    uow.session.add = add
    uow.session.flush = flush
    uow.lock_product = locked
    publishing._binding = binding_check
    return publishing, policy, actor, target, head, added, audit, trace


@pytest.mark.parametrize('previous', [False, True])
def test_save_reviews_every_exact_definition_before_single_final_gate(valid_manifest, previous):
    async def scenario():
        publishing, policy, actor, target, head, added, audit, trace = saving(valid_manifest, previous=previous)
        saved = await publishing.save_draft(actor, target.manifest, int(previous))
        assert saved.manifest == target.manifest.model_dump(mode='json')
        assert saved.revision == int(previous) + 1
        assert head.latest_draft_id == saved.id
        assert added == [saved]
        resolves = [event for event in trace if event[0] == 'resolve']
        assert len(resolves) == 1 + int(previous)
        assert all(event[2] == 'author' for event in resolves)
        assert [event[0] for event in trace].count('finalize') == 1
        final = next(index for index, event in enumerate(trace) if event[0] == 'finalize')
        assert all(event[0] in {'draft-insert', 'flush'} for event in trace[final+1:])
        assert audit.events == []
    asyncio.run(scenario())


def test_save_final_denial_persists_no_draft_and_advances_no_head(valid_manifest):
    async def scenario():
        publishing, policy, actor, target, head, added, audit, trace = saving(valid_manifest, deny_final=True)
        with pytest.raises(GatewayError) as caught:
            await publishing.save_draft(actor, target.manifest, 0)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert added == [] and head.latest_draft_id is None and head.draft_revision == 0
        assert trace[-1][0] == 'finalize'
        assert audit.events == []
    asyncio.run(scenario())


def test_save_never_substitutes_manifest_digest_for_protected_binding(valid_manifest):
    async def scenario():
        publishing, policy, actor, target, head, added, audit, trace = saving(valid_manifest)
        original = policy.source_access.resolve_transient
        seen = []
        async def checked(uow, product, *args, **kwargs):
            seen.append(product.binding_digest)
            assert product.binding_digest != product.manifest.content_digest
            return await original(uow, product, *args, **kwargs)
        policy.source_access.resolve_transient = checked
        await publishing.save_draft(actor, target.manifest, 0)
        assert seen == [target.binding_digest]
    asyncio.run(scenario())
