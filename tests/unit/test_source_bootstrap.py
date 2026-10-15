"""Owner-reviewed identity and policy inputs are inert and bounded."""
import importlib.util
import pytest

pytestmark=pytest.mark.unit


def models():
    assert importlib.util.find_spec('gateway.source_pilot_bootstrap') is not None,'Source bootstrap missing'
    from gateway.source_pilot_bootstrap import IdentityMapping,PolicySeed
    return IdentityMapping,PolicySeed


def mapping():
    grant=lambda role:dict(policy_id='usage',tenant_id='north',roles=[role],entitlements={'groups':['core']})
    return {'schema_version':1,'principals':[
        {'id':'team-author','kind':'user','subject':'author-subject','grants':[grant('author')]},
        {'id':'team-publisher','kind':'user','subject':'publisher-subject','grants':[grant('publisher')]},
        {'id':'team-consumer','kind':'user','subject':'consumer-subject','grants':[grant('consumer')]}]}


def test_identity_mapping_has_explicit_entitlements_and_stable_hash():
    IdentityMapping,_=models();value=IdentityMapping.model_validate(mapping())
    assert value.principals[0].grants[0].entitlements['groups']==('core',)
    assert value.digest==IdentityMapping.model_validate(mapping()).digest
    assert 'author-subject' not in repr(value)


@pytest.mark.parametrize('change',['author_publisher','duplicate_identity','duplicate_subject','duplicate_scope','reserved_fixture','boolean_version','unknown_secret'])
def test_identity_mapping_fails_closed(change):
    IdentityMapping,_=models();raw=mapping()
    if change=='author_publisher':raw['principals'][0]['grants'][0]['roles']=['author','publisher']
    if change=='duplicate_identity':raw['principals'][1]['id']=raw['principals'][0]['id']
    if change=='duplicate_subject':raw['principals'][1]['subject']=raw['principals'][0]['subject']
    if change=='duplicate_scope':raw['principals'][0]['grants']*=2
    if change=='reserved_fixture':raw['principals'][0]['id']='fixture-pretend'
    if change=='boolean_version':raw['schema_version']=True
    if change=='unknown_secret':raw['principals'][0]['token']='token'
    with pytest.raises(ValueError):IdentityMapping.model_validate(raw)


def test_seed_identity_pins_grants_and_issuer_without_logging_subject():
    from gateway.source_pilot_bootstrap import IdentityMapping,seed_identity
    from types import SimpleNamespace
    values=IdentityMapping.model_validate(mapping())
    seed=SimpleNamespace(policy_id='usage',model_dump=lambda **kwargs:{'policy_id':'usage','revision':1})
    first=seed_identity(seed,values,'https://id.example.com')
    assert first.startswith('source-seed-v1-')
    assert seed_identity(seed,values,'https://other.example.com')!=first
    changed=mapping();changed['principals'][0]['grants'][0]['entitlements']['groups']=['special']
    assert seed_identity(seed,IdentityMapping.model_validate(changed),'https://id.example.com')!=first


def test_existing_immutable_row_mismatch_fails_and_revocation_is_not_compared():
    from gateway.source_pilot_bootstrap import verify_principal
    from types import SimpleNamespace
    expected=SimpleNamespace(id='user',kind='user',subject='hash',service_client_id=None)
    verify_principal(SimpleNamespace(id='user',kind='user',subject='hash',service_client_id=None,active=False),expected)
    with pytest.raises(ValueError):verify_principal(SimpleNamespace(id='user',kind='user',subject='changed',service_client_id=None),expected)


def test_bootstrap_plan_command_reads_no_credentials_and_wrong_digest_never_provisions(tmp_path,monkeypatch,capsys):
    from gateway.source_pilot_builder import build_weekly_files
    from gateway.source_pilot_bootstrap import main
    from tests.unit.test_source_pilot_builder import source,write_files
    import gateway.source_pilot_config as config_module
    files=build_weekly_files(consumer_source=source('consumer'),fixture_source=source('fixture_test'),
        consumer_definition_digest='d'*64,fixture_definition_digest='e'*64)
    write_files(tmp_path,files);monkeypatch.setenv('GATEWAY_SOURCE_PILOT_CONFIG_FILE',str(tmp_path/'pilot.json'))
    monkeypatch.setattr(config_module,'_read_password',lambda *a:pytest.fail('Offline plan read credentials'))
    assert main(['plan'])==0
    import json
    assert len(json.loads(capsys.readouterr().out)['plan_digest'])==64
    assert main(['provision','--expected-plan-digest','0'*64])==1


def test_explicit_service_hash_validation_rejects_unmapped_and_fixture_credentials(monkeypatch):
    import gateway.source_pilot_bootstrap as bootstrap
    from types import SimpleNamespace
    raw=mapping();raw['principals'][2]={'id':'team-consumer','kind':'service','service_client_id':'approved-client','grants':raw['principals'][2]['grants']}
    identities=bootstrap.IdentityMapping.model_validate(raw)
    config=SimpleNamespace(credential_hash_file='/run/auth/credential-hashes.json')
    def file(principal,kind='service',active=True):return {'version':1,'credentials':[{'digest':'a'*64,'principal_id':principal,'kind':kind,'active':active}]}
    values=[file('other'),file('fixture-test','demo'),file('team-consumer',active=False)]
    import gateway.pilot_config as files
    import json
    monkeypatch.setattr(files,'_read_file',lambda *a:json.dumps(values.pop(0)).encode())
    with pytest.raises(ValueError):bootstrap.validate_service_credentials(identities,config)
    with pytest.raises(ValueError):bootstrap.validate_service_credentials(identities,config)
    bootstrap.validate_service_credentials(identities,config)


def test_retained_versions_may_share_one_exact_policy_seed(tmp_path):
    from gateway.source_pilot_bootstrap import validate_identity_mapping
    from tests.unit.test_source_pilot_render import prepared
    config,assets=prepared(tmp_path)
    retained=assets.packages.packages[0]
    assert validate_identity_mapping(assets.identities,config,(retained,retained)).digest==assets.identities.digest
    from dataclasses import replace
    from gateway.contracts.canonical import json_value
    changed=json_value(retained.assets);changed['policy_seed_ref']['revision']=2
    with pytest.raises(ValueError):validate_identity_mapping(assets.identities,config,(retained,replace(retained,assets=changed)))
