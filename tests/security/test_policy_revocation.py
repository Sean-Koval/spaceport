"""Real database authorization, immediate revocation and pre-upstream audit gates."""
import asyncio
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import ExecutionView
from gateway.contracts.manifest import validate_manifest
from gateway.registry.repository import UnitOfWork
from tests.integration.conftest import registry_database  # Reuse the genuine disposable fixture.
from tests.integration.test_results import seeded_result
from tests.unit.test_policy import rules

pytestmark = pytest.mark.postgres


async def reviewed_product(database, valid_manifest, *, roles=None):
    """Real reviewed publication/admission and a committed positive release control."""
    ctx = await seeded_result(database, valid_manifest, state="accepted")
    view = await ctx.service.get(ctx.actor, ctx.execution.id)
    assert type(view) is ExecutionView and view.state == "accepted"
    assert database.scalar(
        "SELECT count(*) FROM gateway.audit_events WHERE actor_id=%s AND action='result' "
        "AND decision='allow' AND version_id=%s AND source_approval_id=%s",
        (ctx.actor.id, ctx.product.version_id, ctx.approval["id"]),
    ) == 2
    if roles is not None:
        database.execute_owner(
            "UPDATE gateway.principal_grants SET roles=%s WHERE principal_id=%s AND policy_id=%s",
            (Jsonb(list(roles)), ctx.actor.id, ctx.product.manifest.policy_id),
        )
    return ctx


def assert_denied_audit(database, ctx, action, request_id):
    assert database.scalar(
        "SELECT count(*) FROM gateway.audit_events WHERE actor_id=%s AND service_client_id=%s AND action=%s "
        "AND decision='deny' AND correlation_id=%s",
        (ctx.actor.id, ctx.actor.service_client_id, action, request_id),
    ) == 1
    assert database.scalar(
        "SELECT count(*) FROM gateway.audit_events WHERE correlation_id=%s AND decision='allow'",
        (request_id,),
    ) == 0


async def authorize_reviewed_execute(database, ctx):
    """Complete the original accepted owner's gate, never a preliminary allow."""
    request_id = str(uuid4())
    async with database.registry.transaction() as uow:
        access = ctx.policy.source_access
        await access.prelock(uow, (ctx.product,), version_ids=(ctx.product.version_id,))
        decision = await ctx.policy.evaluate(ctx.actor, "execute", ctx.product, uow, correlation_id=request_id)
        request = await access.resolve_owner(uow, {"kind": "execution", "id": ctx.execution.id},
            ctx.actor, decision, action="execute", request_id=request_id)
        await access.finalize(uow, (request,))
    return decision


@pytest.mark.parametrize("role,action", [("author", "policy_admin"), ("publisher", "execute"), ("publisher", "result")])
def test_rights_are_independent_and_denials_durable(registry_database, valid_manifest, role, action):
    async def scenario():
        ctx = await reviewed_product(registry_database, valid_manifest, roles=(role,))
        request_id = str(uuid4())
        with pytest.raises(GatewayError) as caught:
            async with registry_database.registry.transaction() as uow:
                await ctx.policy.evaluate(ctx.actor, action, ctx.product, uow, correlation_id=request_id)
        assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"
        assert_denied_audit(registry_database, ctx, action, request_id)
    asyncio.run(scenario())


@pytest.mark.parametrize("revocation", ["grant", "principal", "policy", "withdrawal", "kind"])
def test_result_rechecks_current_state_in_same_service_and_session(registry_database, valid_manifest, revocation):
    async def scenario():
        ctx = await reviewed_product(registry_database, valid_manifest)
        request_id = str(uuid4())
        async with registry_database.registry.sessions() as session:
            if revocation in {"policy", "withdrawal"}:
                # Release policy/lifecycle locks before the genuine mutation,
                # then reuse this same ORM session for the current-policy read.
                async with session.begin():
                    assert (await ctx.policy.evaluate(ctx.actor, "execute", ctx.product,
                        UnitOfWork(session))).tenant_id == "north"
                if revocation == "policy":
                    await ctx.policy.change_policy(ctx.actor, ctx.product.manifest.policy_id,
                        rules(tenant_ids=["south"]), "Synthetic immediate revocation")
                else:
                    await ctx.publisher.transition(ctx.publisher_actor, "revenue", ctx.product.manifest.version,
                        "withdraw", "Synthetic immediate withdrawal")
                with pytest.raises(GatewayError) as caught:
                    async with session.begin():
                        await ctx.policy.evaluate(ctx.actor, "result", ctx.product,
                            UnitOfWork(session), correlation_id=request_id)
            else:
                with pytest.raises(GatewayError) as caught:
                    async with session.begin():
                        uow = UnitOfWork(session)
                        # evaluate() is preliminary; reviewed_product already
                        # proved admission and a complete owner-finalized release.
                        assert (await ctx.policy.evaluate(ctx.actor, "execute", ctx.product, uow)).tenant_id == "north"
                        if revocation == "grant":
                            registry_database.execute_owner(
                                "UPDATE gateway.principal_grants SET active=false WHERE principal_id=%s AND policy_id=%s",
                                (ctx.actor.id, ctx.product.manifest.policy_id))
                        elif revocation == "principal":
                            registry_database.execute_owner(
                                "UPDATE gateway.principals SET active=false WHERE id=%s", (ctx.actor.id,))
                        else:
                            assert ctx.actor.kind == "service"
                            registry_database.execute_owner(
                                "UPDATE gateway.principals SET kind='user' WHERE id=%s",
                                (ctx.actor.id,))
                        await ctx.policy.evaluate(ctx.actor, "result", ctx.product, uow, correlation_id=request_id)
            assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"
        assert_denied_audit(registry_database, ctx, "result", request_id)
    asyncio.run(scenario())


def test_hidden_and_nonexistent_policy_have_same_outward_error(registry_database, valid_manifest):
    async def scenario():
        ctx = await reviewed_product(registry_database, valid_manifest, roles=("author",))
        errors = []
        raw = ctx.product.manifest.model_dump(mode="json")
        raw.update(policy_id="missing-policy", content_digest=None)
        for candidate in [ctx.product, ctx.product.model_copy(update={"manifest": validate_manifest(raw)})]:
            request_id = str(uuid4())
            with pytest.raises(GatewayError) as caught:
                async with registry_database.registry.transaction() as uow:
                    await ctx.policy.evaluate(ctx.actor, "execute", candidate, uow, correlation_id=request_id)
            errors.append((caught.value.code, caught.value.safe_message))
            assert_denied_audit(registry_database, ctx, "execute", request_id)
        assert errors[0] == errors[1]
    asyncio.run(scenario())


def test_required_audit_failure_prevents_upstream_and_result_release(registry_database, valid_manifest):
    async def scenario():
        ctx = await reviewed_product(registry_database, valid_manifest)
        await ctx.publisher.transition(ctx.publisher_actor, "revenue", ctx.product.manifest.version,
            "deprecate", "Positive publishing audit control")
        assert registry_database.scalar("SELECT status FROM gateway.version_states WHERE version_id=%s",
            (ctx.product.version_id,)) == "deprecated"
        assert (await authorize_reviewed_execute(registry_database, ctx)).tenant_id == "north"
        assert (await ctx.service.get(ctx.actor, ctx.execution.id)).state == "accepted"
        registry_database.execute_owner('REVOKE INSERT (id,actor_id,service_client_id,action,product_id,version_id,policy_revision,decision,parameter_names,parameter_digest,duration_ms,row_count,adapter,correlation_id) ON gateway.audit_events FROM "' + registry_database.roles["app"] + '"')
        calls = []
        for action in ("execute", "result", "publish"):
            with pytest.raises(GatewayError) as caught:
                if action == "execute":
                    await authorize_reviewed_execute(registry_database, ctx)
                elif action == "result":
                    await ctx.service.get(ctx.actor, ctx.execution.id)
                else:
                    await ctx.publisher.transition(ctx.publisher_actor, "revenue", ctx.product.manifest.version,
                        "withdraw", "Must not pass the required audit gate")
                calls.append(action)  # Represents dispatch/release after the required gate.
            assert caught.value.code == "UPSTREAM_UNAVAILABLE"
        assert calls == []
        assert registry_database.scalar("SELECT status FROM gateway.version_states WHERE version_id=%s",
            (ctx.product.version_id,)) == "deprecated"
    asyncio.run(scenario())


def test_denial_rolls_back_other_caller_changes_but_keeps_denied_audit(registry_database, valid_manifest):
    async def scenario():
        ctx = await reviewed_product(registry_database, valid_manifest, roles=("publisher",))
        from gateway.registry.models import ProductRow
        request_id = str(uuid4())
        with pytest.raises(GatewayError) as caught:
            async with registry_database.registry.transaction() as uow:
                uow.session.add(ProductRow(id="must-rollback"))
                await ctx.policy.evaluate(ctx.actor, "execute", ctx.product, uow, correlation_id=request_id)
        assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"
        assert registry_database.scalar("SELECT count(*) FROM gateway.products WHERE id='must-rollback'") == 0
        assert_denied_audit(registry_database, ctx, "execute", request_id)
    asyncio.run(scenario())


def test_caught_denial_cannot_resume_or_commit_caller_mutations(registry_database, valid_manifest):
    async def scenario():
        from sqlalchemy.exc import InvalidRequestError
        from gateway.registry.models import ProductRow
        ctx = await reviewed_product(registry_database, valid_manifest, roles=("publisher",))
        async with registry_database.registry.transaction() as uow:
            uow.session.add(ProductRow(id="before-denial"))
            with pytest.raises(GatewayError) as caught:
                await ctx.policy.evaluate(ctx.actor, "execute", ctx.product, uow, correlation_id="denied-request")
            assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN" and caught.value.request_id == "denied-request"
            with pytest.raises(InvalidRequestError):
                uow.session.add(ProductRow(id="after-denial"))
                await uow.session.flush()
        assert registry_database.scalar("SELECT count(*) FROM gateway.products WHERE id IN ('before-denial','after-denial')") == 0
        assert_denied_audit(registry_database, ctx, "execute", "denied-request")
    asyncio.run(scenario())


def test_unreadable_current_policy_does_not_allow_or_leak_state(registry_database, valid_manifest):
    async def scenario():
        ctx = await reviewed_product(registry_database, valid_manifest)
        registry_database.execute_owner('REVOKE SELECT ON gateway.policy_revisions FROM "' + registry_database.roles["app"] + '"')
        request_id = str(uuid4())
        with pytest.raises(GatewayError) as caught:
            async with registry_database.registry.transaction() as uow:
                await ctx.policy.evaluate(ctx.actor, "execute", ctx.product, uow, correlation_id=request_id)
        assert caught.value.code == "UPSTREAM_UNAVAILABLE"
        assert "SELECT" not in str(caught.value) and "policy_revisions" not in str(caught.value)
        assert_denied_audit(registry_database, ctx, "execute", request_id)
        assert registry_database.scalar("SELECT policy_id FROM gateway.audit_events WHERE correlation_id=%s",
            (request_id,)) == ctx.product.manifest.policy_id
    asyncio.run(scenario())


def test_policy_change_is_seen_by_existing_orm_session_after_commit(registry_database, valid_manifest):
    async def scenario():
        ctx = await reviewed_product(registry_database, valid_manifest)
        request_id = str(uuid4())
        async with registry_database.registry.sessions() as session:
            async with session.begin():
                assert (await ctx.policy.evaluate(ctx.actor, "execute", ctx.product, UnitOfWork(session))).policy_revision == 1
            await ctx.policy.change_policy(ctx.actor, ctx.product.manifest.policy_id,
                rules(tenant_ids=["south"]), "Synthetic immediate revocation")
            with pytest.raises(GatewayError) as caught:
                async with session.begin():
                    await ctx.policy.evaluate(ctx.actor, "result", ctx.product, UnitOfWork(session), correlation_id=request_id)
            assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"
        assert_denied_audit(registry_database, ctx, "result", request_id)
        assert registry_database.scalar("SELECT policy_revision FROM gateway.audit_events WHERE correlation_id=%s",
            (request_id,)) == 2
    asyncio.run(scenario())
