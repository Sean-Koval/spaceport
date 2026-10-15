"""Source-role and physical isolation checks precede credentials/DB dispatch."""
import importlib.util
import pytest
from types import SimpleNamespace

pytestmark=pytest.mark.unit


def api():
    assert importlib.util.find_spec('gateway.source_pilot_assets') is not None,'Source composition loader missing'
    from gateway.source_pilot_assets import validate_source_roles,validate_bigquery_isolation
    return validate_source_roles,validate_bigquery_isolation


def source(name,context,provider='bigquery'):
    return SimpleNamespace(source_id=name,revision=1,context=context,provider=provider,
        auth=SimpleNamespace(expected_principal=name+'@sample-project.iam.gserviceaccount.com'),
        cube_endpoint='https://'+name+'.example.com',model_bundle_digest=name,
        data_project='sample-project',dataset=name,tables=('daily_usage',))


def test_source_roles_cannot_alias_providers_even_with_same_context():
    validate,_=api()
    descriptors=(source('bq','consumer'),source('domo','consumer','domo'))
    mappings=[SimpleNamespace(login_name='shared_runtime',source_id=s.source_id,revision=1,context=s.context,role_kind='runtime') for s in descriptors]
    with pytest.raises(ValueError):validate(SimpleNamespace(source_role_mappings=mappings,database_roles={}),descriptors)


@pytest.mark.parametrize('field',['principal','tables','endpoint','bundle'])
def test_bigquery_fixture_isolation_has_real_configured_boundaries(field):
    _,validate=api();consumer=source('consumer','consumer');fixture=source('fixture','fixture_test')
    validate(consumer,fixture)
    if field=='principal':fixture.auth=consumer.auth
    if field=='tables':fixture.dataset=consumer.dataset
    if field=='endpoint':fixture.cube_endpoint=consumer.cube_endpoint
    if field=='bundle':fixture.model_bundle_digest=consumer.model_bundle_digest
    with pytest.raises(ValueError):validate(consumer,fixture)
