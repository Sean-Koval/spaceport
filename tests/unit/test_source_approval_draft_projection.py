"""Projection sequencing with fake access storage, not approval qualification."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.auth.principals import PrincipalGrants
from gateway.contracts.errors import GatewayError
from gateway.contracts.identity import Principal
from gateway.publishing.service import PublishingService
from gateway.registry.repository import not_found
from gateway.registry.models import BindingRevision, CapacityLock
from tests.unit.test_policy import grant
from tests.unit.test_source_approval_policy import setup

pytestmark = pytest.mark.unit


def projection(valid_manifest, *, deny_final=False):
    valid_manifest['adapter_revision'] = 'cube-bigquery-v1'
    actor = Principal(id='north-user', subject='synthetic-north', kind='user', service_client_id='review-client')
    policy, target, uow, audit = setup(actor, valid_manifest)
    policy.principals.get_current.return_value = PrincipalGrants(principal_id=actor.id,
        grants=[grant(capabilities=['author'])])
    trace = []

    async def prelock(uow, targets, *, draft_ids=(), version_ids=()):
        trace.append(('prelock', tuple(item.version_id for item in targets)))

    async def resolve(uow, product, principal, decision, *, action, request_id):
        trace.append(('resolve', product.version_id, action))
        assert product.binding_digest == target.binding_digest
        assert principal.service_client_id == 'review-client'
        return SimpleNamespace(product=product, principal=principal, decision=decision, action=action)

    async def finalize(uow, requests):
        trace.append(('finalize', tuple(item.action for item in requests)))
        if deny_final:
            raise not_found()
        return ()

    policy.source_access = SimpleNamespace(prelock=prelock, resolve_transient=resolve, match_transient=resolve, finalize=finalize)
    publishing = PublishingService(registry=policy.registry, policy=policy, bindings=None, bundles=None)
    return publishing, policy, actor, target, uow, audit, trace


def test_reviewed_draft_projection_uses_exact_author_then_final_source_gate(valid_manifest):
    async def scenario():
        publishing, policy, actor, target, uow, audit, trace = projection(valid_manifest)
        publishing._draft = AsyncMock(return_value=(SimpleNamespace(latest_draft_id=target.version_id,
            draft_revision=1, current_version_id=None), SimpleNamespace(id=target.version_id,
            revision=1, content_digest=target.manifest.content_digest), target))
        view = await publishing.get_draft(actor, target.version_id)
        assert view.definition['name'] == target.manifest.name
        assert view.can_test and not view.can_publish
        assert trace == [('prelock', (target.version_id,)), ('resolve', target.version_id, 'author'),
                         ('finalize', ('author',))]
        # The real access finalizer owns the reviewed approval audit. A policy
        # allow before that stage would falsely claim approved release.
        assert audit.events == []
    asyncio.run(scenario())


def test_final_source_denial_releases_no_draft_projection(valid_manifest):
    async def scenario():
        publishing, policy, actor, target, uow, audit, trace = projection(valid_manifest, deny_final=True)
        with pytest.raises(GatewayError) as caught:
            await publishing._review(actor, target, uow)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert trace and trace[-1] == ('finalize', ('author',))
        assert not any(event.decision == 'allow' for event in audit.events)
    asyncio.run(scenario())


def test_prepared_review_does_not_finalize_or_emit_policy_allow(valid_manifest):
    async def scenario():
        publishing, policy, actor, target, uow, audit, trace = projection(valid_manifest)
        assert hasattr(publishing, '_prepare_review'), 'Draft review must expose a preliminary batch phase'
        decision, capabilities, pending = await publishing._prepare_review(actor, target, uow)
        assert decision.principal_id == actor.id and 'author' in capabilities
        assert len(pending) == 1 and pending[0].action == 'author'
        assert trace == [('resolve', target.version_id, 'author')]
        assert audit.events == []
    asyncio.run(scenario())


def test_draft_review_target_uses_protected_binding_digest(valid_manifest):
    async def scenario():
        publishing, policy, actor, target, uow, audit, trace = projection(valid_manifest)
        binding = SimpleNamespace(content_digest=target.binding_digest,
            model_bundle_digest=target.manifest.model_bundle_digest,
            adapter_revision=target.manifest.adapter_revision)

        async def scalar(statement):
            entity = statement.column_descriptions[0].get('entity')
            if entity is CapacityLock:
                return SimpleNamespace(scope_id='deployment')
            if entity is BindingRevision:
                return binding
            return target.manifest.product_id

        uow.session.scalar = scalar
        uow.lock_product = AsyncMock(return_value=SimpleNamespace(latest_draft_id='draft-1', draft_revision=1))
        uow.lock_draft = AsyncMock(return_value=SimpleNamespace(id='draft-1', manifest=target.manifest.model_dump(mode='json')))
        _, _, actual = await publishing._draft(uow, 'draft-1')
        assert actual.binding_digest == binding.content_digest
        assert actual.manifest.content_digest == target.manifest.content_digest
    asyncio.run(scenario())


def test_readonly_publish_hint_uses_independent_current_source_gate(valid_manifest):
    async def scenario():
        publishing, policy, actor, target, uow, audit, trace = projection(valid_manifest)
        policy.principals.get_current.return_value = PrincipalGrants(principal_id=actor.id,
            grants=[grant(capabilities=['publish'])])
        publishing._draft = AsyncMock(return_value=(SimpleNamespace(current_version_id=None), None, target))
        assert await publishing._publish_capability(actor, target.version_id)
        assert trace == [('prelock', (target.version_id,)), ('resolve', target.version_id, 'publish'),
                         ('finalize', ('publish',))]
        assert audit.events == []
    asyncio.run(scenario())


def test_current_transient_review_does_not_authorize_old_fixture_evidence(valid_manifest):
    async def scenario():
        publishing, policy, actor, target, uow, audit, trace = projection(valid_manifest)
        from copy import deepcopy
        from gateway.registry.models import TestRun as RegistryTestRun
        from tests.unit.test_publication_definitions import make_catalog_evidence
        _, _, _, stored, _ = make_catalog_evidence()
        stored.id, stored.draft_id, stored.actor_id = 'old-run', 'draft-1', 'original-author'
        stored.product_id, stored.policy_id = target.manifest.product_id, target.manifest.policy_id
        stored.draft_digest = target.manifest.content_digest
        before = deepcopy(stored.evidence)
        publishing._draft = AsyncMock(return_value=(None, SimpleNamespace(
            id=stored.draft_id, content_digest=stored.draft_digest), target))
        async def scalar(statement):
            if statement.column_descriptions[0].get('entity') is RegistryTestRun:
                assert set(statement.compile().params.values()) == {
                    stored.id, stored.draft_id, stored.draft_digest, stored.product_id, stored.policy_id}
                return stored
            return 'published'
        async def original_owner_denied(actual_uow, test):
            assert actual_uow is uow and test is stored and test.actor_id != actor.id
            trace.append(('original-owner-denied', test.id))
            raise not_found()
        uow.session.scalar = scalar
        publishing.fixture_runner = SimpleNamespace(prepare_evidence_access=original_owner_denied)
        with pytest.raises(GatewayError) as caught:
            await publishing.get_test(actor, 'draft-1', 'old-run')
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert trace == [('prelock', (target.version_id,)), ('resolve', target.version_id, 'author'),
                         ('original-owner-denied', stored.id)]
        assert stored.evidence == before and stored.actor_id == 'original-author'
        assert audit.events == []
    asyncio.run(scenario())


def test_review_does_not_promote_unchecked_author_action_into_capability_hint(valid_manifest):
    async def scenario():
        publishing, policy, actor, target, uow, audit, trace = projection(valid_manifest)
        policy.principals.get_current.return_value = PrincipalGrants(principal_id=actor.id,
            grants=[grant(capabilities=['author', 'publish'])])
        async def match(*args, **kwargs):
            return await policy.source_access.resolve_transient(*args, **kwargs) if kwargs['action'] == 'publish' else None
        policy.source_access.match_transient = match
        _, capabilities = await publishing._review(actor, target, uow)
        assert 'publish' in capabilities
        assert 'author' not in capabilities
        assert trace[-1] == ('finalize', ('publish',))
    asyncio.run(scenario())


def test_source_resolver_denial_is_not_reaudited_as_a_policy_denial(valid_manifest):
    async def scenario():
        publishing, policy, actor, target, uow, audit, trace = projection(valid_manifest)

        async def denied(*args, **kwargs):
            await uow.session.rollback()
            await audit.append(uow, audit.event(actor, 'author', decision='deny'))
            raise not_found()

        policy.source_access.resolve_transient = denied
        with pytest.raises(GatewayError):
            await publishing._prepare_scopes(actor, (target,), 'author', uow)
        assert len(audit.events) == 1
    asyncio.run(scenario())
