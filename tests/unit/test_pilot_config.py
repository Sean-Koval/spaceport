"""Offline-only operator pilot configuration tests; no real credentials or sockets."""
import importlib
import json
import os
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.unit


def api():
    return importlib.import_module('gateway.pilot_config')


def config_document():
    return {
        'schema_version': 1, 'profile': 'oidc-synthetic-pilot',
        'public_origin': 'https://spaceport.example.test',
        'oidc': {'issuer': 'https://issuer.example.test', 'audience': 'https://spaceport.example.test/mcp',
                 'jwks_url': 'https://issuer.example.test/keys', 'client_ids': ['studio-client']},
        'registry': {'host': 'registry.example.test', 'database': 'spaceport_registry',
                     'ca_file': '/run/secrets/registry_ca.pem'},
        'cube_consumer_url': 'https://cube.example.test',
        'cube_fixture_url': 'https://fixtures.example.test',
        'probe_principal_id': 'pilot-probe',
    }


def write_config(tmp_path, monkeypatch, value=None):
    path = tmp_path / 'pilot.json'
    path.write_text(json.dumps(config_document() if value is None else value))
    path.chmod(0o600)
    monkeypatch.setenv('GATEWAY_PILOT_CONFIG_FILE', str(path))
    return path


def test_valid_configuration_drives_identity_and_both_transports(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    config = api().load_pilot_config()
    assert config.profile == 'oidc-synthetic-pilot'
    assert config.probe_product_id == 'revenue'
    assert config.rest_allowed_hosts == ('spaceport.example.test',)
    assert config.allowed_origins == ('https://spaceport.example.test',)
    assert config.mcp_allowed_hosts == ('spaceport.example.test',)
    assert config.mcp_resource_url == 'https://spaceport.example.test/mcp'
    settings = config.identity_settings()
    assert settings.mode == 'production'
    assert settings.source == 'synthetic_postgres'
    assert settings.oidc_access_token_profile == 'rfc9068'
    assert settings.oidc_client_ids == ('studio-client',)
    assert settings.oidc_allowed_algorithms == ('RS256',)
    assert settings.credential_hash_file is None
    assert settings.trust_forwarded_headers is False


@pytest.mark.parametrize('key,value', [
    ('schema_version', True), ('schema_version', '1'), ('schema_version', 2),
    ('profile', 'production'), ('profile', 'demo'), ('probe_principal_id', ''),
    ('probe_principal_id', 'a/b'), ('credential_hash_file', '/tmp/hashes.json'),
    ('source', 'bigquery'), ('cube_consumer_digest', 'a' * 64),
])
def test_profile_forbids_implicit_or_expanded_authority(tmp_path, monkeypatch, key, value):
    document = config_document(); document[key] = value
    write_config(tmp_path, monkeypatch, document)
    with pytest.raises(ValueError, match='Pilot configuration is unavailable or invalid'):
        api().load_pilot_config()


@pytest.mark.parametrize('field', ['schema_version', 'profile', 'public_origin', 'oidc', 'registry',
                                  'cube_consumer_url', 'cube_fixture_url', 'probe_principal_id'])
def test_required_identity_and_endpoints_have_no_demo_fallback(tmp_path, monkeypatch, field):
    document = config_document(); del document[field]
    write_config(tmp_path, monkeypatch, document)
    with pytest.raises(ValueError): api().load_pilot_config()


@pytest.mark.parametrize('value', ['http://app.test', 'https://app.test/', 'https://app.test/path',
    'https://user:secret@app.test', 'https://app.test?x=1', 'https://app.test#x',
    'https://*.test', 'https://app.test:0', 'https://app.test:65536', 'https://app.test:',
    'https://app.test\\evil', 'https://app.test\n', 'https://%61pp.test', '//app.test'])
def test_public_origin_is_strict_https_origin(tmp_path, monkeypatch, value):
    document = config_document(); document['public_origin'] = value
    write_config(tmp_path, monkeypatch, document)
    with pytest.raises(ValueError): api().load_pilot_config()


@pytest.mark.parametrize('value', ['http://cube.test', 'https://cube.test/path',
    'https://user:secret@cube.test', 'https://cube.test?key=secret', 'https://*.test'])
def test_cube_endpoints_are_strict_https_bases(tmp_path, monkeypatch, value):
    document = config_document(); document['cube_consumer_url'] = value
    write_config(tmp_path, monkeypatch, document)
    with pytest.raises(ValueError): api().load_pilot_config()


def test_endpoint_canonicalization_rejects_same_cube_authority(tmp_path, monkeypatch):
    document = config_document()
    document['cube_consumer_url'] = 'https://CUBE.example.test:443/'
    document['cube_fixture_url'] = 'https://cube.example.test'
    write_config(tmp_path, monkeypatch, document)
    with pytest.raises(ValueError): api().load_pilot_config()


def test_ipv6_host_is_preserved_for_transport_authority(tmp_path, monkeypatch):
    document = config_document(); document['public_origin'] = 'https://[::1]'
    document['oidc']['audience'] = 'https://[::1]/mcp'
    write_config(tmp_path, monkeypatch, document)
    config = api().load_pilot_config()
    assert config.rest_allowed_hosts == ('::1',)
    assert config.mcp_allowed_hosts == ('[::1]',)
    assert config.mcp_resource_url == 'https://[::1]/mcp'


@pytest.mark.parametrize('key,value', [('issuer', 'http://issuer.test'), ('jwks_url', 'https://user:secret@issuer.test/keys'),
    ('client_ids', []), ('client_ids', ['client', 'client']), ('algorithms', ['HS256']),
    ('algorithms', []), ('jwks_cache_seconds', 301), ('jwks_cache_seconds', True),
    ('request_timeout_seconds', 2.1), ('request_timeout_seconds', 0), ('request_timeout_seconds', True),
    ('audience', ''), ('role_claim', 'admin')])
def test_oidc_delegates_existing_strict_profile(tmp_path, monkeypatch, key, value):
    document = config_document(); document['oidc'][key] = value
    write_config(tmp_path, monkeypatch, document)
    with pytest.raises(ValueError): api().load_pilot_config()


@pytest.mark.parametrize('key,value', [('host', 'db.test:5432'), ('host', 'user@db.test'), ('host', '*.test'),
    ('host', 'db/test'), ('host', ''), ('database', 'db;drop'), ('port', True), ('port', 0),
    ('ca_file', 'relative.pem'), ('ca_file', '/run/secrets/../key'), ('sslmode', 'disable'),
    ('username', 'postgres')])
def test_registry_does_not_accept_downgrade_or_credentials(tmp_path, monkeypatch, key, value):
    document = config_document(); document['registry'][key] = value
    write_config(tmp_path, monkeypatch, document)
    with pytest.raises(ValueError): api().load_pilot_config()


def test_database_urls_fix_roles_and_verify_full(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    module = api(); config = module.load_pilot_config(); reads = []
    monkeypatch.setattr(module, 'read_secret', lambda path: reads.append(path) or 'a' * 43)
    for role in ('app', 'owner', 'audit_retention'):
        url = make_url(config.database_url(role))
        assert url.username == 'gateway_' + role
        assert url.host == 'registry.example.test'
        assert url.database == 'spaceport_registry'
        expected = {'sslmode': 'verify-full', 'sslrootcert': '/run/secrets/registry_ca.pem'}
        if role != 'owner':
            expected['options'] = '-c statement_timeout=4000 -c lock_timeout=2000'
        assert url.query == expected
        from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg
        _, driver_arguments = PGDialect_psycopg().create_connect_args(url)
        assert driver_arguments['sslmode'] == 'verify-full'
        assert driver_arguments.get('options') == expected.get('options')
        assert reads[-1] == '/run/secrets/registry_' + role
    with pytest.raises(ValueError): config.database_url('postgres')


def test_assets_only_replace_endpoint_urls_and_preserve_pinned_authority(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    module = api(); config = module.load_pilot_config()
    from gateway.runtime_config import load_assets
    original = load_assets(); assets = config.load_assets()
    assert assets.manifest == original.manifest
    assert assets.bindings == original.bindings
    assert assets.fixture_catalog == original.fixture_catalog
    assert assets.policy_content == original.policy_content
    for binding, expected in zip(assets.bindings, (config.cube_consumer_url, config.cube_fixture_url)):
        old = original.bundles.resolve(binding.model_bundle_digest)
        new = assets.bundles.resolve(binding.model_bundle_digest)
        assert new.url == expected
        assert new.model_dump(exclude={'url'}) == old.model_dump(exclude={'url'})


@pytest.mark.parametrize('text', ['{"profile":"a","profile":"b"}', '{"oidc":{"issuer":"a","issuer":"b"}}',
                                  '{"x":NaN}', '[]', 'null', '{broken'])
def test_duplicate_json_and_invalid_documents_fail_safely(tmp_path, monkeypatch, text):
    path = write_config(tmp_path, monkeypatch); path.write_text(text)
    with pytest.raises(ValueError) as caught: api().load_pilot_config()
    assert str(caught.value) == 'Pilot configuration is unavailable or invalid'


def test_missing_config_path_is_not_a_demo_fallback(monkeypatch):
    monkeypatch.delenv('GATEWAY_PILOT_CONFIG_FILE', raising=False)
    with pytest.raises(ValueError): api().load_pilot_config()


@pytest.mark.parametrize('value', ['relative.json', '/tmp/../pilot.json', '', '/tmp/pilot\x00.json'])
def test_config_path_must_be_explicit_absolute_safe_path(monkeypatch, value):
    # NUL cannot be inserted into os.environ; inject only that synthetic read.
    if '\x00' in value: monkeypatch.setattr(api().os, 'environ', {'GATEWAY_PILOT_CONFIG_FILE': value})
    else: monkeypatch.setenv('GATEWAY_PILOT_CONFIG_FILE', value)
    with pytest.raises(ValueError): api().load_pilot_config()


def test_config_rejects_symlinks_in_file_or_parent(tmp_path, monkeypatch):
    path = write_config(tmp_path, monkeypatch)
    link = tmp_path / 'link.json'; link.symlink_to(path)
    monkeypatch.setenv('GATEWAY_PILOT_CONFIG_FILE', str(link))
    with pytest.raises(ValueError): api().load_pilot_config()
    real = tmp_path / 'real'; real.mkdir(); (real / 'config.json').write_bytes(path.read_bytes())
    parent = tmp_path / 'alias'; parent.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv('GATEWAY_PILOT_CONFIG_FILE', str(parent / 'config.json'))
    with pytest.raises(ValueError): api().load_pilot_config()


@pytest.mark.parametrize('kind', ['writable', 'oversized', 'directory', 'fifo'])
def test_unsafe_file_storage_fails_bounded(tmp_path, monkeypatch, kind):
    path = write_config(tmp_path, monkeypatch)
    if kind == 'writable': path.chmod(0o666)
    elif kind == 'oversized': path.write_bytes(b'x' * (api().MAX_CONFIG_BYTES + 1))
    elif kind == 'directory': path.unlink(); path.mkdir()
    else: path.unlink(); os.mkfifo(path)
    with pytest.raises(ValueError): api().load_pilot_config()


def test_replacement_during_read_is_rejected(tmp_path, monkeypatch):
    path = write_config(tmp_path, monkeypatch)
    module = api(); real_read = module.os.read; replaced = False
    def racing_read(fd, size):
        nonlocal replaced
        result = real_read(fd, size)
        if not replaced:
            replacement = tmp_path / 'replacement.json'
            replacement.write_bytes(path.read_bytes()); replacement.chmod(0o600)
            replacement.replace(path); replaced = True
        return result
    monkeypatch.setattr(module.os, 'read', racing_read)
    with pytest.raises(ValueError): module.load_pilot_config()


def test_in_place_change_during_read_is_rejected(tmp_path, monkeypatch):
    path = write_config(tmp_path, monkeypatch)
    module = api(); real_read = module.os.read; changed = False
    def racing_read(fd, size):
        nonlocal changed
        result = real_read(fd, size)
        if not changed:
            path.write_bytes(path.read_bytes() + b' '); changed = True
        return result
    monkeypatch.setattr(module.os, 'read', racing_read)
    with pytest.raises(ValueError): module.load_pilot_config()


def test_validation_never_opens_network_or_reads_secrets(tmp_path, monkeypatch):
    path = write_config(tmp_path, monkeypatch)
    module = api()
    monkeypatch.setattr(module, 'read_secret', lambda _: pytest.fail('Secret read during config parsing'))
    import socket
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('Socket during config parsing'))
    config = module.load_pilot_config(); config.identity_settings(); config.load_assets()
    assert path.exists()


@pytest.mark.parametrize('role,expected', [
    ('app', {'registry_app', 'cube_signing_key', 'cube_test_signing_key', 'audit_hmac', 'cursor_key'}),
    ('owner', {'registry_owner'}), ('audit_retention', {'registry_app', 'registry_audit_retention'}),
])
def test_file_preflight_is_role_scoped(tmp_path, monkeypatch, role, expected):
    write_config(tmp_path, monkeypatch)
    module = api(); config = module.load_pilot_config(); reads = []
    monkeypatch.setattr(module, 'read_secret', lambda path: reads.append(Path(path).name) or 'a' * 43)
    monkeypatch.setattr(module, '_validate_ca', lambda path: None)
    config.validate_files(role=role)
    assert set(reads) == expected


def test_file_preflight_sanitizes_secret_errors(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    module = api(); config = module.load_pilot_config()
    def unavailable(_): raise ValueError('PRIVATE CREDENTIAL CANARY')
    monkeypatch.setattr(module, 'read_secret', unavailable)
    with pytest.raises(ValueError) as caught: config.validate_files()
    assert 'CANARY' not in str(caught.value)


def test_ca_requires_parseable_certificate_and_no_private_key(tmp_path):
    module = api(); path = tmp_path / 'ca.pem'; path.write_text('NOT A CERTIFICATE'); path.chmod(0o600)
    with pytest.raises(ValueError): module._validate_ca(str(path))
    # Public system trust certificate is test input, never a private credential.
    import ssl
    defaults = ssl.get_default_verify_paths()
    source = Path(defaults.cafile)
    text = source.read_text(); end = text.index('-----END CERTIFICATE-----') + len('-----END CERTIFICATE-----')
    path.write_text(text[:end] + '\n')
    module._validate_ca(str(path))
    path.write_text(path.read_text() + '\n-----BEGIN PRIVATE KEY-----\ncanary\n-----END PRIVATE KEY-----\n')
    with pytest.raises(ValueError): module._validate_ca(str(path))


def test_oidc_audience_is_exact_protected_resource(tmp_path, monkeypatch):
    document = config_document(); document['oidc']['audience'] = 'another-api'
    write_config(tmp_path, monkeypatch, document)
    with pytest.raises(ValueError): api().load_pilot_config()


def test_snapshot_config_and_ca_layout_are_supported(tmp_path, monkeypatch):
    document = config_document(); document['registry']['ca_file'] = '/run/pilot/config/registry-ca.pem'
    write_config(tmp_path, monkeypatch, document)
    assert api().load_pilot_config().registry.ca_file == '/run/pilot/config/registry-ca.pem'


def test_identity_environment_does_not_overlay_operator_profile(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    monkeypatch.setenv('GATEWAY_IDENTITY_MODE', 'demo')
    monkeypatch.setenv('GATEWAY_SOURCE', 'bigquery')
    monkeypatch.setenv('GATEWAY_OIDC_ISSUER', 'https://attacker.example.test')
    config = api().load_pilot_config()
    assert config.identity_settings().mode == 'production'
    assert config.identity_settings().source == 'synthetic_postgres'
    assert config.identity_settings().oidc_issuer == 'https://issuer.example.test'


@pytest.mark.parametrize('records,valid', [
    ([], True),
    ([{'digest': 'a' * 64, 'principal_id': 'service-pilot', 'kind': 'service', 'active': True}], True),
    ([{'digest': 'a' * 64, 'principal_id': 'demo-pilot', 'kind': 'demo', 'active': True}], False),
    ([{'digest': 'a' * 64, 'principal_id': 'service-pilot', 'kind': 'service', 'active': True, 'token': 'PRIVATE'}], False),
])
def test_optional_hash_snapshot_contains_only_hash_only_service_records(tmp_path, monkeypatch, records, valid):
    document = config_document(); document['credential_hash_file'] = '/run/auth/credential-hashes.json'
    write_config(tmp_path, monkeypatch, document)
    module = api(); config = module.load_pilot_config()
    monkeypatch.setattr(module, 'read_secret', lambda path: 'a' * 43)
    monkeypatch.setattr(module, '_validate_ca', lambda path: None)
    monkeypatch.setattr(module, '_read_file', lambda path, limit: json.dumps({'version': 1, 'credentials': records}).encode())
    if valid: config.validate_files()
    else:
        with pytest.raises(ValueError) as caught: config.validate_files()
        assert str(caught.value) == 'Required pilot files are unavailable or invalid'


def test_invalid_ca_fails_preflight_before_any_connection(tmp_path, monkeypatch):
    document = config_document(); ca = tmp_path / 'ca.pem'; ca.write_text('INVALID CA CANARY'); ca.chmod(0o600)
    document['registry']['ca_file'] = str(ca)
    write_config(tmp_path, monkeypatch, document)
    module = api(); config = module.load_pilot_config()
    monkeypatch.setattr(module, 'read_secret', lambda path: 'a' * 43)
    with pytest.raises(ValueError) as caught: config.validate_files(role='owner')
    assert str(caught.value) == 'Required pilot files are unavailable or invalid'


def test_config_is_immutable_and_arrays_are_frozen(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    config = api().load_pilot_config()
    with pytest.raises(ValueError): config.public_origin = 'https://changed.example.test'
    assert isinstance(config.oidc.client_ids, tuple)


def test_parent_replacement_during_read_is_rejected(tmp_path, monkeypatch):
    directory = tmp_path / 'snapshot'; directory.mkdir()
    path = write_config(directory, monkeypatch)
    module = api(); original = module.os.read; replaced = False
    def racing_read(fd, size):
        nonlocal replaced
        result = original(fd, size)
        if not replaced:
            directory.rename(tmp_path / 'old-snapshot')
            directory.mkdir(); (directory / path.name).write_text(json.dumps(config_document()))
            replaced = True
        return result
    monkeypatch.setattr(module.os, 'read', racing_read)
    with pytest.raises(ValueError): module.load_pilot_config()


def test_asset_endpoint_identity_change_is_rejected(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    module = api(); config = module.load_pilot_config()
    from dataclasses import replace
    from gateway.adapters.bundles import BundleCatalog
    original = module.load_synthetic_assets()
    records = {}
    for binding in original.bindings:
        endpoint = original.bundles.resolve(binding.model_bundle_digest).model_dump()
        if endpoint['context'] == 'consumer': endpoint['secret_ref'] = '/run/secrets/unreviewed_key'
        records[endpoint['context']] = endpoint
    monkeypatch.setattr(module, 'load_synthetic_assets', lambda: replace(original, bundles=BundleCatalog(records)))
    with pytest.raises(ValueError) as caught: config.load_assets()
    assert str(caught.value) == 'Reviewed pilot assets are unavailable or invalid'


def test_public_origin_rejects_port_not_supported_by_production_mcp(tmp_path, monkeypatch):
    document = config_document(); document['public_origin'] = 'https://spaceport.example.test:8443'
    document['oidc']['audience'] = 'https://spaceport.example.test:8443/mcp'
    write_config(tmp_path, monkeypatch, document)
    with pytest.raises(ValueError): api().load_pilot_config()
