"""Standalone callers cannot acquire release rights from ordinary policy alone."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.contracts.errors import GatewayError
from gateway.contracts.query import QueryRequest
from gateway.execution.service import ExecutionService
from tests.unit.test_source_approval_policy import setup

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('adapter', ['cube-rest-v1', 'cube-rest-v2', 'cube-bigquery-v1', 'domo-named-v2'])
@pytest.mark.parametrize('action', ['discover', 'describe', 'execute', 'result'])
def test_direct_authorize_without_trusted_composition_never_grants_release(
    north_principal, valid_manifest, adapter, action,
):
    async def scenario():
        valid_manifest['adapter_revision'] = adapter
        policy, target, uow, audit = setup(north_principal, valid_manifest)
        with pytest.raises(GatewayError) as caught:
            await policy.authorize(north_principal, action, target, uow)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert not [event for event in audit.events if event.decision == 'allow']
    asyncio.run(scenario())


@pytest.mark.parametrize('value', ['synthetic-sensitive-value', 'person@example.invalid'])
def test_unapproved_query_values_never_enter_audit_processing(
    north_principal, valid_manifest, clock, value,
):
    async def scenario():
        valid_manifest['adapter_revision'] = 'cube-bigquery-v1'
        policy, target, uow, audit = setup(north_principal, valid_manifest)
        processed = []
        summarize = audit.parameter_summary
        def observed(parameters, **kwargs):
            processed.append(dict(parameters))
            return summarize(parameters, **kwargs)
        audit.parameter_summary = observed
        policy.registry.resolve = AsyncMock(return_value=target)
        service = ExecutionService(registry=policy.registry, policy=policy,
            bindings=None, clock=clock)
        service.queue.lock_capacity = AsyncMock()
        service.queue.maintain = AsyncMock()
        service.queue.find_existing = AsyncMock(return_value=None)
        with pytest.raises(GatewayError):
            await service.submit(north_principal, QueryRequest(product_id=target.manifest.product_id,
                inputs={'start': value, 'end': '2024-03-12'}))
        assert all(value not in parameters.values() for parameters in processed)
        assert not [event for event in audit.events if event.decision == 'allow']
    asyncio.run(scenario())


@pytest.mark.parametrize('action', ['author', 'publish'])
def test_direct_authoring_authorize_requires_trusted_composition(north_principal, valid_manifest, action):
    async def scenario():
        policy, target, uow, audit = setup(north_principal, valid_manifest)
        with pytest.raises(GatewayError) as caught:
            await policy.authorize(north_principal, action, target, uow)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert not [event for event in audit.events if event.decision == 'allow']
    asyncio.run(scenario())


def test_direct_publisher_review_cannot_bypass_missing_source_approval(north_principal, valid_manifest):
    from gateway.publishing.service import PublishingService
    from tests.unit.test_policy import grant
    from gateway.auth.principals import PrincipalGrants
    async def scenario():
        policy, target, uow, audit = setup(north_principal, valid_manifest)
        policy.principals.get_current.return_value = PrincipalGrants(principal_id=north_principal.id,
            grants=[grant(capabilities=['author', 'publish'])])
        publishing = PublishingService(registry=policy.registry, policy=policy, bindings=None, bundles=None)
        with pytest.raises(GatewayError) as caught:
            await publishing._review(north_principal, target, uow)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert not [event for event in audit.events if event.decision == 'allow']
    asyncio.run(scenario())


def test_permission_negative_fixture_without_approval_is_configuration_failure(north_principal, valid_manifest):
    from gateway.publishing.test_runner import ApprovedFixture, FixtureRunner
    from gateway.contracts.manifest import Fixture
    async def scenario():
        policy, target, uow, audit = setup(north_principal, valid_manifest, allowed=False)
        policy.principals.resolve_principal = AsyncMock(return_value=north_principal)
        runner = FixtureRunner(registry=policy.registry, policy=policy, bindings=None, bundles=None,
            catalog_provider=None, adapter_factory=None)
        case = ApprovedFixture(principal_id=north_principal.id, kind='permission_negative',
            fixture=Fixture(id='permission-denied', version='1', inputs={'start':'2024-03-09', 'end':'2024-03-12'},
                expected_error='NOT_FOUND_OR_FORBIDDEN'))
        with pytest.raises(GatewayError) as caught:
            await runner._decision_for_case(uow, target, 1, case)
        assert caught.value.code == 'BINDING_DRIFT'
        assert audit.events == []
    asyncio.run(scenario())


def test_exact_registered_synthetic_composition_keeps_legacy_authorize(north_principal):
    from gateway import runtime_config
    from gateway.registry.repository import Registry
    from gateway.contracts.execution import PinnedProduct
    async def scenario():
        assets = runtime_config.load_assets()
        registry = Registry('postgresql+psycopg://gateway_app:synthetic@registry-db:5432/gateway_registry')
        policy, _, uow, audit = setup(north_principal, assets.manifest.model_dump(mode='json'))
        policy.registry = registry
        policy.synthetic_capability = runtime_config._synthetic_capability(assets, registry, profile='legacy')
        binding = next(item for item in assets.bindings if item.context == 'consumer')
        target = PinnedProduct(manifest=assets.manifest, binding_digest=binding.digest, version_id='synthetic-version')
        try:
            result = await policy.authorize(north_principal, 'execute', target, uow)
            assert result.principal_id == north_principal.id
            assert len(audit.events) == 1 and audit.events[0].decision == 'allow'
        finally:
            runtime_config._revoke_synthetic_capability(policy.synthetic_capability)
            await registry.close()
    asyncio.run(scenario())
