"""Exact checked-in synthetic packages through genuine registry/service gates.

No adapter is constructed and no provider query or credential resolver is used.
These tests qualify only the trusted synthetic composition compatibility path.
"""
import asyncio

import pytest

from gateway.adapters.base import StaticBindingResolver
from gateway.auth.principals import PrincipalStore
from gateway.contracts.canonical import canonical_digest
from gateway.policy.audit import AuditWriter
from gateway.policy.service import PolicyService
from gateway.publishing.service import PublishingService
from gateway.publishing.test_runner import FixtureRunner
from gateway.registry.models import BindingRevision, PolicyRow, PolicyRevision, PrincipalRow, PrincipalGrant
from gateway.registry.repository import Registry
from gateway import runtime_config

pytestmark = pytest.mark.postgres


async def seed_exact_assets(database, assets):
    owner = Registry(database.urls['owner'].render_as_string(hide_password=False))
    try:
        async with owner.transaction() as uow:
            uow.session.add(PolicyRow(id=assets.policy_id, current_revision=assets.policy_revision))
            await uow.session.flush()
            uow.session.add(PolicyRevision(id='exact-policy-1', policy_id=assets.policy_id,
                revision=assets.policy_revision, content=assets.policy_content,
                content_digest=canonical_digest(assets.policy_content), actor_id='synthetic-operator'))
            for binding in assets.bindings:
                uow.session.add(BindingRevision(id='exact-' + binding.binding_id,
                    binding_id=binding.binding_id, revision=binding.revision,
                    content=binding.model_dump(mode='json'), content_digest=binding.digest,
                    model_bundle_digest=binding.model_bundle_digest,
                    adapter_revision=binding.adapter_revision, actor_id='synthetic-operator'))
            for account in assets.accounts:
                uow.session.add(PrincipalRow(id=account['id'], subject='synthetic-' + account['id'], kind='demo'))
            await uow.session.flush()
            for account in assets.accounts:
                if account['roles']:
                    uow.session.add(PrincipalGrant(principal_id=account['id'], policy_id=assets.policy_id,
                        tenant_id=account['tenant_id'], roles=account['roles'], entitlements=account['entitlements']))
    finally:
        await owner.close()


@pytest.mark.parametrize('profile', ['legacy', 'reviewed'])
def test_exact_factory_save_read_and_fixture_authority_preserve_binding_witness(registry_database, profile):
    async def scenario():
        from gateway.reviewed_runtime import load_assets as reviewed_assets
        assets = runtime_config.load_assets() if profile == 'legacy' else reviewed_assets()
        await seed_exact_assets(registry_database, assets)
        registry = registry_database.registry
        principals = PrincipalStore(registry.sessions)
        capability = runtime_config._synthetic_capability(assets, registry, profile=profile)
        policy = PolicyService(registry, principals, AuditWriter(b'exact-synthetic-publishing-key-32'),
            synthetic_capability=capability)
        bindings = StaticBindingResolver(list(assets.bindings))
        runner = FixtureRunner(registry=registry, policy=policy, bindings=bindings, bundles=assets.bundles,
            catalog_provider=lambda: assets.fixture_catalog, adapter_factory=None)
        publishing = PublishingService(registry=registry, policy=policy, bindings=bindings,
            bundles=assets.bundles, fixture_runner=runner)
        try:
            actor = await principals.resolve_principal('demo-author')
            draft = await publishing.save_draft(actor, assets.manifest, 0)
            view = await publishing.get_draft(actor, draft.id)
            assert view.id == draft.id and view.can_test and not view.can_publish
            consumer = next(binding for binding in assets.bindings if binding.context == 'consumer')
            async with registry.transaction() as uow:
                authority = await runner._authority(uow, actor, draft.id)
                assert authority.product.binding_digest == consumer.digest
                assert authority.product.manifest == assets.manifest
                assert authority.actor == actor
            assert registry_database.scalar('SELECT count(*) FROM gateway.fixture_attempts') == 0
            assert registry_database.scalar('SELECT count(*) FROM gateway.source_operation_groups') == 0
        finally:
            runtime_config._revoke_synthetic_capability(capability)
    asyncio.run(scenario())


def test_legacy_publishing_helper_cannot_authorize_changed_definition(registry_database):
    from gateway.contracts.errors import GatewayError
    from gateway.contracts.manifest import validate_manifest
    from tests.integration.test_publishing import setup, principal
    async def scenario():
        service, manifest = await setup(registry_database)
        draft = await service.save_draft(principal(), manifest, 0)
        assert (await service.get_draft(principal(), draft.id)).id == draft.id
        changed = validate_manifest(manifest.model_dump(mode='json', exclude={'content_digest'}) |
            {'description':'A different definition needs an explicit owner review'})
        with pytest.raises(GatewayError) as caught:
            await service.save_draft(principal(), changed, 1)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert registry_database.scalar('SELECT count(*) FROM gateway.draft_revisions') == 1
    asyncio.run(scenario())


def test_legacy_rest_composition_shares_the_exact_factory_policy(registry_database, clock):
    from gateway.contracts.errors import GatewayError
    from tests.integration.test_rest_services import composition, principal
    async def scenario():
        services, _ = await composition(registry_database, clock)
        assert services.policy is services.publishing.policy
        assert (await services.catalog.describe(principal(), 'revenue')).product_id == 'revenue'
        registry_database.execute_owner('UPDATE gateway.principal_grants SET active=false')
        with pytest.raises(GatewayError):
            await services.catalog.describe(principal(), 'revenue')
    asyncio.run(scenario())
