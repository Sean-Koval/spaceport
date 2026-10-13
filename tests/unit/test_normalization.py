"""Deterministic lossless conversion tests, using explicitly synthetic Cube rows."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import json
from pathlib import Path

import pytest

from gateway.contracts.canonical import canonical_bytes, canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import Completed, ExecutionSpec, ExecutionView, PinnedProduct
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import FieldSchema, Manifest
from gateway.contracts.query import QueryRequest, validate_query

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def api():
    try:
        return importlib.import_module("gateway.adapters.normalize")
    except ModuleNotFoundError as error:
        pytest.fail(f"Normalization implementation is missing: {error.name}")


def setup(clock, *, manifest_raw=None, visible=None, limits=None):
    module=api()
    base=importlib.import_module("gateway.adapters.base")
    raw=json.loads((ROOT/"fixtures/bindings/revenue.json").read_text())
    binding=base.CubeBinding.model_validate(raw)
    m=Manifest.model_validate(manifest_raw or json.loads((ROOT/"fixtures/products/revenue.json").read_text()))
    product=PinnedProduct(manifest=m,binding_digest=canonical_digest(raw),version_id="revenue-1")
    decision=Decision(principal_id="north-user",policy_revision=1,tenant_id="north",row_constraints=[
        {"member":"Orders.tenant_id","operator":"eq","values":["north"]},
        {"member":"Orders.access_group","operator":"in","values":["north"]}],visible_fields=visible or ["day","segment","revenue"],limits=limits or m.limits)
    request=validate_query(m,QueryRequest(product_id="revenue",version=m.version,inputs={"start":"2024-03-09","end":"2024-03-12"}))
    canonical=request.model_dump(mode="json",exclude={"idempotency_key"})
    spec=ExecutionSpec(principal_id=decision.principal_id,pinned_product=product,canonical_query=canonical,canonical_hash=canonical_digest(canonical),policy_revision=1,context="consumer",request_id="request-1")
    context=module.NormalizationContext(decision=decision,executed_decision=decision,binding=binding,spec=spec,clock=clock)
    view=ExecutionView(execution_id="execution-1",state="succeeded",accepted_at=clock.now(),started_at=clock.now(),completed_at=clock.now())
    return module,product,context,view


def completed(rows=None,watermark="2024-03-12T00:00:00Z"):
    rows=rows if rows is not None else [{"Orders.ordered_at.day":"2024-03-09T00:00:00.000","Orders.segment":"A","Orders.revenue":"10.10"},
        {"Orders.ordered_at.day":"2024-03-10T00:00:00.000","Orders.segment":None,"Orders.revenue":"20.20"},
        {"Orders.ordered_at.day":"2024-03-11T00:00:00.000","Orders.segment":"A","Orders.revenue":"9007199254740993.01"}]
    return Completed(raw_rows=rows,source_watermark=watermark,cache_status="unknown")


def test_exact_grain_null_dimension_decimal_and_freshness(clock):
    module,product,context,view=setup(clock)
    result=module.normalize(product,completed(),view,context=context)
    assert result.rows == tuple(product.manifest.fixtures[0].expected_rows)
    assert sum((Decimal(row["revenue"]) for row in result.rows),Decimal(0)) == Decimal("9007199254741023.31")
    assert result.rows[2]["revenue"] == "9007199254740993.01"
    assert result.row_count == 3 and result.truncated is False
    assert result.freshness.status == "fresh" and result.freshness.age_seconds == 3600
    assert result.effective_range.start == "2024-03-09" and result.effective_range.end == "2024-03-12"
    assert result.cache_status == "unknown"
    assert result.provenance.links == ()


def test_empty_rows_keep_independent_watermark(clock):
    module,product,context,view=setup(clock)
    result=module.normalize(product,completed([]),view,context=context)
    assert result.rows == () and result.row_count == 0
    assert result.source_watermark == datetime(2024,3,12,tzinfo=timezone.utc)
    assert result.freshness.status == "fresh"


@pytest.mark.parametrize("watermark,code", [(None,"FRESHNESS_UNKNOWN"),("2024-03-10T00:00:00Z","STALE_DATA"),("2024-03-13T00:00:00Z","FRESHNESS_UNKNOWN")])
def test_required_freshness_fails_closed(watermark,code,clock):
    module,product,context,view=setup(clock)
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed(watermark=watermark),view,context=context)
    assert caught.value.code == code and caught.value.request_id == "request-1"


def test_optional_freshness_is_reported_with_warning(clock):
    raw=json.loads((ROOT/"fixtures/products/revenue.json").read_text())
    raw["freshness"]["required"]=False
    module,product,context,view=setup(clock,manifest_raw=raw)
    result=module.normalize(product,completed(watermark=None),view,context=context)
    assert result.freshness.status == "unknown" and result.warnings


@pytest.mark.parametrize("value", ["9007199254740993.01","0","1.2","-0.01","999999999999999999999999999999999999.99"])
def test_decimal_declared_scale_without_binary_float(value):
    result=api().normalize_value(FieldSchema(type="decimal",precision=38,scale=2),value)
    assert type(result) is str
    assert Decimal(result) == Decimal(value)
    assert len(result.split(".")[1]) == 2


@pytest.mark.parametrize("value", [9007199254740993.0,"1.001","1000000000000000000000000000000000000.00","NaN","1e2",True])
def test_invalid_decimal_never_rounds_or_coerces(value):
    with pytest.raises((GatewayError,ValueError)):
        api().normalize_value(FieldSchema(type="decimal",precision=38,scale=2),value)


def test_timestamp_is_normalized_to_utc():
    assert api().normalize_value(FieldSchema(type="timestamp"),"2024-11-03T01:30:00-04:00") == "2024-11-03T05:30:00Z"


@pytest.mark.parametrize("value", ["2024-03-09T13:00:00.000","not-a-date"])
def test_daily_grain_rejects_nonmidnight_or_invalid_bucket(value,clock):
    module,product,context,view=setup(clock)
    row={"Orders.ordered_at.day":value,"Orders.segment":None,"Orders.revenue":"1.00"}
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed([row]),view,context=context)
    assert caught.value.code == "BINDING_DRIFT"


def test_hidden_grain_field_fails_closed_without_reaggregation(clock):
    module,product,context,view=setup(clock,visible=["day","revenue"])
    rows=[dict(completed().raw_rows[0]),dict(completed().raw_rows[0])]
    rows[1]["Orders.segment"]="B";rows[1]["Orders.revenue"]="100.00"
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed(rows),view,context=context)
    assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"


def test_1001_rows_fails_instead_of_partial_success(clock):
    module,product,context,view=setup(clock)
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed([dict(completed().raw_rows[0])]*1001),view,context=context)
    assert caught.value.code == "RESOURCE_LIMIT"


def test_effective_decision_row_limit(clock):
    from gateway.contracts.manifest import Limits
    module,product,context,view=setup(clock,limits=Limits(max_rows=2,page_size=2))
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed(),view,context=context)
    assert caught.value.code == "RESOURCE_LIMIT"


def test_complete_envelope_size_not_only_rows_is_counted(clock):
    module,product,context,view=setup(clock)
    row=dict(completed().raw_rows[0]); row["Orders.segment"]="x"
    baseline=module.normalize(product,completed([row]),view,context=context)
    length=len(canonical_bytes(baseline))
    row["Orders.segment"]="x"*(1048576-length+2)
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed([row]),view,context=context)
    assert caught.value.code == "RESOURCE_LIMIT"


@pytest.mark.parametrize("change", ["principal","policy","binding","request","product"])
def test_context_inconsistency_cannot_release_result(change,clock):
    from dataclasses import replace
    module,product,context,view=setup(clock)
    if change=="principal": context=replace(context,decision=context.decision.model_copy(update={"principal_id":"other"}))
    elif change=="policy": context=replace(context,decision=context.decision.model_copy(update={"policy_revision":2}))
    elif change=="binding": product=product.model_copy(update={"binding_digest":"0"*64})
    elif change=="request": context=replace(context,spec=context.spec.model_copy(update={"canonical_hash":"0"*64}))
    else: product=product.model_copy(update={"version_id":"other-version"})
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed(),view,context=context)
    assert caught.value.code in {"BINDING_DRIFT","NOT_FOUND_OR_FORBIDDEN"}


def test_normalization_binding_context_matches_execution(clock):
    from dataclasses import replace
    module,product,context,view=setup(clock)
    context=replace(context,spec=context.spec.model_copy(update={"context":"fixture_test"}))
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed(),view,context=context)
    assert caught.value.code=="BINDING_DRIFT"


def test_source_bucket_outside_effective_range_is_rejected(clock):
    module,product,context,view=setup(clock)
    row=dict(completed().raw_rows[0]); row["Orders.ordered_at.day"]="2024-03-12T00:00:00.000"
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed([row]),view,context=context)
    assert caught.value.code=="BINDING_DRIFT"


def test_duplicate_reviewed_grain_is_rejected(clock):
    module,product,context,view=setup(clock)
    row=dict(completed().raw_rows[0])
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed([row,row]),view,context=context)
    assert caught.value.code=="BINDING_DRIFT"


def test_exact_maximum_envelope_bytes_succeeds(clock):
    module,product,context,view=setup(clock)
    row=dict(completed().raw_rows[0]);row["Orders.segment"]="x"
    baseline=module.normalize(product,completed([row]),view,context=context)
    row["Orders.segment"]="x"*(1048576-len(canonical_bytes(baseline))+1)
    result=module.normalize(product,completed([row]),view,context=context)
    assert len(canonical_bytes(result))==1048576


def test_1000_unique_grain_rows_succeed(clock):
    module,product,context,view=setup(clock)
    rows=[]
    for index in range(1000):
        row=dict(completed().raw_rows[0]);row["Orders.segment"]=f"segment-{index}"; rows.append(row)
    result=module.normalize(product,completed(rows),view,context=context)
    assert len(result.rows)==1000 and result.row_count==1000 and result.truncated is False


@pytest.mark.parametrize("change",["tenant","constraint","visibility"])
def test_same_revision_authorization_expansion_or_row_change_cannot_release(change,clock):
    from dataclasses import replace
    module,product,context,view=setup(clock)
    current=context.decision
    if change=="tenant":current=current.model_copy(update={"tenant_id":"south"})
    elif change=="constraint":
        constraints=list(current.row_constraints)
        constraints.append({"member":"Orders.segment","operator":"eq","values":["A"]})
        current=Decision.model_validate({**current.model_dump(mode="python"),"row_constraints":constraints})
    else:
        context=replace(context,executed_decision=current.model_copy(update={"visible_fields":("day","revenue")}))
    context=replace(context,decision=current)
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed(),view,context=context)
    assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"


def test_scope_digest_normalizes_constraint_and_value_order(clock):
    _,_,context,_=setup(clock)
    base=importlib.import_module("gateway.adapters.base")
    decision=context.decision
    raw=decision.model_dump(mode="python")
    raw["row_constraints"]=[{"member":"Orders.access_group","operator":"in","values":["north","group"]},raw["row_constraints"][0]]
    first=Decision.model_validate(raw)
    raw["row_constraints"]=[raw["row_constraints"][1],{"member":"Orders.access_group","operator":"in","values":["group","north"]}]
    second=Decision.model_validate(raw)
    assert base.authorization_scope_digest(first)==base.authorization_scope_digest(second)
    raw=decision.model_dump(mode="python");raw["row_constraints"][0]["operator"]="in"
    assert base.authorization_scope_digest(Decision.model_validate(raw))==base.authorization_scope_digest(decision)


def test_scope_digest_preserves_scalar_types(clock):
    _,_,context,_=setup(clock)
    base=importlib.import_module("gateway.adapters.base")
    def changed(value):
        raw=context.decision.model_dump(mode="python")
        raw["row_constraints"][1]["values"]=[value]
        return Decision.model_validate(raw)
    assert base.authorization_scope_digest(changed(True))!=base.authorization_scope_digest(changed(1))


def test_no_data_scope_cannot_normalize_a_completion(clock):
    from dataclasses import replace
    module,product,context,view=setup(clock)
    no_data=context.decision.model_copy(update={"row_constraints":context.decision.row_constraints[:1]})
    context=replace(context,decision=no_data,executed_decision=no_data)
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed(),view,context=context)
    assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"


def test_current_visibility_may_tighten_executed_visibility(clock):
    from dataclasses import replace
    module,product,context,view=setup(clock)
    context=replace(context,decision=context.decision.model_copy(update={"visible_fields":("day","segment")}))
    result=module.normalize(product,completed(),view,context=context)
    assert set(result.typed_schema)=={"day","segment"}
    assert all("revenue" not in row for row in result.rows)
    assert result.grain==product.manifest.grain
    assert result.rows==({"day":"2024-03-09","segment":"A"},{"day":"2024-03-10","segment":None},{"day":"2024-03-11","segment":"A"})


def test_duplicate_semantically_equal_bucket_forms_are_rejected(clock):
    module,product,context,view=setup(clock)
    first=dict(completed().raw_rows[0]);second=dict(first)
    second["Orders.ordered_at.day"]="2024-03-09"
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed([first,second]),view,context=context)
    assert caught.value.code=="BINDING_DRIFT"


@pytest.mark.parametrize("visible", [["segment","revenue"],["day","revenue"]])
def test_every_original_grouping_axis_must_remain_visible(visible,clock):
    module,product,context,view=setup(clock,visible=visible)
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed(),view,context=context)
    assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"


def test_pinned_cube_matching_base_time_alias_preserves_original_grain(clock):
    module,product,context,view=setup(clock)
    rows=[{**row,"Orders.ordered_at":row["Orders.ordered_at.day"]} for row in completed().raw_rows]
    result=module.normalize(product,completed(rows),view,context=context)
    assert result.rows==product.manifest.fixtures[0].expected_rows
    assert result.grain==product.manifest.grain
    assert sum((Decimal(row["revenue"]) for row in result.rows),Decimal(0))==Decimal("9007199254741023.31")
    assert all("Orders.ordered_at" not in row for row in result.rows)


@pytest.mark.parametrize("extra,value",[("Orders.ordered_at","2024-03-10T00:00:00.000"),
    ("Orders.ordered_at",None),("Orders.ordered_at.week","2024-03-09T00:00:00.000"),("Unexpected.member","canary")])
def test_conflicting_or_unreviewed_time_alias_remains_binding_drift(extra,value,clock):
    module,product,context,view=setup(clock)
    row={**completed().raw_rows[0],extra:value}
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed([row]),view,context=context)
    assert caught.value.code=="BINDING_DRIFT" and "canary" not in str(caught.value)


def test_hidden_measure_saved_sort_cannot_release_relative_values(clock):
    from dataclasses import replace
    raw=json.loads((ROOT/"fixtures/products/revenue.json").read_text())
    raw["sort"]={"fields":["day","segment","revenue"],"max_fields":3}
    module,product,context,view=setup(clock,manifest_raw=raw)
    request=validate_query(product.manifest,QueryRequest(product_id="revenue",version=product.manifest.version,
        inputs={"start":"2024-03-09","end":"2024-03-12"},sort=[{"field":"revenue","direction":"desc"}]))
    canonical=request.model_dump(mode="json",exclude={"idempotency_key"})
    spec=ExecutionSpec(**{**context.spec.model_dump(mode="python"),"canonical_query":canonical,"canonical_hash":canonical_digest(canonical)})
    context=replace(context,spec=spec,decision=context.decision.model_copy(update={"visible_fields":("day","segment")}))
    with pytest.raises(GatewayError) as caught:
        module.normalize(product,completed(list(reversed(completed().raw_rows))),view,context=context)
    assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN" and caught.value.request_id=="request-1"


@pytest.mark.parametrize("dependency",["sort","filter","mapped_input"])
def test_saved_query_visibility_rechecks_every_selection_dependency(dependency,clock):
    _,product,context,_=setup(clock)
    base=importlib.import_module("gateway.adapters.base")
    changes={"inputs":{"start":"2024-03-09","end":"2024-03-12"}}
    if dependency=="sort":changes["sort"]=[{"field":"segment","direction":"desc"}]
    elif dependency=="filter":changes["filters"]=[{"field":"segment","operator":"eq","value":"A"}]
    else:changes["inputs"]["segment"]="A"
    request=validate_query(product.manifest,QueryRequest(product_id="revenue",**changes))
    decision=context.decision.model_copy(update={"visible_fields":("day","revenue")})
    with pytest.raises(GatewayError) as caught:
        base.validate_query_visibility(context.binding,request,decision,"dependency-request")
    assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN" and caught.value.request_id=="dependency-request"
