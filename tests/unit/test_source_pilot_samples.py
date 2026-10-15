import pytest
from gateway.source_pilot_samples import weekly_manifest,fixed_decisions,LIMITS,CASE_PRINCIPALS
from gateway.contracts.query import QueryRequest,validate_query

pytestmark=pytest.mark.unit


def test_weekly_sample_preserves_daily_date_exact_decimal_and_scope_variants():
    manifest=weekly_manifest(adapter_revision='cube-bigquery-v1',binding_id='weekly-usage-bq',model_bundle_digest='a'*64)
    assert manifest.grain=='daily' and manifest.timezone=='UTC'
    assert manifest.outputs['day'].type=='date' and manifest.outputs['usage_events'].scale==0
    assert manifest.freshness.required is False and manifest.limits==LIMITS
    assert {f.id for f in manifest.fixtures}==set(CASE_PRINCIPALS)
    assert fixed_decisions('consumer')[0].principal_id=='usage-consumer-all'
    assert fixed_decisions('fixture_test')[1].principal_id=='fixture-usage-core'
    for case in manifest.fixtures:
        if not case.expected_error:
            validate_query(manifest,QueryRequest(product_id=manifest.product_id,version=manifest.version,inputs=case.inputs))
    assert next(f for f in manifest.fixtures if f.id=='core-account-b').expected_rows==()
