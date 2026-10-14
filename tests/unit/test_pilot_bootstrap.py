"""Strict operator identity input; all subjects/credentials below are synthetic."""
import copy
import asyncio
from contextlib import asynccontextmanager
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.auth.principals import oidc_subject_key
from gateway.auth.settings import Settings
from gateway.runtime_config import load_assets
from gateway.registry.models import BindingRevision, PolicyRevision, PolicyRow, PrincipalGrant, PrincipalRow

pytestmark = pytest.mark.unit


def api():
    return importlib.import_module('gateway.pilot_bootstrap')


def config(*, issuer='https://issuer.example.test', probe='north-user', hashes=None):
    settings = Settings(mode='production', source='synthetic_postgres',
        oidc_issuer=issuer, oidc_jwks_url=issuer + '/jwks', oidc_audience='spaceport',
        oidc_client_ids=['publisher-ui'], oidc_access_token_profile='rfc9068',
        credential_hash_file=str(hashes) if hashes else None)
    return SimpleNamespace(identity_settings=lambda: settings, probe_principal_id=probe,
                           load_assets=load_assets)


def identity_data():
    return {'version': 1, 'principals': [
        {'id': 'north-user', 'kind': 'user', 'subject': 'reviewed-north-subject',
         'grants': [{'tenant_id': 'north', 'roles': ['consumer']}]},
        {'id': 'author-user', 'kind': 'user', 'subject': 'reviewed-author-subject',
         'grants': [{'tenant_id': 'north', 'roles': ['author']}]},
        {'id': 'publisher-user', 'kind': 'user', 'subject': 'reviewed-publisher-subject',
         'grants': [{'tenant_id': 'north', 'roles': ['publisher']}]},
    ]}


def write_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def load(tmp_path, data=None, cfg=None):
    path = write_json(tmp_path / 'identities.json', identity_data() if data is None else data)
    return api().load_identity_mapping(path, config() if cfg is None else cfg)


def test_mapping_binds_users_to_issuer_and_exact_subject_without_retaining_raw_subject(tmp_path):
    mapping = load(tmp_path)
    principal = next(p for p in mapping.principals if p.id == 'north-user')
    assert principal.kind == 'user' and principal.service_client_id is None
    assert principal.subject == oidc_subject_key('https://issuer.example.test', 'reviewed-north-subject')
    assert 'reviewed-north-subject' not in repr(mapping)
    assert principal.grants[0].roles == ('consumer',)
    assert principal.grants[0].tenant_id == 'north'
    with pytest.raises(Exception):
        principal.id = 'changed'


def test_mapping_digest_covers_issuer_and_initial_grants_but_not_input_order(tmp_path):
    first = load(tmp_path)
    changed_issuer = load(tmp_path, cfg=config(issuer='https://other.example.test'))
    assert changed_issuer.digest != first.digest
    reordered = identity_data()
    reordered['principals'].reverse()
    assert load(tmp_path, reordered).digest == first.digest
    altered = identity_data()
    altered['principals'][1]['grants'][0]['roles'].append('consumer')
    assert load(tmp_path, altered).digest != first.digest


@pytest.mark.parametrize('change', [
    'unknown_top', 'version_bool', 'version_string', 'empty', 'too_many', 'duplicate_id',
    'duplicate_subject', 'subject_empty', 'subject_long', 'subject_control', 'subject_wrong_type',
    'demo_kind', 'user_service_id', 'unknown_claims', 'demo_id', 'fixture_id', 'pilot_id',
    'invalid_id', 'unknown_tenant', 'duplicate_tenant', 'empty_grants', 'empty_roles',
    'unknown_role', 'privileged_role', 'duplicate_role', 'author_publisher', 'split_author_publisher',
    'foreign_policy', 'groups', 'missing_subject',
])
def test_mapping_rejects_ambiguous_or_broadened_authority(tmp_path, change):
    data = identity_data()
    row = data['principals'][0]
    grant = row['grants'][0]
    if change == 'unknown_top': data['credentials'] = []
    elif change == 'version_bool': data['version'] = True
    elif change == 'version_string': data['version'] = '1'
    elif change == 'empty': data['principals'] = []
    elif change == 'too_many': data['principals'] *= 101
    elif change == 'duplicate_id': data['principals'][1]['id'] = row['id']
    elif change == 'duplicate_subject': data['principals'][1]['subject'] = row['subject']
    elif change == 'subject_empty': row['subject'] = ''
    elif change == 'subject_long': row['subject'] = 's' * 513
    elif change == 'subject_control': row['subject'] = 'private\x00subject'
    elif change == 'subject_wrong_type': row['subject'] = 123
    elif change == 'demo_kind': row['kind'] = 'demo'
    elif change == 'user_service_id': row['service_client_id'] = 'forged-service'
    elif change == 'unknown_claims': row['claims'] = {'groups': ['south']}
    elif change in {'demo_id', 'fixture_id', 'pilot_id'}: row['id'] = change.split('_')[0] + '-reserved'
    elif change == 'invalid_id': row['id'] = 'bad id'
    elif change == 'unknown_tenant': grant['tenant_id'] = 'organization-live'
    elif change == 'duplicate_tenant': row['grants'].append(copy.deepcopy(grant))
    elif change == 'empty_grants': row['grants'] = []
    elif change == 'empty_roles': grant['roles'] = []
    elif change == 'unknown_role': grant['roles'] = ['admin']
    elif change == 'privileged_role': grant['roles'] = ['operator']
    elif change == 'duplicate_role': grant['roles'] = ['consumer', 'consumer']
    elif change == 'author_publisher': grant['roles'] = ['author', 'publisher']
    elif change == 'split_author_publisher':
        grant['roles'] = ['author']
        row['grants'].append({'tenant_id': 'south', 'roles': ['publisher']})
    elif change == 'foreign_policy': grant['policy_id'] = 'another-policy'
    elif change == 'groups': grant['entitlements'] = {'groups': ['north', 'south']}
    elif change == 'missing_subject': del row['subject']
    with pytest.raises(ValueError, match='^The approved identity mapping is unavailable or invalid$') as caught:
        load(tmp_path, data)
    assert 'reviewed-' not in str(caught.value)


@pytest.mark.parametrize('probe', ['absent-user', 'author-user', 'publisher-user', 'fixture-north'])
def test_probe_must_be_an_explicit_approved_consumer(tmp_path, probe):
    with pytest.raises(ValueError): load(tmp_path, cfg=config(probe=probe))


def test_probe_can_use_either_reviewed_synthetic_consumer_tenant(tmp_path):
    data = identity_data()
    data['principals'][0]['grants'][0]['tenant_id'] = 'south'
    assert load(tmp_path, data).principals


@pytest.mark.parametrize('missing', ['author', 'publisher'])
def test_initial_mapping_requires_separate_author_and_publisher_for_first_publication(tmp_path, missing):
    data = identity_data()
    data['principals'] = [row for row in data['principals'] if row['grants'][0]['roles'] != [missing]]
    with pytest.raises(ValueError, match='unavailable or invalid'):
        load(tmp_path, data)


@pytest.mark.parametrize('unsafe', ['relative', 'symlink', 'ancestor', 'directory', 'fifo', 'writable', 'large', 'duplicate', 'nonfinite', 'deep'])
def test_mapping_file_is_bounded_nofollow_regular_and_strict_json(tmp_path, unsafe, monkeypatch):
    path = write_json(tmp_path / 'input.json', identity_data())
    if unsafe == 'relative':
        monkeypatch.chdir(tmp_path)
        path = Path('input.json')
    elif unsafe == 'symlink':
        linked = tmp_path / 'link.json'; linked.symlink_to(path); path = linked
    elif unsafe == 'ancestor':
        directory = tmp_path / 'real'; directory.mkdir()
        path = write_json(directory / 'identities.json', identity_data())
        linked = tmp_path / 'linked'; linked.symlink_to(directory, target_is_directory=True)
        path = linked / 'identities.json'
    elif unsafe == 'directory': path = tmp_path
    elif unsafe == 'fifo': path.unlink(); os.mkfifo(path)
    elif unsafe == 'writable': path.chmod(0o622)
    elif unsafe == 'large': path.write_bytes(b' ' * (api().MAX_IDENTITY_BYTES + 1))
    elif unsafe == 'duplicate': path.write_text('{"version":1,"version":1,"principals":[]}')
    elif unsafe == 'nonfinite': path.write_text('{"version":NaN,"principals":[]}')
    elif unsafe == 'deep': path.write_text('[' * 2000 + ']' * 2000)
    with pytest.raises(ValueError, match='^The approved identity mapping is unavailable or invalid$'):
        api().load_identity_mapping(path, config())


def service_data():
    data = identity_data()
    data['principals'].append({'id': 'revenue-service', 'kind': 'service',
        'service_client_id': 'approved-service',
        'grants': [{'tenant_id': 'south', 'roles': ['consumer']}]})
    return data


def hashes(tmp_path, records=None):
    return write_json(tmp_path / 'hashes.json', {'version': 1, 'credentials': records if records is not None else [
        {'digest': 'a' * 64, 'principal_id': 'revenue-service', 'kind': 'service', 'active': True}]})


def test_service_identity_is_distinct_and_uses_only_existing_hash_records(tmp_path):
    path = hashes(tmp_path)
    original = path.read_bytes()
    mapping = load(tmp_path, service_data(), config(hashes=path))
    service = next(p for p in mapping.principals if p.id == 'revenue-service')
    assert service.kind == 'service' and service.service_client_id == 'approved-service'
    assert service.subject.startswith('service-v1-')
    assert service.subject != oidc_subject_key('https://issuer.example.test', 'approved-service')
    assert path.read_bytes() == original


def test_revoked_service_hash_is_not_reenabled_or_rejected_on_rerun(tmp_path):
    records = [{'digest': 'a' * 64, 'principal_id': 'revenue-service', 'kind': 'service', 'active': False}]
    path = hashes(tmp_path, records)
    mapping = load(tmp_path, service_data(), config(hashes=path))
    assert any(p.kind == 'service' for p in mapping.principals)
    assert json.loads(path.read_text())['credentials'][0]['active'] is False


def test_service_hash_file_also_rejects_symlink_ancestors(tmp_path):
    directory = tmp_path / 'real'; directory.mkdir()
    path = hashes(directory)
    linked = tmp_path / 'linked'; linked.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError):
        load(tmp_path, service_data(), config(hashes=linked / path.name))


@pytest.mark.parametrize('change', ['no_file', 'no_hash', 'wrong_kind', 'fixture_hash', 'unknown_hash', 'raw_subject', 'duplicate_client'])
def test_service_mapping_requires_exact_existing_service_hash_support(tmp_path, change):
    data = service_data()
    records = [{'digest': 'a' * 64, 'principal_id': 'revenue-service', 'kind': 'service', 'active': True}]
    if change == 'no_hash': records = []
    elif change == 'wrong_kind': records[0]['kind'] = 'demo'
    elif change == 'fixture_hash': records[0]['principal_id'] = 'fixture-north'
    elif change == 'unknown_hash': records[0]['principal_id'] = 'unknown-service'
    elif change == 'raw_subject': data['principals'][-1]['subject'] = 'forged-user-subject'
    elif change == 'duplicate_client':
        duplicate = copy.deepcopy(data['principals'][-1]); duplicate['id'] = 'another-service'
        data['principals'].append(duplicate)
    cfg = config(hashes=None if change == 'no_file' else hashes(tmp_path, records))
    with pytest.raises(ValueError): load(tmp_path, data, cfg)


class MemorySession:
    """A query/row recorder only; PostgreSQL behavior has separate integration tests."""
    def __init__(self, *, owner=True):
        self.rows = []
        self.added = []
        self.statements = []
        self.queried_tables = []
        self.owner = owner

    def add(self, row):
        self.rows.append(row)
        self.added.append(row)

    async def flush(self):
        pass

    async def execute(self, statement):
        self.statements.append(str(statement))

    async def get(self, model, key, **kwargs):
        return next((row for row in self.rows if isinstance(row, model) and row.id == key), None)

    async def scalar(self, statement):
        if str(statement).startswith('SELECT current_user'):
            return self.owner
        table = statement.get_final_froms()[0].name
        self.queried_tables.append(table)
        values = statement.compile().params
        for row in self.rows:
            if row.__table__.name != table:
                continue
            if all(getattr(row, key.rsplit('_', 1)[0], None) == value
                   for key, value in values.items() if not key.startswith('param_')):
                return row
        return None


class MemoryOwner:
    def __init__(self, *, is_owner=True):
        self.session = MemorySession(owner=is_owner)
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        yield SimpleNamespace(session=self.session)


def test_provision_builds_only_reviewed_identity_grants_policy_and_bindings(tmp_path):
    mapping = load(tmp_path)
    owner = MemoryOwner()
    asyncio.run(api().provision(owner, config(), mapping))
    assert owner.transactions == 1
    assert any('pg_advisory_xact_lock' in sql for sql in owner.session.statements)
    principals = [r for r in owner.session.rows if isinstance(r, PrincipalRow)]
    assert {p.id for p in principals} == {'north-user', 'author-user', 'publisher-user',
        'fixture-north', 'fixture-south', 'fixture-denied'}
    assert {p.id for p in principals if p.kind == 'demo'} == {'fixture-north', 'fixture-south', 'fixture-denied'}
    grants = [r for r in owner.session.rows if isinstance(r, PrincipalGrant)]
    assert len(grants) == 5
    assert all(g.policy_id == 'revenue-access' and g.entitlements == {'groups': [g.tenant_id]} for g in grants)
    assert all(type(r) in {PrincipalRow, PrincipalGrant, PolicyRow, PolicyRevision, BindingRevision}
               for r in owner.session.rows)
    policy = next(r for r in owner.session.rows if isinstance(r, PolicyRevision))
    assert mapping.digest in policy.id


def test_empty_registry_check_includes_fixture_and_publication_tables(tmp_path):
    owner = MemoryOwner()
    asyncio.run(api().provision(owner, config(), load(tmp_path)))
    assert {'fixture_attempts', 'fixture_attempt_states', 'fixture_source_reservations',
            'publication_events'} <= set(owner.session.queried_tables)


def test_provision_rerun_records_no_writes_or_revocation_reset(tmp_path):
    owner = MemoryOwner()
    mapping = load(tmp_path)
    asyncio.run(api().provision(owner, config(), mapping))
    for row in owner.session.rows:
        if isinstance(row, (PrincipalRow, PrincipalGrant)):
            row.active = False
        if isinstance(row, PrincipalGrant):
            row.roles = ['consumer']; row.entitlements = {'groups': ['operator-changed']}
    owner.session.added.clear()
    asyncio.run(api().provision(owner, config(), mapping))
    assert not owner.session.added
    assert all(not r.active for r in owner.session.rows if isinstance(r, (PrincipalRow, PrincipalGrant)))
    assert all(r.entitlements == {'groups': ['operator-changed']} for r in owner.session.rows if isinstance(r, PrincipalGrant))


@pytest.mark.parametrize('change', ['mapping', 'issuer', 'principal', 'kind', 'service_id', 'policy', 'binding', 'missing_fixture'])
def test_provision_rerun_rejects_changed_immutable_deployment(tmp_path, change):
    owner = MemoryOwner()
    mapping = load(tmp_path)
    cfg = config()
    asyncio.run(api().provision(owner, cfg, mapping))
    principal = next(r for r in owner.session.rows if isinstance(r, PrincipalRow) and r.id == 'north-user')
    if change == 'mapping':
        data = identity_data(); data['principals'][1]['grants'][0]['roles'].append('consumer')
        mapping = load(tmp_path, data)
    elif change == 'issuer':
        cfg = config(issuer='https://changed.example.test'); mapping = load(tmp_path, cfg=cfg)
    elif change == 'principal': principal.subject = 'changed'
    elif change == 'kind': principal.kind = 'service'
    elif change == 'service_id': principal.service_client_id = 'changed'
    elif change == 'policy': next(r for r in owner.session.rows if isinstance(r, PolicyRevision)).content = {}
    elif change == 'binding': next(r for r in owner.session.rows if isinstance(r, BindingRevision)).content = {}
    elif change == 'missing_fixture': owner.session.rows = [r for r in owner.session.rows if getattr(r, 'id', None) != 'fixture-denied']
    owner.session.added.clear()
    with pytest.raises(ValueError, match='reviewed bootstrap'):
        asyncio.run(api().provision(owner, cfg, mapping))
    assert not owner.session.added


def test_provision_refuses_nonowner_even_on_matching_rerun(tmp_path):
    mapping = load(tmp_path)
    owner = MemoryOwner()
    asyncio.run(api().provision(owner, config(), mapping))
    owner.session.owner = False
    owner.session.added.clear()
    with pytest.raises(ValueError, match='owner'):
        asyncio.run(api().provision(owner, config(), mapping))
    assert not owner.session.added


@pytest.mark.parametrize('row', [
    PrincipalRow(id='existing', subject='somebody', kind='user'),
    PolicyRow(id='existing-policy', current_revision=1),
    BindingRevision(id='existing-binding', binding_id='another', revision=1),
])
def test_provision_requires_empty_registry_including_policy_and_binding_state(tmp_path, row):
    owner = MemoryOwner()
    owner.session.rows.append(row)
    with pytest.raises(ValueError, match='empty registry'):
        asyncio.run(api().provision(owner, config(), load(tmp_path)))
    assert not owner.session.added


def test_cli_validates_mapping_before_constructing_registry(tmp_path, monkeypatch, caplog):
    subject = 'private-subject-must-not-appear'
    path = write_json(tmp_path / 'bad.json', {'subject': subject})
    constructed = []
    monkeypatch.setattr(api(), 'load_pilot_config', config)
    monkeypatch.setattr(api(), 'Registry', lambda *args: constructed.append(args))
    monkeypatch.setattr(api(), 'configure_logging', lambda: None)
    assert api().main(['provision', '--identities', str(path)]) == 1
    assert not constructed
    assert subject not in caplog.text and str(path) not in caplog.text


@pytest.mark.parametrize('args', [['provision'], ['provision', '--identities'], ['migrate', '--identities', 'private-value'], ['secret-invalid-operation']])
def test_cli_requires_explicit_mapping_and_does_not_echo_invalid_arguments(monkeypatch, caplog, capsys, args):
    monkeypatch.setattr(api(), 'configure_logging', lambda: None)
    monkeypatch.setattr(api(), 'load_pilot_config', lambda: pytest.fail('Invalid CLI must not load config'))
    assert api().main(args) == 1
    rendered = caplog.text + capsys.readouterr().out + capsys.readouterr().err
    assert 'private-value' not in rendered and 'secret-invalid-operation' not in rendered


def test_migrate_uses_pilot_owner_tls_config_and_pins_role_names(monkeypatch):
    seen = []
    sentinel = object()
    class Engine:
        def begin(self):
            from contextlib import contextmanager
            @contextmanager
            def connection():
                yield SimpleNamespace(execute=lambda statement: seen.append(str(statement)))
            return connection()
        def dispose(self): seen.append('disposed')
    cfg = SimpleNamespace(validate_files=lambda **kw: seen.append(('validate', kw)),
        database_url=lambda role: seen.append(('url', role)) or sentinel)
    monkeypatch.setattr(api(), 'create_engine', lambda url, **kw: seen.append(('engine', url, kw)) or Engine())
    captured = []
    monkeypatch.setattr(api().command, 'upgrade', lambda alembic, head: captured.append((alembic, head)))
    monkeypatch.setenv('GATEWAY_OWNER_ROLE', 'ignored_environment_owner')
    api().migrate(cfg)
    assert seen[0] == ('validate', {'role': 'owner'})
    assert ('url', 'owner') in seen
    engine = next(entry for entry in seen if isinstance(entry, tuple) and entry[0] == 'engine')
    assert engine[1] is sentinel and engine[2]['hide_parameters'] is True and engine[2]['echo'] is False
    alembic, head = captured[0]
    assert head == 'head' and alembic.attributes['registry_roles'] == {
        name: 'gateway_' + name for name in ('owner', 'app', 'audit_retention', 'source_reader')}
    assert any('pg_advisory_xact_lock' in value for value in seen if isinstance(value, str))
    assert seen[-1] == 'disposed'


def test_mapping_rejects_file_mutation_during_read(tmp_path, monkeypatch):
    path = write_json(tmp_path / 'input.json', identity_data())
    real_fstat = os.fstat
    calls = 0
    def changed_fstat(fd):
        nonlocal calls
        value = real_fstat(fd); calls += 1
        return SimpleNamespace(st_mode=value.st_mode, st_size=value.st_size,
            st_mtime_ns=value.st_mtime_ns + (calls > 1), st_ctime_ns=value.st_ctime_ns,
            st_dev=value.st_dev, st_ino=value.st_ino)
    monkeypatch.setattr(os, 'fstat', changed_fstat)
    with pytest.raises(ValueError, match='unavailable or invalid'):
        api().load_identity_mapping(path, config())


def test_mapping_rejects_path_replacement_during_read(tmp_path, monkeypatch):
    path = write_json(tmp_path / 'identities.json', identity_data())
    replacement = write_json(tmp_path / 'replacement.json', identity_data())
    real_read = os.read
    changed = False
    def replace_while_reading(fd, size):
        nonlocal changed
        data = real_read(fd, size)
        if not changed:
            changed = True; replacement.replace(path)
        return data
    monkeypatch.setattr(os, 'read', replace_while_reading)
    with pytest.raises(ValueError, match='unavailable or invalid'):
        api().load_identity_mapping(path, config())


def test_mapping_composes_with_actual_pilot_configuration(tmp_path):
    from gateway.pilot_config import PilotRuntimeConfig
    cfg = PilotRuntimeConfig(schema_version=1, profile='oidc-synthetic-pilot',
        public_origin='https://gateway.example.test',
        oidc={'issuer': 'https://issuer.example.test', 'audience': 'https://gateway.example.test/mcp',
              'jwks_url': 'https://issuer.example.test/jwks', 'client_ids': ['publisher-ui']},
        registry={'host': 'registry.example.test', 'database': 'pilot_registry', 'ca_file': '/run/pilot/ca.pem'},
        cube_consumer_url='https://cube.example.test', cube_fixture_url='https://fixture.example.test',
        probe_principal_id='north-user')
    mapping = load(tmp_path, cfg=cfg)
    owner = MemoryOwner()
    asyncio.run(api().provision(owner, cfg, mapping))
    assert any(isinstance(row, PrincipalRow) and row.id == 'north-user' for row in owner.session.rows)
    assert cfg.load_assets().manifest.product_id == 'revenue'


def test_run_provision_validates_owner_files_and_closes_registry_after_failure(tmp_path, monkeypatch):
    cfg = config(); mapping = load(tmp_path)
    events = []
    cfg.validate_files = lambda **kw: events.append(('files', kw))
    cfg.database_url = lambda role: events.append(('url', role)) or 'synthetic-owner-url'
    class Owner:
        def __init__(self, url): events.append(('registry', url))
        async def close(self): events.append('closed')
    async def fail(*args):
        events.append('provision'); raise RuntimeError('synthetic failure')
    monkeypatch.setattr(api(), 'Registry', Owner)
    monkeypatch.setattr(api(), 'provision', fail)
    with pytest.raises(RuntimeError): asyncio.run(api().run_provision(cfg, mapping))
    assert events == [('files', {'role': 'owner'}), ('url', 'owner'),
                      ('registry', 'synthetic-owner-url'), 'provision', 'closed']


def test_cli_dispatches_successful_explicit_provision_after_validation(tmp_path, monkeypatch):
    cfg = config(); path = write_json(tmp_path / 'identities.json', identity_data())
    seen = []
    async def record(configured, mapping): seen.append((configured, mapping))
    monkeypatch.setattr(api(), 'load_pilot_config', lambda: cfg)
    monkeypatch.setattr(api(), 'run_provision', record)
    monkeypatch.setattr(api(), 'configure_logging', lambda: None)
    assert api().main(['provision', '--identities', str(path)]) == 0
    assert seen[0][0] is cfg and isinstance(seen[0][1], api().ApprovedIdentityMapping)


def test_cli_dispatches_migration_without_any_identity_mapping(monkeypatch):
    cfg = config(); seen = []
    monkeypatch.setattr(api(), 'load_pilot_config', lambda: cfg)
    monkeypatch.setattr(api(), 'migrate', lambda configured: seen.append(configured))
    monkeypatch.setattr(api(), 'load_identity_mapping', lambda *args: pytest.fail('Migration must not read mapping'))
    monkeypatch.setattr(api(), 'configure_logging', lambda: None)
    assert api().main(['migrate']) == 0 and seen == [cfg]
