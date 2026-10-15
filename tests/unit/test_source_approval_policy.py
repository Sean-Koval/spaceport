"""Preliminary policy derivation does not claim approval or release authority."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.auth.principals import PrincipalGrants
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.manifest import validate_manifest
from gateway.policy.audit import AuditWriter
from gateway.policy.service import PolicyService
from tests.unit.test_policy import grant, rules

pytestmark = pytest.mark.unit


class Audit(AuditWriter):
    def __init__(self):
        super().__init__(b'synthetic-key-with-thirty-two-bytes')
        self.events = []
    async def append(self, uow, event):
        self.events.append(event)
        return '0134dc63-001c-4883-834a-871849c3158c'


def setup(principal, manifest, *, allowed=True):
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        one_or_none=lambda: SimpleNamespace(kind=principal.kind, service_client_id=principal.service_client_id))),
        scalar=AsyncMock(return_value='published'), rollback=AsyncMock())
    content = {'rules': rules(), 'reason': 'Synthetic review'}
    revision = SimpleNamespace(revision=1, content=content, content_digest=canonical_digest(content))
    uow = SimpleNamespace(session=session, lock_policy=AsyncMock(return_value=revision))
    @asynccontextmanager
    async def transaction():
        yield uow
    principals = SimpleNamespace(get_current=AsyncMock(return_value=PrincipalGrants(
        principal_id=principal.id, grants=[grant()] if allowed else [])))
    audit = Audit()
    policy = PolicyService(SimpleNamespace(transaction=transaction), principals, audit)
    target = PinnedProduct(manifest=validate_manifest(manifest), binding_digest='b'*64, version_id='version-1')
    return policy, target, uow, audit


def test_evaluate_derives_current_policy_without_allow_audit(north_principal, valid_manifest):
    async def scenario():
        policy, target, uow, audit = setup(north_principal, valid_manifest)
        result = await policy.evaluate(north_principal, 'execute', target, uow)
        assert result.principal_id == north_principal.id
        assert result.policy_revision == 1
        assert result.tenant_id == 'north'
        assert audit.events == []
        uow.session.rollback.assert_not_awaited()
    asyncio.run(scenario())


def test_evaluate_denies_current_policy_with_safe_audit(north_principal, valid_manifest):
    async def scenario():
        policy, target, uow, audit = setup(north_principal, valid_manifest, allowed=False)
        with pytest.raises(GatewayError) as caught:
            await policy.evaluate(north_principal, 'execute', target, uow)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert len(audit.events) == 1 and audit.events[0].decision == 'deny'
        assert audit.events[0].parameter_names == ()
        uow.session.rollback.assert_awaited_once()
    asyncio.run(scenario())


def test_evaluate_revalidates_mutated_private_product(north_principal, valid_manifest):
    async def scenario():
        policy, target, uow, audit = setup(north_principal, valid_manifest)
        object.__setattr__(target, 'binding_digest', 'invalid')
        with pytest.raises(GatewayError):
            await policy.evaluate(north_principal, 'execute', target, uow)
        assert not [event for event in audit.events if event.decision == 'allow']
    asyncio.run(scenario())
