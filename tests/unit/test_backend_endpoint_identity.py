"""Synthetic Docker-inspect controls, never evidence of a real backend pass."""
import json
from types import SimpleNamespace

import pytest

from scripts import verify_demo

pytestmark = pytest.mark.unit
HOST_PORT = '49152'


@pytest.fixture
def backend_endpoints(tmp_path, monkeypatch):
    images = {service: {'image_id': 'sha256:' + str(number) * 64}
        for number, service in enumerate(('registry-db', 'cube', 'cube-test', 'demo-db'), 1)}
    records = {}
    hosts = {'kind': 'private-synthetic-cube-only', 'run_label': 'synthetic-cube-local',
        'hosts': {}, 'container_ids': {}}
    for number, service in enumerate(images, 1):
        identifier = str(number) * 64
        records[identifier] = {'Id': identifier, 'Image': images[service]['image_id'],
            'Config': {'Labels': {'gateway.cube.run': hosts['run_label']}},
            'NetworkSettings': {'Networks': {'private': {'IPAddress': f'172.23.0.{number}'}}}}
        if service != 'registry-db':
            hosts['hosts'][service] = f'172.23.0.{number}'
            hosts['container_ids'][service] = identifier
    postgres = records['1' * 64]
    postgres['Config']['Labels'] = {'gateway.registry.run': 'synthetic-registry-local'}
    postgres['HostConfig'] = {'PortBindings': {
        '5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': HOST_PORT}]}}
    postgres['NetworkSettings']['Ports'] = {
        '5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': HOST_PORT}]}
    hosts_file = tmp_path / 'hosts.json'
    hosts_file.write_text(json.dumps(hosts))
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_AUTHORIZATION', 'disposable-local-postgres-only')
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_ADMIN_URL',
        f'postgresql+psycopg://gateway_test_admin:synthetic@127.0.0.1:{HOST_PORT}/gateway_test_admin')
    monkeypatch.setenv('GATEWAY_CUBE_TEST_AUTHORIZATION', 'private-synthetic-cube-only')
    monkeypatch.setenv('GATEWAY_CUBE_TEST_HOSTS_FILE', str(hosts_file))

    def docker(command, *, timeout):
        assert timeout == 15
        if command == ['docker', 'ps', '--quiet', '--filter', 'publish=' + HOST_PORT]:
            return SimpleNamespace(stdout='1' * 64 + '\n')
        assert command[:2] == ['docker', 'inspect'] and len(command) == 3
        return SimpleNamespace(stdout=json.dumps([records[command[2]]]))

    monkeypatch.setattr(verify_demo, 'run', docker)
    return images, postgres, {'registry-db': '1' * 64, **hosts['container_ids']}


@pytest.mark.parametrize('requested_port', ['', HOST_PORT], ids=['ephemeral', 'explicit'])
def test_registry_admits_requested_loopback_binding_with_exact_assigned_port(backend_endpoints, requested_port):
    images, postgres, identities = backend_endpoints
    # Docker preserves the empty requested HostPort from --publish 127.0.0.1::5432;
    # only NetworkSettings.Ports contains the host port allocated at startup.
    postgres['HostConfig']['PortBindings']['5432/tcp'][0]['HostPort'] = requested_port
    assert verify_demo.validate_backend_endpoints(images) == identities


@pytest.mark.parametrize('surface', ['requested', 'actual'])
@pytest.mark.parametrize('defect', ['public-ip', 'unspecified-ip', 'ipv6-ip', 'wrong-port',
    'extra-port', 'extra-binding', 'missing-binding'])
def test_registry_rejects_widened_or_mismatched_binding_on_either_surface(backend_endpoints, surface, defect):
    images, postgres, _ = backend_endpoints
    bindings = (postgres['HostConfig']['PortBindings'] if surface == 'requested'
        else postgres['NetworkSettings']['Ports'])
    binding = bindings['5432/tcp'][0]
    if defect.endswith('-ip'):
        binding['HostIp'] = {'public-ip': '0.0.0.0', 'unspecified-ip': '', 'ipv6-ip': '::'}[defect]
    elif defect == 'wrong-port':
        binding['HostPort'] = '49153'
    elif defect == 'extra-port':
        bindings['5433/tcp'] = [dict(binding)]
    elif defect == 'extra-binding':
        bindings['5432/tcp'].append(dict(binding))
    else:
        bindings.clear()
    with pytest.raises(ValueError, match='Registry loopback identity mismatch'):
        verify_demo.validate_backend_endpoints(images)


def test_registry_rejects_unallocated_actual_port(backend_endpoints):
    images, postgres, _ = backend_endpoints
    postgres['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort'] = ''
    with pytest.raises(ValueError, match='Registry loopback identity mismatch'):
        verify_demo.validate_backend_endpoints(images)
