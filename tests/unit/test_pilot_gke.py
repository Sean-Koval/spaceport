"""Inspectable manifest contracts, not Kubernetes schema or live GKE validation."""
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2] / 'infra' / 'gke'
APP_SECRETS = {'registry_app', 'cube_signing_key', 'cube_test_signing_key', 'audit_hmac', 'cursor_key'}


def objects(name):
    value = json.loads((ROOT / name).read_text())
    return value['items'] if value['kind'] == 'List' else [value]


def pods():
    return [obj for filename in ('workloads.reference.yaml', 'bootstrap.reference.yaml') for obj in objects(filename)]


def test_workloads_and_jobs_remain_inert_and_privilege_separated():
    workloads = objects('workloads.reference.yaml')
    assert {obj['metadata']['name'] for obj in workloads} == {'spaceport-api', 'spaceport-worker', 'spaceport-retention'}
    assert all(obj['kind'] == 'Deployment' and obj['spec']['replicas'] == 0 for obj in workloads)
    jobs = objects('bootstrap.reference.yaml')
    assert {obj['metadata']['name'] for obj in jobs} == {'spaceport-migrate', 'spaceport-provision'}
    assert all(obj['kind'] == 'Job' and obj['spec']['suspend'] is True for obj in jobs)
    for obj in workloads + jobs:
        assert 'REPLACE_' in obj['metadata']['namespace']
        pod = obj['spec']['template']['spec']
        assert pod['automountServiceAccountToken'] is False
        assert 'REPLACE_' in pod['serviceAccountName']
        assert pod['securityContext'] == {'runAsNonRoot': True, 'runAsUser': 65532, 'runAsGroup': 65532,
                                          'fsGroup': 65532, 'seccompProfile': {'type': 'RuntimeDefault'}}
        for container in pod['containers'] + pod['initContainers']:
            assert '@sha256:REPLACE_' in container['image']
            assert container['securityContext'] == {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                                     'capabilities': {'drop': ['ALL']}}
            assert container['resources']['requests'] and container['resources']['limits']
        runtime = pod['containers'][0]
        assert runtime['env'] == [{'name': 'GATEWAY_PILOT_CONFIG_FILE', 'value': '/run/pilot/config/pilot.json'}]
        assert not any(mount['mountPath'].startswith('/mnt/pilot') for mount in runtime['volumeMounts'])
        for mount in runtime['volumeMounts']:
            if mount['name'] == 'snapshot':
                assert mount['readOnly'] is True
                assert mount['subPath'].startswith('runtime/')
        assert not any(mount['mountPath'] == '/run/auth' for mount in runtime['volumeMounts'])
        snapshot = next(volume for volume in pod['volumes'] if volume['name'] == 'snapshot')
        assert snapshot['emptyDir'] == {'medium': 'Memory', 'sizeLimit': '8Mi'}
        secret_volume = next(volume for volume in pod['volumes'] if volume['name'] == 'projected-secrets')
        assert secret_volume['csi']['driver'] == 'secrets-store-gke.csi.k8s.io'
        assert secret_volume['csi']['readOnly'] is True
        assert runtime['command'][0] == 'python'
        assert all('pilot_bootstrap' not in part for part in runtime['command']) == (obj['kind'] == 'Deployment')


def test_secret_provider_classes_have_exact_bounded_role_files_and_pinned_versions():
    classes = objects('secret-providers.reference.yaml')
    expected = {'api': APP_SECRETS, 'worker': APP_SECRETS,
                'retention': {'registry_app', 'registry_audit_retention'}, 'bootstrap': {'registry_owner'}}
    assert len(classes) == len(expected)
    for obj in classes:
        role = obj['metadata']['labels']['spaceport-role']
        assert obj['spec']['provider'] == 'gke'
        # JSON is YAML, including the provider's string-valued YAML list.
        secrets = json.loads(obj['spec']['parameters']['secrets'])
        assert {secret['path'] for secret in secrets} == expected[role]
        assert all(secret['resourceName'].startswith('projects/REPLACE_') for secret in secrets)
        assert all('/versions/REPLACE_WITH_PINNED_' in secret['resourceName'] for secret in secrets)
        assert 'secretObjects' not in obj['spec']
    assert 'registry_owner' not in json.dumps(objects('workloads.reference.yaml'))


def test_private_service_and_default_deny_do_not_create_public_ingress():
    service, = objects('service.reference.yaml')
    assert service['kind'] == 'Service' and service['spec']['type'] == 'ClusterIP'
    policy, = objects('network-policy.reference.yaml')
    assert policy['spec']['policyTypes'] == ['Ingress', 'Egress']
    assert policy['spec']['ingress'] == [] and policy['spec']['egress'] == []
    for path in ROOT.glob('*.yaml'):
        assert all(obj['kind'] not in {'Ingress', 'Gateway', 'ServiceAccount', 'Secret', 'ClusterRoleBinding'} for obj in objects(path.name))


def test_termination_grace_covers_inflight_work_and_owned_resource_cleanup():
    for obj in pods():
        assert obj['spec']['template']['spec']['terminationGracePeriodSeconds'] >= 140


def test_reference_edge_has_exact_metadata_route_before_frontend_fallback_and_tls_only():
    text = (ROOT / 'edge-tls.reference.conf').read_text()
    exact = 'location = /.well-known/oauth-protected-resource/mcp'
    assert exact in text
    assert text.index(exact) < text.index('location / {')
    assert 'listen 8443 ssl;' in text and 'listen 80;' not in text
    assert 'ssl_certificate ' in text and 'ssl_certificate_key ' in text
    assert 'proxy_pass http://spaceport-api:8000;' in text
    assert 'proxy_set_header Host spaceport.example.com;' in text
    for header in ('X-Forwarded-For', 'X-Forwarded-Host', 'X-Forwarded-Proto'):
        assert f'proxy_set_header {header} "";' in text


def test_edge_checks_raw_authority_before_normalizing_upstream_host():
    text = (ROOT / 'edge-tls.reference.conf').read_text()
    assert 'map $http_host $pilot_authority_valid {' in text
    assert 'default 0;' in text
    assert '"spaceport.example.com" 1;' in text
    assert '"spaceport.example.com:443" 1;' in text
    assert 'if ($pilot_authority_valid = 0) { return 444; }' in text
    # Retain the parsed/request-line hostname guard as well as the raw header check.
    assert 'if ($host != spaceport.example.com) { return 444; }' in text
    assert 'proxy_set_header Forwarded "";' in text


def test_fictional_config_and_mapping_preserve_pilot_boundaries():
    config = json.loads((ROOT / 'pilot.example.json').read_text())
    identities = json.loads((ROOT / 'identities.example.json').read_text())
    assert config['public_origin'] == 'https://spaceport.example.com'
    assert config['oidc']['audience'] == config['public_origin'] + '/mcp'
    assert config['profile'] == 'oidc-synthetic-pilot'
    assert config['registry']['ca_file'] == '/run/pilot/config/registry-ca.pem'
    assert config['cube_consumer_url'] != config['cube_fixture_url']
    assert config['credential_hash_file'] is None
    principals = identities['principals']
    assert all(principal['kind'] == 'user' and len(principal['grants']) == 1 for principal in principals)
    assert any(principal['id'] == config['probe_principal_id'] and principal['grants'][0]['roles'] == ['consumer'] for principal in principals)
    assert all(set(principal['grants'][0]['roles']) <= {'consumer', 'author', 'publisher'} for principal in principals)
    assert not any({'author', 'publisher'} <= set(principal['grants'][0]['roles']) for principal in principals)
