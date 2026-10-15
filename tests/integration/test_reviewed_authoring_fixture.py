"""Genuine owner-installed authoring approval boundaries; no provider I/O."""
import asyncio

import pytest

from gateway.contracts.errors import GatewayError
from gateway.contracts.manifest import validate_manifest
from tests.integration.reviewed_publishing_fixture import reviewed_authoring_fixture, principal

pytestmark = pytest.mark.postgres


def changed(manifest, **updates):
    raw = manifest.model_dump(mode='json', exclude={'content_digest'})
    return validate_manifest(raw | updates)


def test_exact_owner_approved_authoring_roundtrip(registry_database, valid_manifest):
    fixture = reviewed_authoring_fixture(registry_database, valid_manifest)
    async def scenario():
        draft = await fixture.service.save_draft(principal(), fixture.manifest, 0)
        view = await fixture.service.get_draft(principal(), draft.id)
        assert view.id == draft.id
        assert draft.content_digest == fixture.manifest.content_digest
    asyncio.run(scenario())


def test_metadata_drift_denies_until_explicit_owner_variant_review(registry_database, valid_manifest):
    fixture = reviewed_authoring_fixture(registry_database, valid_manifest)
    variant = changed(fixture.manifest, description='Separately reviewed synthetic definition')
    async def scenario():
        with pytest.raises(GatewayError) as caught:
            await fixture.service.save_draft(principal(), variant, 0)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert registry_database.scalar('SELECT count(*) FROM gateway.draft_revisions') == 0
        fixture.approve_variant(variant)
        saved = await fixture.service.save_draft(principal(), variant, 0)
        assert saved.content_digest == variant.content_digest
    asyncio.run(scenario())


def test_owner_approval_does_not_supply_another_client_identity(registry_database, valid_manifest):
    fixture = reviewed_authoring_fixture(registry_database, valid_manifest)
    async def scenario():
        other = principal().model_copy(update={'service_client_id': 'other-client'})
        with pytest.raises(GatewayError) as caught:
            await fixture.service.save_draft(other, fixture.manifest, 0)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert registry_database.scalar('SELECT count(*) FROM gateway.draft_revisions') == 0
    asyncio.run(scenario())
