"""Offline reviewed-v2 harness admission; never actual backend evidence."""
import json
from pathlib import Path
import runpy

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]
REVIEWED = {'cube-reviewed': 'consumer', 'cube-reviewed-test': 'fixture_test'}


def test_reviewed_services_are_explicit_opt_in_without_new_secrets_or_ports():
    base = json.loads((ROOT / 'infra/compose.yaml').read_text())
    test = json.loads((ROOT / 'infra/compose.test.yaml').read_text())
    overlay = json.loads((ROOT / 'infra/compose.reviewed.yaml').read_text())
    assert set(overlay) == {'services'}
    assert set(overlay['services']) == set(REVIEWED)
    assert not set(REVIEWED) & (set(base['services']) | set(test['services']))
    for name, context in REVIEWED.items():
        original = base['services']['cube'] if context == 'consumer' else test['services']['cube-test']
        service = overlay['services'][name]
        assert service['image'] == original['image']
        assert service['platform'] == 'linux/amd64'
        assert service['networks'] == ['private']
        assert service['secrets'] == original['secrets']
        assert service['depends_on'] == original['depends_on']
        assert service['labels'] == original['labels']
        assert not service.get('ports') and not service.get('network_mode')
        assert all(mount.endswith(':ro') for mount in service['volumes'])
        for key in ('NODE_ENV', 'CUBEJS_DEV_MODE', 'CUBEJS_TELEMETRY', 'CUBEJS_REFRESH_WORKER',
                    'CUBEJS_SQL_PORT', 'CUBEJS_PG_SQL_PORT', 'CUBEJS_NATIVE_API_GATEWAY_PORT'):
            assert service['environment'][key] == original['environment'][key]
        assert service['environment']['GATEWAY_CUBE_CONTEXT'] == context
        catalog = json.loads((ROOT / 'cube/reviewed-postgres/bundles.json').read_text())
        assert service['environment']['GATEWAY_MODEL_BUNDLE_DIGEST'] == catalog[context]['bundle_digest']
        directory = 'model' if context == 'consumer' else 'test-model'
        assert service['volumes'] == [
            '../cube/reviewed-postgres/cube.js:/cube/conf/cube.js:ro',
            '../cube/reviewed-postgres/security.js:/cube/conf/security.js:ro',
            '../cube/reviewed-postgres/security.json:/cube/conf/security.json:ro',
            '../cube/reviewed-postgres/' + directory + ':/cube/conf/model:ro',
            *(['../cube/bigquery:/cube/conf/bigquery:ro',
                '../tests/runtime:/cube/conf/tests/runtime:ro'] if context == 'consumer' else [])]


def test_backend_verifier_starts_restarts_and_cleans_the_same_reviewed_stack():
    source = (ROOT / 'scripts/verify_cube_runtime.sh').read_text()
    assert '-f infra/compose.reviewed.yaml)' in source
    assert 'start_private_stack registry-db demo-db cube cube-test cube-reviewed cube-reviewed-test edge' in source
    assert 'restart registry-db demo-db cube cube-test cube-reviewed cube-reviewed-test' in source
    assert 'start_private_stack registry-db demo-db cube cube-test cube-reviewed cube-reviewed-test\n' in source
    assert 'for service in cube cube-test cube-reviewed cube-reviewed-test; do' in source
    assert 'tests/integration/test_cube_reviewed_runtime.py' in source
    assert 'reviewed_rows_and_checkpoint_survive_restart' in source
    assert '"${COMPOSE[@]}" down --volumes --remove-orphans' in source
    assert "register_compatibility(Path(sys.argv[1]), 'cube', sys.argv[2], sys.argv[3])" in source
    assert source.index('register_compatibility(') < source.index('start_private_stack registry-db')


def test_all_five_cube_source_hosts_are_mandatory_in_exact_runtime_admission(monkeypatch):
    monkeypatch.delenv('GATEWAY_CUBE_TEST_HOSTS_FILE', raising=False)
    stack_class = runpy.run_path(str(ROOT / 'tests/integration/test_cube_compatibility.py'))['CubeStack']
    stack = stack_class()
    try:
        assert set(stack.hosts) == {'cube', 'cube-test', 'demo-db', *REVIEWED}
    finally:
        stack.client.close()


def test_existing_atomic_seed_cannot_publish_orders_without_unique_tenant_checkpoint():
    source = (ROOT / 'infra/postgres/init.sql').read_text()
    transaction = source.split('BEGIN;', 1)[1].split('COMMIT;', 1)[0]
    assert "COPY demo_source.orders FROM '/fixtures/orders.csv'" in transaction
    assert 'CREATE TABLE demo_source.watermark (tenant_id text PRIMARY KEY, loaded_at timestamptz NOT NULL)' in transaction
    assert 'INSERT INTO demo_source.watermark SELECT DISTINCT tenant_id, transaction_timestamp()' in transaction
    assert 'CREATE TABLE test_source.watermark (LIKE demo_source.watermark INCLUDING ALL)' in transaction
    assert 'INSERT INTO test_source.orders SELECT * FROM demo_source.orders' in transaction
    assert 'INSERT INTO test_source.watermark SELECT * FROM demo_source.watermark' in transaction


def test_exact_cleanup_includes_both_reviewed_service_containers(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from scripts import gateway_integration as harness
    project = 'gateway-cube-' + 'a' * 16
    label = project + '-local'
    harness.register_compatibility(tmp_path, 'cube', project, label)
    existing = {'container': {'b' * 64, 'c' * 64}, 'network': {project + '_private'},
        'volume': {project + '_demo-data'}, 'image': {'sha256:' + 'd' * 64}}
    removed = []

    def command(args, *, timeout):
        assert args[:1] == ['docker'] and timeout == 15
        assert args[-2:] == ['--filter', 'label=gateway.cube.run=' + label]
        return SimpleNamespace(stdout='\n'.join(existing[args[1]]))

    def remove(kind, identifier):
        existing[kind].remove(identifier)
        removed.append((kind, identifier))

    monkeypatch.setattr(harness.runtime, 'run', command)
    monkeypatch.setattr(harness.runtime, 'remove_exact', remove)
    harness.cleanup_compatibility(tmp_path)
    assert ('container', 'b' * 64) in removed and ('container', 'c' * 64) in removed
    assert not any(existing.values())


def test_reviewed_backend_union_includes_every_required_new_identity():
    from scripts.gateway_integration import collect_backend_identities
    identities = collect_backend_identities()
    for context in ('consumer', 'fixture_test'):
        assert ('tests.integration.test_cube_reviewed_runtime',
            f'test_reviewed_rows_and_checkpoint_survive_restart[{context}]') in identities
    assert ('tests.integration.test_reviewed_execution_runtime',
        'test_actual_reviewed_publication_worker_replay_and_reauthorized_stored_result') in identities


def test_missing_reviewed_stack_is_required_failure_never_skip():
    import os
    import subprocess
    import sys
    environment = {key: value for key, value in os.environ.items()
        if not key.startswith('GATEWAY_CUBE_TEST_') and key not in {'PYTEST_ADDOPTS', 'PYTEST_CURRENT_TEST'}}
    environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    result = subprocess.run([sys.executable, '-m', 'pytest',
        'tests/integration/test_cube_reviewed_runtime.py::test_actual_reviewed_empty_result_cannot_claim_fresh_checkpoint',
        '-q'], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert 'explicitly prepared private synthetic Cube stack is required' in result.stdout
    assert '1 error' in result.stdout and 'skipped' not in result.stdout


def test_required_bigquery_package_probe_uses_existing_isolated_reviewed_service():
    source = (ROOT / 'scripts/verify_cube_runtime.sh').read_text()
    command = 'bounded 60 "${COMPOSE[@]}" exec -T -e GATEWAY_BIGQUERY_CONTROLS_DIR=/cube/conf/bigquery cube-reviewed node --test --test-reporter=spec tests/runtime/bigquery-package.test.js'
    assert source.splitlines().count(command) == 1
    assert source.index('CHECKPOINT=cube-bigquery-package') < source.index(command) < source.index('CHECKPOINT=cube-egress')
    assert 'set -euo pipefail' in source
