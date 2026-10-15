"""Private actual-package + real-PG serving acceptance; fake provider only."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[2]


def test_python_minimal_bigquery_contract_is_accepted_by_node():
    from gateway.adapters.bigquery_runtime import BigQueryBinding, bigquery_security_contract
    from tests.unit.test_bigquery_serving_source import binding_value
    raw = binding_value()
    raw['fields'].pop('account')
    raw['input_fields'] = {}
    value = bigquery_security_contract(BigQueryBinding.model_validate(raw))
    assert len(value['members']) == 4
    result = subprocess.run(['node', '-e', 'const S=require(process.argv[1]);S.validateContract(JSON.parse(process.argv[2]));',
        str(ROOT/'cube/bigquery-runtime/security.cjs'), json.dumps(value)], capture_output=True, timeout=5)
    assert result.returncode == 0, 'Python-generated minimal security contract must match Node'


def test_source_fixture_has_only_fixed_control_operations():
    from tests.integration.source_runtime_fixture import OPERATIONS, validate_operation
    assert OPERATIONS == ('revoke_queued', 'reclaim', 'close_main', 'close_zero')
    for operation in OPERATIONS:
        assert validate_operation(operation) == operation
    for operation in ('close_unknown', 'revoke_main', '', None, {'operation': 'reclaim'}):
        with pytest.raises(ValueError):
            validate_operation(operation)



def test_source_evidence_is_bounded_regular_atomic_and_one_shot(tmp_path):
    from tests.integration.source_runtime_fixture import _read, _atomic
    target = tmp_path/'safe.json'
    _atomic(target, {'schema_version': 1})
    assert json.loads(_read(target)) == {'schema_version': 1}
    assert target.stat().st_mode & 0o777 == 0o444
    with pytest.raises(ValueError): _atomic(target, {'schema_version': 2})
    with pytest.raises(ValueError): _read(target, 1)
    link = tmp_path/'link.json'; link.symlink_to(target)
    with pytest.raises(ValueError): _read(link)
    assert json.loads(_read(target)) == {'schema_version': 1}


def test_fixed_host_control_rejects_wrong_order_before_credentials_or_database(tmp_path, monkeypatch):
    from tests.integration import source_runtime_fixture as F
    fixture = tmp_path/'source-runtime-fixture.json'
    fixture.write_text(json.dumps({'schema_version': 1, 'deployment_epoch': F.EPOCH,
        'registry': {'database': F.DATABASE}, 'source': F.source_profile().model_dump(mode='json'),
        'cases': [{'name': name} for name in F.CASES]}))
    monkeypatch.setattr(F, '_secret', lambda *args: pytest.fail('Unexpected credential read'))
    monkeypatch.setattr(F, '_connect', lambda *args: pytest.fail('Unexpected DB connection'))
    with pytest.raises(ValueError, match='order'):
        asyncio.run(F._control(fixture, 'reclaim', secret_dir=tmp_path))


def test_local_control_rejects_a_different_registry_before_secret_reads(tmp_path, monkeypatch):
    from tests.integration import source_runtime_fixture as F
    fixture = tmp_path/'source-runtime-fixture.json'
    fixture.write_text(json.dumps({'registry': {'host': '127.0.0.1', 'port': 5433, 'database': F.DATABASE}}))
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_AUTHORIZATION', 'disposable-local-postgres-only')
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_ADMIN_URL', 'postgresql+psycopg://gateway_test_admin:synthetic@127.0.0.1:5432/gateway_test_admin')
    monkeypatch.setattr(F, '_secret', lambda *args: pytest.fail('Unexpected credential read'))
    with pytest.raises(ValueError, match='Exact disposable'):
        asyncio.run(F.control_local(fixture, 'revoke_queued'))


def test_fixture_cli_without_explicit_synthetic_authorization_cannot_bootstrap(monkeypatch):
    from tests.integration import source_runtime_fixture as F
    monkeypatch.delenv('GATEWAY_SOURCE_TEST_AUTHORIZATION', raising=False)
    monkeypatch.setattr('sys.argv', ['source_runtime_fixture', 'bootstrap', '--secret-dir', '/run/secrets', '--output', '/evidence/source-runtime-fixture.json'])
    monkeypatch.setattr(F, '_secret', lambda *args: pytest.fail('Unexpected credential read'))
    assert F.main() == 1


def test_portable_source_checks_are_selected_by_the_mandatory_contract_gate():
    result = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q', __file__, '-m', 'unit or contract'],
        cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert 'deselected' not in result.stdout
    assert 'test_source_evidence_is_bounded_regular_atomic_and_one_shot' in result.stdout
    assert 'test_fixed_host_control_rejects_wrong_order_before_credentials_or_database' in result.stdout
    assert 'test_local_control_rejects_a_different_registry_before_secret_reads' in result.stdout
