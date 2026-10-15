"""Offline configuration: never a request to authenticate or open a source."""
import importlib.util
import json
import socket
import pytest

pytestmark=pytest.mark.unit


def model():
    assert importlib.util.find_spec('gateway.source_pilot_config') is not None, 'Reviewed-source configuration is missing'
    from gateway.source_pilot_config import SourcePilotConfig
    return SourcePilotConfig


def config():
    return dict(schema_version=1,profile='reviewed-source-pilot',deployment_epoch='pilot-2026-10-15',
        database_roles={role:{'username':'gateway_'+role,'password_file':'/run/secrets/registry_'+role}
            for role in ('app','owner','audit_retention')},source_role_mappings=[],public_origin='https://spaceport.example.com',
        oidc=dict(issuer='https://identity.example.com',audience='https://spaceport.example.com/mcp',
            jwks_url='https://identity.example.com/jwks',client_ids=['reviewed-client'],algorithms=['RS256']),
        registry=dict(host='registry.example.com',port=5432,database='gateway_registry',ca_file='/run/pilot/config/registry-ca.pem'),
        product_catalog_ref={'path':'products/index.json','sha256':'a'*64},
        source_catalog_ref={'path':'sources/index.json','sha256':'b'*64},
        identity_mapping_ref={'path':'identities.json','sha256':'c'*64},
        probe_product_id='weekly-account-usage',probe_principal_id='pilot-consumer',credential_hash_file=None,
        runtime={'api_replicas':1,'worker_replicas':1,'shutdown_seconds':140})


def test_valid_source_config_is_offline_and_identity_is_not_synthetic(monkeypatch):
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('configuration opened a socket'))
    value=model().model_validate(config())
    assert value.identity_settings().source=='reviewed_sources'
    assert value.mcp_resource_url=='https://spaceport.example.com/mcp'
    assert value.probe_product_id=='weekly-account-usage'


@pytest.mark.parametrize('field,value',[
    ('profile','oidc-synthetic-pilot'),('schema_version',True),('public_origin','http://spaceport.example.com'),
    ('public_origin','https://spaceport.example.com:8443'),('public_origin','https://user:secret@spaceport.example.com'),
    ('probe_product_id',''),('source_catalog_ref',{'path':'../private/secrets','sha256':'b'*64}),
    ('product_catalog_ref',{'path':'https://evil.example/data','sha256':'a'*64}),
])
def test_source_config_rejects_unsafe_or_implicit_values(field,value):
    raw=config();raw[field]=value
    with pytest.raises(ValueError):model().model_validate(raw)


def test_source_config_unknown_credentials_and_profiles_forbidden():
    raw=config();raw['credentials']='private-token'
    with pytest.raises(ValueError):model().model_validate(raw)
    raw=config();raw['runtime']['worker_replicas']=2
    with pytest.raises(ValueError):model().model_validate(raw)


def test_demo_cannot_select_reviewed_sources():
    from gateway.auth.settings import Settings,validate_mode
    with pytest.raises(ValueError):validate_mode(Settings(source='reviewed_sources'))


def test_original_pilot_identity_rejects_new_source_profile():
    from gateway.pilot_runtime import _identity
    from gateway.auth.settings import Settings
    class NewProfile:
        mcp_resource_url='https://spaceport.example.com/mcp'
        def identity_settings(self):
            return Settings(mode='production',source='reviewed_sources',oidc_issuer='https://identity.example.com',
                oidc_audience=self.mcp_resource_url,oidc_jwks_url='https://identity.example.com/jwks',
                oidc_access_token_profile='rfc9068',oidc_client_ids=('reviewed-client',))
    with pytest.raises(ValueError):_identity(NewProfile())


def test_database_roles_are_explicit_and_owner_does_not_read_source_credentials(monkeypatch):
    import gateway.source_pilot_config as module
    value=model().model_validate(config())
    reads=[]
    monkeypatch.setattr(module,'_read_password',lambda path:reads.append(path) or 'local-test-password')
    url=value.database_url('owner')
    assert 'gateway_owner' in url and reads==['/run/secrets/registry_owner']
    with pytest.raises(ValueError):value.database_url('arbitrary')
    assert len(reads)==1


def test_database_role_alias_or_password_in_config_rejected():
    raw=config();raw['database_roles']['app']['username']='gateway_owner'
    with pytest.raises(ValueError):model().model_validate(raw)
    raw=config();raw['database_roles']['app']['password']='secret'
    with pytest.raises(ValueError):model().model_validate(raw)


@pytest.mark.parametrize('change',['context','role','duplicate_key'])
def test_source_login_cannot_merge_context_or_role_authority(change):
    raw=config()
    one={'login_name':'source_runtime','source_id':'usage','revision':1,'context':'consumer','role_kind':'runtime'}
    two={**one,'source_id':'other'}
    if change=='context':two['context']='fixture_test'
    if change=='role':two['role_kind']='observer'
    if change=='duplicate_key':two={**one,'role_kind':'observer'}
    raw['source_role_mappings']=[one,two]
    with pytest.raises(ValueError):model().model_validate(raw)


def test_legacy_source_reader_is_explicit_name_only_and_distinct():
    raw=config();raw['legacy_source_reader_username']='legacy_reader'
    assert model().model_validate(raw).legacy_source_reader_username=='legacy_reader'
    raw['legacy_source_reader_username']='gateway_app'
    with pytest.raises(ValueError):model().model_validate(raw)


def test_bigquery_metadata_observer_may_span_explicit_context_mappings():
    raw=config();raw['source_role_mappings']=[{'login_name':'bq_observer','source_id':'usage-'+context,
        'revision':1,'context':context,'role_kind':'observer'} for context in ('consumer','fixture_test')]
    assert len(model().model_validate(raw).source_role_mappings)==2


def test_deployment_epoch_matches_node_and_registry_wire_alphabet():
    raw=config();raw['deployment_epoch']='pilot.v1'
    with pytest.raises(ValueError):model().model_validate(raw)
