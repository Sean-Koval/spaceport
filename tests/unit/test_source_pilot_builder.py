"""The bounded builder writes reviewable fictional assets, never provider I/O."""
import importlib.util
import pytest
from gateway.adapters.domo_runtime import DomoSource

pytestmark=pytest.mark.unit


def source(context):
    return DomoSource(source_id='usage-'+context,revision=1,context=context,
        auth={'mode':'oauth-client-credentials','client_id_ref':'/run/secrets/domo_'+context+'_id',
            'client_secret_ref':'/run/secrets/domo_'+context+'_secret','client_id_sha256':('a' if context=='consumer' else 'b')*64,
            'expected_customer':'example-org','expected_user_id':1 if context=='consumer' else 2},
        dataset_ids=[('11111111-1111-1111-1111-111111111111' if context=='consumer' else '22222222-2222-2222-2222-222222222222')],
        source_policy_digest='c'*64,max_dispatches=100,max_active_sends=2)


def test_weekly_package_builder_produces_semantically_coherent_real_domo_assets(tmp_path):
    assert importlib.util.find_spec('gateway.source_pilot_builder') is not None,'Product package builder missing'
    from gateway.source_pilot_builder import build_weekly_files
    from gateway.source_pilot_assets import load_source_assets
    from gateway.source_pilot_config import SourcePilotConfig
    import json
    files=build_weekly_files(consumer_source=source('consumer'),fixture_source=source('fixture_test'),
        consumer_definition_digest='d'*64,fixture_definition_digest='e'*64)
    for name,value in files.items():(tmp_path/name).write_text(json.dumps(value,sort_keys=True,indent=2)+'\n')
    config=SourcePilotConfig.model_validate(files['pilot.json'])
    assets=load_source_assets(config,tmp_path)
    assert assets.packages.packages[0].package.manifest.product_id=='weekly-account-usage'
    assert len(assets.bindings)==2 and len(assets.statements)==2
    assert assets.fixtures.resolve(assets.packages.packages[0].package.manifest,assets.bindings[0]).fixture_binding.context=='fixture_test'


def write_files(tmp_path,files):
    from gateway.source_pilot_builder import encoded
    for name,value in files.items():(tmp_path/name).write_bytes(encoded(value))


def refresh(files,name):
    """Re-pin edited operator bytes, to exercise semantics beyond hash checks."""
    import hashlib
    from gateway.source_pilot_builder import encoded
    digest=hashlib.sha256(encoded(files[name])).hexdigest()
    changed=[]
    def walk(value):
        touched=False
        if isinstance(value,dict):
            if value.get('path')==name and 'sha256' in value and value['sha256']!=digest:
                value['sha256']=digest;touched=True
            for item in value.values():touched=walk(item) or touched
        elif isinstance(value,list):
            for item in value:touched=walk(item) or touched
        return touched
    for filename,value in files.items():
        if walk(value):changed.append(filename)
    for filename in changed:refresh(files,filename)


@pytest.mark.parametrize('change',['policy','fixture_principal','unsupported_consumer_request'])
def test_repinned_package_cannot_bypass_semantic_composition(tmp_path,change):
    from gateway.source_pilot_builder import build_weekly_files
    from gateway.source_pilot_assets import load_source_assets
    from gateway.source_pilot_config import SourcePilotConfig
    files=build_weekly_files(consumer_source=source('consumer'),fixture_source=source('fixture_test'),
        consumer_definition_digest='d'*64,fixture_definition_digest='e'*64)
    if change=='policy':
        files['policy.json']['content']['rules']['tenant_constraint']['member']='Other.tenant_id';refresh(files,'policy.json')
    elif change=='fixture_principal':
        files['fixtures.json']['cases'][0]['principal_id']='fixture-unmapped';refresh(files,'fixtures.json')
    else:
        files['package.json']['consumer_check']['request']['inputs']['end']='2026-10-11';refresh(files,'package.json')
    write_files(tmp_path,files)
    with pytest.raises(ValueError):load_source_assets(SourcePilotConfig.model_validate(files['pilot.json']),tmp_path)


def test_policy_limit_drift_rejected_before_any_source_or_secret_read(tmp_path,monkeypatch):
    import socket
    from gateway.source_pilot_builder import build_weekly_files
    from gateway.source_pilot_assets import load_source_assets
    from gateway.source_pilot_config import SourcePilotConfig
    import gateway.runtime_config
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('offline source socket'))
    monkeypatch.setattr(gateway.runtime_config,'read_secret',lambda *a,**k:pytest.fail('offline secret read'))
    files=build_weekly_files(consumer_source=source('consumer'),fixture_source=source('fixture_test'),
        consumer_definition_digest='d'*64,fixture_definition_digest='e'*64)
    files['policy.json']['content']['rules']['limits']['max_bytes']=32768;refresh(files,'policy.json');write_files(tmp_path,files)
    with pytest.raises(ValueError):load_source_assets(SourcePilotConfig.model_validate(files['pilot.json']),tmp_path)
