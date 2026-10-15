"""The reviewed profile rejects assertions without identifying unseen agents."""
import asyncio

import pytest

from gateway.auth.settings import Settings
from tests.unit.test_identity import authenticator, code, production_settings, signed_token

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('claim', ['act', 'may_act'])
@pytest.mark.parametrize('value', [None, {}, {'sub': 'synthetic-actor'}, [], '', False])
def test_reviewed_signed_delegation_presence_denies(clock, claim, value):
    async def scenario():
        settings = production_settings().model_copy(update={'source': 'reviewed_sources'})
        auth, requests, client = authenticator(clock, settings=settings)
        try:
            assert await code(auth, 'Bearer ' + signed_token(clock, **{claim: value})) == 'UNAUTHENTICATED'
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('claim', ['act', 'may_act'])
def test_legacy_signed_assertion_behavior_is_unchanged(clock, claim):
    async def scenario():
        auth, _, client = authenticator(clock)
        try:
            assert await code(auth, 'Bearer ' + signed_token(clock, **{claim: {}})) == 'accepted'
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_plain_reviewed_bearer_preserves_authenticated_client(clock):
    async def scenario():
        settings = production_settings().model_copy(update={'source': 'reviewed_sources'})
        auth, _, client = authenticator(clock, settings=settings)
        try:
            principal = await auth.authenticate('Bearer ' + signed_token(clock))
            assert principal.id == 'db-user'
            assert principal.service_client_id == 'studio-client'
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('field', ['on_behalf_of', 'act', 'may_act', 'actor_chain', 'delegated_source'])
def test_identity_configuration_rejects_unrecognized_delegation(field):
    with pytest.raises(ValueError):
        Settings.model_validate({field: 'synthetic-actor'})
