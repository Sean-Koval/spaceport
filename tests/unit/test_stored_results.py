"""Deterministic stored-result validation; these are not database acceptance."""
from datetime import timedelta
from dataclasses import replace
import importlib

import pytest

from gateway.contracts.canonical import canonical_bytes, canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import ResultEnvelope
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import Limits
from gateway.registry.models import Execution
from tests.unit.test_normalization import setup, completed

pytestmark = pytest.mark.unit


def api():
    try:
        return importlib.import_module("gateway.execution.results")
    except ModuleNotFoundError as error:
        pytest.fail(f"Result access implementation is missing: {error.name}")


def fixture(clock, *, count=3):
    module, product, context, view = setup(clock)
    rows = None if count == 3 else [{"Orders.ordered_at.day": "2024-03-09T00:00:00.000",
        "Orders.segment": f"segment-{index}", "Orders.revenue": "10.10"} for index in range(count)]
    envelope = module.normalize(product, completed(rows), view, context=context)
    execution = Execution(id=view.execution_id, created_at=view.accepted_at,
        principal_id=context.decision.principal_id, product_id=product.manifest.product_id,
        version_id=product.version_id, policy_id=product.manifest.policy_id, policy_revision=1,
        canonical_query=context.spec.model_dump(mode="json")["canonical_query"], canonical_hash=context.spec.canonical_hash,
        context="consumer", request_id="request-1", deadline_at=clock.now()+timedelta(seconds=120))
    return execution, envelope, product, context


def project(clock, *, current=None, envelope=None, execution=None, context=None, product=None):
    ex, stored, pinned, ctx = fixture(clock)
    return api().project_stored_result(execution or ex, envelope or stored, product or pinned,
        (context or ctx).binding, (context or ctx).executed_decision, current or (context or ctx).decision, now=clock.now())


def test_prepares_complete_result_with_database_bounded_retention(clock):
    execution, envelope, _, context = fixture(clock)
    stored = api().prepare_stored_result(execution, envelope, context.executed_decision, clock.now())
    assert stored.envelope == envelope.model_dump(mode="json")
    assert stored.execution_id == execution.id and stored.canonical_hash == execution.canonical_hash
    assert stored.principal_id == execution.principal_id and stored.version_id == execution.version_id
    assert stored.policy_revision == 1 and stored.expires_at == clock.now()+timedelta(hours=24)
    assert "created_at" not in stored.__dict__  # App role cannot backdate the retention clock.


@pytest.mark.parametrize("change", ["cursor", "partial", "execution", "principal", "policy", "hash", "version", "accepted", "future", "naive"])
def test_storage_rejects_inconsistent_or_partial_completion(change, clock):
    execution, envelope, _, context = fixture(clock)
    now=clock.now()
    decision=context.executed_decision
    if change == "cursor": envelope=envelope.model_copy(update={"next_cursor":"untrusted"})
    elif change == "partial": envelope=envelope.model_copy(update={"rows":envelope.rows[:1]})
    elif change == "execution": execution.id="other-execution"
    elif change == "principal": decision=decision.model_copy(update={"principal_id":"other-principal"})
    elif change == "policy": decision=decision.model_copy(update={"policy_revision":2})
    elif change == "hash": execution.canonical_hash="0"*64
    elif change == "version": envelope=envelope.model_copy(update={"version":"2.0.0"})
    elif change == "accepted": execution.created_at=now-timedelta(seconds=1)
    elif change == "future": now-=timedelta(seconds=1)
    else: now=now.replace(tzinfo=None)
    with pytest.raises(GatewayError) as caught:
        api().prepare_stored_result(execution,envelope,decision,now)
    assert caught.value.code=="BINDING_DRIFT"


def test_projection_preserves_rows_and_watermark_but_reassesses_recorded_age(clock):
    execution,envelope,product,context=fixture(clock)
    projected=project(clock)
    assert projected==envelope and projected.rows[-1]["revenue"]=="9007199254740993.01"
    clock.instant+=timedelta(hours=4)
    later=api().project_stored_result(execution,envelope,product,context.binding,context.executed_decision,context.decision,now=clock.now())
    assert later.source_watermark==envelope.source_watermark and later.completed_at==envelope.completed_at
    assert later.freshness.assessed_at==clock.now() and later.freshness.age_seconds==18000
    assert envelope.freshness.age_seconds==3600


def test_measure_only_tightening_projects_rows_schema_and_units(clock):
    _,_,_,context=fixture(clock)
    current=context.decision.model_copy(update={"visible_fields":("day","segment")})
    result=project(clock,current=current)
    assert set(result.typed_schema)=={"day","segment"} and result.units=={}
    assert all(set(row)=={"day","segment"} for row in result.rows)
    assert result.row_count==3 and not result.truncated


@pytest.mark.parametrize("change", ["principal","revision","tenant","entitlement","typed_entitlement","grain","expansion"])
def test_changed_scope_or_unsafe_visibility_requires_new_query(change,clock):
    execution,envelope,product,context=fixture(clock)
    current=context.decision
    if change=="principal": current=current.model_copy(update={"principal_id":"south-user"})
    elif change=="revision": current=current.model_copy(update={"policy_revision":2})
    elif change=="tenant": current=current.model_copy(update={"tenant_id":"south"})
    elif change in {"entitlement","typed_entitlement"}:
        raw=current.model_dump(mode="python");raw["row_constraints"][1]["values"]=["changed" if change=="entitlement" else True]
        current=Decision.model_validate(raw)
    elif change=="grain": current=current.model_copy(update={"visible_fields":("day","revenue")})
    else:
        envelope=ResultEnvelope.model_validate({**envelope.model_dump(mode="python"),"typed_schema":{k:v for k,v in envelope.typed_schema.items() if k!="revenue"},"units":{},"rows":[{k:v for k,v in row.items() if k!="revenue"} for row in envelope.rows]})
    with pytest.raises(GatewayError) as caught:
        project(clock,current=current,envelope=envelope)
    assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"


@pytest.mark.parametrize("dependency", ["filter","sort","mapped_input"])
def test_hidden_saved_query_dependency_is_denied_before_projection(dependency,clock):
    execution,envelope,product,context=fixture(clock)
    # Hiding a dimension is denied both for full-grain and saved-selection semantics.
    raw=dict(execution.canonical_query)
    if dependency=="filter": raw["filters"]=[{"field":"segment","operator":"eq","value":"A"}]
    elif dependency=="sort": raw["sort"]=[{"field":"segment","direction":"asc"}]
    else: raw["inputs"]={**raw["inputs"],"segment":"A"}
    execution.canonical_query=raw;execution.canonical_hash=canonical_digest(raw)
    current=context.decision.model_copy(update={"visible_fields":("day","revenue")})
    with pytest.raises(GatewayError) as caught:
        project(clock,current=current,execution=execution)
    assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"


@pytest.mark.parametrize("limit", ["rows","bytes","dates"])
def test_current_limits_apply_to_complete_result_before_any_page(limit,clock):
    _,envelope,_,context=fixture(clock)
    values=context.decision.limits.model_dump(mode="python")
    if limit=="rows": values.update(max_rows=2,page_size=2)
    elif limit=="bytes": values["max_bytes"]=len(canonical_bytes(envelope))-1
    else: values["max_date_range_days"]=2
    current=context.decision.model_copy(update={"limits":Limits(**values)})
    with pytest.raises(GatewayError) as caught: project(clock,current=current)
    assert caught.value.code=="RESOURCE_LIMIT"


@pytest.mark.parametrize("change", ["digest","schema","range","provenance","timezone","partial","warnings"])
def test_stored_metadata_must_match_pinned_product_and_execution(change,clock):
    _,envelope,_,_=fixture(clock)
    data=envelope.model_dump(mode="python")
    if change=="digest": data["product_digest"]="0"*64
    elif change=="schema": data["typed_schema"]["segment"]["nullable"]=False
    elif change=="range": data["effective_range"]["end"]="2024-03-13"
    elif change=="provenance": data["provenance"]["binding_revision"]=2
    elif change=="timezone": data["timezone"]="UTC"
    elif change=="partial": data["rows"]=data["rows"][:1]
    else: data["warnings"]=["Unexpected source metadata"]
    unsafe=ResultEnvelope.model_construct(**data)
    with pytest.raises(GatewayError) as caught: project(clock,envelope=unsafe)
    assert caught.value.code=="BINDING_DRIFT"


@pytest.mark.parametrize("limit",["rows","bytes"])
def test_completion_enforces_executed_decision_limits(limit,clock):
    execution,envelope,_,context=fixture(clock)
    values=context.executed_decision.limits.model_dump(mode="python")
    if limit=="rows": values.update(max_rows=2,page_size=2)
    else: values["max_bytes"]=len(canonical_bytes(envelope))-1
    decision=context.executed_decision.model_copy(update={"limits":Limits(**values)})
    with pytest.raises(GatewayError) as caught:
        api().prepare_stored_result(execution,envelope,decision,clock.now())
    assert caught.value.code=="BINDING_DRIFT"


def test_hidden_measure_sort_is_denied_without_hiding_any_grain_column(clock):
    from tests.unit.test_normalization import ROOT
    from gateway.contracts.manifest import validate_manifest
    import json
    execution,envelope,product,context=fixture(clock)
    raw=json.loads((ROOT/"fixtures/products/revenue.json").read_text())
    raw.update(sort={"fields":["day","segment","revenue"],"max_fields":3},content_digest=None)
    manifest=validate_manifest(raw)
    product=product.model_copy(update={"manifest":manifest})
    execution.canonical_query["sort"]=[{"field":"revenue","direction":"desc"}]
    execution.canonical_hash=canonical_digest(execution.canonical_query)
    envelope=envelope.model_copy(update={"product_digest":manifest.content_digest})
    current=context.decision.model_copy(update={"visible_fields":("day","segment")})
    with pytest.raises(GatewayError) as caught:
        api().project_stored_result(execution,envelope,product,context.binding,context.executed_decision,current,now=clock.now())
    assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"


def test_retention_cutoff_is_never_a_future_database_instant(clock):
    try: retention=importlib.import_module("gateway.execution.retention")
    except ModuleNotFoundError as error: pytest.fail(f"Retention implementation is missing: {error.name}")
    now=clock.now()
    assert retention.retention_cutoff(now+timedelta(days=90),now)==now
    assert retention.retention_cutoff(now-timedelta(days=1),now)==now-timedelta(days=1)
    with pytest.raises(ValueError): retention.retention_cutoff(now.replace(tzinfo=None),now)


def test_retention_rejects_shared_role_and_wrong_database_without_connecting():
    from gateway.execution.retention import RetentionSweep
    from gateway.registry.repository import Registry
    app=Registry("postgresql+psycopg://app:synthetic@127.0.0.1:5432/synthetic")
    same=Registry("postgresql+psycopg://app:synthetic@127.0.0.1:5432/synthetic")
    wrong=Registry("postgresql+psycopg://retention:synthetic@127.0.0.1:5432/other")
    with pytest.raises(ValueError): RetentionSweep(app,same)
    with pytest.raises(ValueError): RetentionSweep(app,wrong)


def test_completion_rejects_timestamp_inconsistent_with_freshness(clock):
    execution,envelope,_,context=fixture(clock)
    # A normalized result must not assert its assessment predates completion.
    late=envelope.model_copy(update={"completed_at":clock.now()+timedelta(seconds=1)})
    with pytest.raises(GatewayError) as caught:
        api().prepare_stored_result(execution,late,context.executed_decision,clock.now()+timedelta(seconds=1))
    assert caught.value.code=="BINDING_DRIFT"


class TransactionProbe:
    """Unit-only transaction probe, not PostgreSQL/ACL or authorization evidence."""
    def __init__(self, clock, *, audit_failure=False, commit_failure=False, count=3,
                 outcome_audit_failure=False):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from gateway.adapters.base import StaticBindingResolver, authorization_scope_digest
        from gateway.policy.audit import AuditWriter
        from gateway.registry.models import ExecutionAuthorization, ExecutionState, Result, VersionState
        execution,envelope,product,context=fixture(clock,count=count)
        self.committed=False
        self.rolled_back=False
        self.audit_failure=audit_failure
        self.commit_failure=commit_failure
        self.outcome_audit_failure=outcome_audit_failure
        self.events=[]
        self.cursors=[]
        self.committed_events=[]
        self.committed_cursors=[]
        self.added=[]
        self.clock=clock
        self.execution=execution
        self.envelope=envelope
        self.product=product
        self.context=context
        self.authorization=ExecutionAuthorization(execution_id=execution.id,principal_snapshot={"id":"north-user","subject":"synthetic-north","kind":"demo","service_client_id":None},
            decision=context.executed_decision.model_dump(mode="json"),scope_digest=authorization_scope_digest(context.executed_decision),full_decision_digest=canonical_digest(context.executed_decision))
        self.state=ExecutionState(execution_id=execution.id,state="succeeded",started_at=envelope.started_at,completed_at=envelope.completed_at)
        self.lifecycle=VersionState(version_id=product.version_id,status="published")
        self.result=Result(id="result-1",execution_id=execution.id,principal_id=execution.principal_id,
            version_id=execution.version_id,policy_revision=1,canonical_hash=execution.canonical_hash,
            envelope=envelope.model_dump(mode="json"),expires_at=clock.now()+timedelta(hours=24))
        self.audit=AuditWriter(b"synthetic-unit-audit-key-32-bytes!!")
        self.policy=SimpleNamespace(audit=self.audit,authorize=self.authorize)
        self.bindings=StaticBindingResolver([context.binding])
        self.uow=SimpleNamespace(session=self,lock_product=self.lock_product)
        @asynccontextmanager
        async def transaction():
            event_start,cursor_start=len(self.events),len(self.cursors)
            try:
                yield self.uow
                if self.commit_failure:
                    if isinstance(self.commit_failure, Exception):
                        raise self.commit_failure
                    from sqlalchemy.exc import SQLAlchemyError
                    raise SQLAlchemyError("private synthetic commit failure")
                self.committed=True
                self.committed_events.extend(self.events[event_start:])
                self.committed_cursors.extend(self.cursors[cursor_start:])
            except BaseException:
                self.rolled_back=True
                del self.events[event_start:]
                del self.cursors[cursor_start:]
                raise
        self.transaction=transaction

    async def lock_product(self,_): return None
    def in_transaction(self): return True
    def add(self, value):
        from gateway.registry.models import AuditEvent,ResultCursor
        self.added.append(value)
        if isinstance(value, AuditEvent):
            if self.audit_failure or self.outcome_audit_failure and value.duration_ms is not None:
                raise RuntimeError("private synthetic audit failure")
            self.events.append(value)
        elif isinstance(value, ResultCursor): self.cursors.append(value)
        else: raise AssertionError("Unexpected transaction-probe write")
    async def flush(self): return None
    async def rollback(self): self.rolled_back=True
    async def get(self,_,key): return self.authorization
    async def execute(self,statement):
        from types import SimpleNamespace
        return SimpleNamespace(one_or_none=lambda:(SimpleNamespace(id=self.product.version_id,
            manifest=self.product.manifest.model_dump(mode="json"),content_digest=self.product.manifest.content_digest,
            binding_digest=self.product.binding_digest),SimpleNamespace(content_digest=self.product.binding_digest)))
    async def scalar(self,statement):
        from sqlalchemy.sql.elements import TextClause
        from gateway.registry.models import Execution,ExecutionState,Result,ResultCursor,VersionState
        if isinstance(statement,TextClause): return self.clock.now()
        if statement.column_descriptions[0]["entity"] is ResultCursor:
            values=set(statement.compile().params.values())
            return next((row for row in self.committed_cursors if row.token_digest in values
                and row.result_id==self.result.id and row.principal_id==self.result.principal_id
                and row.version_id==self.result.version_id and row.policy_revision==self.result.policy_revision),None)
        return {Execution:self.execution,VersionState:self.lifecycle,ExecutionState:self.state,Result:self.result}[statement.column_descriptions[0]["entity"]]
    async def authorize(self,actor,action,product,uow,**kwargs):
        await self.audit.append(uow,self.audit.event(actor,action,product=product,policy_revision=1,decision="allow",correlation_id=kwargs["correlation_id"]))
        return self.context.decision


def test_result_service_releases_only_after_transaction_commit(clock,north_principal):
    import asyncio
    probe=TransactionProbe(clock)
    response=asyncio.run(api().ResultService(probe,probe.policy,probe.bindings,clock).get(north_principal,"execution-1"))
    assert response==probe.envelope and probe.committed and not probe.rolled_back
    assert len(probe.events)==2 and all(event.action=="result" for event in probe.events)


@pytest.mark.parametrize("failure",["audit","commit"])
def test_result_service_never_returns_envelope_when_audit_or_commit_fails(failure,clock,north_principal):
    import asyncio
    probe=TransactionProbe(clock,audit_failure=failure=="audit",commit_failure=failure=="commit")
    with pytest.raises(GatewayError) as caught:
        asyncio.run(api().ResultService(probe,probe.policy,probe.bindings,clock).get(north_principal,"execution-1"))
    assert caught.value.code=="UPSTREAM_UNAVAILABLE" and "private" not in str(caught.value)
    assert not probe.committed and probe.rolled_back


@pytest.mark.parametrize("change",["scope_digest","decision_digest","principal","partial","state_timestamp"])
def test_result_service_checks_stored_authority_and_completion_before_release(change,clock,north_principal):
    import asyncio
    probe=TransactionProbe(clock)
    if change=="scope_digest": probe.authorization.scope_digest="0"*64
    elif change=="decision_digest": probe.authorization.full_decision_digest="0"*64
    elif change=="principal": probe.authorization.principal_snapshot={**probe.authorization.principal_snapshot,"subject":"changed-identity"}
    elif change=="partial": probe.result.envelope["row_count"]+=1
    else: probe.state.started_at=clock.now()-timedelta(seconds=1)
    with pytest.raises(GatewayError) as caught:
        asyncio.run(api().ResultService(probe,probe.policy,probe.bindings,clock).get(north_principal,"execution-1"))
    assert caught.value.code==("NOT_FOUND_OR_FORBIDDEN" if change=="principal" else "BINDING_DRIFT")


@pytest.mark.parametrize("state,code",[("failed","UPSTREAM_UNAVAILABLE"),("timed_out","EXECUTION_TIMEOUT")])
def test_failed_or_timed_out_owned_view_is_safe_and_audited(state,code,clock,north_principal):
    import asyncio
    from gateway.contracts.execution import ExecutionView
    probe=TransactionProbe(clock)
    probe.state.state=state;probe.state.error_code=code
    view=asyncio.run(api().ResultService(probe,probe.policy,probe.bindings,clock).get(north_principal,"execution-1"))
    assert type(view) is ExecutionView and view.state==state and view.error.code==code
    assert not hasattr(view,"rows") and probe.committed


def test_consumer_store_rejects_fixture_execution(clock):
    execution,envelope,_,context=fixture(clock)
    execution.context="fixture_test"
    with pytest.raises(GatewayError) as caught:
        api().prepare_stored_result(execution,envelope,context.executed_decision,clock.now())
    assert caught.value.code=="BINDING_DRIFT"


def test_required_stored_freshness_exact_threshold_is_fresh_then_fails(clock):
    execution,envelope,product,context=fixture(clock)
    threshold=envelope.source_watermark+timedelta(seconds=product.manifest.freshness.max_age_seconds)
    result=api().project_stored_result(execution,envelope,product,context.binding,context.executed_decision,context.decision,now=threshold)
    assert result.freshness.status=="fresh" and result.freshness.age_seconds==86400
    with pytest.raises(GatewayError) as caught:
        api().project_stored_result(execution,envelope,product,context.binding,context.executed_decision,context.decision,now=threshold+timedelta(microseconds=1))
    assert caught.value.code=="STALE_DATA"
    assert envelope.freshness.age_seconds==3600 and envelope.freshness.status=="fresh"


@pytest.mark.parametrize("source",["stale","missing","future"])
def test_optional_stored_freshness_reassesses_truthfully_with_warning(source,clock):
    import json
    from tests.unit.test_normalization import ROOT
    raw=json.loads((ROOT/"fixtures/products/revenue.json").read_text())
    raw["freshness"]["required"]=False
    module,product,context,view=setup(clock,manifest_raw=raw)
    watermark={"stale":"2024-03-12T00:00:00Z","missing":None,"future":"2024-03-13T00:00:00Z"}[source]
    envelope=module.normalize(product,completed(watermark=watermark),view,context=context)
    execution,_,_,_=fixture(clock)
    now=clock.now()+timedelta(seconds=86400 if source=="stale" else 1)
    result=api().project_stored_result(execution,envelope,product,context.binding,context.executed_decision,context.decision,now=now)
    expected="stale" if source=="stale" else "unknown"
    assert result.freshness.status==expected and result.freshness.assessed_at==now
    assert result.warnings==("Source-load freshness is "+expected+".",)
    assert result.source_watermark==envelope.source_watermark and result.completed_at==envelope.completed_at
    if source!="stale": assert result.freshness.age_seconds is None


def test_freshness_reassessment_enforces_complete_byte_limit(clock):
    execution,envelope,product,context=fixture(clock)
    # Fractional-second metadata needs more bytes even though row data is unchanged.
    values=context.decision.limits.model_dump(mode="python");values["max_bytes"]=len(canonical_bytes(envelope))
    current=context.decision.model_copy(update={"limits":Limits(**values)})
    with pytest.raises(GatewayError) as caught:
        api().project_stored_result(execution,envelope,product,context.binding,context.executed_decision,current,now=clock.now()+timedelta(microseconds=1))
    assert caught.value.code=="RESOURCE_LIMIT"


def test_required_unprovable_current_source_age_is_not_returned(clock):
    execution,envelope,product,context=fixture(clock)
    # A backwards operator clock cannot claim known source age in the pure seam.
    with pytest.raises(GatewayError) as caught:
        api().project_stored_result(execution,envelope,product,context.binding,context.executed_decision,context.decision,now=envelope.source_watermark-timedelta(seconds=1))
    assert caught.value.code=="FRESHNESS_UNKNOWN"


@pytest.mark.parametrize("sqlstate", ["P0001", "23505", "40001", "40P01"])
def test_result_commit_uses_real_registry_translation_without_exposing_conflict(sqlstate,clock,north_principal):
    import asyncio
    from psycopg.errors import lookup
    from sqlalchemy.exc import DBAPIError
    from gateway.registry.repository import database_error
    original=lookup(sqlstate)("private synthetic commit failure")
    translated=database_error(DBAPIError("private statement",{},original))
    assert original.sqlstate==sqlstate and translated.code=="CONFLICT"
    probe=TransactionProbe(clock,commit_failure=translated,count=201)
    with pytest.raises(GatewayError) as caught:
        asyncio.run(api().ResultService(probe,probe.policy,probe.bindings,clock).get(north_principal,"execution-1"))
    assert caught.value.code=="UPSTREAM_UNAVAILABLE"
    assert caught.value.safe_message=="The result store is unavailable."
    assert not probe.committed and probe.rolled_back
    assert probe.committed_events==[] and probe.committed_cursors==[]


def test_deprecation_is_current_on_first_and_cursor_pages_and_leaves_storage_immutable(clock,north_principal):
    import asyncio
    from copy import deepcopy
    probe=TransactionProbe(clock,count=201)
    stored=deepcopy(probe.result.envelope)
    async def scenario():
        service=api().ResultService(probe,probe.policy,probe.bindings,clock)
        published=await service.get(north_principal,"execution-1")
        assert published.next_cursor and published.warnings==()
        probe.lifecycle.status="deprecated"
        for cursor in (None,published.next_cursor):
            page=await service.get(north_principal,"execution-1",cursor)
            assert page.warnings==("This product version is deprecated.",)
            assert page.source_watermark==published.source_watermark
            assert page.completed_at==published.completed_at
        probe.lifecycle.status="withdrawn"
        for cursor in (None,published.next_cursor):
            with pytest.raises(GatewayError) as caught: await service.get(north_principal,"execution-1",cursor)
            assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"
    asyncio.run(scenario())
    assert probe.result.envelope==stored


def test_deprecation_preserves_optional_freshness_warning_and_omits_lifecycle_reason(clock,north_principal):
    import asyncio,json
    from copy import deepcopy
    from tests.unit.test_normalization import ROOT
    probe=TransactionProbe(clock)
    raw=json.loads((ROOT/"fixtures/products/revenue.json").read_text())
    raw["freshness"]["required"]=False
    module,product,context,view=setup(clock,manifest_raw=raw)
    probe.product=product
    probe.context=context
    envelope=module.normalize(product,completed(watermark=None),view,context=context)
    probe.result.envelope=envelope.model_dump(mode="json")
    probe.lifecycle.status="deprecated"
    probe.lifecycle.reason="private lifecycle reason and unsafe external instructions"
    stored=deepcopy(probe.result.envelope)
    page=asyncio.run(api().ResultService(probe,probe.policy,probe.bindings,clock).get(north_principal,"execution-1"))
    assert page.warnings==("Source-load freshness is unknown.","This product version is deprecated.")
    assert "private" not in str(page.warnings) and probe.result.envelope==stored


def test_deprecation_warning_is_included_in_complete_byte_limit(clock,north_principal):
    import asyncio
    probe=TransactionProbe(clock)
    limits=probe.context.decision.limits.model_dump(mode="python")
    limits["max_bytes"]=len(canonical_bytes(probe.envelope))
    probe.context=replace(probe.context,decision=probe.context.decision.model_copy(update={"limits":Limits(**limits)}))
    probe.lifecycle.status="deprecated"
    with pytest.raises(GatewayError) as caught:
        asyncio.run(api().ResultService(probe,probe.policy,probe.bindings,clock).get(north_principal,"execution-1"))
    assert caught.value.code=="RESOURCE_LIMIT"
    assert probe.committed_events==[] and probe.committed_cursors==[]


@pytest.mark.parametrize("count", [0,3,201])
def test_result_outcome_audit_appends_complete_count_and_measured_duration(count,clock,north_principal,monkeypatch):
    import asyncio
    from gateway.registry.models import AuditEvent
    probe=TransactionProbe(clock,count=count)
    # The monotonic duration includes construction/validation and excludes the
    # final required outcome-audit flush, transaction commit and network delivery.
    ticks=iter([1_000_000_000,1_025_000_000]*2)
    monkeypatch.setattr(api(),"perf_counter_ns",lambda:next(ticks),raising=False)
    async def scenario():
        service=api().ResultService(probe,probe.policy,probe.bindings,clock)
        first=await service.get(north_principal,"execution-1")
        if first.next_cursor:
            later=await service.get(north_principal,"execution-1",first.next_cursor)
            assert len(later.rows)==1 and later.row_count==count
        outcome=[event for event in probe.added if isinstance(event,AuditEvent) and event.duration_ms is not None]
        assert len(outcome)==(2 if first.next_cursor else 1)
        assert all(event.row_count==count and event.duration_ms==25 for event in outcome)
        assert all(event.action=="result" and event.decision=="allow" and event.actor_id=="north-user"
            and event.product_id==probe.product.manifest.product_id and event.version_id==probe.product.version_id
            and event.policy_revision==1 and event.adapter==probe.product.manifest.adapter_revision
            and event.parameter_names==["end","segment","start"] and len(event.parameter_digest)==64 for event in outcome)
        # A new audit INSERT records the outcome; the authorization record stays
        # unchanged, rather than acquiring UPDATE rights or mutating its metrics.
        authorizations=[event for event in probe.added if isinstance(event,AuditEvent) and event.duration_ms is None]
        assert len(authorizations)==len(outcome)
        assert all(event.row_count is None for event in authorizations)
        assert all(a is not b and a.correlation_id==b.correlation_id for a,b in zip(authorizations,outcome))
    asyncio.run(scenario())


def test_required_outcome_audit_failure_rolls_back_issued_cursor_and_prevents_response(clock,north_principal):
    import asyncio
    from gateway.registry.models import ResultCursor
    probe=TransactionProbe(clock,count=201,outcome_audit_failure=True)
    with pytest.raises(GatewayError) as caught:
        asyncio.run(api().ResultService(probe,probe.policy,probe.bindings,clock).get(north_principal,"execution-1"))
    assert caught.value.code=="UPSTREAM_UNAVAILABLE"
    assert any(isinstance(value,ResultCursor) for value in probe.added)
    assert probe.rolled_back and not probe.committed
    assert probe.committed_events==[] and probe.committed_cursors==[]


@pytest.mark.parametrize("blocked", ["stale","rows","bytes","dates"])
@pytest.mark.parametrize("invalid", ["unknown","foreign","expired"])
def test_valid_looking_invalid_cursor_precedes_freshness_and_limits(blocked,invalid,clock,north_principal):
    import asyncio
    from gateway.execution.cursors import cursor_digest
    from gateway.registry.models import ResultCursor
    probe=TransactionProbe(clock)
    token="a"*43
    if invalid!="unknown":
        probe.committed_cursors.append(ResultCursor(token_digest=cursor_digest(token),result_id="foreign-result" if invalid=="foreign" else probe.result.id,
            principal_id=probe.result.principal_id,version_id=probe.result.version_id,policy_revision=1,position=1,
            expires_at=clock.now()-timedelta(seconds=1) if invalid=="expired" else probe.result.expires_at))
    if blocked=="stale": clock.instant=probe.envelope.source_watermark+timedelta(seconds=86400,microseconds=1)
    else:
        limits=probe.context.decision.limits.model_dump(mode="python")
        if blocked=="rows": limits.update(max_rows=2,page_size=2)
        elif blocked=="bytes": limits["max_bytes"]=len(canonical_bytes(probe.envelope))-1
        else: limits["max_date_range_days"]=2
        probe.context=replace(probe.context,decision=probe.context.decision.model_copy(update={"limits":Limits(**limits)}))
    with pytest.raises(GatewayError) as caught:
        asyncio.run(api().ResultService(probe,probe.policy,probe.bindings,clock).get(north_principal,"execution-1",token))
    assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"
    assert caught.value.safe_message=="The requested product is unavailable."


def test_denied_result_audit_commit_uses_storage_unavailable_contract(clock,north_principal):
    import asyncio
    from psycopg.errors import RaiseException
    from sqlalchemy.exc import DBAPIError
    from gateway.registry.repository import database_error
    translated=database_error(DBAPIError("private statement",{},RaiseException("private synthetic commit failure")))
    probe=TransactionProbe(clock,commit_failure=translated)
    with pytest.raises(GatewayError) as caught:
        asyncio.run(api().ResultService(probe,probe.policy,probe.bindings,clock).get(north_principal,"invalid execution identifier"))
    assert caught.value.code=="UPSTREAM_UNAVAILABLE"
    assert caught.value.safe_message=="The result store is unavailable."
    assert probe.rolled_back and not probe.committed and probe.committed_events==[]


def test_deprecation_warning_and_cursor_overhead_are_included_in_page_byte_limit(clock,north_principal):
    import asyncio
    from gateway.registry.models import ResultCursor
    probe=TransactionProbe(clock)
    limits=probe.context.decision.limits.model_dump(mode="python");limits["page_size"]=2
    current=probe.context.decision.model_copy(update={"visible_fields":("day","segment"),"limits":Limits(**limits)})
    complete=api().project_stored_result(probe.execution,probe.envelope,probe.product,probe.context.binding,
        probe.context.executed_decision,current,now=clock.now())
    warned=complete.model_dump(mode="python");warned["warnings"]=["This product version is deprecated."]
    limits["max_bytes"]=len(canonical_bytes(warned))
    probe.context=replace(probe.context,decision=current.model_copy(update={"limits":Limits(**limits)}))
    probe.lifecycle.status="deprecated"
    with pytest.raises(GatewayError) as caught:
        asyncio.run(api().ResultService(probe,probe.policy,probe.bindings,clock).get(north_principal,"execution-1"))
    assert caught.value.code=="RESOURCE_LIMIT"
    assert any(isinstance(value,ResultCursor) for value in probe.added)
    assert probe.rolled_back and probe.committed_cursors==[] and probe.committed_events==[]


@pytest.mark.parametrize("blocked", ["stale","rows","bytes","dates"])
def test_valid_cursor_still_enforces_complete_freshness_and_limits(blocked,clock,north_principal):
    import asyncio
    from gateway.execution.cursors import cursor_digest
    from gateway.registry.models import ResultCursor
    probe=TransactionProbe(clock)
    token="a"*43
    probe.committed_cursors.append(ResultCursor(token_digest=cursor_digest(token),result_id=probe.result.id,
        principal_id=probe.result.principal_id,version_id=probe.result.version_id,policy_revision=1,position=1,expires_at=probe.result.expires_at))
    if blocked=="stale": clock.instant=probe.envelope.source_watermark+timedelta(seconds=86400,microseconds=1)
    else:
        limits=probe.context.decision.limits.model_dump(mode="python")
        if blocked=="rows": limits.update(max_rows=2,page_size=2)
        elif blocked=="bytes": limits["max_bytes"]=len(canonical_bytes(probe.envelope))-1
        else: limits["max_date_range_days"]=2
        probe.context=replace(probe.context,decision=probe.context.decision.model_copy(update={"limits":Limits(**limits)}))
    with pytest.raises(GatewayError) as caught:
        asyncio.run(api().ResultService(probe,probe.policy,probe.bindings,clock).get(north_principal,"execution-1",token))
    assert caught.value.code==("STALE_DATA" if blocked=="stale" else "RESOURCE_LIMIT")
    assert probe.rolled_back and probe.committed_events==[]


@pytest.mark.parametrize("blocked,code", [("scope","BINDING_DRIFT"),("identity","NOT_FOUND_OR_FORBIDDEN"),
    ("withdrawn","NOT_FOUND_OR_FORBIDDEN"),("result_expired","NOT_FOUND_OR_FORBIDDEN"),("timestamp","BINDING_DRIFT")])
def test_current_authority_identity_and_expiry_precede_cursor_resolution(blocked,code,clock,north_principal):
    import asyncio
    probe=TransactionProbe(clock)
    if blocked=="scope": probe.authorization.scope_digest="0"*64
    elif blocked=="identity": probe.authorization.principal_snapshot["subject"]="changed-identity"
    elif blocked=="withdrawn": probe.lifecycle.status="withdrawn"
    elif blocked=="result_expired": probe.result.expires_at=clock.now()
    else: probe.state.started_at=clock.now()-timedelta(seconds=1)
    with pytest.raises(GatewayError) as caught:
        asyncio.run(api().ResultService(probe,probe.policy,probe.bindings,clock).get(north_principal,"execution-1","a"*43))
    assert caught.value.code==code
