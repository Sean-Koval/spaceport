"""Action selection only; fake approval storage grants no source qualification."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.auth.principals import PrincipalGrants
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.registry.repository import not_found
from tests.unit.test_policy import grant
from tests.unit.test_source_approval_draft_projection import projection

pytestmark = pytest.mark.unit


def review_actions(valid_manifest, allowed):
    publishing, policy, actor, target, uow, audit, trace = projection(valid_manifest)
    policy.principals.get_current.return_value = PrincipalGrants(principal_id=actor.id,
        grants=[grant(capabilities=['author', 'publish'])])
    selected = []

    async def match(uow, product, principal, decision, *, action, request_id):
        selected.append((action, principal, decision, request_id))
        if action not in allowed:
            return None
        return SimpleNamespace(product=product, principal=principal, decision=decision, action=action)

    async def strict(*args, **kwargs):
        request = await match(*args, **kwargs)
        if request is None:
            raise not_found()
        return request

    policy.source_access.match_transient = match
    policy.source_access.resolve_transient = strict
    return publishing, policy, actor, target, uow, audit, trace, selected


@pytest.mark.parametrize('allowed', [('author',), ('publish',), ('author', 'publish')])
def test_review_only_returns_independently_matched_action_hints(valid_manifest, allowed):
    async def scenario():
        publishing, policy, actor, target, uow, audit, trace, selected = review_actions(valid_manifest, allowed)
        decision, capabilities = await publishing._review(actor, target, uow)
        assert set(capabilities) == set(allowed)
        assert trace[-1][0] == 'finalize'
        assert set(trace[-1][1]) == set(allowed)
        assert {item[0] for item in selected} == {'author', 'publish'}
        assert all(item[1] == actor for item in selected)
        assert all(canonical_digest(item[2]) == canonical_digest(decision) for item in selected)
        assert len({item[3] for item in selected}) == 2
        assert audit.events == []
        uow.session.rollback.assert_not_awaited()
    asyncio.run(scenario())


def test_missing_both_action_matches_has_one_outer_denial_without_allow(valid_manifest):
    async def scenario():
        publishing, policy, actor, target, uow, audit, trace, selected = review_actions(valid_manifest, ())
        with pytest.raises(GatewayError) as caught:
            await publishing._review(actor, target, uow)
        assert getattr(caught.value, 'code', None) == 'NOT_FOUND_OR_FORBIDDEN'
        assert {item[0] for item in selected} == {'author', 'publish'}
        assert len(audit.events) == 1 and audit.events[0].decision == 'deny'
        assert audit.events[0].parameter_names == ()
        assert not any(item[0] == 'finalize' for item in trace)
        uow.session.rollback.assert_awaited_once()
    asyncio.run(scenario())
