"""Test contract behavior directly; no network or upstream mocks are needed."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import importlib
from types import SimpleNamespace

from pydantic import ValidationError
import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]


def api():
    try:
        return SimpleNamespace(**{name: importlib.import_module(f"gateway.contracts.{name}") for name in ("canonical", "manifest", "identity", "query", "execution", "errors", "clock")})
    except ModuleNotFoundError as error:
        pytest.fail(f"Contract implementation is missing: {error.name}")


def manifest(raw):
    return api().manifest.validate_manifest(raw)


def query(inputs=None, **changes):
    payload = {"product_id": "revenue", "inputs": {"start": "2024-03-09", "end": "2024-03-12"} if inputs is None else inputs, **changes}
    return api().query.QueryRequest(**payload)


def reject_manifest(raw):
    with pytest.raises(api().errors.GatewayError) as caught:
        manifest(raw)
    assert caught.value.code == "INVALID_INPUT"
    assert caught.value.request_id
    return caught.value


def reject_query(m, **changes):
    with pytest.raises(api().errors.GatewayError) as caught:
        api().query.validate_query(m, query(**changes))
    assert caught.value.code == "INVALID_INPUT"


def test_all_contract_modules_are_importable():
    assert api().manifest.Manifest is not None


def test_manifest_digest_is_independent_of_object_key_order(valid_manifest):
    m = manifest(valid_manifest)
    reordered = dict(reversed(list(valid_manifest.items())))
    reordered["inputs"] = dict(reversed(list(valid_manifest["inputs"].items())))
    assert api().canonical.manifest_digest(m) == api().canonical.manifest_digest(manifest(reordered))


def test_manifest_digest_changes_with_description(valid_manifest):
    m = manifest(valid_manifest)
    changed = deepcopy(valid_manifest)
    changed["description"] = "Changed"
    assert api().canonical.manifest_digest(m) != api().canonical.manifest_digest(manifest(changed))


def test_manifest_digest_excludes_content_digest_and_validates_supplied_digest(valid_manifest):
    m = manifest(valid_manifest)
    assert m.content_digest == api().canonical.manifest_digest(m)
    assert manifest(m.model_dump(mode="json")).content_digest == m.content_digest
    invalid = deepcopy(valid_manifest)
    invalid["content_digest"] = "f" * 64
    reject_manifest(invalid)


def test_canonical_json_follows_rfc8785_unicode_order_and_number_format():
    # UTF-16 property ordering differs from Python codepoint sorting here.
    actual = api().canonical.canonical_bytes({"\ue000": 1e-7, "😀": 1.0, "amount": Decimal("9007199254740993.01")})
    assert actual == '{"amount":"9007199254740993.01","😀":1,"\ue000":1e-7}'.encode()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 2**53, {1: "invalid key"}])
def test_canonical_json_rejects_unrepresentable_values(value):
    with pytest.raises(api().errors.GatewayError, match="canonical"):
        api().canonical.canonical_bytes({"value": value})


@pytest.mark.parametrize("path,value", [
    (("sql",), "select 1"),
    (("status",), "published"),
    (("freshness", "extra"), True),
    (("inputs", "start", "extra"), "x"),
    (("inputs", "segment", "default", "secret"), "x"),
    (("filters", "segment", "extra"), True),
    (("sort", "extra"), True),
    (("links", 0, "extra"), "x"),
    (("fixtures", 0, "extra"), "x"),
    (("examples", 0, "extra"), "x"),
    (("units", "revenue", "extra"), "x"),
    (("date_boundaries", "extra"), "x"),
])
def test_manifest_rejects_unknown_fields_recursively(valid_manifest, path, value):
    raw = deepcopy(valid_manifest)
    target = raw
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    reject_manifest(raw)


@pytest.mark.parametrize("replacement", [
    {"type": "integer", "default": {"value": "1"}},
    {"type": "integer", "default": {"value": True}},
    {"type": "string", "default": {"value": None}},
    {"type": "string", "enum": ["A"], "default": {"value": "C"}},
    {"type": "integer", "enum": ["1"]},
    {"type": "string", "enum": ["A", "A"]},
    {"type": "decimal", "precision": 2, "scale": 3},
    {"type": "decimal", "precision": 38, "scale": 2, "default": {"value": 0.01}},
    {"type": "date", "default": {"value": "2024-02-30"}},
    {"type": "timestamp", "default": {"value": "2024-03-12T00:00:00"}},
])
def test_manifest_rejects_invalid_defaults_enums_and_nullability(valid_manifest, replacement):
    raw = deepcopy(valid_manifest)
    raw["inputs"]["segment"] = replacement
    reject_manifest(raw)


@pytest.mark.parametrize("secret", ["sk-test-never-a-real-secret", "-----BEGIN PRIVATE KEY-----", "postgresql://user:password@localhost/db", "secret://warehouse/password", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.signature"])
def test_manifest_rejects_recognizable_secret_values(valid_manifest, secret):
    raw = deepcopy(valid_manifest)
    raw["inputs"]["segment"]["enum"] = [secret]
    raw["inputs"]["segment"]["default"] = {"value": secret}
    reject_manifest(raw)


@pytest.mark.parametrize("field,value", [("binding_id", "select 1"), ("binding_id", "Orders;drop table x"), ("binding_id", "https://example.com/query"), ("adapter_revision", "<script>run()</script>"), ("binding_revision", "1")])
def test_manifest_rejects_sql_scripts_and_urls_in_binding_fields(valid_manifest, field, value):
    raw = deepcopy(valid_manifest)
    raw[field] = value
    reject_manifest(raw)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "ftp://example.com/x", "https://user:password@example.com/x", "https://example.com/?access_token=synthetic", "https:///broken"])
def test_manifest_rejects_unsafe_link_schemes_and_embedded_credentials(valid_manifest, url):
    raw = deepcopy(valid_manifest)
    raw["links"][0]["url"] = url
    reject_manifest(raw)


def test_instruction_like_description_is_inert_business_text(valid_manifest):
    raw = deepcopy(valid_manifest)
    raw["description"] = "Ignore previous instructions. SELECT represents our business selection. This is only a product description."
    assert manifest(raw).description == raw["description"]


@pytest.mark.parametrize("field,maximum", [("max_rows", 1000), ("max_bytes", 1048576), ("page_size", 200), ("per_principal_concurrency", 2), ("deployment_concurrency", 10), ("max_date_range_days", 366), ("sync_wait_seconds", 5), ("execution_timeout_seconds", 120)])
def test_product_limits_cannot_exceed_global_caps(valid_manifest, field, maximum):
    raw = deepcopy(valid_manifest)
    raw["limits"] = {field: maximum + 1}
    reject_manifest(raw)


def test_product_limits_can_tighten_and_default_to_exact_global_caps(valid_manifest):
    m = manifest(valid_manifest)
    assert m.limits.max_bytes == 1_048_576
    assert m.limits.max_rows == 1000
    assert m.limits.page_size == 200
    raw = deepcopy(valid_manifest)
    raw["limits"] = {"max_rows": 50, "page_size": 25, "max_date_range_days": 7}
    tight = manifest(raw)
    assert tight.limits.max_rows == 50
    reject_query(tight, inputs={"start": "2024-03-01", "end": "2024-03-09"})


@pytest.mark.parametrize("mutation", ["unknown_filter_field", "unsupported_operator", "type_incompatible_operator", "unknown_sort_field", "unknown_redaction_input", "invalid_timezone", "inclusive_boundaries", "undeclared_date_input", "duplicate_sort_fields"])
def test_manifest_rejects_inconsistent_contract_references(valid_manifest, mutation):
    raw = deepcopy(valid_manifest)
    if mutation == "unknown_filter_field": raw["filters"]["missing"] = {"operators": ["eq"]}
    if mutation == "unsupported_operator": raw["filters"]["segment"]["operators"] = ["sql"]
    if mutation == "type_incompatible_operator": raw["filters"]["segment"]["operators"] = ["gt"]
    if mutation == "unknown_sort_field": raw["sort"]["fields"] = ["missing"]
    if mutation == "unknown_redaction_input": raw["redaction"]["password"] = "omit"
    if mutation == "invalid_timezone": raw["timezone"] = "Not/A_Zone"
    if mutation == "inclusive_boundaries": raw["date_boundaries"]["interval"] = "[start,end]"
    if mutation == "undeclared_date_input": raw["date_boundaries"]["start_input"] = "missing"
    if mutation == "duplicate_sort_fields": raw["sort"]["fields"] = ["day", "day"]
    reject_manifest(raw)


@pytest.mark.parametrize("field", ["claimed_user_id", "tenant_id", "roles", "sql", "source_url", "policy_override"])
def test_query_rejects_identity_source_and_policy_arguments(valid_manifest, field):
    reject_query(manifest(valid_manifest), inputs={"start": "2024-03-09", "end": "2024-03-12", field: "admin"})


@pytest.mark.parametrize("inputs", [
    {}, {"start": "2024-03-09"},
    {"start": "2024-02-30", "end": "2024-03-12"},
    {"start": "2024-03-09T00:00:00Z", "end": "2024-03-12"},
    {"start": "2024-01-01", "end": "2025-01-02"},
    {"start": "2024-03-12", "end": "2024-03-09"},
    {"start": "2024-03-09", "end": "2024-03-09"},
    {"start": "2024-03-09", "end": "2024-03-12", "segment": "C"},
])
def test_query_rejects_invalid_inputs_and_date_ranges(valid_manifest, inputs):
    reject_query(manifest(valid_manifest), inputs=inputs)


def test_date_range_uses_local_calendar_days_across_leap_year_and_dst(valid_manifest):
    m = manifest(valid_manifest)
    q = api().query.validate_query(m, query(inputs={"start": "2024-01-01", "end": "2025-01-01"}))
    assert q.inputs["segment"] is None
    assert q.inputs["end"] == "2025-01-01"
    assert api().query.validate_query(m, query()).inputs["start"] == "2024-03-09"


@pytest.mark.parametrize("filters", [
    [{"field": "missing", "operator": "eq", "value": "A"}],
    [{"field": "segment", "operator": "ne", "value": "A"}],
    [{"field": "segment", "operator": "eq", "value": 1}],
    [{"field": "segment", "operator": "in", "value": []}],
    [{"field": "segment", "operator": "in", "value": ["A"] * 11}],
    [{"field": "segment", "operator": "is_null", "value": "yes"}],
])
def test_query_rejects_unapproved_filters_and_invalid_values(valid_manifest, filters):
    reject_query(manifest(valid_manifest), filters=filters)


def test_query_accepts_declared_filters_and_sort(valid_manifest):
    q = query(filters=[{"field": "segment", "operator": "in", "value": ["A", "B"]}], sort=[{"field": "day", "direction": "desc"}])
    validated = api().query.validate_query(manifest(valid_manifest), q)
    assert validated.filters[0].value == ("A", "B")
    assert validated.sort[0].direction == "desc"


@pytest.mark.parametrize("sort", [[{"field": "revenue", "direction": "asc"}], [{"field": "day", "direction": "asc"}] * 2])
def test_query_rejects_unapproved_or_duplicate_sort(valid_manifest, sort):
    reject_query(manifest(valid_manifest), sort=sort)


@pytest.mark.parametrize("changes", [{"claimed_user_id": "admin"}, {"inputs": {"x": "a" * 4097}}, {"filters": [{"field": "day", "operator": "eq", "value": "2024-03-09"}] * 21}, {"sort": [{"field": "day"}] * 6}, {"idempotency_key": "a" * 129}])
def test_query_request_is_strict_and_bounded(changes):
    with pytest.raises(ValidationError):
        query(**changes)


def test_query_product_and_version_must_match_manifest(valid_manifest):
    m = manifest(valid_manifest)
    reject_query(m, product_id="different")
    reject_query(m, version="2.0.0")


def test_validated_manifest_and_pinned_product_are_deeply_immutable(valid_manifest):
    m = manifest(valid_manifest)
    with pytest.raises(ValidationError): m.description = "changed"
    with pytest.raises(TypeError): m.inputs["extra"] = m.inputs["start"]
    with pytest.raises(ValidationError): m.inputs["start"].nullable = True
    with pytest.raises(TypeError): m.examples[0].inputs["start"] = "2000-01-01"
    pinned = api().execution.PinnedProduct(manifest=m, binding_digest="b" * 64, version_id="version-1")
    with pytest.raises(ValidationError): pinned.version_id = "version-2"


def test_principal_has_verified_identity_fields_only(north_principal):
    assert north_principal.kind == "demo"
    with pytest.raises(ValidationError):
        api().identity.Principal(**{**north_principal.model_dump(), "roles": ["admin"]})


def test_execution_spec_validates_pinned_request_and_canonical_hash(valid_manifest):
    modules = api()
    m = manifest(valid_manifest)
    q = modules.query.validate_query(m, query(version=m.version)).model_dump(mode="json", exclude={"idempotency_key"})
    pinned = modules.execution.PinnedProduct(manifest=m, binding_digest="b" * 64, version_id="version-1")
    spec = modules.execution.ExecutionSpec(principal_id="north-user", pinned_product=pinned, canonical_query=q, canonical_hash=modules.canonical.canonical_digest(q), policy_revision=1, context="consumer", request_id="request-1")
    assert spec.canonical_hash == modules.canonical.canonical_digest(q)
    with pytest.raises(ValidationError):
        modules.execution.ExecutionSpec(**{**spec.model_dump(), "canonical_hash": "f" * 64})


def test_decision_uses_server_constraints_and_capped_limits():
    d = api().identity.Decision(principal_id="north-user", policy_revision=1, tenant_id="north", row_constraints=[{"member": "Orders.tenantId", "operator": "eq", "values": ["north"]}], visible_fields=["day", "revenue"], limits={})
    assert d.row_constraints[0].values == ("north",)
    with pytest.raises(ValidationError):
        api().identity.Decision(**{**d.model_dump(), "limits": {"max_rows": 1001}})


def test_adapter_poll_and_prepared_query_do_not_invent_evidence(clock):
    ex = api().execution
    assert ex.Pending(retry_after_seconds=1.0).retry_after_seconds == 1.0
    completed = ex.Completed(raw_rows=[{"revenue": "9007199254741023.31"}], source_watermark=None)
    assert completed.cache_status == "unknown"
    assert completed.source_watermark is None
    prepared = ex.PreparedQuery(endpoint_id="demo-cube", model_bundle_digest="a" * 64, cube_query={"measures": ["Orders.revenue"], "limit": 1000})
    assert prepared.endpoint_id == "demo-cube"
    with pytest.raises(ValidationError):
        ex.PreparedQuery(**{**prepared.model_dump(), "endpoint_id": "https://example.com"})
    with pytest.raises(ValidationError): ex.Pending(retry_after_seconds=-1.0)


def result_payload(clock):
    return {
        "execution_id": "execution-1", "state": "succeeded", "accepted_at": clock.now(), "started_at": clock.now(), "completed_at": clock.now(),
        "product_id": "revenue", "version": "1.0.0", "product_digest": "a" * 64,
        "typed_schema": {"revenue": {"type": "decimal", "precision": 38, "scale": 2}, "segment": {"type": "string", "nullable": True}},
        "rows": [{"revenue": "9007199254741023.31", "segment": None}], "units": {"revenue": {"unit": "money", "currency": "USD"}}, "grain": "daily",
        "effective_range": {"start": "2024-03-09", "end": "2024-03-12", "interval": "[start,end)"}, "timezone": "America/New_York",
        "provenance": {"binding_id": "revenue-orders", "binding_revision": 1, "adapter_revision": "cube-rest-v1", "model_bundle_digest": "a" * 64, "links": []},
        "row_count": 1, "truncated": False,
        "source_watermark": datetime(2024, 3, 12, tzinfo=timezone.utc),
        "freshness": {"status": "fresh", "assessed_at": clock.now(), "age_seconds": 3600.0, "max_age_seconds": 86400},
        "cache_status": "unknown", "warnings": [],
    }


def test_result_preserves_decimal_precision_nulls_and_explicit_formats(clock):
    envelope = api().execution.ResultEnvelope(**result_payload(clock))
    encoded = envelope.model_dump(mode="json")
    assert encoded["rows"][0]["revenue"] == "9007199254741023.31"
    assert encoded["rows"][0]["segment"] is None
    assert encoded["source_watermark"] == "2024-03-12T00:00:00Z"
    assert encoded["truncated"] is False


@pytest.mark.parametrize("mutation", ["float_decimal", "undeclared_column", "missing_column", "nonnullable_null", "truncated", "impossible_row_count", "missing_watermark", "false_freshness", "naive_timestamp", "expired_with_rows"])
def test_result_rejects_lossy_partial_or_inconsistent_output(clock, mutation):
    raw = result_payload(clock)
    if mutation == "float_decimal": raw["rows"][0]["revenue"] = 0.01
    if mutation == "undeclared_column": raw["rows"][0]["password"] = "hidden"
    if mutation == "missing_column": del raw["rows"][0]["segment"]
    if mutation == "nonnullable_null": raw["rows"][0]["revenue"] = None
    if mutation == "truncated": raw["truncated"] = True
    if mutation == "impossible_row_count": raw["row_count"] = 0
    if mutation == "missing_watermark": raw["source_watermark"] = None
    if mutation == "false_freshness": raw["freshness"]["age_seconds"] = 0.0
    if mutation == "naive_timestamp": raw["accepted_at"] = datetime(2024, 3, 12)
    if mutation == "expired_with_rows": raw["state"] = "expired"
    with pytest.raises(ValidationError): api().execution.ResultEnvelope(**raw)


def test_complete_result_byte_limit_counts_utf8_and_metadata(clock):
    raw = result_payload(clock)
    raw["typed_schema"] = {"text": {"type": "string"}}
    raw["units"] = {}
    raw["rows"] = [{"text": "😀" * 262144}]
    with pytest.raises(ValidationError, match="1,048,576"):
        api().execution.ResultEnvelope(**raw)


def test_expired_result_contains_no_rows(clock):
    raw = result_payload(clock)
    raw.update(state="expired", rows=[], row_count=0, freshness=None, source_watermark=None)
    assert api().execution.ResultEnvelope(**raw).rows == ()


def test_stable_errors_are_safe_serializable_and_exhaustive():
    errors = api().errors
    expected = {"UNAUTHENTICATED", "NOT_FOUND_OR_FORBIDDEN", "INVALID_INPUT", "BINDING_DRIFT", "UPSTREAM_UNAVAILABLE", "RESOURCE_LIMIT", "EXECUTION_TIMEOUT", "FRESHNESS_UNKNOWN", "STALE_DATA", "CONFLICT"}
    assert {code.value for code in errors.ErrorCode} == expected
    error = errors.GatewayError("INVALID_INPUT", "request-1", "The input is invalid.")
    assert error.as_dict() == {"code": "INVALID_INPUT", "request_id": "request-1", "safe_message": "The input is invalid."}


def test_injectable_clock_is_deterministic_and_system_clock_is_aware(clock):
    assert isinstance(clock, api().clock.Clock)
    asyncio.run(clock.sleep(5.0))
    assert clock.now() == datetime(2024, 3, 12, 1, 0, 5, tzinfo=timezone.utc)
    assert api().clock.SystemClock().now().utcoffset().total_seconds() == 0
    asyncio.run(api().clock.SystemClock().sleep(0.0))


@pytest.mark.parametrize("model_name,payload", [
    ("PreparedQuery", {"endpoint_id": "demo-cube", "model_bundle_digest": "a" * 64, "cube_query": {"measures": ["Orders.revenue"], "filters": [{"member": "Orders.tenantId", "operator": "equals", "values": ["north"]}]}}),
    ("Completed", {"raw_rows": [{"values": ["A", "B"]}], "source_watermark": None}),
])
def test_immutable_internal_json_roundtrips_without_serializer_warnings(model_name, payload):
    import warnings
    model = getattr(api().execution, model_name)(**payload)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        encoded = model.model_dump(mode="json")
    assert getattr(api().execution, model_name).model_validate(encoded).model_dump(mode="json") == encoded


@pytest.mark.parametrize("location", ["default", "enum"])
def test_manifest_bounds_scalar_defaults_and_enums(valid_manifest, location):
    raw = deepcopy(valid_manifest)
    value = "A" * 4097
    raw["inputs"]["segment"]["enum"] = [value]
    raw["inputs"]["segment"]["default"] = {"value": value if location == "default" else None}
    reject_manifest(raw)


def test_recognizable_secret_cannot_be_used_as_a_binding_identifier(valid_manifest):
    raw = deepcopy(valid_manifest)
    raw["binding_id"] = "sk-test-never-a-real-secret"
    reject_manifest(raw)


def test_decimal_zero_fits_a_type_with_no_whole_digits():
    schema = api().manifest.FieldSchema(type="decimal", precision=2, scale=2, default={"value": "0"})
    assert api().manifest.validate_value(schema, "0.00") == "0.00"


def test_number_default_outside_canonical_integer_range_is_a_safe_error(valid_manifest):
    raw = deepcopy(valid_manifest)
    raw["inputs"]["segment"] = {"type": "number", "default": {"value": 10**400}}
    reject_manifest(raw)


def test_truncated_flag_rejects_integer_zero_in_strict_result(clock):
    raw = result_payload(clock)
    raw["truncated"] = 0
    with pytest.raises(ValidationError): api().execution.ResultEnvelope(**raw)


def test_revenue_fixture_records_exact_daily_rows_and_three_day_total(valid_manifest):
    rows = manifest(valid_manifest).fixtures[0].expected_rows
    assert [row["day"] for row in rows] == ["2024-03-09", "2024-03-10", "2024-03-11"]
    assert rows[1]["segment"] is None
    assert sum(Decimal(row["revenue"]) for row in rows) == Decimal("9007199254741023.31")


@pytest.mark.parametrize("pinned", [False, True], ids=["manifest", "pinned_product"])
@pytest.mark.parametrize("map_name", ["inputs", "example_inputs", "fixture_inputs", "fixture_row"])
def test_frozen_manifest_maps_reject_reinitialization_without_changing_digest(valid_manifest, pinned, map_name):
    modules = api()
    m = manifest(valid_manifest)
    if pinned:
        m = modules.execution.PinnedProduct(manifest=m, binding_digest="b" * 64, version_id="version-1").manifest
    maps = {
        "inputs": m.inputs,
        "example_inputs": m.examples[0].inputs,
        "fixture_inputs": m.fixtures[0].inputs,
        "fixture_row": m.fixtures[0].expected_rows[0],
    }
    before = m.model_dump(mode="json")
    before_digest = m.content_digest
    with pytest.raises(TypeError, match="immutable"):
        maps[map_name].__init__({"claimed_user_id": modules.manifest.FieldSchema(type="string", required=False)})
    assert m.model_dump(mode="json") == before
    assert m.content_digest == before_digest == modules.canonical.manifest_digest(m)
    reject_query(m, inputs={"start": "2024-03-09", "end": "2024-03-12", "claimed_user_id": "admin"})


@pytest.mark.parametrize("nested", [False, True], ids=["root", "nested_filter"])
def test_frozen_internal_json_rejects_reinitialization_without_changing_values(nested):
    modules = api()
    prepared = modules.execution.PreparedQuery(
        endpoint_id="demo-cube", model_bundle_digest="a" * 64,
        cube_query={"measures": ["Orders.revenue"], "filters": [{"member": "Orders.tenantId", "operator": "equals", "values": ["north"]}]},
    )
    target = prepared.cube_query["filters"][0] if nested else prepared.cube_query
    before = prepared.model_dump(mode="json")
    before_digest = modules.canonical.canonical_digest(before)
    with pytest.raises(TypeError, match="immutable"):
        target.__init__({"member": "Orders.unrestricted"})
    assert prepared.model_dump(mode="json") == before
    assert modules.canonical.canonical_digest(prepared.model_dump(mode="json")) == before_digest


@pytest.mark.parametrize("input_name", ["segment", "safe_unknown_input"])
@pytest.mark.parametrize("secret", [
    "sk-test-never-a-real-secret", "ghp_synthetic", "github_pat_synthetic", "AKIAsynthetic",
    "secret://warehouse/password", "env:WAREHOUSE_PASSWORD",
    "postgres://user:synthetic@localhost/db", "postgresql://user:synthetic@localhost/db",
    "-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.signature",
])
def test_invalid_input_fixture_rejects_credentials_independently_of_query_validity(valid_manifest, input_name, secret):
    raw = deepcopy(valid_manifest)
    raw["fixtures"].append({
        "id": "negative-secret", "version": "1", "expected_error": "INVALID_INPUT",
        "inputs": {"start": "2024-03-09", "end": "2024-03-12", input_name: secret},
    })
    reject_manifest(raw)


@pytest.mark.parametrize("inputs", [
    {"start": "2024-02-30", "end": "2024-03-12"},
    {"start": "2024-03-09", "end": "2024-03-12", "safe_unknown_input": "benign negative case"},
])
def test_invalid_input_fixture_still_accepts_safe_negative_cases(valid_manifest, inputs):
    raw = deepcopy(valid_manifest)
    raw["fixtures"].append({"id": "negative-safe", "version": "1", "expected_error": "INVALID_INPUT", "inputs": inputs})
    m = manifest(raw)
    assert m.fixtures[-1].inputs == inputs
    reject_query(m, inputs=inputs)


UTC_OVERFLOW_TIMESTAMPS = ["0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59"]


@pytest.mark.parametrize("value", UTC_OVERFLOW_TIMESTAMPS, ids=["lower_bound", "upper_bound"])
def test_utc_normalization_overflow_in_manifest_default_is_invalid_input(valid_manifest, value):
    raw = deepcopy(valid_manifest)
    raw["inputs"]["segment"] = {"type": "timestamp", "required": False, "default": {"value": value}}
    reject_manifest(raw)


@pytest.mark.parametrize("value", UTC_OVERFLOW_TIMESTAMPS, ids=["lower_bound", "upper_bound"])
def test_utc_normalization_overflow_in_query_input_is_invalid_input(valid_manifest, value):
    raw = deepcopy(valid_manifest)
    raw["inputs"]["segment"] = {"type": "timestamp", "required": False}
    reject_query(manifest(raw), inputs={"start": "2024-03-09", "end": "2024-03-12", "segment": value})


@pytest.mark.parametrize("value", UTC_OVERFLOW_TIMESTAMPS, ids=["lower_bound", "upper_bound"])
def test_utc_normalization_overflow_in_aware_datetime_is_a_safe_canonical_error(value):
    with pytest.raises(api().errors.GatewayError) as caught:
        api().canonical.canonical_bytes(datetime.fromisoformat(value))
    assert caught.value.code == "INVALID_INPUT"
    assert caught.value.request_id
    assert value not in caught.value.safe_message


@pytest.mark.parametrize("value", UTC_OVERFLOW_TIMESTAMPS, ids=["lower_bound", "upper_bound"])
def test_utc_normalization_overflow_in_aware_timestamp_model_is_validation_error(value):
    with pytest.raises(ValidationError):
        api().execution.Completed(raw_rows=[], source_watermark=value)


@pytest.mark.parametrize("value", UTC_OVERFLOW_TIMESTAMPS, ids=["lower_bound", "upper_bound"])
@pytest.mark.parametrize("aware_datetime", [False, True], ids=["string", "datetime"])
def test_utc_normalization_helper_rejects_unrepresentable_times_with_value_error(value, aware_datetime):
    with pytest.raises(ValueError, match="UTC"):
        api().canonical.timestamp(datetime.fromisoformat(value) if aware_datetime else value)


@pytest.mark.parametrize("value,expected", [
    ("0001-01-01T23:59:00+23:59", "0001-01-01T00:00:00Z"),
    ("9999-12-31T00:00:00-23:59", "9999-12-31T23:59:00Z"),
])
def test_utc_normalization_nearby_representable_offset_timestamps_roundtrip(value, expected):
    modules = api()
    completed = modules.execution.Completed(raw_rows=[], source_watermark=value)
    assert completed.model_dump(mode="json")["source_watermark"] == expected
    assert modules.canonical.canonical_bytes({"at": datetime.fromisoformat(value)}) == ('{"at":"' + expected + '"}').encode()
    assert modules.execution.Completed.model_validate(completed.model_dump(mode="json")).source_watermark == completed.source_watermark
