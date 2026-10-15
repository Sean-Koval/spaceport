"""BigQuery fixture identities require physically separate operator sources."""
from copy import deepcopy
import pytest
from gateway.adapters.bigquery_runtime import BigQuerySource
from gateway.publishing.test_runner import parse_fixture_catalog
from tests.unit.test_bigquery_serving_source import source_value,binding_value

pytestmark=pytest.mark.unit


def catalog_value():
    c=BigQuerySource.model_validate(source_value())
    f=BigQuerySource.model_validate(source_value(source_id='fixture-usage',context='fixture_test',dataset='fixture',
        auth={'mode':'gke-workload-identity','expected_principal':'fixture@billing-project.iam.gserviceaccount.com'},
        cube_endpoint='https://fixture.spaceport.svc.cluster.local',model_bundle_digest='f'*64))
    return dict(product_id='usage',execution_contract_digest='e'*64,
        consumer_source=c.model_dump(mode='json'),fixture_source=f.model_dump(mode='json'),
        consumer_binding=binding_value(source_digest=c.digest),
        fixture_binding=binding_value(context='fixture_test',binding_id='fixture-binding',source_id=f.source_id,
            source_digest=f.digest,model_bundle_digest=f.model_bundle_digest),cases=[
            dict(kind='known_answer',principal_id='fixture',fixture=dict(id='answer',version='1',inputs={},expected_rows=[])),
            dict(kind='permission_negative',principal_id='denied',fixture=dict(id='denied',version='1',inputs={},expected_error='NOT_FOUND_OR_FORBIDDEN'))])


def test_exact_bigquery_fixture_catalog_accepts_shared_billing_pool_only():
    catalog=parse_fixture_catalog(catalog_value())
    assert catalog.consumer_source.capacity_pool_id==catalog.fixture_source.capacity_pool_id
    assert catalog.consumer_source.dataset!=catalog.fixture_source.dataset


@pytest.mark.parametrize('field',['source_id','dataset','auth','cube_endpoint','model_bundle_digest'])
def test_aliases_cannot_certify_bigquery_fixture_isolation(field):
    raw=catalog_value();raw['fixture_source'][field]=deepcopy(raw['consumer_source'][field])
    source=BigQuerySource.model_validate(raw['fixture_source'])
    raw['fixture_binding'].update(source_id=source.source_id,source_digest=source.digest,model_bundle_digest=source.model_bundle_digest)
    with pytest.raises(ValueError):parse_fixture_catalog(raw)
