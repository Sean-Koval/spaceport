import json
import pytest
from scripts.spaceport import main

pytestmark=pytest.mark.unit


def test_offline_init_then_build_and_validate_needs_no_token_or_source(tmp_path,monkeypatch,capsys):
    import socket
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('offline network'))
    inputs=tmp_path/'inputs';output=tmp_path/'configured'
    assert main(['pilot','init','--provider','domo','--directory',str(inputs)])==0
    assert (inputs/'setup-input.json').is_file()
    assert main(['pilot','build','--input-file',str(inputs/'setup-input.json'),'--directory',str(output)])==0
    assert (output/'pilot.json').is_file() and (output/'source-check.json').is_file()
    assert main(['pilot','validate','--config',str(output/'pilot.json')])==0
    status=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert status['config_valid'] is True and status['source_observation']=='unverified'
    assert main(['pilot','init','--provider','domo','--directory',str(inputs)])==2


def test_build_does_not_accept_credentials_or_arbitrary_sql(tmp_path,capsys):
    inputs=tmp_path/'inputs';output=tmp_path/'configured'
    assert main(['pilot','init','--provider','domo','--directory',str(inputs)])==0
    path=inputs/'setup-input.json';value=json.loads(path.read_text());value['sql']='SELECT anything'
    path.write_text(json.dumps(value))
    assert main(['pilot','build','--input-file',str(path),'--directory',str(output)])==2
    assert not output.exists()
    assert 'SELECT anything' not in capsys.readouterr().out


def test_consumer_expected_rows_are_typed_and_repin_only_consumer_recipe():
    from scripts.spaceport_setup import template,build,WeeklySetupInput
    from gateway.product_packages import ProductPackage
    value=template('domo').model_dump(mode='json');before=build(value)
    value['consumer_expected_rows']=[{'day':'2026-10-05','account':'approved-live-account','usage_events':'17'}]
    after=build(value)
    assert before['fixtures.json']==after['fixtures.json']
    assert before['products.json']!=after['products.json']
    assert before['source-check.json']['configured_package_digest']!=after['source-check.json']['configured_package_digest']
    assert after['source-check.json']['expected_rows']==value['consumer_expected_rows']
    assert after['source-check.json']['configured_package_digest']==ProductPackage.model_validate(after['package.json']).digest
    value['consumer_expected_rows'][0]['usage_events']=17
    with pytest.raises(ValueError):WeeklySetupInput.model_validate(value)


def test_bigquery_offline_init_build_validate_has_retained_models_and_no_provider_io(tmp_path,monkeypatch,capsys):
    from scripts.spaceport import main
    import socket,json
    monkeypatch.setattr(socket,'create_connection',lambda *a,**k:pytest.fail('Offline source I/O'))
    assert main(['pilot','init','--provider','bigquery','--directory',str(tmp_path/'input')])==0
    assert main(['pilot','build','--input-file',str(tmp_path/'input'/'setup-input.json'),'--directory',str(tmp_path/'package')])==0
    assert main(['pilot','validate','--config',str(tmp_path/'package'/'pilot.json')])==0
    bundle=json.loads((tmp_path/'package'/'bundles.json').read_text())
    assert len(bundle['cube_bundles'])==2
    plan=json.loads((tmp_path/'package'/'deployment.example.json').read_text())
    assert len(plan['sources'])==2 and 'observer_principal' in plan['sources'][0]
    assert 'unverified' in capsys.readouterr().out


@pytest.mark.parametrize('change',['duplicate_grain','outside_window','too_many_rows'])
def test_consumer_recipe_rejects_impossible_fixed_template_answers(change):
    from scripts.spaceport_setup import template,WeeklySetupInput
    value=template('domo').model_dump(mode='json')
    if change=='duplicate_grain':value['consumer_expected_rows']=[value['consumer_expected_rows'][0]]*2
    if change=='outside_window':value['consumer_expected_rows'][0]['day']='2026-10-12'
    if change=='too_many_rows':value['consumer_expected_rows']=[{'day':'2026-10-05','account':str(i),'usage_events':'1'} for i in range(101)]
    with pytest.raises(ValueError):WeeklySetupInput.model_validate(value)
