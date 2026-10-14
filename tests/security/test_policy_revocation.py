"""Real database authorization, immediate revocation and pre-upstream audit gates."""
import asyncio

import pytest

from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.manifest import validate_manifest
from gateway.registry.models import BindingRevision, TestRun as RegistryTestRun
from tests.integration.conftest import registry_database  # Reuse the genuine disposable fixture.
from tests.integration.test_audit import seed, service, principal
from tests.integration.test_registry import publish
from tests.unit.test_policy import rules

pytestmark = pytest.mark.postgres


async def product(database, valid_manifest, *, roles=("consumer",)):
    seed(database, roles=roles)
    manifest = validate_manifest(valid_manifest)
    async with database.registry.transaction() as uow:
        uow.session.add(BindingRevision(id="binding-1", binding_id=manifest.binding_id, revision=1,
            content={"measures": ["Orders.revenue"]}, content_digest="b" * 64,
            model_bundle_digest=manifest.model_bundle_digest, adapter_revision=manifest.adapter_revision, actor_id="operator"))
    draft = await database.registry.save_draft("revenue", manifest, 0, "editor")
    async with database.registry.transaction() as uow:
        uow.session.add(RegistryTestRun(id="test-1", product_id="revenue", draft_id=draft.id, draft_digest=manifest.content_digest,
            binding_id="binding-1", binding_digest="b" * 64, policy_id=manifest.policy_id, policy_revision=1,
            fixture_version="1", evidence={"synthetic": "passed"}, status="passed", actor_id="tester"))
    await publish(database.registry, draft)
    return await database.registry.resolve("revenue")


@pytest.mark.parametrize("role,action", [("author", "policy_admin"), ("publisher", "execute"), ("publisher", "result")])
def test_rights_are_independent_and_denials_durable(registry_database, valid_manifest, role, action):
    async def scenario():
        pinned = await product(registry_database, valid_manifest, roles=(role,))
        with pytest.raises(GatewayError) as caught:
            async with registry_database.registry.transaction() as uow:
                await service(registry_database).authorize(principal(), action, pinned, uow)
        assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action=%s AND decision='deny'", (action,)) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("revocation", ["grant", "principal", "policy", "withdrawal", "kind"])
def test_result_rechecks_current_state_in_same_service_and_session(registry_database, valid_manifest, revocation):
    async def scenario():
        pinned = await product(registry_database, valid_manifest)
        policy = service(registry_database)
        with pytest.raises(GatewayError) as caught:
            async with registry_database.registry.transaction() as uow:
                assert (await policy.authorize(principal(), "execute", pinned, uow)).tenant_id == "north"
                if revocation == "grant":
                    registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false")
                elif revocation == "principal":
                    registry_database.execute_owner("UPDATE gateway.principals SET active=false")
                elif revocation == "kind":
                    registry_database.execute_owner("UPDATE gateway.principals SET kind='service', service_client_id='new-client'")
                elif revocation == "withdrawal":
                    registry_database.execute_owner("UPDATE gateway.version_states SET status='withdrawn'")
                else:
                    # Policy head was locked by the first decision; update in a new transaction after release below.
                    pass
                if revocation != "policy":
                    await policy.authorize(principal(), "result", pinned, uow)
                else:
                    raise GatewayError("NOT_FOUND_OR_FORBIDDEN", "synthetic", "Synthetic rollback for next policy read")
        assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"
        if revocation == "policy":
            content = {"rules": rules(tenant_ids=["south"]), "reason": "Synthetic immediate revocation"}
            from psycopg.types.json import Jsonb
            import psycopg
            from tests.integration.conftest import connection_kwargs
            with psycopg.connect(**connection_kwargs(registry_database.urls["owner"])) as connection:
                connection.execute("INSERT INTO gateway.policy_revisions (id,policy_id,revision,content,content_digest,actor_id) VALUES ('policy-2','revenue-access',2,%s,%s,'operator')", (Jsonb(content), canonical_digest(content)))
                connection.execute("UPDATE gateway.policies SET current_revision=2")
            with pytest.raises(GatewayError) as revoked:
                async with registry_database.registry.transaction() as uow:
                    await policy.authorize(principal(), "result", pinned, uow)
            assert revoked.value.code == "NOT_FOUND_OR_FORBIDDEN"
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='result' AND decision='deny'") == 1
    asyncio.run(scenario())


def test_hidden_and_nonexistent_policy_have_same_outward_error(registry_database, valid_manifest):
    async def scenario():
        pinned = await product(registry_database, valid_manifest, roles=("author",))
        errors = []
        raw = pinned.manifest.model_dump(mode="json")
        raw.update(policy_id="missing-policy", content_digest=None)
        for candidate in [pinned, pinned.model_copy(update={"manifest": validate_manifest(raw)})]:
            with pytest.raises(GatewayError) as caught:
                async with registry_database.registry.transaction() as uow:
                    await service(registry_database).authorize(principal(), "execute", candidate, uow)
            errors.append((caught.value.code, caught.value.safe_message))
        assert errors[0] == errors[1]
    asyncio.run(scenario())


def test_required_audit_failure_prevents_upstream_and_result_release(registry_database, valid_manifest):
    async def scenario():
        pinned = await product(registry_database, valid_manifest)
        registry_database.execute_owner('REVOKE INSERT (id,actor_id,service_client_id,action,product_id,version_id,policy_revision,decision,parameter_names,parameter_digest,duration_ms,row_count,adapter,correlation_id) ON gateway.audit_events FROM "' + registry_database.roles["app"] + '"')
        calls = []
        for action in ("execute", "result", "publish"):
            with pytest.raises(GatewayError) as caught:
                async with registry_database.registry.transaction() as uow:
                    await service(registry_database).authorize(principal(), action, pinned, uow)
                    calls.append(action)  # Represents dispatch/release after the required gate.
            assert caught.value.code == "UPSTREAM_UNAVAILABLE"
        assert calls == []
    asyncio.run(scenario())


def test_denial_rolls_back_other_caller_changes_but_keeps_denied_audit(registry_database, valid_manifest):
    async def scenario():
        pinned = await product(registry_database, valid_manifest, roles=("publisher",))
        from gateway.registry.models import ProductRow
        with pytest.raises(GatewayError):
            async with registry_database.registry.transaction() as uow:
                uow.session.add(ProductRow(id="must-rollback"))
                await service(registry_database).authorize(principal(), "execute", pinned, uow)
        assert registry_database.scalar("SELECT count(*) FROM gateway.products WHERE id='must-rollback'") == 0
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='execute' AND decision='deny'") == 1
    asyncio.run(scenario())


def test_caught_denial_cannot_resume_or_commit_caller_mutations(registry_database, valid_manifest):
    async def scenario():
        from sqlalchemy.exc import InvalidRequestError
        from gateway.registry.models import ProductRow
        pinned = await product(registry_database, valid_manifest, roles=("publisher",))
        async with registry_database.registry.transaction() as uow:
            uow.session.add(ProductRow(id="before-denial"))
            with pytest.raises(GatewayError) as caught:
                await service(registry_database).authorize(principal(), "execute", pinned, uow, correlation_id="denied-request")
            assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN" and caught.value.request_id == "denied-request"
            with pytest.raises(InvalidRequestError):
                uow.session.add(ProductRow(id="after-denial"))
                await uow.session.flush()
        assert registry_database.scalar("SELECT count(*) FROM gateway.products WHERE id IN ('before-denial','after-denial')") == 0
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE correlation_id='denied-request' AND decision='deny'") == 1
    asyncio.run(scenario())


def test_unreadable_current_policy_does_not_allow_or_leak_state(registry_database, valid_manifest):
    async def scenario():
        pinned = await product(registry_database, valid_manifest)
        registry_database.execute_owner('REVOKE SELECT ON gateway.policy_revisions FROM "' + registry_database.roles["app"] + '"')
        with pytest.raises(GatewayError) as caught:
            async with registry_database.registry.transaction() as uow:
                await service(registry_database).authorize(principal(), "execute", pinned, uow)
        assert caught.value.code == "UPSTREAM_UNAVAILABLE"
        assert "SELECT" not in str(caught.value) and "policy_revisions" not in str(caught.value)
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='execute' AND decision='deny' AND policy_id='revenue-access'") == 1
    asyncio.run(scenario())


def test_policy_change_is_seen_by_existing_orm_session_after_commit(registry_database, valid_manifest):
    async def scenario():
        from gateway.registry.repository import UnitOfWork
        from psycopg.types.json import Jsonb
        import psycopg
        from tests.integration.conftest import connection_kwargs
        pinned = await product(registry_database, valid_manifest)
        policy = service(registry_database)
        async with registry_database.registry.sessions() as session:
            async with session.begin():
                assert (await policy.authorize(principal(), "execute", pinned, UnitOfWork(session))).policy_revision == 1
            content = {"rules": rules(tenant_ids=["south"]), "reason": "Synthetic immediate revocation"}
            with psycopg.connect(**connection_kwargs(registry_database.urls["owner"])) as connection:
                connection.execute("INSERT INTO gateway.policy_revisions (id,policy_id,revision,content,content_digest,actor_id) VALUES ('policy-2','revenue-access',2,%s,%s,'operator')", (Jsonb(content), canonical_digest(content)))
                connection.execute("UPDATE gateway.policies SET current_revision=2")
            with pytest.raises(GatewayError) as caught:
                async with session.begin():
                    await policy.authorize(principal(), "result", pinned, UnitOfWork(session))
            assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"
        assert registry_database.scalar("SELECT policy_revision FROM gateway.audit_events WHERE action='result' AND decision='deny'") == 2
    asyncio.run(scenario())
