"""Deterministic BigQuery-through-Cube mocks, never live connector evidence."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import socket

import httpx
import pytest

from gateway.adapters.base import CubeBinding, SourceCapabilities, StaticBindingResolver
from gateway.adapters.bundles import BundleCatalog
from gateway.adapters.cube import CubeAdapter
from gateway.adapters.normalize import NormalizationContext, normalize, normalize_value
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import Completed, ExecutionSpec, ExecutionView, PinnedProduct
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import FieldSchema, Manifest
from gateway.contracts.query import QueryRequest, validate_query

pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[2]


def api():
    return importlib.import_module("gateway.adapters.bigquery")


def fixture():
    return json.loads((ROOT / "fixtures/adapter/bigquery.json").read_text())


def settings(**updates):
    raw = deepcopy(fixture()["source_settings"])
    raw.update(updates)
    return raw


def assert_error(call, code="INVALID_INPUT"):
    with pytest.raises(GatewayError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


@pytest.fixture(autouse=True)
def deny_external_sockets(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("Mock contracts must never open an external socket")
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


class WireBytes(httpx.AsyncByteStream):
    def __init__(self, value):
        self.value = json.dumps(value).encode()
    async def __aiter__(self):
        yield self.value


def response(value, status=200):
    return httpx.Response(status, stream=WireBytes(value))


def synthetic():
    binding = CubeBinding.model_validate_json((ROOT / "fixtures/bindings/revenue.json").read_text())
    manifest = Manifest.model_validate_json((ROOT / "fixtures/products/revenue.json").read_text())
    product = PinnedProduct(manifest=manifest, binding_digest=binding.digest, version_id="bigquery-mock-revenue")
    decision = Decision(principal_id="north-user", tenant_id="north", policy_revision=1,
        row_constraints=[{"member":"Orders.tenant_id", "operator":"eq", "values":["north"]},
            {"member":"Orders.access_group", "operator":"in", "values":["north"]}],
        visible_fields=["day", "segment", "revenue"], limits=manifest.limits)
    request = QueryRequest(product_id="revenue", version=manifest.version, inputs={"start":"2024-03-09", "end":"2024-03-12"})
    return binding, product, decision, request


def adapter(handler, clock, **source_updates):
    caps = api().BigQueryCubeProfile.validate(settings(**source_updates))
    binding, _, _, _ = synthetic()
    # Existing synthetic members/endpoints are used solely as in-memory mocks.
    # No PostgreSQL bundle is asserted to be a BigQuery deployment.
    return CubeAdapter(catalog=BundleCatalog.from_file(ROOT / "cube/bundles.json"),
        bindings=StaticBindingResolver([binding]), secret_resolver=lambda _: "a" * 43,
        clock=clock, capabilities=caps, max_response_bytes=caps.max_bytes,
        transport=httpx.MockTransport(handler))


def test_lossless_numeric_contract():
    assert api().convert_numeric("12345678901234567890.123456789") == "12345678901234567890.123456789"
    assert api().convert_numeric("9007199254740993", scale=0) == "9007199254740993"
    assert api().convert_numeric("-0.000000001") == "-0.000000001"
    assert api().convert_numeric("99999999999999999999999999999.999999999") == "99999999999999999999999999999.999999999"


@pytest.mark.parametrize("value", ["1e3", "NaN", "Infinity", "01.2", "+1", "1.0000000001", "100000000000000000000000000000.000000000", 1.1, 123, True, {}, []])
def test_numeric_rejects_coercion_rounding_and_overflow(value):
    assert_error(lambda: api().convert_numeric(value))


def test_null_semantics_require_declared_nullable():
    assert api().convert_numeric(None, nullable=True) is None
    assert api().convert_timestamp(None, nullable=True) is None
    assert api().convert_date(None, nullable=True) is None
    assert_error(lambda: api().convert_numeric(None))
    assert_error(lambda: api().convert_timestamp(None))
    assert_error(lambda: api().convert_date(None))


@pytest.mark.parametrize("value", ["2024-03-10T01:59:59.123456-05:00", "2024-03-10T06:59:59.123456Z"])
def test_timestamp_is_absolute_and_lossless(value):
    assert api().convert_timestamp(value) == "2024-03-10T06:59:59.123456Z"


@pytest.mark.parametrize("value", ["2024-03-10T01:59:59", "2024-03-10", "2024-03-10T01:59:59.1234567Z", "2024-02-30T00:00:00Z", "2024-03-10T23:59:60Z", {}, 0])
def test_timestamp_rejects_civil_time_and_invalid_instant(value):
    assert_error(lambda: api().convert_timestamp(value))


@pytest.mark.parametrize("sign", ["+", "-"])
@pytest.mark.parametrize("offset", ["00:60", "01:99", "00:99", "24:00", "99:00"])
def test_timestamp_rejects_invalid_numeric_offsets(sign, offset):
    value = f"2030-01-01T00:00:00{sign}{offset}"
    caught = assert_error(lambda: api().convert_timestamp(value))
    assert caught.safe_message == "The BigQuery timestamp requires a valid explicit instant."
    assert value not in str(caught)


@pytest.mark.parametrize("offset,expected", [
    ("+00:59", "2029-12-31T23:01:00.123456Z"),
    ("-01:30", "2030-01-01T01:30:00.123456Z"),
    ("+23:59", "2029-12-31T00:01:00.123456Z"),
    ("-23:59", "2030-01-01T23:59:00.123456Z"),
])
def test_timestamp_preserves_valid_numeric_offsets(offset, expected):
    assert api().convert_timestamp(f"2030-01-01T00:00:00.123456{offset}") == expected


def test_date_is_calendar_date_not_timestamp():
    assert api().convert_date("2024-03-10") == "2024-03-10"
    assert_error(lambda: api().convert_date("2024-03-10T00:00:00Z"))
    assert_error(lambda: api().convert_date("2024-02-30"))


def test_profile_is_configured_not_production_or_live_proven():
    caps = api().BigQueryCubeProfile.validate(settings())
    assert isinstance(caps, SourceCapabilities)
    assert caps.production_enabled is False
    assert caps.source_cost_bound == "unproven"
    assert caps.proven_statement_timeout_seconds is None
    assert caps.proven_upstream_lifetime_seconds is None
    assert caps.max_rows == 1000
    assert caps.max_bytes == 1_048_576
    assert caps.field_types == ("string", "date", "decimal")


@pytest.mark.parametrize("updates", [
    {"region":"EU"}, {"region":""}, {"region":"Mars"}, {"dataset_region":"EU"},
    {"secret_ref":""}, {"secret_ref":None}, {"secret_ref":"-----BEGIN PRIVATE KEY-----"},
    {"secret_ref":"/run/secrets/../key"}, {"project_id":""}, {"dataset_id":"bad.dataset"},
    {"cube_version":"latest"}, {"cube_version":"1.8.4"}, {"read_only":False},
    {"pre_aggregations":True}, {"export_bucket":"write-bucket"},
    {"max_rows":1001}, {"max_rows":0}, {"max_rows":True},
    {"max_bytes":1_048_577}, {"max_bytes":0},
    {"maximum_bytes_billed":0}, {"maximum_bytes_billed":True},
    {"maximum_bytes_billed":2**63}, {"credentials":{"private_key":"mock-secret-must-not-leak"}},
    {"production_enabled":True}, {"source_cost_bound":"proven"},
])
def test_invalid_operator_configuration_has_safe_error(updates):
    caught = assert_error(lambda: api().BigQueryCubeProfile.validate(settings(**updates)))
    assert "mock-secret-must-not-leak" not in str(caught)
    assert "PRIVATE KEY" not in str(caught)
    assert set(caught.as_dict()) == {"code", "request_id", "safe_message"}


@pytest.mark.parametrize("location", ["US", "EU", "us-central1", "europe-west2"])
def test_location_requires_explicit_matching_operator_dataset_location(location):
    caps = api().BigQueryCubeProfile.validate(settings(region=location, dataset_region=location))
    assert caps.production_enabled is False


def test_declared_bytes_billed_is_not_claimed_as_enforced():
    caps = api().BigQueryCubeProfile.validate(settings(maximum_bytes_billed=10_000_000))
    assert caps.source_cost_bound == "unproven"
    assert caps.production_enabled is False
    assert_error(lambda: api().select_adapter("bigquery", settings=settings(), production=True))


def test_source_selection_preserves_cube_boundary_and_domo_is_unavailable():
    assert api().select_adapter("postgresql") is CubeAdapter
    assert api().select_adapter("bigquery", settings=settings()) is api().BigQueryCubeProfile
    assert_error(lambda: api().select_adapter("bigquery"))
    assert_error(lambda: api().select_adapter("domo"))
    assert_error(lambda: api().select_adapter("mysql"))


@pytest.mark.parametrize("source_type", ["BIGNUMERIC", "BIGDECIMAL", "DATETIME", "ARRAY", "STRUCT", "FLOAT64", "INT64"])
def test_unsupported_full_source_types_cannot_be_silently_coerced(source_type):
    assert_error(lambda: api().BigQueryCubeProfile.validate_source_type(source_type))


def test_numeric_source_binding_precision_is_explicit():
    schema = api().BigQueryCubeProfile.validate_source_type("NUMERIC")
    assert schema.type == "decimal"
    assert (schema.precision, schema.scale) == (38, 9)
    assert_error(lambda: api().convert_numeric("1", precision=76, scale=38))
    with pytest.raises(ValueError):
        normalize_value(FieldSchema(type="integer"), 9007199254740993)


@pytest.mark.parametrize("operator,expected", [
    ("eq", {"and":[{"member":"Orders.ordered_at","operator":"afterOrOnDate","values":["2024-03-10T00:00:00"]}, {"member":"Orders.ordered_at","operator":"beforeDate","values":["2024-03-11T00:00:00"]}]}),
    ("ne", {"or":[{"member":"Orders.ordered_at","operator":"beforeDate","values":["2024-03-10T00:00:00"]}, {"member":"Orders.ordered_at","operator":"afterOrOnDate","values":["2024-03-11T00:00:00"]}]}),
    ("gte", {"member":"Orders.ordered_at","operator":"afterOrOnDate","values":["2024-03-10T00:00:00"]}),
    ("gt", {"member":"Orders.ordered_at","operator":"afterOrOnDate","values":["2024-03-11T00:00:00"]}),
    ("lt", {"member":"Orders.ordered_at","operator":"beforeDate","values":["2024-03-10T00:00:00"]}),
    ("lte", {"member":"Orders.ordered_at","operator":"beforeDate","values":["2024-03-11T00:00:00"]}),
])
def test_date_filters_use_existing_cube_mapping(operator,expected):
    assert CubeAdapter._date_filter("Orders.ordered_at", operator, "2024-03-10") == expected


def test_mock_parameter_mapping_preserves_half_open_dst_null_and_scope(clock):
    _, product, decision, _ = synthetic()
    request = QueryRequest(product_id="revenue", inputs={"start":"2024-03-10", "end":"2024-03-11"},
        filters=[{"field":"day","operator":"eq","value":"2024-03-10"},
            {"field":"segment","operator":"is_null"}], sort=[{"field":"day","direction":"asc"}])
    seen = []
    def handler(req):
        seen.append(req)
        return response(fixture()["metadata"])
    async def scenario():
        async with adapter(handler, clock) as client:
            prepared = await client.prepare(product, request, decision)
        query = prepared.model_dump(mode="json")["cube_query"]
        assert query["timezone"] == "America/New_York"
        assert query["limit"] == 1001
        assert query["order"] == [["Orders.ordered_at", "asc"]]
        filters = query["filters"][0]["and"]
        assert filters[:2] == [
            {"member":"Orders.ordered_at","operator":"afterOrOnDate","values":["2024-03-10T00:00:00"]},
            {"member":"Orders.ordered_at","operator":"beforeDate","values":["2024-03-11T00:00:00"]}]
        assert {"member":"Orders.segment","operator":"notSet"} in filters
        assert {"member":"Orders.tenant_id","operator":"equals","values":["north"]} in filters
        assert {"member":"Orders.access_group","operator":"equals","values":["north"]} in filters
        assert all(req.url.path.endswith("/meta") for req in seen)
        assert "sql" not in query
    asyncio.run(scenario())


def test_mock_result_normalization_keeps_exact_decimal_null_and_watermark(clock):
    binding, product, decision, request = synthetic()
    def handler(req):
        if req.url.path.endswith("/meta"):
            return response(fixture()["metadata"])
        body = json.loads(req.content)
        assert body["cache"] == "must-revalidate"
        if body["query"].get("dimensions") == ["Watermark.loaded_at"]:
            return response({"data":[{"Watermark.loaded_at":fixture()["watermark"]}]})
        return response({"data":fixture()["raw_rows"]})
    async def scenario():
        async with adapter(handler, clock) as client:
            prepared = await client.prepare(product, request, decision)
            completed = await client.poll(prepared, decision, deadline=clock.now()+timedelta(seconds=120), request_id="bigquery-mock")
        assert isinstance(completed, Completed)
        validated = validate_query(product.manifest, request)
        canonical = validated.model_dump(mode="json", exclude={"idempotency_key"})
        spec = ExecutionSpec(principal_id=decision.principal_id, pinned_product=product,
            canonical_query=canonical, canonical_hash=canonical_digest(canonical), policy_revision=1,
            context="consumer", request_id="bigquery-mock")
        execution = ExecutionView(execution_id="bigquery-mock", state="succeeded", accepted_at=clock.now(), completed_at=clock.now())
        result = normalize(product, completed, execution, context=NormalizationContext(
            spec=spec, decision=decision, executed_decision=decision, binding=binding, clock=clock))
        assert result.model_dump(mode="json")["rows"] == fixture()["expected_rows"]
        assert result.cache_status == "unknown"
        assert result.freshness.status == "fresh"
        assert result.truncated is False
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["rows", "bytes", "upstream_error"])
def test_mock_limits_and_upstream_errors_fail_safely(kind,clock):
    _, product, decision, request = synthetic()
    payload = {"rows":{"data":fixture()["raw_rows"]},
        "bytes":{"data":[{"private":"x"*1500}]},
        "upstream_error":{"error":"private SQL SELECT; Bearer token; mock-secret-must-not-leak"}}[kind]
    def handler(req):
        return response(fixture()["metadata"] if req.url.path.endswith("/meta") else payload)
    async def scenario():
        async with adapter(handler, clock, max_rows=2 if kind=="rows" else 1000,
            max_bytes=1024 if kind=="bytes" else 1_048_576) as client:
            prepared = await client.prepare(product, request, decision)
            with pytest.raises(GatewayError) as caught:
                await client.poll(prepared, decision, deadline=clock.now()+timedelta(seconds=120), request_id="bigquery-limit")
        assert caught.value.code == ("UPSTREAM_UNAVAILABLE" if kind=="upstream_error" else "RESOURCE_LIMIT")
        assert caught.value.request_id == "bigquery-limit"
        assert "mock-secret" not in str(caught.value)
        assert "SELECT" not in str(caught.value)
    asyncio.run(scenario())


def authorization(clock):
    return {"authorization_id":"operator-review-13", "operation":"one-read-only-cube-smoke",
        "project_id":"gateway-mock-123", "dataset_id":"reviewed_orders", "region":"US",
        "secret_ref":"/run/secrets/bigquery_reader", "scoped_credentials":True,
        "scope_evidence":"operator-reviewed-job-user-and-dataset-viewer",
        "spend_ceiling":"0.01", "currency":"USD", "expires_at":clock.now()+timedelta(minutes=5)}


def test_live_authorization_is_separate_and_never_enables_unproven_driver(clock):
    approved = api().validate_live_authorization(settings(), authorization(clock), now=clock.now())
    assert approved.spend_ceiling == "0.01"
    assert_error(lambda: api().validate_live_authorization(settings(), None, now=clock.now()))
    assert_error(lambda: api().require_live_source_bounds(settings()), code="RESOURCE_LIMIT")


@pytest.mark.parametrize("updates", [{"project_id":"other-project"}, {"dataset_id":"other_dataset"},
    {"region":"EU"}, {"secret_ref":"/run/secrets/unapproved"}, {"scoped_credentials":False},
    {"scope_evidence":""}, {"spend_ceiling":"0"}, {"spend_ceiling":"-1"},
    {"spend_ceiling":"1e3"}, {"spend_ceiling":0.01}, {"currency":"EUR"},
    {"operation":"any-query"}, {"authorization_id":""}, {"expires_at":"2024-03-11T00:00:00Z"}])
def test_live_authorization_rejects_incomplete_or_widened_scope(updates,clock):
    raw = authorization(clock)
    raw.update(updates)
    assert_error(lambda: api().validate_live_authorization(settings(), raw, now=clock.now()))


@pytest.mark.parametrize("sign", ["+", "-"])
@pytest.mark.parametrize("offset", ["00:60", "01:99", "00:99", "24:00", "99:00"])
def test_live_authorization_rejects_invalid_numeric_expiry_offsets(sign, offset, clock):
    raw = authorization(clock)
    raw["expires_at"] = f"2030-01-01T00:00:00{sign}{offset}"
    caught = assert_error(lambda: api().validate_live_authorization(settings(), raw, now=clock.now()))
    assert caught.safe_message == "Separate scoped, current BigQuery smoke authorization is required."
    assert raw["expires_at"] not in str(caught)


@pytest.mark.parametrize("offset,expected", [
    ("+00:59", datetime(2029, 12, 31, 23, 1, 0, 123456, tzinfo=timezone.utc)),
    ("-01:30", datetime(2030, 1, 1, 1, 30, 0, 123456, tzinfo=timezone.utc)),
    ("+23:59", datetime(2029, 12, 31, 0, 1, 0, 123456, tzinfo=timezone.utc)),
    ("-23:59", datetime(2030, 1, 1, 23, 59, 0, 123456, tzinfo=timezone.utc)),
])
def test_live_authorization_preserves_valid_numeric_expiry_offsets(offset, expected, clock):
    raw = authorization(clock)
    raw["expires_at"] = f"2030-01-01T00:00:00.123456{offset}"
    approved = api().validate_live_authorization(settings(), raw, now=clock.now())
    assert approved.expires_at == expected


@pytest.mark.parametrize("source_type", [None, [], {}, 1, True])
def test_source_type_errors_are_stable_for_malformed_settings(source_type):
    assert_error(lambda: api().BigQueryCubeProfile.validate_source_type(source_type))


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_selection_requires_explicit_boolean_activation_intent(value):
    assert_error(lambda: api().select_adapter("bigquery", settings=settings(), production=value))


@pytest.mark.parametrize("updates", [{"authorization_id":" "}, {"scope_evidence":"\t"}])
def test_blank_authorization_evidence_cannot_count_as_review(updates,clock):
    raw = authorization(clock)
    raw.update(updates)
    assert_error(lambda: api().validate_live_authorization(settings(), raw, now=clock.now()))


def test_required_configuration_and_revalidation_are_fail_closed():
    raw = settings()
    del raw["secret_ref"]
    assert_error(lambda: api().BigQueryCubeProfile.validate(raw))
    parsed = api().SourceSettings.model_validate(settings())
    forged = parsed.model_copy(update={"region":"EU"})
    assert_error(lambda: api().BigQueryCubeProfile.validate(forged))
    assert api().BigQueryCubeProfile.validate(parsed).production_enabled is False


@pytest.mark.parametrize("field,forged", [("production_enabled",True), ("source_cost_bound","proven"),
    ("proven_statement_timeout_seconds",25.0), ("proven_upstream_lifetime_seconds",120.0)])
def test_capability_configuration_cannot_self_certify_live_evidence(field,forged):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        api().BigQuerySourceCapabilities(**{field:forged})


@pytest.mark.parametrize("value", [123.1, "1e2", "1"*70, "0.001", None])
def test_mock_rows_cannot_bypass_manifest_decimal_contract(value,clock):
    binding, product, decision, request = synthetic()
    canonical = validate_query(product.manifest,request).model_dump(mode="json",exclude={"idempotency_key"})
    spec = ExecutionSpec(principal_id=decision.principal_id,pinned_product=product,
        canonical_query=canonical,canonical_hash=canonical_digest(canonical),policy_revision=1,
        context="consumer",request_id="bigquery-mock-type")
    rows = fixture()["raw_rows"]
    rows[0]["Orders.revenue"] = value
    completed = Completed(raw_rows=rows,source_watermark=fixture()["watermark"],cache_status="unknown")
    execution = ExecutionView(execution_id="bigquery-mock-type",state="succeeded",accepted_at=clock.now(),completed_at=clock.now())
    assert_error(lambda: normalize(product,completed,execution,context=NormalizationContext(
        spec=spec,decision=decision,executed_decision=decision,binding=binding,clock=clock)),code="BINDING_DRIFT")


def live_test_function():
    spec = importlib.util.spec_from_file_location("bigquery_live_gate_under_contract_test", ROOT / "tests/integration/test_bigquery_live.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.test_explicitly_authorized_bigquery_cube_smoke


def test_default_live_gate_does_not_even_read_an_approval_file(monkeypatch):
    monkeypatch.delenv("GATEWAY_BIGQUERY_LIVE", raising=False)
    monkeypatch.setenv("GATEWAY_BIGQUERY_LIVE_AUTHORIZATION_FILE", "/run/secrets/never-read-this")
    run_gate = live_test_function()
    def denied_read(*args, **kwargs):
        pytest.fail("Default live gate must not read any file")
    monkeypatch.setattr(Path, "open", denied_read)
    with pytest.raises(pytest.skip.Exception, match="NOT RUN"):
        run_gate()


def test_live_opt_in_is_not_separate_authorization(monkeypatch):
    monkeypatch.setenv("GATEWAY_BIGQUERY_LIVE", "1")
    monkeypatch.delenv("GATEWAY_BIGQUERY_LIVE_AUTHORIZATION_FILE", raising=False)
    with pytest.raises(pytest.fail.Exception, match="separate scoped BigQuery authorization"):
        live_test_function()()


def test_complete_fictional_live_approval_is_still_blocked_before_secret_or_network(tmp_path,monkeypatch,clock):
    from datetime import datetime, timezone
    raw = authorization(clock)
    raw["expires_at"] = (datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
    approval_file = tmp_path / "fictional-bigquery-approval.json"
    approval_file.write_text(json.dumps({"settings":settings(),"authorization":raw}))
    monkeypatch.setenv("GATEWAY_BIGQUERY_LIVE", "1")
    monkeypatch.setenv("GATEWAY_BIGQUERY_LIVE_AUTHORIZATION_FILE", str(approval_file))
    original_open = Path.open
    opened = []
    def approval_only(path,*args,**kwargs):
        assert path == approval_file, "No credential or other file may be read"
        opened.append(path)
        return original_open(path,*args,**kwargs)
    monkeypatch.setattr(Path,"open",approval_only)
    with pytest.raises(pytest.fail.Exception, match="source cost and lifetime enforcement"):
        live_test_function()()
    assert opened == [approval_file]
