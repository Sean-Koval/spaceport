"""Actual adapter path against Task 5's explicitly authorized private pinned stack.

No mocked upstream responses, external accounts or arbitrary endpoint inputs.
The imported fixture verifies synthetic runtime/container/network identity first.
"""
import asyncio
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path

import pytest

from test_cube_compatibility import cube_stack
from gateway.adapters.base import CubeBinding, StaticBindingResolver
from gateway.adapters.bundles import BundleCatalog
from gateway.adapters.cube import CubeAdapter
from gateway.adapters.normalize import NormalizationContext, normalize
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.clock import SystemClock
from gateway.contracts.execution import Completed, ExecutionSpec, ExecutionView, Pending, PinnedProduct
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import Manifest
from gateway.contracts.query import QueryRequest, validate_query

pytestmark=pytest.mark.cube
ROOT=Path(__file__).resolve().parents[2]


def configured(stack, *, context="consumer",tenant="north",segment=None):
    records=json.loads((ROOT/"cube/bundles.json").read_text())
    # Only addresses from the existing fixture's verified private runtime are used.
    if stack.host_mode:
        for record in records.values():
            service="cube-test" if record["context"]=="fixture_test" else "cube"
            record["url"]=f"http://{stack.hosts[service]}:4000"
    catalog=BundleCatalog(records)
    raw=json.loads((ROOT/"fixtures/bindings/revenue.json").read_text())
    raw["context"]=context
    raw["model_bundle_digest"]=records[context]["bundle_digest"]
    binding=CubeBinding.model_validate(raw)
    mraw=json.loads((ROOT/"fixtures/products/revenue.json").read_text())
    mraw["model_bundle_digest"]=binding.model_bundle_digest
    product=PinnedProduct(manifest=Manifest.model_validate(mraw),binding_digest=binding.digest,version_id="runtime-revenue-1")
    constraints=[{"member":"Orders.tenant_id","operator":"eq","values":[tenant]},
        {"member":"Orders.access_group","operator":"in","values":[tenant]}]
    if segment: constraints.append({"member":"Orders.segment","operator":"eq","values":[segment]})
    decision=Decision(principal_id=f"demo-{tenant}",policy_revision=1,tenant_id=tenant,
        row_constraints=constraints,visible_fields=["day","segment","revenue"],limits=product.manifest.limits)
    clock=SystemClock()
    client=CubeAdapter(catalog=catalog,bindings=StaticBindingResolver([binding]),clock=clock,
        secret_resolver=lambda ref:(stack.secrets/Path(ref).name).read_text().strip())
    return client,product,binding,decision,clock


async def execute(stack, *, context="consumer",tenant="north",segment=None,inputs=None):
    client,product,binding,decision,clock=configured(stack,context=context,tenant=tenant,segment=segment)
    request=validate_query(product.manifest,QueryRequest(product_id="revenue",version=product.manifest.version,
        inputs=inputs or {"start":"2024-03-09","end":"2024-03-12"}))
    canonical=request.model_dump(mode="json",exclude={"idempotency_key"})
    spec=ExecutionSpec(principal_id=decision.principal_id,pinned_product=product,canonical_query=canonical,
        canonical_hash=canonical_digest(canonical),policy_revision=decision.policy_revision,
        context=context,request_id="adapter-runtime")
    started=clock.now();deadline=started+timedelta(seconds=120);pending_count=0
    async with client:
        prepared=await client.prepare(product,request,decision,context=context,deadline=deadline,request_id=spec.request_id)
        while True:
            polled=await client.poll(prepared,decision,context=spec.context,deadline=deadline,request_id=spec.request_id)
            if isinstance(polled,Completed):break
            assert isinstance(polled,Pending)
            pending_count+=1
            await clock.sleep(polled.retry_after_seconds)
    # Exact v1.8.5 transformer adds a matching deprecated base-time alias.
    # This assertion is genuine compatibility evidence only when this stack runs.
    assert all(row.get("Orders.ordered_at")==row["Orders.ordered_at.day"] for row in polled.raw_rows)
    view=ExecutionView(execution_id="adapter-runtime-1",state="succeeded",accepted_at=started,started_at=started,completed_at=clock.now())
    result=normalize(product,polled,view,context=NormalizationContext(spec=spec,decision=decision,executed_decision=decision,binding=binding,clock=clock))
    stack.capture(f"adapter-{context}-{tenant}-{segment or 'all'}.json",{
        "context":context,"tenant":tenant,"pending_count":pending_count,"rows":result.model_dump(mode="json")["rows"],
        "row_count":result.row_count,"source_watermark":result.model_dump(mode="json")["source_watermark"],
        "freshness":result.freshness.status,"cache_status":result.cache_status,"truncated":result.truncated})
    return result


@pytest.mark.parametrize("context",["consumer","fixture_test"])
@pytest.mark.parametrize("tenant,total",[("north","9007199254741023.31"),("south","100.01")])
def test_actual_adapter_exact_grain_and_lossless_totals(cube_stack,context,tenant,total):
    result=asyncio.run(execute(cube_stack,context=context,tenant=tenant))
    assert sum((Decimal(row["revenue"]) for row in result.rows),Decimal(0))==Decimal(total)
    expected = {
        "north": (
            {"day":"2024-03-09","segment":"A","revenue":"10.10"},
            {"day":"2024-03-10","segment":None,"revenue":"20.20"},
            {"day":"2024-03-11","segment":"A","revenue":"9007199254740993.01"},
        ),
        "south": (
            {"day":"2024-03-09","segment":"A","revenue":"100.00"},
            {"day":"2024-03-10","segment":"B","revenue":"0.01"},
        ),
    }[tenant]
    # No sort was requested. Match the complete daily/segment grain regardless
    # of upstream order, checking cardinality before a keyed comparison.
    assert len(result.rows)==result.row_count==len(expected)
    assert {(row["day"],row["segment"]):dict(row) for row in result.rows} == {
        (row["day"],row["segment"]):row for row in expected}

    assert result.cache_status=="unknown" and result.truncated is False
    assert result.freshness.status=="fresh"


def test_actual_adapter_same_principal_entitlements_do_not_mix(cube_stack):
    async def scenario():
        for segment,expected in [(None,"9007199254741023.31"),("A","9007199254741003.11"),(None,"9007199254741023.31")]:
            result=await execute(cube_stack,segment=segment)
            assert sum((Decimal(row["revenue"]) for row in result.rows),Decimal(0))==Decimal(expected)
    asyncio.run(scenario())


def test_actual_adapter_empty_query_keeps_recorded_load_watermark(cube_stack):
    result=asyncio.run(execute(cube_stack,inputs={"start":"2025-01-01","end":"2025-01-02"}))
    assert result.rows==() and result.row_count==0
    assert result.source_watermark is not None and result.freshness.status=="fresh"


def test_actual_adapter_dst_day_filter_preserves_daily_grain(cube_stack):
    result=asyncio.run(execute(cube_stack,inputs={"start":"2024-03-10","end":"2024-03-11"}))
    assert result.rows==({"day":"2024-03-10","segment":None,"revenue":"20.20"},)
