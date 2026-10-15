import json
import pytest
from tests.unit.test_source_pilot_bigquery import source
from tests.unit.test_source_pilot_builder import write_files

pytestmark=pytest.mark.unit


def prepared(tmp_path):
    from gateway.source_pilot_builder import build_weekly_files
    from gateway.source_pilot_config import SourcePilotConfig
    from gateway.source_pilot_assets import load_source_assets
    from gateway.source_pilot_bigquery_render import example_bigquery_deployment
    from gateway.source_pilot_render import _example_base
    import hashlib,certifi
    from pathlib import Path
    files=build_weekly_files(consumer_source=source('consumer'),fixture_source=source('fixture_test'))
    write_files(tmp_path,files);config=SourcePilotConfig.model_validate(files['pilot.json']);assets=load_source_assets(config,tmp_path)
    base=_example_base(config,assets)
    text=Path(certifi.where()).read_text();start=text.index('-----BEGIN CERTIFICATE-----');end=text.index('-----END CERTIFICATE-----',start)+len('-----END CERTIFICATE-----')
    ca=(text[start:end]+'\n').encode();(tmp_path/'registry-ca.pem').write_bytes(ca)
    base['registry_ca_ref']={'path':'registry-ca.pem','sha256':hashlib.sha256(ca).hexdigest()}
    return config,assets,example_bigquery_deployment(config,assets,base)


def test_bigquery_renderer_complete_contexts_tls_observers_and_exact_node_envelopes(tmp_path):
    from gateway.source_pilot_render import render
    config,assets,plan=prepared(tmp_path);output=render(config,assets,plan,root=tmp_path)
    base=output['base.json']['items'];active=output['activation.json']['items']
    deployments=[x for x in base if x['kind']=='Deployment']
    assert len(deployments)==8 and all(x['spec']['replicas']==0 for x in deployments)
    assert all(x['spec']['replicas']==1 for x in active if x['kind']=='Deployment')
    source_deploys=[x for x in deployments if x['metadata']['name'].startswith('spaceport-bq-')]
    assert len(source_deploys)==4
    for deployment in source_deploys:
        containers=deployment['spec']['template']['spec']['containers'];node=containers[0]
        assert 'command' not in node and node['args'][0].startswith('/cube/bigquery-runtime/')
        assert all(e['name'] not in {'HTTP_PROXY','GOOGLE_APPLICATION_CREDENTIALS','CUBEJS_PORT'} for e in node['env'])
        if 'observer' in deployment['metadata']['name']:
            assert len(containers)==1 and 'cube_signing_key' not in json.dumps(deployment)
        else:
            assert len(containers)==2 and node['readinessProbe']=={'tcpSocket':{'port':4000},'periodSeconds':10}
    maps=[x['data']['payload'] for x in base if x['kind']=='ConfigMap']
    envelopes=[json.loads(v) for v in maps if v.startswith('{') and '"bundle_ref"' in v]
    assert len(envelopes)==4
    for envelope in envelopes:
        assert envelope['deployment_epoch']==config.deployment_epoch
        assert envelope['profile']['model_bundle_digest']==envelope['bundle_ref']['digest']
        if 'observer_auth' in envelope:
            assert 'signing_key_file' not in envelope and 'root' not in envelope['bundle_ref']
        else:assert envelope['bundle_ref']['root']=='/run/source/bundle'


@pytest.mark.parametrize('change',['missing_source','observer_data_gsa','shared_runtime_login','signing_mismatch','extra_secret','unreviewed_image'])
def test_bigquery_renderer_rejects_cross_context_authority_and_incomplete_inventory(tmp_path,change):
    from gateway.source_pilot_render import render
    config,assets,plan=prepared(tmp_path)
    first=plan['sources'][0]
    if change=='missing_source':plan['sources'].pop()
    if change=='observer_data_gsa':first['observer_principal']=assets.sources.sources[0].auth.expected_principal
    if change=='shared_runtime_login':plan['sources'][1]['runtime_login']=first['runtime_login']
    if change=='signing_mismatch':next(s for s in first['runtime']['secrets'] if s['target']=='/run/source/secrets/cube_signing_key')['name']='wrong-key-v1'
    if change=='extra_secret':first['observer']['secrets'].append({'name':'extra-v1','key':'token','target':'/run/source/secrets/cube_signing_key'})
    if change=='unreviewed_image':plan['bigquery_image']='cube:latest'
    with pytest.raises(ValueError):render(config,assets,plan,root=tmp_path)
