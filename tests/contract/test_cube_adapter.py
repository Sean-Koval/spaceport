"""Mocked HTTP contract fixtures; these do not establish real Cube compatibility."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
from pydantic import ValidationError
import pytest

from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import Completed, Pending, PinnedProduct, PreparedQuery
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import Manifest
from gateway.contracts.query import QueryRequest

pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[2]


class FixtureStream(httpx.AsyncByteStream):
    def __init__(self, data): self.data=data
    async def __aiter__(self):
        for index in range(0,len(self.data),65536): yield self.data[index:index+65536]


def upstream_response(status, *, json=None, content=b"", headers=None, stream=None):
    # Fixtures supply wire bytes so production never needs a pre-decoded test branch.
    if json is not None:
        import json as json_module
        content=json_module.dumps(json).encode()
    return httpx.Response(status,stream=stream or FixtureStream(content),headers=headers or {})


def api():
    try:
        return SimpleNamespace(**{name: importlib.import_module(f"gateway.adapters.{name}") for name in ("base", "bundles", "cube")})
    except ModuleNotFoundError as error:
        pytest.fail(f"Adapter implementation is missing: {error.name}")


def synthetic():
    raw = json.loads((ROOT / "fixtures/bindings/revenue.json").read_text())
    binding = api().base.CubeBinding.model_validate(raw)
    manifest = Manifest.model_validate(json.loads((ROOT / "fixtures/products/revenue.json").read_text()))
    product = PinnedProduct(manifest=manifest, binding_digest=canonical_digest(raw), version_id="revenue-1")
    decision = Decision(principal_id="north-user", policy_revision=1, tenant_id="north", row_constraints=[
        {"member":"Orders.tenant_id", "operator":"eq", "values":["north"]},
        {"member":"Orders.access_group", "operator":"in", "values":["north"]}],
        visible_fields=["day", "segment", "revenue"], limits=manifest.limits)
    request = QueryRequest(product_id="revenue", version="1.0.0", inputs={"start":"2024-03-09", "end":"2024-03-12"})
    return binding, product, decision, request


def meta():
    return {"cubes":[{"name":"Orders", "measures":[{"name":"Orders.revenue","type":"number"}],
        "dimensions":[{"name":"Orders.ordered_at","type":"time"},{"name":"Orders.segment","type":"string"},
            {"name":"Orders.tenant_id","type":"string"},{"name":"Orders.access_group","type":"string"}]},
        {"name":"Watermark","measures":[],"dimensions":[{"name":"Watermark.loaded_at","type":"time"}]}]}


def adapter(handler, clock, *, binding=None, catalog=None, **kwargs):
    binding = binding or synthetic()[0]
    catalog = catalog or api().bundles.BundleCatalog.from_file(ROOT / "cube/bundles.json")
    return api().cube.CubeAdapter(catalog=catalog, bindings=api().base.StaticBindingResolver([binding]),
        secret_resolver=lambda reference: "a" * 43, clock=clock,
        transport=httpx.MockTransport(handler), **kwargs)


def run(coroutine):
    return asyncio.run(coroutine)


def test_catalog_selects_only_exact_retained_bundle():
    catalog = api().bundles.BundleCatalog.from_file(ROOT / "cube/bundles.json")
    digest = synthetic()[1].manifest.model_bundle_digest
    endpoint = catalog.resolve(digest)
    assert endpoint.endpoint_id == "synthetic-consumer"
    assert endpoint.context == "consumer"
    with pytest.raises(GatewayError) as error:
        catalog.resolve("0" * 64)
    assert error.value.code == "BINDING_DRIFT"


@pytest.mark.parametrize("url", ["https://user:pass@example.com", "file:///etc/passwd", "http://cube:4000/?token=x", "http://cube:4000/redirect"])
def test_catalog_rejects_unsafe_operator_endpoint(url):
    raw = json.loads((ROOT / "cube/bundles.json").read_text())
    raw["consumer"]["url"] = url
    with pytest.raises((GatewayError, ValidationError, ValueError)):
        api().bundles.BundleCatalog(raw)


def test_catalog_rejects_duplicate_bundle_and_unknown_fields():
    raw = json.loads((ROOT / "cube/bundles.json").read_text())
    raw["fixture_test"]["bundle_digest"] = raw["consumer"]["bundle_digest"]
    with pytest.raises((GatewayError, ValueError)):
        api().bundles.BundleCatalog(raw)
    raw = json.loads((ROOT / "cube/bundles.json").read_text())
    raw["consumer"]["sql"] = "select 1"
    with pytest.raises((GatewayError, ValueError)):
        api().bundles.BundleCatalog(raw)


def test_prepare_preserves_mandatory_filters_and_overflow_sentinel(clock):
    binding, product, decision, request = synthetic()
    request = request.model_copy(update={"filters": ()})
    seen = []
    def handler(req):
        seen.append(req)
        return upstream_response(200, json=meta())
    async def scenario():
        async with adapter(handler, clock) as client:
            prepared = await client.prepare(product, request, decision)
        query = prepared.model_dump(mode="json")["cube_query"]
        assert query["limit"] == 1001
        assert query["measures"] == ["Orders.revenue"]
        assert query["timeDimensions"] == [{"dimension":"Orders.ordered_at", "granularity":"day"}]
        assert query["timezone"] == "America/New_York"
        filters = query["filters"][0]["and"]
        assert {"member":"Orders.tenant_id","operator":"equals","values":["north"]} in filters
        assert {"member":"Orders.access_group","operator":"equals","values":["north"]} in filters
        assert {"member":"Orders.ordered_at","operator":"afterOrOnDate","values":["2024-03-09T00:00:00"]} in filters
        assert {"member":"Orders.ordered_at","operator":"beforeDate","values":["2024-03-12T00:00:00"]} in filters
        token = seen[0].headers["authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, "a" * 43, algorithms=["HS256"], audience="dpg-cube", issuer="dpg-gateway", options={"verify_exp":False})
        assert set(claims) == {"iss","aud","iat","exp","principal_id","tenant_id","policy_revision","model_bundle_digest","context","row_constraints"}
        assert claims["exp"] - claims["iat"] <= 60
        assert claims["row_constraints"] == decision.model_dump(mode="json")["row_constraints"]
    run(scenario())


@pytest.mark.parametrize("start,end,hours", [("2024-03-10","2024-03-11",23),("2024-11-03","2024-11-04",25)])
def test_dst_range_uses_local_midnights(start,end,hours,clock):
    from zoneinfo import ZoneInfo
    binding, product, decision, request = synthetic()
    request = QueryRequest(product_id="revenue", inputs={"start":start,"end":end})
    async def scenario():
        async with adapter(lambda req: upstream_response(200,json=meta()), clock) as client:
            result = await client.prepare(product, request, decision)
        filters = result.model_dump(mode="json")["cube_query"]["filters"][0]["and"]
        bounds = [f["values"][0] for f in filters if f["member"] == "Orders.ordered_at"]
        actual = [datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(product.manifest.timezone)).astimezone(timezone.utc) for value in bounds]
        assert (actual[1]-actual[0]).total_seconds() == hours*3600
    run(scenario())


@pytest.mark.parametrize("days,valid", [(366,True),(367,False)])
def test_prepare_enforces_calendar_range(days,valid,clock):
    binding,product,decision,_ = synthetic()
    end = (datetime(2024,1,1)+timedelta(days=days)).date().isoformat()
    request = QueryRequest(product_id="revenue",inputs={"start":"2024-01-01","end":end})
    async def scenario():
        async with adapter(lambda req: upstream_response(200,json=meta()),clock) as client:
            if valid:
                assert (await client.prepare(product,request,decision)).cube_query["limit"] == 1001
            else:
                with pytest.raises(GatewayError) as caught:
                    await client.prepare(product,request,decision)
                assert caught.value.code == "INVALID_INPUT"
    run(scenario())


def test_changed_binding_digest_fails_before_network(clock):
    _,product,decision,request = synthetic()
    product = product.model_copy(update={"binding_digest":"0"*64})
    async def scenario():
        async with adapter(lambda req: pytest.fail("Drift must fail before network"),clock) as client:
            with pytest.raises(GatewayError) as caught:
                await client.prepare(product,request,decision)
            assert caught.value.code == "BINDING_DRIFT"
    run(scenario())


@pytest.mark.parametrize("mutation", ["missing","type"])
def test_meta_member_drift_fails_safely(mutation,clock):
    _,product,decision,_ = synthetic()
    recorded=meta()
    recorded["cubes"][0]["measures"] = [] if mutation == "missing" else [{"name":"Orders.revenue","type":"string"}]
    async def scenario():
        async with adapter(lambda req: upstream_response(200,json=recorded),clock) as client:
            with pytest.raises(GatewayError) as caught:
                await client.validate_binding(product.manifest,decision=decision)
            assert caught.value.code == "BINDING_DRIFT"
    run(scenario())


def test_poll_pending_reuses_canonical_query_and_current_scope(clock):
    _,product,decision,request = synthetic()
    queries=[]
    def handler(req):
        if req.url.path.endswith("meta"): return upstream_response(200,json=meta())
        body=json.loads(req.content)
        queries.append(body)
        assert body["cache"] == "must-revalidate"
        return upstream_response(200,json={"error":"Continue wait"})
    async def scenario():
        async with adapter(handler,clock) as client:
            prepared=await client.prepare(product,request,decision)
            for _ in range(2):
                pending=await client.poll(prepared,decision,deadline=clock.now()+timedelta(seconds=120),request_id="request-1")
                assert isinstance(pending,Pending)
                assert 0 < pending.retry_after_seconds <= 30
        assert queries[0] == queries[1]
        assert queries[0]["query"] == prepared.model_dump(mode="json")["cube_query"]
    run(scenario())


def test_completed_empty_rows_fetches_independent_source_watermark(clock):
    _,product,decision,request=synthetic()
    def handler(req):
        if req.url.path.endswith("meta"): return upstream_response(200,json=meta())
        query=json.loads(req.content)["query"]
        rows=[{"Watermark.loaded_at":"2024-03-12T00:00:00.000"}] if query.get("dimensions") == ["Watermark.loaded_at"] else []
        return upstream_response(200,json={"data":rows,"annotation":{}})
    async def scenario():
        async with adapter(handler,clock) as client:
            prepared=await client.prepare(product,request,decision)
            completed=await client.poll(prepared,decision,deadline=clock.now()+timedelta(seconds=120),request_id="request-1")
        assert isinstance(completed,Completed)
        assert completed.raw_rows == ()
        assert completed.source_watermark == datetime(2024,3,12,tzinfo=timezone.utc)
        assert completed.cache_status == "unknown"
    run(scenario())


@pytest.mark.parametrize("status,body", [(500,{"error":"sql secret://key token canary"}),(403,{"error":"context canary"}),(200,{"error":"sql canary"}),(302,{"data":[]})])
def test_upstream_errors_never_echo_diagnostics(status,body,clock):
    _,product,decision,request=synthetic()
    async def scenario():
        async with adapter(lambda req: upstream_response(200,json=meta()) if req.url.path.endswith("meta") else upstream_response(status,json=body,headers={"location":"https://example.com"}),clock) as client:
            prepared=await client.prepare(product,request,decision)
            with pytest.raises(GatewayError) as caught:
                await client.poll(prepared,decision,deadline=clock.now()+timedelta(seconds=120),request_id="request-1")
            assert caught.value.code in {"UPSTREAM_UNAVAILABLE","BINDING_DRIFT"}
            assert caught.value.request_id == "request-1"
            assert "canary" not in str(caught.value) and "secret" not in str(caught.value)
    run(scenario())


def test_oversized_decoded_response_fails_before_json(clock):
    import gzip
    _,product,decision,request=synthetic()
    def handler(req):
        if req.url.path.endswith("meta"): return upstream_response(200,json=meta())
        return upstream_response(200,content=gzip.compress(b" " * 2049),headers={"content-encoding":"gzip"})
    async def scenario():
        async with adapter(handler,clock,max_response_bytes=2048) as client:
            prepared=await client.prepare(product,request,decision)
            with pytest.raises(GatewayError) as caught:
                await client.poll(prepared,decision,deadline=clock.now()+timedelta(seconds=120),request_id="request-1")
            assert caught.value.code == "RESOURCE_LIMIT"
    run(scenario())


def test_overall_deadline_is_not_renewed(clock):
    _,product,decision,request=synthetic()
    async def scenario():
        async with adapter(lambda req: upstream_response(200,json=meta()),clock) as client:
            prepared=await client.prepare(product,request,decision)
            with pytest.raises(GatewayError) as caught:
                await client.poll(prepared,decision,deadline=clock.now(),request_id="request-1")
            assert caught.value.code == "EXECUTION_TIMEOUT"
    run(scenario())


def test_no_data_author_only_gets_explicit_static_validation(clock):
    _,product,decision,_=synthetic()
    decision=decision.model_copy(update={"row_constraints":decision.row_constraints[:1]})
    async def scenario():
        async with adapter(lambda req: pytest.fail("No-data scope cannot request Cube metadata"),clock) as client:
            await client.validate_binding_definition(product.manifest)
            with pytest.raises(GatewayError) as caught:
                await client.validate_binding(product.manifest,decision=decision)
            assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"
    run(scenario())


def test_operator_can_retain_older_consumer_bundle_without_replacement():
    raw=json.loads((ROOT/"cube/bundles.json").read_text())
    old=deepcopy(raw["consumer"]); old.update(endpoint_id="retained-consumer",bundle_digest="b"*64,url="http://retained-cube:4000")
    raw["retained-consumer"]=old
    catalog=api().bundles.BundleCatalog(raw)
    assert catalog.resolve("b"*64).endpoint_id=="retained-consumer"
    assert catalog.resolve(raw["consumer"]["bundle_digest"]).endpoint_id=="synthetic-consumer"


class DripStream(httpx.AsyncByteStream):
    def __init__(self,clock): self.clock,self.closed=clock,False
    async def __aiter__(self):
        for _ in range(4):
            await self.clock.sleep(0.04)
            await asyncio.sleep(0.04)
            yield b" "
    async def aclose(self): self.closed=True


def test_slow_drip_total_transport_deadline_closes_stream(clock):
    _,product,decision,request=synthetic(); stream=DripStream(clock)
    def handler(req):
        return upstream_response(200,json=meta()) if req.url.path.endswith("meta") else upstream_response(200,stream=stream)
    async def scenario():
        async with adapter(handler,clock,transport_deadline_seconds=0.1) as client:
            prepared=await client.prepare(product,request,decision)
            with pytest.raises(GatewayError) as caught:
                await client.poll(prepared,decision,deadline=clock.now()+timedelta(seconds=120),request_id="request-1")
            assert caught.value.code=="EXECUTION_TIMEOUT"
            assert stream.closed
    run(scenario())


def test_chunked_compression_expansion_is_bounded(clock):
    import gzip
    _,product,decision,request=synthetic()
    class CompressedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            compressed=gzip.compress(b" "*1000000)
            for index in range(0,len(compressed),64): yield compressed[index:index+64]
    def handler(req):
        return upstream_response(200,json=meta()) if req.url.path.endswith("meta") else upstream_response(200,stream=CompressedStream(),headers={"content-encoding":"gzip"})
    async def scenario():
        async with adapter(handler,clock,max_response_bytes=2048) as client:
            prepared=await client.prepare(product,request,decision)
            with pytest.raises(GatewayError) as caught:
                await client.poll(prepared,decision,deadline=clock.now()+timedelta(seconds=120),request_id="request-1")
            assert caught.value.code=="RESOURCE_LIMIT"
    run(scenario())


def test_measure_overflow_is_detected_before_watermark_fetch(clock):
    _,product,decision,request=synthetic()
    def handler(req):
        if req.url.path.endswith("meta"): return upstream_response(200,json=meta())
        assert json.loads(req.content)["query"].get("dimensions") != ["Watermark.loaded_at"]
        return upstream_response(200,json={"data":[{"Orders.revenue":"1.00"}]*1001})
    async def scenario():
        async with adapter(handler,clock) as client:
            prepared=await client.prepare(product,request,decision)
            with pytest.raises(GatewayError) as caught:
                await client.poll(prepared,decision,deadline=clock.now()+timedelta(seconds=120),request_id="request-1")
            assert caught.value.code=="RESOURCE_LIMIT"
    run(scenario())


def test_day_equality_targets_complete_local_bucket(clock):
    _,product,decision,_=synthetic()
    request=QueryRequest(product_id="revenue",inputs={"start":"2024-03-09","end":"2024-03-12"},filters=[{"field":"day","operator":"eq","value":"2024-03-10"}])
    async def scenario():
        async with adapter(lambda req:upstream_response(200,json=meta()),clock) as client:
            result=await client.prepare(product,request,decision)
        filters=result.model_dump(mode="json")["cube_query"]["filters"][0]["and"]
        assert {"and":[{"member":"Orders.ordered_at","operator":"afterOrOnDate","values":["2024-03-10T00:00:00"]},
            {"member":"Orders.ordered_at","operator":"beforeDate","values":["2024-03-11T00:00:00"]}]} in filters
    run(scenario())


def test_fixture_endpoint_requires_explicit_internal_context(clock):
    raw=json.loads((ROOT/"fixtures/bindings/revenue.json").read_text())
    raw["context"]="fixture_test"
    raw["model_bundle_digest"]=json.loads((ROOT/"cube/bundles.json").read_text())["fixture_test"]["bundle_digest"]
    binding=api().base.CubeBinding.model_validate(raw)
    _,product,decision,request=synthetic()
    mraw=product.manifest.model_dump(mode="json"); mraw["content_digest"]=None; mraw["model_bundle_digest"]=raw["model_bundle_digest"]
    product=PinnedProduct(manifest=Manifest.model_validate(mraw),binding_digest=binding.digest,version_id="fixture-1")
    async def scenario():
        async with adapter(lambda req:upstream_response(200,json=meta()),clock,binding=binding) as client:
            with pytest.raises(GatewayError) as caught:
                await client.prepare(product,request,decision)
            assert caught.value.code=="BINDING_DRIFT"
            prepared=await client.prepare(product,request,decision,context="fixture_test")
            assert prepared.endpoint_id=="synthetic-fixture"
            with pytest.raises(GatewayError) as caught:
                await client.poll(prepared,decision,deadline=clock.now()+timedelta(seconds=120),request_id="request-1")
            assert caught.value.code=="BINDING_DRIFT"
    run(scenario())


def test_source_caps_never_claim_unproven_source_bounds():
    caps=api().base.SourceCapabilities()
    assert caps.proven_statement_timeout_seconds is None and caps.statement_timeout_evidence is None
    assert caps.proven_upstream_lifetime_seconds is None and caps.upstream_lifetime_evidence is None
    assert caps.source_cost_bound=="unproven"
    with pytest.raises(ValidationError):
        api().base.SourceCapabilities(proven_statement_timeout_seconds=25.0)


def test_real_loopback_http_total_timeout_releases_connection(clock):
    _,product,decision,request=synthetic()
    async def scenario():
        closed=asyncio.Event()
        async def serve(reader,writer):
            try:
                header=await reader.readuntil(b"\r\n\r\n")
                if b"/meta " in header:
                    body=json.dumps(meta()).encode()
                    writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "+str(len(body)).encode()+b"\r\n\r\n"+body)
                    await writer.drain()
                else:
                    length=int(re.search(rb"content-length: (\d+)",header.lower()).group(1))
                    await reader.readexactly(length)
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n ")
                    await writer.drain()
                    assert await reader.read()==b""
                    closed.set()
            finally:
                writer.close();await writer.wait_closed()
        import re
        server=await asyncio.start_server(serve,"127.0.0.1",0)
        try:
            raw=json.loads((ROOT/"cube/bundles.json").read_text())
            raw["consumer"]["url"]=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            catalog=api().bundles.BundleCatalog(raw)
            async with api().cube.CubeAdapter(catalog=catalog,bindings=api().base.StaticBindingResolver([synthetic()[0]]),
                secret_resolver=lambda reference:"a"*43,clock=clock,transport_deadline_seconds=0.1) as client:
                prepared=await client.prepare(product,request,decision)
                with pytest.raises(GatewayError) as caught:
                    await client.poll(prepared,decision,deadline=clock.now()+timedelta(seconds=120),request_id="request-1")
                assert caught.value.code=="EXECUTION_TIMEOUT"
                await asyncio.wait_for(closed.wait(),1)
        finally:
            server.close();await server.wait_closed()
    run(scenario())


def test_preparation_and_live_validation_use_existing_execution_deadline(clock):
    _,product,decision,request=synthetic()
    async def scenario():
        async with adapter(lambda req:pytest.fail("Expired budget must not start HTTP"),clock) as client:
            for action in [client.prepare(product,request,decision,deadline=clock.now(),request_id="expired-1"),
                client.validate_binding(product.manifest,decision=decision,deadline=clock.now(),request_id="expired-1")]:
                with pytest.raises(GatewayError) as caught:
                    await action
                assert caught.value.code=="EXECUTION_TIMEOUT" and caught.value.request_id=="expired-1"
    run(scenario())


def test_compressed_transport_allocates_only_remaining_cap_plus_one(clock,monkeypatch):
    import gzip
    import zlib
    real_factory=zlib.decompressobj; allocations=[]
    class ObservedDecoder:
        def __init__(self,*args): self.decoder=real_factory(*args)
        def decompress(self,chunk,max_length=0):
            value=self.decoder.decompress(chunk,max_length)
            allocations.append((max_length,len(value)))
            return value
        def __getattr__(self,name):return getattr(self.decoder,name)
    compressed=gzip.compress(b" "*1000000)
    monkeypatch.setattr(api().cube.zlib,"decompressobj",ObservedDecoder)
    class CompressedStream(httpx.AsyncByteStream):
        async def __aiter__(self):yield compressed
    _,product,decision,request=synthetic()
    def handler(req):
        return upstream_response(200,json=meta()) if req.url.path.endswith("meta") else upstream_response(200,stream=CompressedStream(),headers={"content-encoding":"gzip"})
    async def scenario():
        async with adapter(handler,clock,max_response_bytes=2048) as client:
            prepared=await client.prepare(product,request,decision)
            with pytest.raises(GatewayError) as caught:
                await client.poll(prepared,decision,deadline=clock.now()+timedelta(seconds=120),request_id="request-1")
            assert caught.value.code=="RESOURCE_LIMIT"
    run(scenario())
    assert allocations==[(2049,2049)]


def test_statement_timeout_evidence_does_not_prove_queued_upstream_lifetime():
    caps=api().base.SourceCapabilities(proven_statement_timeout_seconds=25.0,
        statement_timeout_evidence="synthetic-active-statement-evidence")
    assert caps.proven_statement_timeout_seconds==25.0
    assert caps.proven_upstream_lifetime_seconds is None
    assert caps.upstream_lifetime_evidence is None
    with pytest.raises(ValidationError):
        api().base.SourceCapabilities(proven_upstream_lifetime_seconds=120.0)


@pytest.mark.parametrize("visible", [["segment","revenue"],["day","revenue"]])
def test_prepare_rejects_hidden_grouping_axis_before_http(visible,clock):
    _,product,decision,request=synthetic()
    decision=decision.model_copy(update={"visible_fields":tuple(visible)})
    async def scenario():
        async with adapter(lambda req:pytest.fail("Hidden grain must fail before HTTP"),clock) as client:
            with pytest.raises(GatewayError) as caught:
                await client.prepare(product,request,decision)
            assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"
    run(scenario())


class TrackedStream(FixtureStream):
    def __init__(self,data):super().__init__(data);self.closed=False
    async def aclose(self):self.closed=True


@pytest.mark.parametrize("malformed", [{"cubes":["canary"]},{"cubes":[None]},
    {"cubes":[{"name":"Orders","measures":None,"dimensions":[]}]},
    {"cubes":[{"name":"Orders","measures":{},"dimensions":[]}]},
    {"cubes":[{"name":"Orders","measures":[None],"dimensions":[]}]},
    {"cubes":[{"name":"Orders","measures":["canary"],"dimensions":[]}]},
    {"cubes":[{"name":"Orders","measures":[{"name":[],"type":"number"}],"dimensions":[]}]},
    {"cubes":[{"name":"Orders","measures":[{"name":"Orders.revenue","type":None}],"dimensions":[]}]},
    {"cubes":[{"name":None,"measures":[],"dimensions":[]}]},
    {"cubes":[{"name":"Orders","measures":[],"dimensions":["canary"]}]}])
@pytest.mark.parametrize("path",["direct","poll"])
def test_malformed_metadata_shape_has_safe_code_and_closes_stream(malformed,path,clock):
    _,product,decision,request=synthetic();streams=[];bad_active=False
    def handler(req):
        if not bad_active:return upstream_response(200,json=meta())
        stream=TrackedStream(json.dumps(malformed).encode());streams.append(stream)
        return upstream_response(200,stream=stream)
    async def scenario():
        nonlocal bad_active
        async with adapter(handler,clock) as client:
            if path=="poll":prepared=await client.prepare(product,request,decision)
            bad_active=True
            with pytest.raises(GatewayError) as caught:
                if path=="direct":
                    await client.validate_binding(product.manifest,decision=decision,request_id="malformed-request")
                else:
                    await client.poll(prepared,decision,deadline=clock.now()+timedelta(seconds=120),request_id="malformed-request")
            assert caught.value.code=="BINDING_DRIFT" and caught.value.request_id=="malformed-request"
            assert "canary" not in str(caught.value)
        assert streams and all(stream.closed for stream in streams)
    run(scenario())


@pytest.mark.parametrize("path",["direct","poll-meta","poll-load","poll-watermark"])
def test_deep_bounded_json_is_safe_at_every_response_boundary(path,clock):
    _,product,decision,request=synthetic();streams=[];bad_active=False
    deep=b'{"cubes":'+b'['*10000+b'0'+b']'*10000+b'}'
    def handler(req):
        if not bad_active:return upstream_response(200,json=meta())
        is_meta=req.url.path.endswith("meta")
        is_watermark=not is_meta and json.loads(req.content)["query"].get("dimensions")==["Watermark.loaded_at"]
        if (path in {"direct","poll-meta"} and is_meta or path=="poll-load" and not is_meta or path=="poll-watermark" and is_watermark):
            stream=TrackedStream(deep);streams.append(stream);return upstream_response(200,stream=stream)
        return upstream_response(200,json=meta() if is_meta else {"data":[]})
    async def scenario():
        nonlocal bad_active
        async with adapter(handler,clock) as client:
            if path!="direct":prepared=await client.prepare(product,request,decision)
            bad_active=True
            with pytest.raises(GatewayError) as caught:
                if path=="direct":
                    await client.validate_binding(product.manifest,decision=decision,request_id="nested-request")
                else:
                    await client.poll(prepared,decision,deadline=clock.now()+timedelta(seconds=120),request_id="nested-request")
            assert caught.value.code=="UPSTREAM_UNAVAILABLE" and caught.value.request_id=="nested-request"
            assert "cubes" not in str(caught.value)
        assert streams and all(stream.closed for stream in streams)
    run(scenario())


@pytest.mark.parametrize("body",[b'{"cubes":'+b'['*10000+b'0'+b']'*10000+b'}',
    b'{"cubes":['+b'0,'*60000+b'0]}'], ids=["nested","flat"])
def test_response_structure_bounds_run_before_json_parser(body,clock,monkeypatch):
    binding,product,decision,_=synthetic()
    catalog=api().bundles.BundleCatalog.from_file(ROOT/"cube/bundles.json")
    stream=TrackedStream(body)
    client=adapter(lambda req:upstream_response(200,stream=stream),clock,binding=binding,catalog=catalog)
    real_parser=json.loads
    def must_not_parse(data,*args,**kwargs):
        if data in (body,body.decode("utf-8")):pytest.fail("Structural overflow reached the allocating JSON parser")
        return real_parser(data,*args,**kwargs)
    monkeypatch.setattr(api().cube.json,"loads",must_not_parse)
    async def scenario():
        async with client:
            with pytest.raises(GatewayError) as caught:
                await client.validate_binding(product.manifest,decision=decision,request_id="structural-request")
            assert caught.value.code=="UPSTREAM_UNAVAILABLE" and caught.value.request_id=="structural-request"
        assert stream.closed
    run(scenario())


@pytest.mark.parametrize("encoding",["utf-16-le","utf-16-be","utf-32-le","utf-32-be"])
@pytest.mark.parametrize("structure",["nested","flat"])
def test_unsupported_json_encoding_is_rejected_before_allocating_parser(encoding,structure,clock,monkeypatch):
    binding,product,decision,_=synthetic()
    catalog=api().bundles.BundleCatalog.from_file(ROOT/"cube/bundles.json")
    padding='['*40+'0'+']'*40 if structure=="nested" else '['+'0,'*60000+'0]'
    text='{"p":"\\\"","padding":'+padding+',"q":"\\\",canary",'+json.dumps(meta())[1:]
    # Valid JSON in its original encoding: escaped quotes concealed the padding
    # from the old byte scanner, despite the body remaining below the byte cap.
    assert json.loads(text)["cubes"]==meta()["cubes"]
    body=text.encode(encoding)
    assert len(body)<2_097_152
    stream=TrackedStream(body)
    client=adapter(lambda req:upstream_response(200,stream=stream),clock,binding=binding,catalog=catalog)
    real_parser=json.loads
    def must_not_parse(data,*args,**kwargs):
        if data==body or isinstance(data,str) and "\x00" in data:
            pytest.fail("Unsupported encoding reached the allocating JSON parser")
        return real_parser(data,*args,**kwargs)
    monkeypatch.setattr(api().cube.json,"loads",must_not_parse)
    async def scenario():
        async with client:
            with pytest.raises(GatewayError) as caught:
                await client.validate_binding(product.manifest,decision=decision,request_id="encoding-request")
            assert caught.value.code=="UPSTREAM_UNAVAILABLE" and caught.value.request_id=="encoding-request"
            assert "canary" not in str(caught.value)
        assert stream.closed
    run(scenario())


@pytest.mark.parametrize("invalid",[b"\xff",b"\xc0\x80",b"\xed\xa0\x80",b"\xf4\x90\x80\x80"],
    ids=["invalid-leading-byte","overlong-null","encoded-surrogate","outside-unicode-range"])
def test_invalid_utf8_is_rejected_before_allocating_parser(invalid,clock,monkeypatch):
    binding,product,decision,_=synthetic()
    catalog=api().bundles.BundleCatalog.from_file(ROOT/"cube/bundles.json")
    body=b'{"canary":"'+invalid+b'"}'
    stream=TrackedStream(body)
    client=adapter(lambda req:upstream_response(200,stream=stream),clock,binding=binding,catalog=catalog)
    real_parser=json.loads
    def must_not_parse(data,*args,**kwargs):
        if data==body:pytest.fail("Invalid UTF-8 reached the allocating JSON parser")
        return real_parser(data,*args,**kwargs)
    monkeypatch.setattr(api().cube.json,"loads",must_not_parse)
    async def scenario():
        async with client:
            with pytest.raises(GatewayError) as caught:
                await client.validate_binding(product.manifest,decision=decision,request_id="utf8-request")
            assert caught.value.code=="UPSTREAM_UNAVAILABLE" and caught.value.request_id=="utf8-request"
            assert "canary" not in str(caught.value)
        assert stream.closed
    run(scenario())


def test_response_depth_scan_ignores_escaped_structural_characters_in_utf8_strings(clock,monkeypatch):
    binding,product,decision,_=synthetic()
    catalog=api().bundles.BundleCatalog.from_file(ROOT/"cube/bundles.json")
    recorded=meta();recorded["description"]='[{}:],\\"'*10000+'café🙂\x00'
    text=json.dumps(recorded,ensure_ascii=False)
    body=text.encode("utf-8");stream=TrackedStream(body);parsed=[]
    client=adapter(lambda req:upstream_response(200,stream=stream),clock,binding=binding,catalog=catalog)
    real_parser=json.loads
    def observe_parser(data,*args,**kwargs):
        if data in (body,text):parsed.append(data)
        return real_parser(data,*args,**kwargs)
    monkeypatch.setattr(api().cube.json,"loads",observe_parser)
    async def scenario():
        async with client:
            assert await client.validate_binding(product.manifest,decision=decision) is None
        assert parsed==[text] and stream.closed
    run(scenario())


def test_parser_recursion_fallback_is_sanitized_and_closes_stream(clock,monkeypatch):
    binding,product,decision,_=synthetic()
    catalog=api().bundles.BundleCatalog.from_file(ROOT/"cube/bundles.json")
    body=b'{"cubes":[]}'
    stream=TrackedStream(body)
    client=adapter(lambda req:upstream_response(200,stream=stream),clock,binding=binding,catalog=catalog)
    real_parser=json.loads
    def recursion(data,*args,**kwargs):
        if data in (body,body.decode("utf-8")):raise RecursionError("canary parser diagnostic")
        return real_parser(data,*args,**kwargs)
    monkeypatch.setattr(api().cube.json,"loads",recursion)
    async def scenario():
        async with client:
            with pytest.raises(GatewayError) as caught:
                await client.validate_binding(product.manifest,decision=decision,request_id="parser-request")
            assert caught.value.code=="UPSTREAM_UNAVAILABLE" and caught.value.request_id=="parser-request"
            assert "canary" not in str(caught.value)
        assert stream.closed
    run(scenario())


def test_poll_cancellation_propagates_and_closes_pending_body(clock):
    _,product,decision,request=synthetic()
    class CancellationStream(httpx.AsyncByteStream):
        def __init__(self):self.started=asyncio.Event();self.closed=False
        async def __aiter__(self):
            self.started.set()
            await asyncio.Event().wait()
            yield b"never"
        async def aclose(self):self.closed=True
    stream=CancellationStream()
    def handler(req):
        return upstream_response(200,json=meta()) if req.url.path.endswith("meta") else upstream_response(200,stream=stream)
    async def scenario():
        async with adapter(handler,clock) as client:
            prepared=await client.prepare(product,request,decision)
            pending=asyncio.create_task(client.poll(prepared,decision,deadline=clock.now()+timedelta(seconds=120),request_id="cancel-request"))
            await asyncio.wait_for(stream.started.wait(),1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):await pending
            assert stream.closed
    run(scenario())


@pytest.mark.parametrize("patch",[{"model_bundle_digest":"0"*64},{"endpoint_id":"removed-endpoint"}])
def test_poll_retained_catalog_failures_keep_caller_request_id(patch,clock):
    _,product,decision,request=synthetic()
    async def scenario():
        async with adapter(lambda req:upstream_response(200,json=meta()),clock) as client:
            prepared=await client.prepare(product,request,decision)
            with pytest.raises(GatewayError) as caught:
                await client.poll(prepared.model_copy(update=patch),decision,deadline=clock.now()+timedelta(seconds=120),request_id="caller-request-123")
            assert caught.value.code=="BINDING_DRIFT"
            assert caught.value.request_id=="caller-request-123"
    run(scenario())
