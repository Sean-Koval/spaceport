"""Genuine PostgreSQL result transactions, page cursors, expiry and retention races."""
import asyncio
from datetime import timedelta
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb
from sqlalchemy import select, text

from gateway.adapters.base import CubeBinding, StaticBindingResolver, authorization_scope_digest
from gateway.adapters.normalize import NormalizationContext, normalize
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import Completed, ExecutionSpec, ExecutionView
from gateway.contracts.manifest import validate_manifest
from gateway.contracts.query import QueryRequest, validate_query
from gateway.registry.models import BindingRevision, Execution, ExecutionState, Result, TestRun as RegistryTestRun
from gateway.registry.repository import Registry
from tests.conftest import FakeClock
from tests.integration.test_audit import seed, service as policy_service, principal
from tests.integration.test_registry import publish

pytestmark=pytest.mark.postgres
ROOT=Path(__file__).resolve().parents[2]


def api(name="results"):
    try: return importlib.import_module("gateway.execution."+name)
    except ModuleNotFoundError as error: pytest.fail(f"Secure result implementation is missing: {error.name}")


async def seeded_result(database, valid_manifest, *, count=451, state="succeeded", expired=False, retention_seconds=None):
    seed(database,roles=("consumer","policy_admin"))
    manifest=validate_manifest(valid_manifest)
    raw=json.loads((ROOT/"fixtures/bindings/revenue.json").read_text())
    raw["model_bundle_digest"]=manifest.model_bundle_digest
    binding=CubeBinding.model_validate(raw)
    async with database.registry.transaction() as uow:
        uow.session.add(BindingRevision(id="binding-1",binding_id=manifest.binding_id,revision=1,
            content=binding.model_dump(mode="json"),content_digest=binding.digest,
            model_bundle_digest=manifest.model_bundle_digest,adapter_revision=manifest.adapter_revision,actor_id="operator"))
    draft=await database.registry.save_draft("revenue",manifest,0,"editor")
    async with database.registry.transaction() as uow:
        uow.session.add(RegistryTestRun(id="test-1",product_id="revenue",draft_id=draft.id,draft_digest=manifest.content_digest,
            binding_id="binding-1",binding_digest=binding.digest,policy_id=manifest.policy_id,policy_revision=1,
            fixture_version="1",evidence={"synthetic":"passed"},status="passed",actor_id="tester"))
    await publish(database.registry,draft)
    pinned=await database.registry.resolve("revenue")
    policy=policy_service(database)
    async with database.registry.transaction() as uow:
        now=await uow.session.scalar(text("SELECT clock_timestamp()"))
        decision=await policy.authorize(principal(),"execute",pinned,uow)
    clock=FakeClock(now)
    query=validate_query(manifest,QueryRequest(product_id="revenue",version=manifest.version,inputs={"start":"2024-03-09","end":"2024-03-12"}))
    canonical=query.model_dump(mode="json",exclude={"idempotency_key"})
    execution=Execution(id="execution-1",principal_id="north-user",product_id="revenue",version_id=pinned.version_id,
        policy_id=manifest.policy_id,policy_revision=1,canonical_query=canonical,canonical_hash=canonical_digest(canonical),
        context="consumer",request_id="request-1",deadline_at=now+timedelta(seconds=120))
    async with database.registry.transaction() as uow:
        uow.session.add(execution)
        await uow.session.flush()
        now=await uow.session.scalar(text("SELECT clock_timestamp()"))
        clock.instant=now
        from gateway.registry.models import ExecutionAuthorization
        uow.session.add(ExecutionAuthorization(execution_id=execution.id,
            principal_snapshot=principal().model_dump(mode="json"),decision=decision.model_dump(mode="json"),
            scope_digest=authorization_scope_digest(decision),full_decision_digest=canonical_digest(decision)))
        uow.session.add(ExecutionState(execution_id=execution.id,state=state,
            started_at=None if state=="accepted" else now,
            completed_at=now if state=="succeeded" else None))
        if state=="succeeded":
            spec=ExecutionSpec(principal_id=execution.principal_id,pinned_product=pinned,canonical_query=canonical,
                canonical_hash=execution.canonical_hash,policy_revision=1,context="consumer",request_id="request-1")
            view=ExecutionView(execution_id=execution.id,state="succeeded",accepted_at=execution.created_at,started_at=now,completed_at=now)
            rows=[{"Orders.ordered_at.day":"2024-03-09T00:00:00.000","Orders.segment":f"segment-{i}","Orders.revenue":"10.10"} for i in range(count)]
            envelope=normalize(pinned,Completed(raw_rows=rows,source_watermark=now-timedelta(hours=1)),view,
                context=NormalizationContext(spec=spec,decision=decision,executed_decision=decision,binding=binding,clock=clock))
            stored=api().prepare_stored_result(execution,envelope,decision,now)
            if retention_seconds is not None:
                stored.expires_at=now+timedelta(seconds=retention_seconds)
            if not expired: uow.session.add(stored)
    if state=="succeeded" and expired:
        # Only this disposable owner fixture may backdate clocks. Application ACLs prohibit it.
        database.execute_owner("INSERT INTO gateway.results (id,execution_id,principal_id,version_id,policy_revision,canonical_hash,envelope,created_at,expires_at) VALUES ('expired-result',%s,%s,%s,%s,%s,%s,%s,%s)",
            (execution.id,execution.principal_id,execution.version_id,execution.policy_revision,execution.canonical_hash,
             Jsonb(stored.envelope),now-timedelta(hours=25),now-timedelta(hours=1)))
    result_service=api().ResultService(database.registry,policy,StaticBindingResolver([binding]),clock)
    return SimpleNamespace(service=result_service,clock=clock,execution=execution,product=pinned,decision=decision,
        binding=binding,policy=policy,envelope=envelope if state=="succeeded" else None)


def test_complete_result_paginates_200_rows_and_preserves_total(registry_database,valid_manifest):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest)
        pages=[];cursor=None
        while True:
            page=await ctx.service.get(principal(),ctx.execution.id,cursor)
            pages.append(page)
            if page.next_cursor is None: break
            cursor=page.next_cursor
        assert [len(page.rows) for page in pages]==[200,200,51]
        assert all(page.row_count==451 and not page.truncated for page in pages)
        assert tuple(row for page in pages for row in page.rows)==ctx.envelope.rows
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='result' AND decision='allow'")==6
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='result' AND row_count=451 AND duration_ms>=0")==3
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='result' AND row_count IS NULL AND duration_ms IS NULL")==3
        assert registry_database.scalar("SELECT count(*) FROM gateway.result_cursors")==2
        assert registry_database.scalar("SELECT bool_and(expires_at <= (SELECT expires_at FROM gateway.results)) FROM gateway.result_cursors")
        assert registry_database.scalar("SELECT bool_and(length(token_digest)=64) FROM gateway.result_cursors")
    asyncio.run(scenario())


def test_cursor_replay_is_stable_and_requires_a_fresh_committed_audit(registry_database,valid_manifest):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest,count=201)
        first=await ctx.service.get(principal(),ctx.execution.id)
        pages=await asyncio.gather(*(ctx.service.get(principal(),ctx.execution.id,first.next_cursor) for _ in range(4)))
        assert all(page.rows==ctx.envelope.rows[200:] for page in pages)
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='result' AND decision='allow'")==10
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='result' AND row_count=201 AND duration_ms>=0")==5
    asyncio.run(scenario())


@pytest.mark.parametrize("state",["accepted","running"])
def test_pending_execution_has_no_result_bytes_but_is_audited(registry_database,valid_manifest,state):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest,state=state)
        view=await ctx.service.get(principal(),ctx.execution.id)
        assert type(view) is ExecutionView and view.state==state
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='result'")==2
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='result' AND row_count IS NULL AND duration_ms>=0")==1
    asyncio.run(scenario())


def test_retention_boundary_returns_expired_without_rows(registry_database,valid_manifest):
    async def scenario():
        # Isolate body retention from required freshness, which expires sooner.
        valid_manifest["freshness"]["required"]=False
        ctx=await seeded_result(registry_database,valid_manifest,count=1)
        expiry=registry_database.scalar("SELECT expires_at FROM gateway.results")
        ctx.clock.instant=expiry-timedelta(microseconds=1)
        assert (await ctx.service.get(principal(),ctx.execution.id)).state=="succeeded"
        ctx.clock.instant=expiry
        view=await ctx.service.get(principal(),ctx.execution.id)
        assert type(view) is ExecutionView and view.state=="expired" and not hasattr(view,"rows")
    asyncio.run(scenario())


def test_separate_retention_role_prunes_expired_bodies_and_old_audits(registry_database,valid_manifest):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest,expired=True)
        registry_database.insert_audit("old-audit",ctx.clock.now()-timedelta(days=31))
        registry_database.insert_audit("retained-audit",ctx.clock.now()-timedelta(days=29))
        maintenance=Registry(registry_database.urls["audit_retention"].render_as_string(hide_password=False))
        try:
            counts=await api("retention").RetentionSweep(registry_database.registry,maintenance).run_once(ctx.clock.now())
        finally: await maintenance.close()
        assert counts.deleted_results==1 and counts.deleted_audits==1
        assert registry_database.scalar("SELECT count(*) FROM gateway.results")==0
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE id='retained-audit'")==1
        assert registry_database.scalar("SELECT state FROM gateway.execution_states")=="expired"
        assert registry_database.denied_as_app("DELETE FROM gateway.audit_events")=="permission_denied"
        assert registry_database.denied_as_retention("SELECT * FROM gateway.results")=="permission_denied"
        assert (await ctx.service.get(principal(),ctx.execution.id)).state=="expired"
    asyncio.run(scenario())


def test_future_clock_cannot_delete_live_database_rows_or_audits(registry_database,valid_manifest):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest,count=1)
        maintenance=Registry(registry_database.urls["audit_retention"].render_as_string(hide_password=False))
        try:
            counts=await api("retention").RetentionSweep(registry_database.registry,maintenance).run_once(ctx.clock.now()+timedelta(days=90))
        finally: await maintenance.close()
        assert counts.deleted_results==0 and counts.deleted_audits==0
        assert registry_database.scalar("SELECT state FROM gateway.execution_states")=="succeeded"
    asyncio.run(scenario())


def test_access_and_two_sweeps_serialize_without_resurrecting_result(registry_database,valid_manifest):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest,expired=True)
        maintenance=Registry(registry_database.urls["audit_retention"].render_as_string(hide_password=False))
        try:
            sweep=api("retention").RetentionSweep(registry_database.registry,maintenance)
            results=await asyncio.gather(ctx.service.get(principal(),ctx.execution.id),sweep.run_once(ctx.clock.now()),sweep.run_once(ctx.clock.now()))
        finally: await maintenance.close()
        assert results[0].state=="expired"
        assert sum(item.deleted_results for item in results[1:])==1
        assert registry_database.scalar("SELECT count(*) FROM gateway.results")==0
        assert registry_database.scalar("SELECT count(*) FROM gateway.result_cursors")==0
        assert registry_database.scalar("SELECT state FROM gateway.execution_states")=="expired"
    asyncio.run(scenario())


def test_required_audit_commit_failure_prevents_bytes_and_cursor_release(registry_database,valid_manifest):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest)
        registry_database.execute_owner("CREATE FUNCTION gateway.reject_result_audit_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action='result' THEN RAISE EXCEPTION 'synthetic commit failure'; END IF; RETURN NEW; END $$; CREATE CONSTRAINT TRIGGER fail_result_audit AFTER INSERT ON gateway.audit_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.reject_result_audit_commit()")
        with pytest.raises(GatewayError) as caught: await ctx.service.get(principal(),ctx.execution.id)
        assert caught.value.code=="UPSTREAM_UNAVAILABLE"
        assert "synthetic" not in str(caught.value)
        assert registry_database.scalar("SELECT count(*) FROM gateway.result_cursors")==0
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='result'")==0
    asyncio.run(scenario())


async def add_owned_result(database, ctx):
    """A distinct immutable execution/query for cursor cross-scope attacks."""
    from gateway.registry.models import ExecutionAuthorization
    canonical={**ctx.execution.canonical_query,"inputs":{**ctx.execution.canonical_query["inputs"],"end":"2024-03-11"}}
    async with database.registry.transaction() as uow:
        now=await uow.session.scalar(text("SELECT clock_timestamp()"))
        execution=Execution(id="execution-2",principal_id="north-user",product_id="revenue",version_id=ctx.product.version_id,
            policy_id="revenue-access",policy_revision=1,canonical_query=canonical,canonical_hash=canonical_digest(canonical),
            context="consumer",request_id="request-2",deadline_at=now+timedelta(seconds=120))
        uow.session.add(execution)
        await uow.session.flush()
        now=await uow.session.scalar(text("SELECT clock_timestamp()"))
        uow.session.add(ExecutionAuthorization(execution_id=execution.id,principal_snapshot=principal().model_dump(mode="json"),
            decision=ctx.decision.model_dump(mode="json"),scope_digest=authorization_scope_digest(ctx.decision),full_decision_digest=canonical_digest(ctx.decision)))
        uow.session.add(ExecutionState(execution_id=execution.id,state="succeeded",started_at=now,completed_at=now))
        data=ctx.envelope.model_dump(mode="python")
        data.update(execution_id=execution.id,accepted_at=execution.created_at,started_at=now,completed_at=now)
        data["effective_range"]["end"]="2024-03-11"
        data["freshness"].update(assessed_at=now,age_seconds=(now-ctx.envelope.source_watermark).total_seconds())
        from gateway.contracts.execution import ResultEnvelope
        uow.session.add(api().prepare_stored_result(execution,ResultEnvelope.model_validate(data),ctx.decision,now))
    return execution


def test_retention_cascades_previously_live_cursor_without_app_audit_delete(registry_database,valid_manifest):
    async def scenario():
        from datetime import datetime,timezone
        ctx=await seeded_result(registry_database,valid_manifest,retention_seconds=2)
        page=await ctx.service.get(principal(),ctx.execution.id)
        assert page.next_cursor and registry_database.scalar("SELECT count(*) FROM gateway.result_cursors")==1
        expiry=registry_database.scalar("SELECT expires_at FROM gateway.results")
        await asyncio.sleep(max(0,(expiry-datetime.now(timezone.utc)).total_seconds())+.05)
        ctx.clock.instant=datetime.now(timezone.utc)
        maintenance=Registry(registry_database.urls["audit_retention"].render_as_string(hide_password=False))
        try: counts=await api("retention").RetentionSweep(registry_database.registry,maintenance).run_once(ctx.clock.now())
        finally: await maintenance.close()
        assert counts.deleted_results==1 and registry_database.scalar("SELECT count(*) FROM gateway.result_cursors")==0
        with pytest.raises(GatewayError) as caught: await ctx.service.get(principal(),ctx.execution.id,page.next_cursor)
        assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"
    asyncio.run(scenario())


def test_worker_completion_is_readable_only_through_reauthorized_result_service(registry_database):
    async def scenario():
        from gateway.contracts.clock import SystemClock
        from gateway.execution.worker import Worker
        from tests.integration.test_execution_queue import setup,RecordingAdapter
        execution_service,request=await setup(registry_database)
        request=request.model_copy(update={"idempotency_key":"result-replay"})
        accepted=await execution_service.submit(principal(),request)
        adapter=RecordingAdapter()
        assert await Worker(execution_service.queue,adapter_factory=lambda:adapter,worker_id="result-worker").run_once()
        results=api().ResultService(registry_database.registry,execution_service.policy,execution_service.bindings,SystemClock())
        result=await results.get(principal(),accepted.execution_id)
        assert result.state=="succeeded" and result.rows==() and result.row_count==0
        assert (await execution_service.submit(principal(),request)).execution_id==accepted.execution_id
        # The owner commits revocation before each subsequent authorization read.
        registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false")
        for operation in (results.get(principal(),accepted.execution_id),execution_service.submit(principal(),request)):
            with pytest.raises(GatewayError) as caught: await operation
            assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"
    asyncio.run(scenario())


def test_retention_skips_execution_state_lock_held_by_active_reader(registry_database,valid_manifest):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest,expired=True)
        maintenance=Registry(registry_database.urls["audit_retention"].render_as_string(hide_password=False))
        try:
            sweep=api("retention").RetentionSweep(registry_database.registry,maintenance)
            async with registry_database.registry.transaction() as reader:
                # ResultService holds this exact lock through the audit commit.
                await reader.session.scalar(select(ExecutionState).where(ExecutionState.execution_id==ctx.execution.id).with_for_update())
                counts=await asyncio.wait_for(sweep.run_once(ctx.clock.now()),timeout=5)
                assert counts.deleted_results==0
                assert registry_database.scalar("SELECT count(*) FROM gateway.results")==1
            counts=await sweep.run_once(ctx.clock.now())
            assert counts.deleted_results==1
        finally: await maintenance.close()
    asyncio.run(scenario())


def test_required_freshness_replay_and_cursor_fail_after_exact_threshold(registry_database,valid_manifest):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest)
        original=registry_database.scalar("SELECT envelope FROM gateway.results")
        first=await ctx.service.get(principal(),ctx.execution.id)
        assert first.next_cursor
        threshold=ctx.envelope.source_watermark+timedelta(seconds=ctx.product.manifest.freshness.max_age_seconds)
        ctx.clock.instant=threshold
        exact=await ctx.service.get(principal(),ctx.execution.id,first.next_cursor)
        assert exact.freshness.status=="fresh" and exact.freshness.age_seconds==86400
        assert exact.source_watermark==ctx.envelope.source_watermark
        ctx.clock.instant=threshold+timedelta(microseconds=1)
        for cursor in (None,first.next_cursor,exact.next_cursor):
            with pytest.raises(GatewayError) as caught: await ctx.service.get(principal(),ctx.execution.id,cursor)
            assert caught.value.code=="STALE_DATA"
        assert registry_database.scalar("SELECT envelope FROM gateway.results")==original
    asyncio.run(scenario())


def test_optional_freshness_updates_warning_on_pages_without_mutating_storage(registry_database,valid_manifest):
    async def scenario():
        valid_manifest["freshness"]["required"]=False
        ctx=await seeded_result(registry_database,valid_manifest)
        original=registry_database.scalar("SELECT envelope FROM gateway.results")
        first=await ctx.service.get(principal(),ctx.execution.id)
        ctx.clock.instant=ctx.envelope.source_watermark+timedelta(seconds=86400,microseconds=1)
        page=await ctx.service.get(principal(),ctx.execution.id,first.next_cursor)
        assert page.freshness.status=="stale" and page.freshness.assessed_at==ctx.clock.now()
        assert page.freshness.age_seconds==86400.000001
        assert page.warnings==("Source-load freshness is stale.",)
        assert page.source_watermark==first.source_watermark and page.completed_at==first.completed_at
        assert registry_database.scalar("SELECT envelope FROM gateway.results")==original
    asyncio.run(scenario())


def test_empty_result_outcome_audit_records_zero_and_timing_without_update_privilege(registry_database,valid_manifest):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest,count=0)
        page=await ctx.service.get(principal(),ctx.execution.id)
        assert page.rows==() and page.row_count==0
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='result' AND row_count=0 AND duration_ms>=0")==1
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='result' AND row_count IS NULL AND duration_ms IS NULL")==1
        assert registry_database.scalar("SELECT count(DISTINCT correlation_id) FROM gateway.audit_events WHERE action='result'")==1
        assert registry_database.denied_as_app("UPDATE gateway.audit_events SET row_count=0")=="permission_denied"
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["insert","commit"])
def test_required_outcome_audit_failure_rolls_back_previously_flushed_cursor(registry_database,valid_manifest,phase):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest,count=201)
        registry_database.execute_owner("CREATE FUNCTION gateway.reject_result_outcome() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action='result' AND NEW.duration_ms IS NOT NULL THEN RAISE EXCEPTION 'synthetic outcome audit failure'; END IF; RETURN NEW; END $$")
        trigger=("CREATE TRIGGER fail_result_outcome BEFORE INSERT ON gateway.audit_events FOR EACH ROW EXECUTE FUNCTION gateway.reject_result_outcome()" if phase=="insert" else
            "CREATE CONSTRAINT TRIGGER fail_result_outcome AFTER INSERT ON gateway.audit_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.reject_result_outcome()")
        registry_database.execute_owner(trigger)
        with pytest.raises(GatewayError) as caught: await ctx.service.get(principal(),ctx.execution.id)
        assert caught.value.code=="UPSTREAM_UNAVAILABLE" and "synthetic" not in str(caught.value)
        assert registry_database.scalar("SELECT count(*) FROM gateway.result_cursors")==0
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='result'")==0
    asyncio.run(scenario())


@pytest.mark.parametrize("optional_stale", [False,True])
def test_current_deprecation_warning_composes_on_every_page_without_stored_mutation(registry_database,valid_manifest,optional_stale):
    async def scenario():
        if optional_stale: valid_manifest["freshness"]["required"]=False
        ctx=await seeded_result(registry_database,valid_manifest,count=201)
        original=registry_database.scalar("SELECT envelope FROM gateway.results")
        first=await ctx.service.get(principal(),ctx.execution.id)
        assert first.warnings==()
        registry_database.execute_owner("UPDATE gateway.version_states SET status='deprecated',reason='private synthetic lifecycle reason'")
        if optional_stale: ctx.clock.instant=ctx.envelope.source_watermark+timedelta(seconds=86400,microseconds=1)
        expected=(("Source-load freshness is stale.",) if optional_stale else ())+ ("This product version is deprecated.",)
        for cursor in (None,first.next_cursor):
            page=await ctx.service.get(principal(),ctx.execution.id,cursor)
            assert page.warnings==expected
            assert page.source_watermark==first.source_watermark and page.completed_at==first.completed_at
        assert registry_database.scalar("SELECT envelope FROM gateway.results")==original
        registry_database.execute_owner("UPDATE gateway.version_states SET status='withdrawn'")
        for cursor in (None,first.next_cursor):
            with pytest.raises(GatewayError) as caught: await ctx.service.get(principal(),ctx.execution.id,cursor)
            assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"
    asyncio.run(scenario())
