"""Private dependency/fingerprint helpers; no final approval authority evidence."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.api import catalog
from gateway.auth.principals import PolicyGrant, PrincipalGrants
from gateway.contracts.errors import GatewayError
from gateway.contracts.identity import Principal

pytestmark = pytest.mark.unit


def scope():
    actor = Principal(id='actor', subject='synthetic', service_client_id='client', kind='user')
    grants = PrincipalGrants(principal_id=actor.id, grants=[
        PolicyGrant(policy_id=policy, tenant_id=tenant, capabilities=['author'], entitlements={})
        for policy, tenant in [('z-policy','north'),('a-policy','north'),('z-policy','south')]])
    policy = SimpleNamespace(_identity=AsyncMock(),
        principals=SimpleNamespace(get_current=AsyncMock(return_value=grants)),
        _current=AsyncMock(return_value=(SimpleNamespace(revision=1,content_digest='a'*64),None)),
        source_access=SimpleNamespace(scope_fingerprint=AsyncMock(return_value='b'*64)))
    uow = SimpleNamespace(session=SimpleNamespace(get=AsyncMock(return_value=object())))
    return actor, policy, uow


def test_scope_dependency_discovery_is_sorted_deduplicated_and_nonlocking():
    actor, policy, uow = scope()
    assert asyncio.run(catalog.scope_policy_ids(policy,actor,uow)) == ('a-policy','z-policy')
    policy._identity.assert_awaited_once()
    policy._current.assert_not_awaited()
    uow.session.get.assert_not_awaited()
    policy.source_access.scope_fingerprint.assert_not_awaited()


def test_scope_new_policy_denies_before_unheld_policy_access():
    actor, policy, uow = scope()
    with pytest.raises(GatewayError):
        asyncio.run(catalog.scope_fingerprint(policy,actor,uow,policy_ids=('a-policy',)))
    uow.session.get.assert_not_awaited()
    policy._current.assert_not_awaited()


def test_scope_fingerprint_changes_with_source_approval_generation():
    actor, policy, uow = scope()
    before = asyncio.run(catalog.scope_fingerprint(policy,actor,uow))
    policy.source_access.scope_fingerprint.return_value = 'c'*64
    assert asyncio.run(catalog.scope_fingerprint(policy,actor,uow)) != before
