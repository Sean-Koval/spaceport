"""Offline pairing controls; real migration/backend acceptance stays separate."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import URL

pytestmark = [pytest.mark.unit, pytest.mark.contract]
TOKEN = 'a' * 16
ROLES = {key: f'gwt_{TOKEN}_{key}' for key in ('owner', 'app', 'audit_retention', 'source_reader')}
URL_VALUE = URL.create('postgresql+psycopg', username=ROLES['owner'], password='PRIVATE_PASSWORD',
    host='127.0.0.1', port=54321, database='gateway_test_' + TOKEN)
SNAPSHOT = [['gateway', 'products', 100, 101], ['public', 'alembic_version', 102, 103]]


def api():
    path = Path(__file__).resolve().parents[2] / 'tests/integration/registry_migration_pair.py'
    assert path.is_file(), 'Missing bounded paired migration helper'
    return importlib.import_module('tests.integration.registry_migration_pair')


def receipt(**changes):
    return {'schema_version': 1, 'database': URL_VALUE.database, 'owner': ROLES['owner'],
        'target': 'head', 'first': SNAPSHOT, 'second': SNAPSHOT, **changes}


def environment(monkeypatch):
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_AUTHORIZATION', 'disposable-local-postgres-only')
    monkeypatch.setenv('GATEWAY_MIGRATION_DATABASE_URL', URL_VALUE.render_as_string(hide_password=False))
    for key, role in ROLES.items(): monkeypatch.setenv('GATEWAY_' + key.upper() + '_ROLE', role)


@pytest.mark.parametrize('target', ['head', '0001_registry', '0006_source_preparations'])
def test_pair_preserves_two_fresh_configs_and_real_snapshot_order(monkeypatch, target):
    module = api(); environment(monkeypatch)
    events = []; configs = []; snapshots = []
    def upgrade(config, revision):
        events.append('upgrade'); configs.append(config)
        assert revision == target and config.attributes == {}
    def snapshot(url):
        assert url == URL_VALUE
        events.append('snapshot'); value = [row[:] for row in SNAPSHOT]; snapshots.append(value)
        return value
    monkeypatch.setattr(module.command, 'upgrade', upgrade)
    monkeypatch.setattr(module, 'migration_snapshot', snapshot)
    value = module.collect_pair(target)
    assert events == ['upgrade', 'snapshot', 'upgrade', 'snapshot']
    assert configs[0] is not configs[1]
    assert configs[0].config_file_name == configs[1].config_file_name == str(module.ROOT / 'alembic.ini')
    assert value['first'] is snapshots[0] and value['second'] is snapshots[1]
    assert value == receipt(target=target)


@pytest.mark.parametrize('failed_stage', [0, 1, 2, 3])
def test_any_failed_pair_stage_emits_no_partial_receipt_or_private_error(monkeypatch, capsys, failed_stage):
    module = api(); environment(monkeypatch); events = []
    def action(name):
        events.append(name)
        if len(events) - 1 == failed_stage: raise RuntimeError('PRIVATE_PASSWORD PRIVATE_ERROR')
    def upgrade(*args): action('upgrade')
    def snapshot(*args): action('snapshot'); return [row[:] for row in SNAPSHOT]
    monkeypatch.setattr(module.command, 'upgrade', upgrade)
    monkeypatch.setattr(module, 'migration_snapshot', snapshot)
    assert module.main(['head']) == 1
    output = capsys.readouterr()
    assert output.out == '' and 'PRIVATE_' not in output.err
    assert events == ['upgrade', 'snapshot', 'upgrade', 'snapshot'][:failed_stage + 1]


@pytest.mark.parametrize('change', [
    {'schema_version': True}, {'schema_version': 2}, {'database': 'other'}, {'owner': 'other'},
    {'target': '0001_registry'}, {'first': []}, {'second': []}, {'second': None}, {'extra': 'PRIVATE_ERROR'},
    {'second': [['gateway', 'products', 999, 101], ['public', 'alembic_version', 102, 103]]},
    {'first': [['gateway', 'products', True, 101]]},
    {'first': [['gateway', 'products', -1, 101]]},
    {'first': [['gateway', 'products', 2**32, 101]]},
    {'first': [['gateway', 'products', 100, 101, 'extra']]},
    {'first': [SNAPSHOT[0], SNAPSHOT[0]]},
    {'first': [SNAPSHOT[0]]},
])
def test_pair_receipt_rejects_wrong_incomplete_or_forged_evidence(change):
    module = api()
    value = receipt(**change)
    if 'first' in change: value['second'] = value['first']
    with pytest.raises(ValueError):
        module.parse_receipt(json.dumps(value), URL_VALUE, 'head')


@pytest.mark.parametrize('kind', ['missing_second', 'trailing', 'duplicate_key', 'oversized', 'deeply_nested'])
def test_pair_receipt_rejects_ambiguous_or_unbounded_json(kind):
    module = api(); value = receipt()
    if kind == 'missing_second': value.pop('second')
    wire = json.dumps(value)
    if kind == 'trailing': wire += '\nPRIVATE_ERROR'
    if kind == 'duplicate_key': wire = wire.replace('"schema_version": 1', '"schema_version": 2, "schema_version": 1')
    if kind == 'oversized': wire += ' ' * (256 * 1024)
    if kind == 'deeply_nested': wire = '[' * 2000 + '0' + ']' * 2000
    with pytest.raises(ValueError): module.parse_receipt(wire, URL_VALUE, 'head')


def test_pair_receipt_preserves_equal_raw_order_and_zero_relfilenode():
    module = api()
    rows = [['public', 'alembic_version', 102, 103], ['gateway', 'capacity_view', 100, 0]]
    value = receipt(first=rows, second=rows)
    assert module.parse_receipt(json.dumps(value), URL_VALUE, 'head') == value


@pytest.mark.parametrize('change', ['target', 'authority', 'database', 'host', 'owner', 'role'])
def test_pair_input_guard_rejects_nonfixture_targets_before_upgrade(monkeypatch, change):
    module = api(); environment(monkeypatch)
    if change == 'authority': monkeypatch.setenv('GATEWAY_TEST_POSTGRES_AUTHORIZATION', 'other')
    if change in {'database', 'host', 'owner'}:
        field = 'username' if change == 'owner' else change
        monkeypatch.setenv('GATEWAY_MIGRATION_DATABASE_URL', URL_VALUE.set(**{field: 'other'}).render_as_string(hide_password=False))
    if change == 'role': monkeypatch.setenv('GATEWAY_APP_ROLE', 'other')
    calls = []
    monkeypatch.setattr(module.command, 'upgrade', lambda *args: calls.append(args))
    with pytest.raises(ValueError): module.collect_pair('unreviewed' if change == 'target' else 'head')
    assert calls == []


def test_fixture_pair_uses_one_60_second_process_with_env_only_credentials(monkeypatch):
    module = api()
    fixture = importlib.import_module('tests.integration.conftest')
    assert hasattr(fixture, 'run_migration_pair'), 'Fixture still starts two independent interpreters'
    calls = []
    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout=json.dumps(receipt()), stderr='PRIVATE_ERROR')
    monkeypatch.setattr(fixture.subprocess, 'run', run)
    first, second = fixture.run_migration_pair(URL_VALUE, ROLES)
    assert first == second == SNAPSHOT and len(calls) == 1
    command, options = calls[0]
    assert command == [sys.executable, '-m', 'tests.integration.registry_migration_pair', 'head']
    assert options['timeout'] == 60 and options['cwd'] == fixture.ROOT
    assert options['capture_output'] is True and options['text'] is True
    assert 'PRIVATE_PASSWORD' not in ' '.join(command)
    assert options['env']['GATEWAY_MIGRATION_DATABASE_URL'] == URL_VALUE.render_as_string(hide_password=False)
    assert all(options['env']['GATEWAY_' + key.upper() + '_ROLE'] == role for key, role in ROLES.items())


@pytest.mark.parametrize('failure', ['timeout', 'os', 'nonzero', 'invalid'])
def test_fixture_pair_fails_closed_without_echoing_private_child_output(monkeypatch, failure):
    api(); fixture = importlib.import_module('tests.integration.conftest')
    def run(command, **kwargs):
        if failure == 'timeout': raise subprocess.TimeoutExpired(command, 60, output='PRIVATE_ERROR')
        if failure == 'os': raise OSError('PRIVATE_ERROR')
        return SimpleNamespace(returncode=1 if failure == 'nonzero' else 0, stdout='PRIVATE_ERROR', stderr='PRIVATE_ERROR')
    monkeypatch.setattr(fixture.subprocess, 'run', run)
    with pytest.raises(pytest.fail.Exception) as caught: fixture.run_migration_pair(URL_VALUE, ROLES)
    assert 'PRIVATE_' not in str(caught.value)


def test_legacy_single_upgrade_path_remains_independent(monkeypatch):
    fixture = importlib.import_module('tests.integration.conftest'); calls = []
    sentinel = SimpleNamespace(returncode=1, stdout='', stderr='safe original rejection')
    def run(command, **kwargs): calls.append((command, kwargs)); return sentinel
    monkeypatch.setattr(fixture.subprocess, 'run', run)
    assert fixture.run_migration(URL_VALUE, ROLES, '0002_audit_policy_id') is sentinel
    assert calls[0][0] == [sys.executable, '-m', 'alembic', 'upgrade', '0002_audit_policy_id']
    assert calls[0][1]['timeout'] == 60


@pytest.mark.parametrize('failure', ['timeout', 'os', 'nonzero', 'invalid'])
def test_failed_pair_still_cleans_only_its_fresh_database_and_four_roles(monkeypatch, failure):
    api(); fixture = importlib.import_module('tests.integration.conftest')
    environment(monkeypatch)
    admin_url = URL_VALUE.set(username='gateway_test_admin', database='gateway_test_admin')
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_ADMIN_URL', admin_url.render_as_string(hide_password=False))
    monkeypatch.setattr(fixture.secrets, 'token_hex', lambda count: TOKEN)
    queries = []; closed = []
    class Admin:
        def execute(self, statement): queries.append(statement.as_string())
        def close(self): closed.append(True)
    monkeypatch.setattr(fixture.psycopg, 'connect', lambda **kwargs: Admin())
    def run(command, **kwargs):
        if failure == 'timeout': raise subprocess.TimeoutExpired(command, 60, output='PRIVATE_ERROR')
        if failure == 'os': raise OSError('PRIVATE_ERROR')
        return SimpleNamespace(returncode=1 if failure == 'nonzero' else 0, stdout='PRIVATE_ERROR', stderr='PRIVATE_ERROR')
    monkeypatch.setattr(fixture.subprocess, 'run', run)
    monkeypatch.setattr(fixture, 'session_migration_worker', lambda request: None)
    with pytest.raises(pytest.fail.Exception):
        next(fixture.registry_database.__wrapped__(SimpleNamespace(param={})))
    drops = [query for query in queries if query.startswith('DROP ')]
    assert drops == ['DROP DATABASE "' + URL_VALUE.database + '" WITH (FORCE)',
        *['DROP ROLE "' + role + '"' for role in reversed(list(ROLES.values()))]]
    assert closed == [True]


@pytest.mark.parametrize('target', ['base', '0005_source_lifecycle', '0007_source_access_approvals', 'head-1'])
def test_historical_aging_target_does_not_enable_arbitrary_revisions(monkeypatch, target):
    module = api(); environment(monkeypatch); calls = []
    monkeypatch.setattr(module.command, 'upgrade', lambda *args: calls.append(args))
    with pytest.raises(ValueError): module.collect_pair(target)
    assert calls == []
