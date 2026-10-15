import importlib.util
import json
import pytest
from tests.unit.test_source_pilot_builder import source,write_files

pytestmark=pytest.mark.unit


def prepared(tmp_path):
    from gateway.source_pilot_builder import build_weekly_files
    from gateway.source_pilot_config import SourcePilotConfig
    from gateway.source_pilot_assets import load_source_assets
    files=build_weekly_files(consumer_source=source('consumer'),fixture_source=source('fixture_test'),
        consumer_definition_digest='d'*64,fixture_definition_digest='e'*64)
    write_files(tmp_path,files);config=SourcePilotConfig.model_validate(files['pilot.json'])
    return config,load_source_assets(config,tmp_path)


def test_render_complete_domo_workloads_with_explicit_activation_and_no_credentials(tmp_path):
    assert importlib.util.find_spec('gateway.source_pilot_render') is not None,'GKE renderer missing'
    from gateway.source_pilot_render import render,example_deployment
    config,assets=prepared(tmp_path);plan=example_deployment(config,assets)
    import hashlib,certifi
    from pathlib import Path
    text=Path(certifi.where()).read_text();start=text.index('-----BEGIN CERTIFICATE-----');end=text.index('-----END CERTIFICATE-----',start)+len('-----END CERTIFICATE-----')
    ca=(text[start:end]+'\n').encode();(tmp_path/'registry-ca.pem').write_bytes(ca)
    plan['registry_ca_ref']={'path':'registry-ca.pem','sha256':hashlib.sha256(ca).hexdigest()}
    files=render(config,assets,plan,root=tmp_path)
    base=files['base.json']['items'];active=files['activation.json']['items']
    deploys=[x for x in base if x['kind']=='Deployment']
    assert {d['metadata']['name'] for d in deploys}=={'spaceport-api','spaceport-worker','spaceport-retention','spaceport-edge'}
    assert all(d['spec']['replicas']==0 for d in deploys)
    assert all(d['spec']['replicas']==1 for d in active if d['kind']=='Deployment')
    assert all(x['spec']['suspend'] for x in base if x['kind']=='CronJob')
    assert not any(x['kind'] in {'Secret','ServiceAccount','Ingress','RoleBinding','ClusterRoleBinding'} for x in base)
    wire=json.dumps(files)
    assert 'HTTP_PROXY' not in wire and '0.0.0.0/0' not in wire
    api=next(d for d in deploys if d['metadata']['name']=='spaceport-api')
    worker=next(d for d in deploys if d['metadata']['name']=='spaceport-worker')
    assert 'domo_fixture_test_secret' in json.dumps(api) and 'domo_consumer_secret' not in json.dumps(api)
    assert 'domo_consumer_secret' in json.dumps(worker) and 'domo_fixture_test_secret' not in json.dumps(worker)
    assert all('subPath' in m and m['readOnly'] for m in api['spec']['template']['spec']['containers'][0]['volumeMounts'] if m['mountPath']!='/tmp')


def valid_plan(tmp_path):
    from gateway.source_pilot_render import example_deployment
    import hashlib,certifi
    from pathlib import Path
    config,assets=prepared(tmp_path);plan=example_deployment(config,assets)
    text=Path(certifi.where()).read_text();start=text.index('-----BEGIN CERTIFICATE-----');end=text.index('-----END CERTIFICATE-----',start)+len('-----END CERTIFICATE-----')
    ca=(text[start:end]+'\n').encode();(tmp_path/'registry-ca.pem').write_bytes(ca)
    plan['registry_ca_ref']={'path':'registry-ca.pem','sha256':hashlib.sha256(ca).hexdigest()}
    return config,assets,plan


@pytest.mark.parametrize('change',['extra_secret','missing_secret','cross_role_secret','mutable_image','ca_hash','broad_cidr','universal_ipv6'])
def test_render_refuses_unreviewed_mount_image_ca_and_network_changes(tmp_path,change):
    from gateway.source_pilot_render import render
    config,assets,plan=valid_plan(tmp_path)
    if change=='extra_secret':plan['workloads']['api']['secrets'].append({'name':'unapproved-v1','key':'extra','target':'/run/secrets/unapproved'})
    if change=='missing_secret':plan['workloads']['api']['secrets'].pop()
    if change=='cross_role_secret':plan['workloads']['api']['secrets']=plan['workloads']['worker']['secrets']
    if change=='mutable_image':plan['gateway_image']='example.invalid/gateway:latest'
    if change=='ca_hash':plan['registry_ca_ref']['sha256']='0'*64
    if change=='broad_cidr':plan['egress']['destination_cidrs']=['10.0.0.0/8']
    if change=='universal_ipv6':plan['egress']['destination_cidrs']=['::/0']
    with pytest.raises(ValueError):render(config,assets,plan,root=tmp_path)


def test_renderer_requires_complete_bigquery_workload_plan(tmp_path):
    from gateway.source_pilot_render import render
    from dataclasses import replace
    from gateway.product_packages import SourceCatalog
    from tests.unit.test_source_pilot_bigquery import source as bq_source
    config,assets,plan=valid_plan(tmp_path)
    bq=SourceCatalog([bq_source('consumer'),bq_source('fixture_test')])
    with pytest.raises(ValueError):render(config,replace(assets,sources=bq),plan,root=tmp_path)
