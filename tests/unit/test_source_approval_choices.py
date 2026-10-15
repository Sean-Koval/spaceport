"""Exact binding witness release sequencing; fake repositories are not approval proof."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.registry.repository import not_found
from tests.unit.test_api_services import choice_setup, decision

pytestmark = pytest.mark.unit


def test_policy_scope_alone_cannot_disclose_binding_choices(valid_manifest,north_principal,clock):
    service, policy = choice_setup(valid_manifest,north_principal,clock)
    policy.requires_source_approval=lambda target:True
    policy.source_access=None
    page=asyncio.run(service.bindings(north_principal))
    assert page.items == () and page.next_cursor is None


def test_binding_choice_finalizes_exact_candidate_after_scope_checks(valid_manifest,north_principal,clock):
    service, policy = choice_setup(valid_manifest,north_principal,clock)
    trace=[]
    policy.requires_source_approval=lambda target:True
    async def prelock(uow,targets,**kwargs):
        trace.append(('prelock',len(targets)))
    async def evaluate(actor,action,target,uow,**kwargs):
        trace.append(('policy',target.manifest.content_digest))
        return decision(actor,['day','segment','revenue'])
    async def resolve(uow,target,actor,scope,*,action,request_id):
        assert target.binding_digest == service.binding_resolver.resolve.return_value.digest
        assert action == 'author'
        trace.append(('resolve',target.manifest.content_digest))
        return SimpleNamespace(product=target)
    async def finalize(uow,requests):
        trace.append(('finalize',len(requests)))
        return ()
    policy.evaluate=evaluate
    policy.source_access=SimpleNamespace(prelock=prelock,resolve_transient=resolve,finalize=finalize,
        scope_fingerprint=AsyncMock(return_value='b'*64))
    page=asyncio.run(service.bindings(north_principal))
    assert len(page.items)==1
    assert [value[0] for value in trace] == ['prelock','policy','resolve','finalize']*2


def test_binding_choice_final_denial_returns_no_inventory(valid_manifest,north_principal,clock):
    service, policy = choice_setup(valid_manifest,north_principal,clock)
    policy.requires_source_approval=lambda target:True
    calls=[]
    async def prelock(*args,**kwargs): pass
    async def evaluate(actor,action,target,uow,**kwargs): return decision(actor,['day','segment','revenue'])
    async def resolve(*args,**kwargs): return SimpleNamespace()
    async def finalize(uow,requests):
        calls.append(1)
        if len(calls)>1: raise not_found()
        return ()
    policy.evaluate=evaluate
    policy.source_access=SimpleNamespace(prelock=prelock,resolve_transient=resolve,finalize=finalize,
        scope_fingerprint=AsyncMock(return_value='b'*64))
    from gateway.contracts.errors import GatewayError
    with pytest.raises(GatewayError):
        asyncio.run(service.bindings(north_principal))
    assert len(calls)==2


def contextual_policy_choices(valid_manifest, north_principal, clock, *, deny_final=False):
    service, policy = choice_setup(valid_manifest, north_principal, clock)
    service._context = AsyncMock(return_value=service.templates[0])
    policy.requires_source_approval = lambda target: True
    trace = []
    async def prelock(uow, targets, *, draft_ids=(), policy_ids=()):
        trace.append(('prelock', tuple(item.manifest.content_digest for item in targets), draft_ids, policy_ids))
    async def evaluate(actor, action, target, uow, **kwargs):
        trace.append(('policy', action))
        return decision(actor, ['day', 'segment', 'revenue'])
    async def resolve(uow, target, actor, current, *, action, request_id):
        assert action == 'author' and target.manifest == service.templates[0]
        trace.append(('resolve', action))
        return SimpleNamespace(product=target)
    async def finalize(uow, requests):
        trace.append(('finalize', len(requests)))
        if deny_final and sum(event[0] == 'finalize' for event in trace) == 2:
            raise not_found()
        return ()
    policy.evaluate = evaluate
    policy.source_access = SimpleNamespace(prelock=prelock, resolve_transient=resolve, finalize=finalize,
        scope_fingerprint=AsyncMock(return_value='b'*64))
    return service, policy, trace


def test_contextual_policy_choices_require_exact_draft_source_authority(valid_manifest, north_principal, clock):
    service, policy, trace = contextual_policy_choices(valid_manifest, north_principal, clock)
    policy.source_access = None
    from gateway.contracts.errors import GatewayError
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.policies(north_principal, draft_id='draft-1'))
    assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'


def test_contextual_policy_choices_finalize_full_draft_author_request(valid_manifest, north_principal, clock):
    service, policy, trace = contextual_policy_choices(valid_manifest, north_principal, clock)
    result = asyncio.run(service.policies(north_principal, draft_id='draft-1'))
    assert [item.policy_id for item in result.items] == ['revenue-access']
    assert sum(event[0] == 'finalize' for event in trace) == 2
    assert trace[-1] == ('finalize', 1)
    assert all(event[2] == ('draft-1',) for event in trace if event[0] == 'prelock')
    assert trace[-4][0] == 'prelock'
    assert trace[-4][3] == ('hidden-policy', 'revenue-access')


def test_contextual_policy_revocation_before_final_gate_releases_no_page(valid_manifest, north_principal, clock):
    service, policy, trace = contextual_policy_choices(valid_manifest, north_principal, clock, deny_final=True)
    from gateway.contracts.errors import GatewayError
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.policies(north_principal, draft_id='draft-1'))
    assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
    assert trace[-1] == ('finalize', 1)
    assert not service.registry.transactions[-1].committed


def test_uncontextual_policy_inventory_does_not_invent_product_authority(valid_manifest, north_principal, clock):
    service, policy = choice_setup(valid_manifest, north_principal, clock)
    policy.requires_source_approval = lambda target: True
    policy.source_access = None
    result = asyncio.run(service.policies(north_principal))
    assert [item.policy_id for item in result.items] == ['revenue-access']
