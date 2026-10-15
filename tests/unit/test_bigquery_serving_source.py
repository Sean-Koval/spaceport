import pytest

pytestmark = pytest.mark.unit


def source_value(**overrides):
    return dict(provider="bigquery", source_id="own-usage", revision=1, context="consumer",
        auth={"mode": "gke-workload-identity", "expected_principal": "spaceport@billing-project.iam.gserviceaccount.com"},
        billing_project="billing-project", data_project="data-project", dataset="analytics",
        tables=["daily_usage"], location="US", dataset_location="US",
        cube_endpoint="https://cube.spaceport.svc.cluster.local",
        model_bundle_digest="a" * 64, source_policy_digest="b" * 64,
        max_jobs_per_execution=2, per_job_maximum_bytes_billed=1000,
        total_execution_bytes=2000, max_active_jobs=2, max_global_active_jobs=4,
        job_timeout_ms=60000) | overrides


def test_source_identity_does_not_change_pool_on_alias_revision_or_credential_rotation():
    from gateway.adapters.bigquery_runtime import BigQuerySource
    value = BigQuerySource.model_validate(source_value())
    changed = BigQuerySource.model_validate(source_value(source_id="alias", revision=2,
        auth={"mode": "gke-workload-identity", "expected_principal": "rotated@billing-project.iam.gserviceaccount.com"}))
    assert value.capacity_pool_id == changed.capacity_pool_id
    assert value.digest != changed.digest


@pytest.mark.parametrize("overrides", [
    {"capacity_pool_id": "escape"}, {"auth": {"mode": "adc"}},
    {"secret_ref": "/run/secrets/source"}, {"dataset_location": "EU"},
    {"tables": ["daily_usage", "daily_usage"]}, {"tables": ["*"]},
    {"cube_endpoint": "https://cube.example/?token=x"}, {"cube_endpoint": "http://cube.example"},
    {"total_execution_bytes": 999}, {"job_timeout_ms": True},
])
def test_source_descriptor_fails_closed(overrides):
    from gateway.adapters.bigquery_runtime import BigQuerySource
    with pytest.raises(ValueError):
        BigQuerySource.model_validate(source_value(**overrides))


def binding_value(**overrides):
    return dict(context="consumer", binding_id="usage-binding", revision=1,
        adapter_revision="cube-bigquery-v1", source_id="own-usage", source_revision=1,
        source_digest="b" * 64, model_bundle_digest="a" * 64,
        fields={"day": {"member":"Usage.day","kind":"time","cube_type":"time","response_key":"Usage.day.day","granularity":"day"},
            "account": {"member":"Usage.account","kind":"dimension","cube_type":"string","response_key":"Usage.account"},
            "usage_events": {"member":"Usage.events","kind":"measure","cube_type":"number","response_key":"Usage.events"}},
        input_fields={"account":"account"}, scope={"tenant_member":"Usage.tenant","access_group_member":"Usage.group"},
        freshness_mode="unknown", checkpoint_member=None) | overrides


def test_unknown_freshness_has_no_fictional_checkpoint():
    from gateway.adapters.bigquery_runtime import BigQueryBinding, bigquery_security_contract, validate_bigquery_completed
    from gateway.contracts.execution import Completed
    binding = BigQueryBinding.model_validate(binding_value())
    contract = bigquery_security_contract(binding)
    assert contract["checkpoint_member"] is None
    assert len(contract["members"]) == 5
    assert validate_bigquery_completed(binding, Completed(raw_rows=[], source_watermark=None, cache_status="unknown"))
    with pytest.raises(ValueError):
        BigQueryBinding.model_validate(binding_value(checkpoint_member="Usage.loaded_at"))


def test_checkpoint_mode_requires_one_distinct_hidden_member():
    from gateway.adapters.bigquery_runtime import BigQueryBinding
    for checkpoint in (None, "Usage.tenant", "Usage.events", "Other.loaded_at"):
        with pytest.raises(ValueError):
            BigQueryBinding.model_validate(binding_value(freshness_mode="same-query-checkpoint", checkpoint_member=checkpoint))


def test_bigquery_compilation_is_bounded_and_does_not_add_hidden_checkpoint():
    from gateway.adapters.bigquery_runtime import BigQueryBinding, compile_bigquery_query
    from gateway.contracts.manifest import Manifest
    from tests.contract.test_cube_adapter import synthetic
    original, product, decision, request = synthetic()
    value = original.model_dump(mode="json")
    value.update(adapter_revision="cube-bigquery-v1",source_id="own-usage",source_revision=1,source_digest="b"*64,
        scope={"tenant_member":"Orders.tenant_id","access_group_member":"Orders.access_group"},
        freshness_mode="unknown",checkpoint_member=None)
    value.pop("constraint_members",None)
    value.pop("watermark_member",None)
    binding = BigQueryBinding.model_validate(value)
    data = product.manifest.model_dump(mode="json")
    data.update(adapter_revision="cube-bigquery-v1",content_digest=None)
    data["freshness"]["required"] = False
    manifest = Manifest.model_validate(data)
    query = compile_bigquery_query(binding,manifest,request,decision,max_rows=5)
    assert query["limit"] == 6
    assert query["dimensions"] == ["Orders.segment"]
    assert query["filters"][0]["and"][-2:] == [
        {"member":"Orders.tenant_id","operator":"equals","values":["north"]},
        {"member":"Orders.access_group","operator":"equals","values":["north"]}]
