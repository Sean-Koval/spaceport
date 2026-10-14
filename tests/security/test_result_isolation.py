"""Real database result isolation and every-page revocation, never SQLite stand-ins."""
import asyncio

import pytest
from sqlalchemy import text

from gateway.contracts.errors import GatewayError
from gateway.contracts.identity import Principal
from tests.integration.conftest import registry_database
from tests.integration.test_audit import principal
from tests.integration.test_results import seeded_result
from tests.unit.test_policy import rules

pytestmark=pytest.mark.postgres


async def unavailable(operation):
    with pytest.raises(GatewayError) as caught: await operation
    assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"
    assert caught.value.safe_message=="The requested product is unavailable."
    return caught.value.code,caught.value.safe_message


def test_foreign_and_guessed_execution_ids_have_same_error(registry_database,valid_manifest):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest)
        other=Principal(id="other-user",subject="synthetic-other",service_client_id=None,kind="demo")
        a=await unavailable(ctx.service.get(other,ctx.execution.id))
        b=await unavailable(ctx.service.get(other,"guessed-execution"))
        c=await unavailable(ctx.service.get(principal(),"guessed-execution"))
        assert a==b==c
    asyncio.run(scenario())


@pytest.mark.parametrize("revocation",["grant","principal","withdrawal","same_revision_entitlement","same_revision_tenant","policy_revision"])
@pytest.mark.parametrize("use_cursor",[False,True])
def test_every_replay_or_cursor_rechecks_live_authorization(registry_database,valid_manifest,revocation,use_cursor):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest)
        first=await ctx.service.get(principal(),ctx.execution.id)
        cursor=first.next_cursor if use_cursor else None
        if revocation=="grant": registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false")
        elif revocation=="principal": registry_database.execute_owner("UPDATE gateway.principals SET active=false")
        elif revocation=="withdrawal": registry_database.execute_owner("UPDATE gateway.version_states SET status='withdrawn'")
        elif revocation=="same_revision_entitlement": registry_database.execute_owner("UPDATE gateway.principal_grants SET entitlements='{\"groups\":[\"south\"]}'")
        elif revocation=="same_revision_tenant": registry_database.execute_owner("UPDATE gateway.principal_grants SET tenant_id='south'")
        else: await ctx.policy.change_policy(principal(),"revenue-access",rules(),"New revision requires a new query")
        await unavailable(ctx.service.get(principal(),ctx.execution.id,cursor))
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='result' AND decision='deny'")==1
    asyncio.run(scenario())


@pytest.mark.parametrize("bad_cursor",["tampered","unknown","foreign_execution","expired"])
def test_tampered_expired_and_foreign_cursors_are_uniformly_unavailable(registry_database,valid_manifest,bad_cursor):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest)
        first=await ctx.service.get(principal(),ctx.execution.id)
        cursor=first.next_cursor
        execution_id=ctx.execution.id
        if bad_cursor=="tampered": cursor=cursor[:-1]+("a" if cursor[-1]!="a" else "b")
        elif bad_cursor=="unknown": cursor="a"*43
        elif bad_cursor=="foreign_execution": execution_id="other-execution"
        else:
            # Cursor rows cannot be edited; create an independently short-lived token.
            from gateway.execution.cursors import CursorStore
            from datetime import timedelta
            async with registry_database.registry.transaction() as uow:
                cursor=await CursorStore(uow,ctx.clock.now()).issue("north-user",ctx.execution.id,1,200,ctx.clock.now()+timedelta(seconds=1))
            ctx.clock.instant+=timedelta(seconds=2)
        await unavailable(ctx.service.get(principal(),execution_id,cursor))
    asyncio.run(scenario())


def test_foreign_cursor_does_not_unlock_an_owned_result(registry_database,valid_manifest):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest)
        page=await ctx.service.get(principal(),ctx.execution.id)
        other=Principal(id="other-user",subject="synthetic-other",service_client_id=None,kind="demo")
        await unavailable(ctx.service.get(other,ctx.execution.id,page.next_cursor))
    asyncio.run(scenario())


def test_audit_insert_failure_blocks_existing_first_page_and_cursor(registry_database,valid_manifest):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest)
        page=await ctx.service.get(principal(),ctx.execution.id)
        registry_database.execute_owner('REVOKE INSERT (id,actor_id,service_client_id,action,product_id,version_id,policy_revision,policy_id,decision,parameter_names,parameter_digest,duration_ms,row_count,adapter,correlation_id) ON gateway.audit_events FROM "'+registry_database.roles["app"]+'"')
        for cursor in (None,page.next_cursor):
            with pytest.raises(GatewayError) as caught: await ctx.service.get(principal(),ctx.execution.id,cursor)
            assert caught.value.code=="UPSTREAM_UNAVAILABLE"
    asyncio.run(scenario())


def test_revocation_hides_expired_and_pending_execution_states(registry_database,valid_manifest):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest,expired=True)
        registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false")
        await unavailable(ctx.service.get(principal(),ctx.execution.id))
    asyncio.run(scenario())


def test_cursor_cannot_cross_two_authorized_queries_for_same_principal(registry_database,valid_manifest):
    async def scenario():
        from tests.integration.test_results import add_owned_result
        ctx=await seeded_result(registry_database,valid_manifest)
        other=await add_owned_result(registry_database,ctx)
        first=await ctx.service.get(principal(),ctx.execution.id)
        second=await ctx.service.get(principal(),other.id)
        assert first.next_cursor and second.next_cursor
        assert ctx.execution.canonical_hash!=other.canonical_hash
        await unavailable(ctx.service.get(principal(),other.id,first.next_cursor))
        await unavailable(ctx.service.get(principal(),ctx.execution.id,second.next_cursor))
        assert (await ctx.service.get(principal(),other.id,second.next_cursor)).row_count==451
    asyncio.run(scenario())


@pytest.mark.parametrize("state",["accepted","running"])
def test_revocation_hides_pending_execution(registry_database,valid_manifest,state):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest,state=state)
        registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false")
        await unavailable(ctx.service.get(principal(),ctx.execution.id))
    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["unknown","foreign_query","expired"])
def test_stale_owned_result_does_not_change_invalid_cursor_error(registry_database,valid_manifest,invalid):
    async def scenario():
        from datetime import timedelta
        from gateway.execution.cursors import CursorStore
        from tests.integration.test_results import add_owned_result
        ctx=await seeded_result(registry_database,valid_manifest)
        first=await ctx.service.get(principal(),ctx.execution.id)
        token="a"*43
        if invalid=="foreign_query":
            other=await add_owned_result(registry_database,ctx)
            token=(await ctx.service.get(principal(),other.id)).next_cursor
        elif invalid=="expired":
            async with registry_database.registry.transaction() as uow:
                token=await CursorStore(uow,ctx.clock.now()).issue("north-user",ctx.execution.id,1,200,ctx.clock.now()+timedelta(seconds=1))
        ctx.clock.instant=ctx.envelope.source_watermark+timedelta(seconds=86400,microseconds=1)
        await unavailable(ctx.service.get(principal(),ctx.execution.id,token))
        with pytest.raises(GatewayError) as caught: await ctx.service.get(principal(),ctx.execution.id,first.next_cursor)
        assert caught.value.code=="STALE_DATA"
    asyncio.run(scenario())
