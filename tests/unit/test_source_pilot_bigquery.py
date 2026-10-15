import importlib.util
import pytest
from gateway.adapters.bigquery_runtime import BigQuerySource

pytestmark=pytest.mark.unit


def source(context='consumer'):
    return BigQuerySource(provider='bigquery',source_id='usage-'+context.replace('_','-'),revision=1,context=context,
        auth={'mode':'gke-workload-identity','expected_principal':('usage-reader' if context=='consumer' else 'usage-fixture')+'@pilot-project.iam.gserviceaccount.com'},
        billing_project='pilot-project',data_project='pilot-project',dataset='consumer' if context=='consumer' else 'fixtures',
        tables=['usage'],location='US',dataset_location='US',cube_endpoint='https://'+('usage' if context=='consumer' else 'fixture')+'.example.com',
        model_bundle_digest='0'*64,source_policy_digest='a'*64,max_jobs_per_execution=2,per_job_maximum_bytes_billed=10000000,
        total_execution_bytes=20000000,max_active_jobs=1,max_global_active_jobs=2,job_timeout_ms=30000)


def test_bigquery_weekly_assets_pin_static_model_source_security_and_unknown_freshness():
    assert importlib.util.find_spec('gateway.source_pilot_bigquery') is not None,'BQ package builder missing'
    from gateway.source_pilot_bigquery import build_weekly_bigquery_assets,CubeBundleFiles
    from gateway.adapters.bigquery_runtime import bigquery_security_contract,compile_bigquery_query
    from gateway.source_pilot_samples import fixed_decisions,WINDOW
    from gateway.contracts.query import QueryRequest
    from gateway.contracts.canonical import json_value
    import json
    ca=build_weekly_bigquery_assets(source('consumer'));fa=build_weekly_bigquery_assets(source('fixture_test'))
    assert ca.binding.source_digest==ca.source.digest
    assert ca.binding.model_bundle_digest==ca.source.model_bundle_digest==ca.bundle.digest
    assert ca.bundle.digest!=fa.bundle.digest
    assert ca.binding.freshness_mode=='unknown' and ca.binding.checkpoint_member is None
    assert ca.manifest.freshness.required is False
    assert json.loads(ca.bundle.security_content)==bigquery_security_contract(ca.binding)
    assert 'FROM `pilot-project.consumer.usage`' in ca.bundle.model_files['usage.yml']
    assert 'CAST(SUM({CUBE}.usage_events) AS STRING)' in ca.bundle.model_files['usage.yml']
    query=compile_bigquery_query(ca.binding,ca.manifest,QueryRequest(product_id=ca.manifest.product_id,
        version=ca.manifest.version,inputs=dict(WINDOW)),fixed_decisions('consumer')[0])
    assert query is not None
    changed=ca.bundle.model_dump(mode='json');changed['model_files']['usage.yml']+='\n'
    with pytest.raises(ValueError):CubeBundleFiles.model_validate(changed)
    changed=ca.bundle.model_dump(mode='json');changed['model_files']['bad.js']='malicious'
    with pytest.raises(ValueError):CubeBundleFiles.model_validate(changed)


@pytest.mark.parametrize('change',['context','interpolation','filter','path','unicode_size','multiple_tables'])
def test_bigquery_static_bundle_refuses_dynamic_or_unbounded_models(change):
    from gateway.source_pilot_bigquery import build_weekly_bigquery_assets,CubeBundleFiles,cube_bundle_digest
    value=source()
    if change=='multiple_tables':
        value=BigQuerySource.model_validate({**value.model_dump(mode='json'),'tables':['usage','other']})
        with pytest.raises(ValueError):build_weekly_bigquery_assets(value)
        return
    raw=build_weekly_bigquery_assets(value).bundle.model_dump(mode='json')
    if change=='context':raw['model_files']['usage.yml']+='\n# securityContext'
    if change=='interpolation':raw['model_files']['usage.yml']+='\n# ${danger}'
    if change=='filter':raw['model_files']['usage.yml']+='\n# FILTER_PARAMS'
    if change=='path':raw['model_files']['../usage.yml']=raw['model_files'].pop('usage.yml')
    if change=='unicode_size':raw['model_files']['usage.yml']='é'*32769
    raw['digest']=cube_bundle_digest(raw['security_content'],raw['model_files'])
    with pytest.raises(ValueError):CubeBundleFiles.model_validate(raw)


def test_bundle_envelope_duplicate_member_is_rejected_at_file_parse_boundary():
    from gateway.pilot_config import _parse_json
    with pytest.raises(ValueError):_parse_json(b'{"model_files":{"usage.yml":"one","usage.yml":"two"}}')


def test_complete_bigquery_package_composes_exact_sources_bindings_bundles_and_plan(tmp_path):
    from gateway.source_pilot_builder import build_weekly_files
    from gateway.source_pilot_assets import load_source_assets
    from gateway.source_pilot_config import SourcePilotConfig
    from tests.unit.test_source_pilot_builder import write_files
    files=build_weekly_files(consumer_source=source('consumer'),fixture_source=source('fixture_test'))
    write_files(tmp_path,files);config=SourcePilotConfig.model_validate(files['pilot.json'])
    assets=load_source_assets(config,tmp_path)
    assert len(assets.cube_bundles)==2 and not assets.statements
    assert {b.adapter_revision for b in assets.bindings}=={'cube-bigquery-v1'}
    assert files['source-check.json']['configured_source_digest']==assets.packages.packages[0].consumer_source.digest
    assert len(config.source_role_mappings)==4
    assert {r.login_name for r in config.source_role_mappings if r.role_kind=='observer'}=={'gateway_bigquery_observer'}


def replace_value(value,old,new):
    if isinstance(value,dict):return {k:replace_value(v,old,new) for k,v in value.items()}
    if isinstance(value,list):return [replace_value(v,old,new) for v in value]
    return new if value==old else value


@pytest.mark.parametrize('change',['security','bytes','fixture_source','missing_bundle','extra_bundle','endpoint'])
def test_repinned_bigquery_assets_fail_offline_before_credentials(tmp_path,monkeypatch,change):
    import json,socket
    from gateway.source_pilot_builder import build_weekly_files
    from gateway.source_pilot_assets import load_source_assets
    from gateway.source_pilot_config import SourcePilotConfig
    from gateway.source_pilot_bigquery import cube_bundle_digest
    from gateway.adapters.bigquery_runtime import BigQueryBinding,BigQuerySource
    from gateway.contracts.manifest import Manifest
    from gateway.publishing.test_runner import execution_contract_digest
    from tests.unit.test_source_pilot_builder import write_files,refresh
    import gateway.runtime_config
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('Offline provider I/O'))
    monkeypatch.setattr(gateway.runtime_config,'read_secret',lambda *a:pytest.fail('Offline credentials'))
    files=build_weekly_files(consumer_source=source('consumer'),fixture_source=source('fixture_test'))
    bundles=files['bundles.json'];bundle=bundles['cube_bundles'][0]
    if change=='security':
        old_digest=bundle['digest'];old_source=BigQuerySource.model_validate(files['consumer-source.json']).digest
        old_binding=BigQueryBinding.model_validate(files['consumer-binding.json']).digest
        contract=json.loads(bundle['security_content']);contract['tenant_member']='Usage.other_tenant'
        bundle['security_content']=json.dumps(contract)
        new_digest=cube_bundle_digest(bundle['security_content'],bundle['model_files'])
        files=replace_value(files,old_digest,new_digest)
        files=replace_value(files,old_source,BigQuerySource.model_validate(files['consumer-source.json']).digest)
        files=replace_value(files,old_binding,BigQueryBinding.model_validate(files['consumer-binding.json']).digest)
        raw=files['package.json']['manifest'];raw['content_digest']=None
        manifest=Manifest.model_validate(raw)
        files['package.json']['manifest']=manifest.model_dump(mode='json');files['manifest.json']=manifest.model_dump(mode='json')
        files['fixtures.json']['execution_contract_digest']=execution_contract_digest(manifest)
        for name in ('consumer-source.json','consumer-binding.json','fixtures.json','bundles.json','package.json'):refresh(files,name)
    elif change=='bytes':bundle['model_files']['usage.yml']+='\n';refresh(files,'bundles.json')
    elif change=='fixture_source':
        files['fixtures.json']['fixture_source']['auth']['expected_principal']='another-reader@pilot-project.iam.gserviceaccount.com';refresh(files,'fixtures.json')
    elif change=='missing_bundle':bundles['cube_bundles'].pop();refresh(files,'bundles.json')
    elif change=='extra_bundle':
        extra=json.loads(json.dumps(bundle));extra['model_files']['usage.yml']+='\n# extra'
        extra['digest']=cube_bundle_digest(extra['security_content'],extra['model_files']);bundles['cube_bundles'].append(extra);refresh(files,'bundles.json')
    else:bundles['endpoints']['consumer']['url']='https://different.example.com';refresh(files,'bundles.json')
    write_files(tmp_path,files)
    with pytest.raises(ValueError):load_source_assets(SourcePilotConfig.model_validate(files['pilot.json']),tmp_path)
