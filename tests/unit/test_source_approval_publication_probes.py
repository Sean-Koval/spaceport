"""Strict migrated review probes; these cannot qualify protected SQL authority."""
import asyncio
from copy import deepcopy

import pytest

from gateway.contracts.errors import GatewayError
from gateway.contracts.manifest import validate_manifest
from tests.unit.test_publication_definitions import review_service

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('change', ['client','missing','manifest'])
def test_review_probe_never_approves_unmatched_client_or_exact_package(change):
    context = review_service(grant_kind='author')
    actor = context.actor
    if change == 'client':
        actor = actor.model_copy(update={'service_client_id':'unreviewed-client'})
    elif change == 'missing':
        context.access.approvals.clear()
    else:
        raw = deepcopy(context.draft.manifest)
        raw.pop('content_digest')
        raw['name'] = 'A different exact definition'
        changed = validate_manifest(raw)
        context.draft.manifest = changed.model_dump(mode='json')
        context.draft.content_digest = changed.content_digest
    with pytest.raises(GatewayError):
        asyncio.run(context.service.get_draft(actor,context.draft.id))
    assert len(context.records) == 1 and context.records[0].decision == 'deny'
    assert context.access.batches == []


def test_review_probe_matches_each_action_without_promoting_policy_author():
    context = review_service(grant_kind='both')
    context.approve(validate_manifest(context.draft.manifest),actions=('publish',))
    view = asyncio.run(context.service.get_draft(context.actor,context.draft.id))
    assert view.can_publish and not view.can_test and not view.editable_full_definition
    assert [request.action for request in context.access.batches[0]] == ['publish']
    assert [record.action for record in context.records] == ['publish']


def test_review_probe_final_batch_rechecks_original_request_without_rebinding():
    context = review_service(grant_kind='author')
    async def scenario():
        async with context.service.registry.transaction() as uow:
            _, _, target = await context.service._draft(uow,context.draft.id)
            decision, _, requests = await context.service._prepare_review(context.actor,target,uow)
            assert requests[0].decision == decision
            context.approve(target.manifest)  # New approval ID cannot replace the prepared request.
            with pytest.raises(GatewayError):
                await context.access.finalize(uow,requests)
    asyncio.run(scenario())
    assert len(context.records) == 1 and context.records[0].decision == 'deny'
    assert context.access.batches == []
