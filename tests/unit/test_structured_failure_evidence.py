"""Offline disclosure controls, never evidence that a backend passed."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]


def diagnostics():
    module = importlib.import_module('scripts.pytest_runtime_diagnostics')
    assert hasattr(module, 'phase_failure_evidence'), 'Missing structured failure evidence seam'
    return module


def commit_fixture(root):
    subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), 'add', 'tests'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c',
        'user.email=fixture@example.test', 'commit', '-qm', 'Fixture source'], check=True, capture_output=True)


def test_source_identity_strips_parameter_payload_and_rejects_undeclared_names():
    api = diagnostics()
    node = 'tests/unit/test_gateway_integration_harness.py::test_modes_are_explicit_and_have_exact_argument_arity'
    assert api.source_test_identity(node + '[private-password::payload]') == node
    for forged in ('../../private.py::test_secret', node + '_forged', 'tests/conftest.py::test_forged'):
        assert api.source_test_identity(forged) is None


@pytest.mark.parametrize('reason,result', [
    ('process_exit', {'exit_status': 7}), ('timeout', {'exit_status': 124, 'timed_out': True}),
    ('output_limit', {'exit_status': 125, 'output_limit_exceeded': True}),
    ('interrupted', {'exit_status': 143, 'interrupted': True}),
    ('unverified_cleanup', {'exit_status': 126, 'descendant_cleanup': False})])
def test_every_failed_process_has_only_a_fixed_reason(tmp_path, reason, result):
    api = diagnostics()
    log = tmp_path / 'private.log'; log.write_text('PRIVATE_SENTINEL\n'); log.chmod(0o600)
    control = {'descendant_cleanup': True, **result}
    value = api.phase_failure_evidence(log, control)
    assert value == {'reason': reason, 'details_status': 'unavailable', 'markers': []}
    assert 'PRIVATE_SENTINEL' not in json.dumps(value)


@pytest.mark.parametrize('payload', ['not-json', '{"kind":"checkpoint","checkpoint":"PRIVATE_SENTINEL","exit_status":7}',
    '{"kind":"checkpoint","checkpoint":"cube-images","exit_status":7,"raw":"PRIVATE_SENTINEL"}', 'x' * 9000],
    ids=['malformed-json', 'unknown-enum', 'extra-field', 'oversized'])
def test_malformed_marker_is_explicitly_unavailable_without_raw_fallback(tmp_path, payload):
    api = diagnostics()
    path = tmp_path / 'private.log'; path.write_text(api.EVIDENCE_PREFIX + payload + '\n'); path.chmod(0o600)
    value = api.phase_failure_evidence(path, {'exit_status': 7, 'descendant_cleanup': True})
    assert value['details_status'] == 'invalid'
    assert value['markers'] == []
    assert 'PRIVATE_SENTINEL' not in json.dumps(value)


def test_known_checkpoint_is_selected_without_adjacent_private_output(tmp_path):
    api = diagnostics()
    marker = {'kind': 'checkpoint', 'checkpoint': 'cube-images', 'exit_status': 7}
    path = tmp_path / 'phase.log'
    path.write_text('PRIVATE_SENTINEL\n' + api.EVIDENCE_PREFIX + json.dumps(marker) + '\n'); path.chmod(0o600)
    value = api.phase_failure_evidence(path, {'exit_status': 7, 'descendant_cleanup': True})
    assert value['details_status'] == 'available' and value['markers'] == [marker]


@pytest.mark.parametrize('mode', ['backend', 'demo'])
def test_real_pytest_marker_arrives_before_slow_teardown_and_timeout(tmp_path, mode):
    api = diagnostics()
    tests = tmp_path / 'tests'; tests.mkdir()
    (tests / 'conftest.py').write_text('from pathlib import Path\n'
        'from scripts import pytest_runtime_diagnostics as api\napi.ROOT = Path.cwd()\n')
    secret = 's' * 43
    (tests / 'test_failure.py').write_text('import time\nfrom pathlib import Path\nimport pytest\n'
        '@pytest.fixture\ndef teardown():\n    yield\n    Path("teardown").touch()\n    time.sleep(30)\n'
        '@pytest.mark.parametrize("value", [' + repr(secret) + '])\n'
        'def test_failure(value, teardown):\n    private_local = "PRIVATE_LOCAL"\n'
        '    raise RuntimeError("PRIVATE_MESSAGE " + value)\n')
    commit_fixture(tmp_path)
    log = tmp_path / 'phase.log'
    environment = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    command = [sys.executable, '-m', 'pytest', 'tests/test_failure.py', '-q',
        '-p', 'scripts.pytest_runtime_diagnostics', '--gateway-failure-evidence']
    code = 'from scripts.runtime_process import bounded_process; import json; '
    code += 'print(json.dumps(bounded_process(' + repr(command) + ',' + repr(str(log)) + ',timeout=3,cwd=' + repr(str(tmp_path)) + ')))'
    child = subprocess.Popen([sys.executable, '-c', code], env=environment, stdout=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 2
        text = ''
        while time.monotonic() < deadline:
            text = log.read_text() if log.exists() else ''
            if (tmp_path / 'teardown').exists() and api.EVIDENCE_PREFIX in text: break
            time.sleep(.01)
        assert (tmp_path / 'teardown').exists(), 'Fixture teardown was not reached'
        assert api.EVIDENCE_PREFIX in text, 'Structured evidence must be flushed before teardown/timeout'
        assert child.poll() is None
        marker_line = next(line for line in text.splitlines() if line.startswith(api.EVIDENCE_PREFIX))
        marker = json.loads(marker_line[len(api.EVIDENCE_PREFIX):])
        assert marker['test'] == 'tests/test_failure.py::test_failure'
        assert marker['stage'] == 'call' and marker['exception'] == 'RuntimeError'
        assert marker['trace'][-1] == {'file': 'tests/test_failure.py', 'line': 12}
        assert all(value not in marker_line for value in (secret, 'PRIVATE_MESSAGE', 'PRIVATE_LOCAL'))
        control = json.loads(child.communicate(timeout=5)[0])
        assert control['timed_out'] and control['descendant_cleanup']
    finally:
        if child.poll() is None: child.terminate(); child.wait(timeout=5)


@pytest.mark.parametrize('mode', ['backend', 'demo'])
def test_phase_diagnostics_are_emitted_before_outer_cleanup(tmp_path, monkeypatch, capsys, mode):
    api = diagnostics()
    harness = importlib.import_module('scripts.gateway_integration')
    monkeypatch.setattr(harness, 'preflight', lambda mode: None)
    monkeypatch.setattr(harness, 'current_source', lambda: {})
    monkeypatch.setattr(harness.runtime, 'run', lambda *a, **k: type('Result', (), {'stdout': 'fixed'})())
    real_phase = harness.run_phase
    program = tmp_path / 'fail.py'; program.write_text('print("PRIVATE_SENTINEL");raise SystemExit(7)')
    def phase(name, command, root, budget, seconds, **kwargs):
        if name == 'exact-cleanup':
            assert api.PHASE_PREFIX in capsys.readouterr().err
            return {'phase': name, 'status': 'PASS'}
        return real_phase(name, [sys.executable, str(program)], root, budget, 2)
    monkeypatch.setattr(harness, 'run_phase', phase)
    report = harness.integrate(mode, tmp_path / 'run')
    assert report['status'] == 'BLOCKED'
    assert report['phases'][0]['failure_evidence']['reason'] == 'process_exit'
    assert 'PRIVATE_SENTINEL' not in json.dumps(report)


@pytest.mark.parametrize('script', ['verify_cube_runtime.sh', 'verify_registry_postgres.sh'])
def test_shell_checkpoint_is_emitted_before_private_resources_are_removed(tmp_path, script):
    import re
    api = diagnostics()
    source = (ROOT / 'scripts' / script).read_text()
    def function(name):
        found = re.search(rf'^{name}\(\) \{{\n.*?^\}}', source, re.M | re.S)
        assert found, 'Missing failure checkpoint before private cleanup'
        return found.group()
    code = 'set -euo pipefail\n' + function('failure_checkpoint') + '\n' + function('cleanup')
    code += '\nCHECKPOINT=cube-images\nTEMP_DIR="$TEMP"\nCOMPOSE=(unused)\n'
    code += 'CONTAINER_ID=unused\nGATEWAY_PROBE_IMAGE=unused\nexec 3>&1\n'
    code += 'bounded() { echo CLEANUP_STARTED >&3; }\ndocker() { echo CLEANUP_STARTED >&3; }\ntrap cleanup EXIT\nexit 7\n'
    # Both streams are one descriptor in the real phase supervisor.
    result = subprocess.run(['bash', '-c', code], env={**os.environ, 'TEMP': str(tmp_path / 'private')},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5)
    assert result.returncode == 7
    assert result.stdout.splitlines()[0].startswith(api.EVIDENCE_PREFIX)
    assert 'CLEANUP_STARTED' in result.stdout.splitlines()[1:]
    marker = json.loads(result.stdout.splitlines()[0][len(api.EVIDENCE_PREFIX):])
    assert marker == {'kind': 'checkpoint', 'checkpoint': 'cube-images', 'exit_status': 7}


@pytest.mark.parametrize('selection', ['backend', 'demo'])
def test_acceptance_pytest_adds_only_explicit_evidence_plugin(selection):
    api = diagnostics()
    command = [sys.executable, '-m', 'pytest', 'tests/integration/test_registry.py', '-m', selection, '-q']
    assert api.evidence_pytest_command(command) == [*command, '-p', 'scripts.pytest_runtime_diagnostics', '--gateway-failure-evidence']
    assert api.evidence_pytest_command(['npm', 'test']) == ['npm', 'test']


def test_nested_suite_evidence_reaches_parent_log_without_private_output(tmp_path, capsys):
    api = diagnostics()
    marker = {'kind': 'checkpoint', 'checkpoint': 'cube-probe-tests', 'exit_status': 1}
    evidence = {'reason': 'process_exit', 'details_status': 'available', 'markers': [marker]}
    assert hasattr(api, 'emit_phase_failure'), 'Missing safe nested phase evidence propagation'
    api.emit_phase_failure(evidence)
    log = tmp_path/'outer.log'; log.write_text(capsys.readouterr().err); log.chmod(0o600)
    assert api.phase_failure_evidence(log, {'exit_status': 1, 'descendant_cleanup': True}) == evidence


@pytest.mark.parametrize('failure', ['timeout', 'phase_budget'])
def test_nested_backend_failure_retains_inner_reason_when_outer_process_did_not_timeout(tmp_path, capsys, failure):
    api = diagnostics()
    harness = importlib.import_module('scripts.gateway_integration')
    command = [sys.executable, '-c', 'import time; time.sleep(30)']
    budget, seconds = (harness.Budget(), .1) if failure == 'timeout' else (harness.Budget(1), 5)
    row = harness.run_phase('backends', command, tmp_path, budget, seconds)
    assert row['status'] == 'BLOCKED'
    outer = tmp_path / 'outer.log'
    outer.write_text('PRIVATE_SENTINEL\n' + capsys.readouterr().err)
    outer.chmod(0o600)
    evidence = api.phase_failure_evidence(outer,
        {'exit_status': 1, 'timed_out': False, 'descendant_cleanup': True})
    assert evidence == {'reason': 'process_exit', 'details_status': 'available',
        'markers': [{'kind': 'phase', 'phase': 'backends', 'reason': failure}]}
    assert 'PRIVATE_SENTINEL' not in json.dumps(evidence)


@pytest.mark.parametrize('marker', [
    {'kind': 'phase', 'phase': 'PRIVATE_PHASE', 'reason': 'timeout'},
    {'kind': 'phase', 'phase': 'backends', 'reason': 'PRIVATE_REASON'},
    {'kind': 'phase', 'phase': 'backends', 'reason': 'timeout', 'raw': 'PRIVATE_SENTINEL'},
])
def test_nested_phase_metadata_rejects_unknown_or_extra_fields(tmp_path, marker):
    api = diagnostics()
    path = tmp_path / 'outer.log'
    path.write_text(api.EVIDENCE_PREFIX + json.dumps(marker) + '\n'); path.chmod(0o600)
    evidence = api.phase_failure_evidence(path, {'exit_status': 1, 'descendant_cleanup': True})
    assert evidence == {'reason': 'process_exit', 'details_status': 'invalid', 'markers': []}


def test_nested_phase_metadata_rejects_duplicate_json_keys(tmp_path):
    api = diagnostics()
    path = tmp_path / 'outer.log'
    path.write_text(api.EVIDENCE_PREFIX + '{"kind":"phase","phase":"backends",'
        '"reason":"PRIVATE_REASON","reason":"timeout"}\n'); path.chmod(0o600)
    evidence = api.phase_failure_evidence(path, {'exit_status': 1, 'descendant_cleanup': True})
    assert evidence == {'reason': 'process_exit', 'details_status': 'invalid', 'markers': []}


def test_backend_timeout_keeps_only_final_source_bound_progress_before_cleanup(tmp_path, monkeypatch, capsys):
    api = diagnostics()
    harness = importlib.import_module('scripts.gateway_integration')
    tests = tmp_path / 'tests'; tests.mkdir()
    (tests / 'conftest.py').write_text('from pathlib import Path\n'
        'from scripts import pytest_runtime_diagnostics as api\napi.ROOT = Path.cwd()\n')
    (tests / 'test_progress.py').write_text('import time\nimport pytest\n'
        '@pytest.fixture\ndef teardown():\n    yield\n    time.sleep(30)\n'
        'def test_first():\n    assert True\n'
        '@pytest.mark.parametrize("value", ["PRIVATE_PARAMETER"] )\n'
        'def test_second(value, teardown):\n    assert value\n')
    commit_fixture(tmp_path)
    monkeypatch.setattr(api, 'ROOT', tmp_path)
    monkeypatch.setattr(api, '_source_blobs', None)
    monkeypatch.setattr(api, '_source_commit', None)
    monkeypatch.setattr(harness, 'ROOT', tmp_path)
    command = [sys.executable, '-m', 'pytest', 'tests/test_progress.py', '-q']
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    row = harness.run_phase('backends', command, tmp_path, harness.Budget(), 1.5, env=env)
    expected = {'kind': 'progress', 'phase': 'backends', 'test': 'tests/test_progress.py::test_second',
        'stage': 'teardown', 'collected': 2, 'completed': 1}
    assert row['timed_out'] and row['descendant_cleanup']
    assert row['failure_evidence'] == {'reason': 'timeout', 'details_status': 'available', 'markers': [expected]}
    outer = tmp_path / 'outer.log'; outer.write_text(capsys.readouterr().err); outer.chmod(0o600)
    evidence = api.phase_failure_evidence(outer, {'exit_status': 1, 'descendant_cleanup': True})
    assert evidence == {'reason': 'process_exit', 'details_status': 'available',
        'markers': [expected, {'kind': 'phase', 'phase': 'backends', 'reason': 'timeout'}]}
    assert 'PRIVATE_PARAMETER' not in outer.read_text()


@pytest.mark.parametrize('change', [
    {'test': 'tests/unit/test_structured_failure_evidence.py::test_FORGED'},
    {'test': 'tests/unit/test_structured_failure_evidence.py::test_nested_phase_metadata_rejects_duplicate_json_keys[PRIVATE_PARAMETER]'},
    {'phase': 'PRIVATE_PHASE'}, {'stage': 'PRIVATE_STAGE'}, {'collected': True},
    {'collected': 1000001}, {'completed': -1}, {'completed': 3}, {'completed': 2}, {'raw': 'PRIVATE_SENTINEL'},
])
def test_backend_progress_rejects_forged_identity_counts_stage_and_extra_fields(tmp_path, change):
    api = diagnostics()
    marker = {'kind': 'progress', 'phase': 'backends',
        'test': 'tests/unit/test_gateway_integration_harness.py::test_modes_are_explicit_and_have_exact_argument_arity',
        'stage': 'call', 'collected': 2, 'completed': 1, **change}
    path = tmp_path / 'outer.log'; path.write_text(api.EVIDENCE_PREFIX + json.dumps(marker) + '\n'); path.chmod(0o600)
    evidence = api.phase_failure_evidence(path, {'exit_status': 1, 'descendant_cleanup': True})
    assert evidence == {'reason': 'process_exit', 'details_status': 'invalid', 'markers': []}


@pytest.mark.parametrize('count', [1320, 10000, 10001])
def test_private_progress_has_independent_encounter_limit_and_exports_one_snapshot(tmp_path, count):
    api = diagnostics()
    marker = {'kind': 'progress', 'phase': 'backends',
        'test': 'tests/unit/test_gateway_integration_harness.py::test_modes_are_explicit_and_have_exact_argument_arity',
        'stage': 'complete', 'collected': 329, 'completed': 329}
    path = tmp_path / 'private.log'
    path.write_text((api.PROGRESS_PREFIX + json.dumps(marker) + '\n') * count); path.chmod(0o600)
    evidence = api.phase_failure_evidence(path, {'exit_status': 1, 'descendant_cleanup': True}, progress=True)
    assert evidence == {'reason': 'process_exit', 'details_status': 'available' if count <= 10000 else 'invalid',
        'markers': [marker] if count <= 10000 else []}


@pytest.mark.parametrize('excess', [False, True])
def test_private_progress_has_independent_byte_limit(tmp_path, monkeypatch, excess):
    api = diagnostics()
    marker = {'kind': 'progress', 'phase': 'backends',
        'test': 'tests/unit/test_gateway_integration_harness.py::test_modes_are_explicit_and_have_exact_argument_arity',
        'stage': 'call', 'collected': 329, 'completed': 328}
    line = api.PROGRESS_PREFIX + json.dumps(marker) + '\n'
    monkeypatch.setattr(api, 'MAX_PROGRESS_BYTES', len(line.encode()) * 2 - int(excess))
    path = tmp_path / 'private.log'; path.write_text(line * 2); path.chmod(0o600)
    evidence = api.phase_failure_evidence(path, {'exit_status': 1, 'descendant_cleanup': True}, progress=True)
    assert evidence['details_status'] == ('invalid' if excess else 'available')
    assert evidence['markers'] == ([] if excess else [marker])


def test_private_progress_totals_count_final_selected_parameterized_cases(tmp_path, monkeypatch):
    api = diagnostics()
    harness = importlib.import_module('scripts.gateway_integration')
    tests = tmp_path / 'tests'; tests.mkdir()
    (tests / 'conftest.py').write_text('from pathlib import Path\n'
        'from scripts import pytest_runtime_diagnostics as api\napi.ROOT = Path.cwd()\n')
    (tests / 'test_progress.py').write_text('import pytest\n'
        '@pytest.mark.parametrize("value", ["PRIVATE_FIRST", "PRIVATE_SECOND"])\n'
        'def test_selected(value):\n    assert value\n'
        'def test_deselected():\n    assert True\n')
    commit_fixture(tmp_path)
    monkeypatch.setattr(api, 'ROOT', tmp_path)
    monkeypatch.setattr(api, '_source_blobs', None)
    monkeypatch.setattr(api, '_source_commit', None)
    monkeypatch.setattr(harness, 'ROOT', tmp_path)
    command = [sys.executable, '-m', 'pytest', 'tests/test_progress.py', '-q', '-k', 'test_selected']
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    row = harness.run_phase('backends', command, tmp_path, harness.Budget(), 5, env=env)
    assert row['status'] == 'PASS' and 'failure_evidence' not in row
    # Simulate a later non-test failure solely to inspect the private progress;
    # these counters are progress, never proof of testcase/JUnit acceptance.
    evidence = api.phase_failure_evidence(tmp_path / 'backends.log',
        {'exit_status': 1, 'descendant_cleanup': True}, progress=True)
    assert evidence['markers'] == [{'kind': 'progress', 'phase': 'backends',
        'test': 'tests/test_progress.py::test_selected', 'stage': 'complete', 'collected': 2, 'completed': 2}]
    assert 'PRIVATE_' not in json.dumps(evidence)


@pytest.mark.parametrize('setup_outcome', ['raise RuntimeError("PRIVATE_MESSAGE")', 'pytest.skip("PRIVATE_MESSAGE")'],
    ids=['failed', 'skipped'])
def test_unsuccessful_setup_progress_moves_directly_to_teardown(tmp_path, monkeypatch, setup_outcome):
    api = diagnostics()
    harness = importlib.import_module('scripts.gateway_integration')
    tests = tmp_path / 'tests'; tests.mkdir()
    (tests / 'conftest.py').write_text('from pathlib import Path\n'
        'from scripts import pytest_runtime_diagnostics as api\napi.ROOT = Path.cwd()\n')
    (tests / 'test_progress.py').write_text('import time\nimport pytest\n'
        '@pytest.fixture\ndef fixture(request):\n'
        '    request.addfinalizer(lambda: time.sleep(30))\n    ' + setup_outcome + '\n'
        'def test_case(fixture):\n    assert False\n')
    commit_fixture(tmp_path)
    monkeypatch.setattr(api, 'ROOT', tmp_path)
    monkeypatch.setattr(api, '_source_blobs', None)
    monkeypatch.setattr(api, '_source_commit', None)
    monkeypatch.setattr(harness, 'ROOT', tmp_path)
    command = [sys.executable, '-m', 'pytest', 'tests/test_progress.py', '-q']
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    row = harness.run_phase('backends', command, tmp_path, harness.Budget(), 1.5, env=env)
    assert row['timed_out'] and row['descendant_cleanup']
    assert row['failure_evidence']['markers'][-1] == {'kind': 'progress', 'phase': 'backends',
        'test': 'tests/test_progress.py::test_case', 'stage': 'teardown', 'collected': 1, 'completed': 0}
    assert 'PRIVATE_MESSAGE' not in json.dumps(row['failure_evidence'])


@pytest.mark.parametrize('failure,checkpoint', [
    ('preflight', 'backend-preflight'),
    ('collection', 'backend-collection'),
    ('tests', 'backend-tests'),
    ('junit', 'backend-junit-admission'),
    ('identity', 'backend-source-recheck'),
    ('endpoint', 'backend-endpoint-recheck'),
    ('write', 'backend-receipt-write'),
    ('report', 'backend-report-admission'),
])
def test_backend_recorder_failure_retains_admission_checkpoint_without_exception_text(
        tmp_path, monkeypatch, capsys, failure, checkpoint):
    api = diagnostics()
    harness = importlib.import_module('scripts.gateway_integration')
    monkeypatch.setattr(harness.verify_demo, 'prerequisite_images', lambda _: {})
    if failure == 'preflight':
        def reject_images(*args): raise ValueError('PRIVATE_EXCEPTION_MESSAGE')
        monkeypatch.setattr(harness.verify_demo, 'prerequisite_images', reject_images)
    endpoint_calls = []
    def endpoints(_):
        endpoint_calls.append(None)
        return {'identity': 'changed' if failure == 'endpoint' and len(endpoint_calls) > 1 else 'fixed'}
    monkeypatch.setattr(harness.verify_demo, 'validate_backend_endpoints', endpoints)
    source_calls = []
    def source():
        source_calls.append(None)
        return {'identity': 'changed' if failure == 'identity' and len(source_calls) > 1 else 'fixed'}
    monkeypatch.setattr(harness, 'current_source', source)
    def collection():
        if failure == 'collection': raise ValueError('PRIVATE_EXCEPTION_MESSAGE')
        return {('tests.integration.test_registry', 'test_a')}
    monkeypatch.setattr(harness, 'collect_backend_identities', collection)
    def phase(name, command, root, *args, **kwargs):
        actual = 'test_other' if failure == 'junit' else 'test_a'
        (root / 'backends.xml').write_text('<testsuites><testsuite tests="1" errors="0" failures="0" skipped="0">'
            '<testcase classname="tests.integration.test_registry" name="' + actual + '"/>'
            '</testsuite></testsuites>')
        return {'phase': name, 'status': 'BLOCKED' if failure == 'tests' else 'PASS',
            'exit_status': 1 if failure == 'tests' else 0}
    monkeypatch.setattr(harness, 'run_phase', phase)
    if failure == 'write':
        def reject_write(*args): raise OSError('PRIVATE_EXCEPTION_MESSAGE')
        monkeypatch.setattr(harness.runtime, 'private_json', reject_write)
    if failure == 'report':
        def reject(*args): raise ValueError('PRIVATE_EXCEPTION_MESSAGE')
        monkeypatch.setattr(harness, 'validate_backend_report', reject)
    tmp_path.chmod(0o700)
    with pytest.raises((ValueError, OSError, harness.Blocked)):
        harness.record_backends(tmp_path / 'receipts')
    output = capsys.readouterr().err
    log = tmp_path / 'outer.log'; log.write_text(output); log.chmod(0o600)
    evidence = api.phase_failure_evidence(log, {'exit_status': 1, 'descendant_cleanup': True})
    assert evidence == {'reason': 'process_exit', 'details_status': 'available',
        'markers': [{'kind': 'checkpoint', 'checkpoint': checkpoint, 'exit_status': 1}]}
    assert 'PRIVATE_EXCEPTION_MESSAGE' not in output
    if failure != 'report': assert not (tmp_path / 'receipts' / 'backends.json').exists()


def test_phase_budget_failure_emits_evidence_without_starting_work(tmp_path, capsys):
    api = diagnostics()
    harness = importlib.import_module('scripts.gateway_integration')
    row = harness.run_phase('sample', [sys.executable], tmp_path, harness.Budget(1), 5)
    assert row['failure_evidence'] == {'reason': 'phase_budget', 'details_status': 'unavailable', 'markers': []}
    assert api.PHASE_PREFIX in capsys.readouterr().err
    assert not (tmp_path / 'sample.log').exists()


def test_unverified_phase_row_reaches_outer_summary_before_cleanup(tmp_path, monkeypatch, capsys):
    api = diagnostics()
    harness = importlib.import_module('scripts.gateway_integration')
    from scripts import runtime_process
    monkeypatch.setattr(harness, 'preflight', lambda mode: None)
    monkeypatch.setattr(harness, 'current_source', lambda: {})
    monkeypatch.setattr(harness.runtime, 'run', lambda *a, **k: type('Result', (), {'stdout': 'fixed'})())
    monkeypatch.setattr(runtime_process, 'bounded_process', lambda *a, **k: {'exit_status': 126, 'descendant_cleanup': False})
    real_phase = harness.run_phase
    def phase(name, *args, **kwargs):
        if name == 'exact-cleanup':
            assert api.PHASE_PREFIX in capsys.readouterr().err
            return {'phase': name, 'status': 'PASS'}
        return real_phase(name, *args, **kwargs)
    monkeypatch.setattr(harness, 'run_phase', phase)
    report = harness.integrate('backend', tmp_path / 'run')
    assert report['phases'][0]['phase'] == 'client-dependencies'
    assert report['phases'][0]['status'] == 'BLOCKED'
    assert report['phases'][0]['failure_evidence']['reason'] == 'unverified_cleanup'
    assert report['phases'][-1]['phase'] == 'exact-cleanup'


@pytest.mark.parametrize('probe_manifest', [False, True])
def test_real_assertion_marker_keeps_assertion_class_and_source_line(tmp_path, monkeypatch, probe_manifest):
    api = diagnostics()
    tests = tmp_path / 'tests'; tests.mkdir()
    (tests / 'conftest.py').write_text('from pathlib import Path\n'
        'from scripts import pytest_runtime_diagnostics as api\napi.ROOT=Path.cwd()\n')
    (tests / 'test_assert.py').write_text('def test_assertion():\n    assert False\n')
    commit_fixture(tmp_path)
    command = [sys.executable, '-m', 'pytest', '-q', '-p', 'scripts.pytest_runtime_diagnostics', '--gateway-failure-evidence']
    environment = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    if probe_manifest:
        monkeypatch.setattr(api, 'ROOT', tmp_path)
        monkeypatch.setattr(api, '_source_blobs', None)
        monkeypatch.setattr(api, '_source_commit', None)
        manifest = tmp_path / 'source.json'; manifest.write_text(json.dumps(api.freeze_source())); manifest.chmod(0o600)
        command.append('--gateway-failure-source=' + str(manifest))
        environment['PATH'] = '/git-is-unavailable-in-probe'
    result = subprocess.run(command, cwd=tmp_path, env=environment,
        text=True, capture_output=True, timeout=5)
    assert result.returncode == 1
    line = next(line for line in result.stdout.splitlines() if line.startswith(api.EVIDENCE_PREFIX))
    assert json.loads(line[len(api.EVIDENCE_PREFIX):]) == {'kind': 'test', 'test': 'tests/test_assert.py::test_assertion',
        'stage': 'call', 'exception': 'AssertionError', 'trace': [{'file': 'tests/test_assert.py', 'line': 2}]}


def test_source_admission_rejects_untracked_and_modified_declarations(tmp_path, monkeypatch):
    api = diagnostics()
    tests = tmp_path / 'tests'; tests.mkdir()
    source = tests / 'test_bound.py'; source.write_text('def test_bound():\n    assert True\n')
    commit_fixture(tmp_path)
    monkeypatch.setattr(api, 'ROOT', tmp_path)
    assert hasattr(api, 'freeze_source'), 'Missing committed source binding'
    monkeypatch.setattr(api, '_source_blobs', None)
    api.freeze_source()
    assert api.source_test_identity('tests/test_bound.py::test_bound') is not None
    token = 'PRIVATE_RUNTIME_TOKEN'
    untracked = tests / ('test_' + token + '.py')
    untracked.write_text('def test_' + token + '():\n    assert False\n')
    assert api.source_test_identity('tests/' + untracked.name + '::test_' + token) is None
    source.write_text(source.read_text() + 'def test_' + token + '():\n    assert False\n')
    assert api.source_test_identity('tests/test_bound.py::test_' + token) is None
    assert api.source_test_identity('tests/test_bound.py::test_bound') is None


@pytest.mark.parametrize('exception', ['runtime', 'os', 'alarm'])
def test_supervisor_exception_retains_fixed_phase_evidence(tmp_path, monkeypatch, capsys, exception):
    api = diagnostics()
    harness = importlib.import_module('scripts.gateway_integration')
    from scripts import runtime_process
    error = {'runtime': RuntimeError, 'os': OSError, 'alarm': harness.Blocked}[exception]
    def fail(*args, **kwargs): raise error('PRIVATE_EXCEPTION_MESSAGE')
    monkeypatch.setattr(runtime_process, 'bounded_process', fail)
    with pytest.raises(harness.Blocked) as caught:
        harness.run_phase('backend-compatibility', [sys.executable], tmp_path, harness.Budget(), 5)
    row = caught.value.phase_row
    assert row['status'] == 'BLOCKED'
    assert row['failure_evidence']['reason'] == ('work_interrupted' if exception == 'alarm' else 'supervision_exception')
    output = capsys.readouterr().err
    assert api.PHASE_PREFIX in output and 'PRIVATE_EXCEPTION_MESSAGE' not in output


def test_marker_processing_caps_encounters_before_repeated_ast_work(tmp_path, monkeypatch):
    api = diagnostics()
    marker = {'kind': 'test', 'test': 'tests/unit/test_gateway_integration_harness.py::test_modes_are_explicit_and_have_exact_argument_arity',
        'stage': 'call', 'exception': 'AssertionError', 'trace': []}
    path = tmp_path / 'private.log'; path.write_text((api.EVIDENCE_PREFIX + json.dumps(marker) + '\n') * 200); path.chmod(0o600)
    actual = api.validate_failure_marker; calls = []
    def validate(value): calls.append(value); return actual(value)
    monkeypatch.setattr(api, 'validate_failure_marker', validate)
    value = api.phase_failure_evidence(path, {'exit_status': 1, 'descendant_cleanup': True})
    assert len(calls) <= 32
    assert value['details_status'] == 'invalid' and value['markers'] == []


def test_exact_cleanup_runs_when_diagnostic_source_binding_is_unavailable(tmp_path, monkeypatch):
    api = diagnostics()
    harness = importlib.import_module('scripts.gateway_integration')
    from scripts import runtime_process
    launched = []
    def source_failure(*args, **kwargs): raise OSError('PRIVATE_GIT_ERROR')
    def process(command, output, **kwargs):
        launched.append(command); output.write_text('cleanup verified'); output.chmod(0o600)
        return {'exit_status': 0, 'descendant_cleanup': True}
    monkeypatch.setattr(api, 'freeze_source', source_failure)
    monkeypatch.setattr(runtime_process, 'bounded_process', process)
    row = harness.run_phase('exact-cleanup', [sys.executable], tmp_path, harness.Budget(), 5)
    assert launched == [[sys.executable]]
    assert row['phase'] == 'exact-cleanup' and row['status'] == 'PASS'
    launched.clear()
    monkeypatch.setattr(harness, 'preflight', lambda mode: None)
    monkeypatch.setattr(harness, 'current_source', lambda: {})
    monkeypatch.setattr(harness.runtime, 'run', lambda *a, **k: type('Result', (), {'stdout': 'fixed'})())
    report = harness.integrate('backend', tmp_path / 'run')
    assert report['status'] == 'BLOCKED' and report['phases'][-1]['status'] == 'PASS'
    assert len(launched) == 1 and '--cleanup-run' in launched[0]


def test_cleanup_exception_retains_its_blocked_phase_evidence(tmp_path, monkeypatch):
    harness = importlib.import_module('scripts.gateway_integration')
    monkeypatch.setattr(harness, 'preflight', lambda mode: None)
    monkeypatch.setattr(harness, 'current_source', lambda: {})
    monkeypatch.setattr(harness.runtime, 'run', lambda *a, **k: type('Result', (), {'stdout': 'fixed'})())
    expected = {'phase': 'exact-cleanup', 'status': 'BLOCKED', 'exit_status': None,
        'failure_evidence': {'reason': 'supervision_exception', 'details_status': 'unavailable', 'markers': []}}
    def phase(name, *args, **kwargs):
        if name == 'exact-cleanup':
            error = harness.Blocked('Fixed cleanup supervisor error'); error.phase_row = expected
            raise error
        return {'phase': name, 'status': 'BLOCKED', 'exit_status': 7}
    monkeypatch.setattr(harness, 'run_phase', phase)
    report = harness.integrate('backend', tmp_path / 'run')
    assert report['status'] == 'BLOCKED'
    assert report['phases'][-1] == expected


def test_cleanup_deadline_failure_emits_unavailable_fixed_evidence(tmp_path, monkeypatch, capsys):
    api = diagnostics()
    harness = importlib.import_module('scripts.gateway_integration')
    monkeypatch.setattr(harness, 'preflight', lambda mode: None)
    monkeypatch.setattr(harness, 'current_source', lambda: {})
    monkeypatch.setattr(harness.runtime, 'run', lambda *a, **k: type('Result', (), {'stdout': 'fixed'})())
    launched = []
    def phase(name, *args, **kwargs):
        launched.append(name)
        return {'phase': name, 'status': 'BLOCKED'}
    def deadline(root): raise harness.Blocked('PRIVATE_DEADLINE_DETAIL')
    monkeypatch.setattr(harness, 'run_phase', phase)
    monkeypatch.setattr(harness, 'cleanup_remaining', deadline)
    report = harness.integrate('backend', tmp_path / 'run')
    assert 'exact-cleanup' not in launched
    assert report['phases'][-1]['failure_evidence'] == {'reason': 'phase_budget', 'details_status': 'unavailable', 'markers': []}
    output = capsys.readouterr().err
    assert api.PHASE_PREFIX in output and 'PRIVATE_DEADLINE_DETAIL' not in output


def test_actual_bigquery_shell_checkpoint_survives_private_phase_and_summary(tmp_path, capsys):
    """Replay the exact shell emitter, without Docker or package substitution."""
    import re
    api = diagnostics()
    source = (ROOT / 'scripts/verify_cube_runtime.sh').read_text()
    function = re.search(r'^failure_checkpoint\(\) \{\n.*?^\}', source, re.MULTILINE | re.DOTALL).group(0)
    checkpoint = next(line for line in source.splitlines() if line == 'CHECKPOINT=cube-bigquery-package')
    result = subprocess.run(['bash', '-c', function + '\n' + checkpoint + '\nfailure_checkpoint 1\n'],
        text=True, capture_output=True, timeout=5)
    assert result.returncode == 0
    private = tmp_path / 'private-phase.log'
    private.write_text('PRIVATE_PACKAGE_ERROR\n' + result.stderr)
    private.chmod(0o600)
    evidence = api.phase_failure_evidence(private, {'exit_status': 1, 'descendant_cleanup': True})
    marker = {'kind': 'checkpoint', 'checkpoint': 'cube-bigquery-package', 'exit_status': 1}
    assert evidence == {'reason': 'process_exit', 'details_status': 'available', 'markers': [marker]}
    api.emit_phase_failure(evidence)
    selected = capsys.readouterr().err
    assert 'PRIVATE_PACKAGE_ERROR' not in selected
    outer = tmp_path / 'outer-phase.log'; outer.write_text(selected); outer.chmod(0o600)
    assert api.validate_phase_failure_evidence(api.phase_failure_evidence(
        outer, {'exit_status': 1, 'descendant_cleanup': True})) == evidence


def declared_shell_checkpoints(source):
    import re
    import shlex
    declared = set()
    for line in source.splitlines():
        if line.lstrip().startswith('#') or not re.search(r'\bCHECKPOINT\s*=', line):
            continue
        assignment = re.fullmatch(r'\s*CHECKPOINT=(.*)', line)
        assert assignment, 'Checkpoint assignment requires an explicit fixed literal'
        words = shlex.split(assignment[1], comments=True)
        assert len(words) == 1, 'Checkpoint assignment requires one fixed literal'
        declared.add(words[0])
    return declared


def test_every_declared_runtime_shell_checkpoint_has_explicit_evidence_admission():
    api = diagnostics()
    declared = set()
    for name in ('verify_cube_runtime.sh', 'verify_registry_postgres.sh'):
        declared.update(declared_shell_checkpoints((ROOT / 'scripts' / name).read_text()))
    assert 'cube-bigquery-package' in declared
    assert declared <= api.CHECKPOINTS


@pytest.mark.parametrize('assignment', ['CHECKPOINT=cube-v2', 'CHECKPOINT="cube-v2"',
    "CHECKPOINT='cube-v2'", 'CHECKPOINT=$DYNAMIC', 'CHECKPOINT="$(private_command)"'])
def test_checkpoint_inventory_cannot_silently_omit_unknown_or_dynamic_assignments(assignment):
    checkpoints = declared_shell_checkpoints(assignment)
    assert len(checkpoints) == 1 and not checkpoints <= diagnostics().CHECKPOINTS


@pytest.mark.parametrize('assignment', ['CHECKPOINT=', 'CHECKPOINT=cube-images; arbitrary',
    'if condition; then CHECKPOINT=cube-images; fi'])
def test_checkpoint_inventory_rejects_unreviewed_assignment_syntax(assignment):
    with pytest.raises(AssertionError):
        declared_shell_checkpoints(assignment)


@pytest.mark.parametrize('checkpoint', [
    'cube-bigquery-module-resolution', 'cube-bigquery-package-versions',
    'cube-bigquery-ledger', 'cube-bigquery-driver-construction', 'cube-bigquery-query',
    'cube-bigquery-serialization', 'cube-bigquery-result-hydration', 'cube-bigquery-replay',
    'cube-bigquery-io-guard', 'cube-bigquery-cleanup'])
def test_fixed_bigquery_substage_survives_actual_node_reporter_without_raw_errors(tmp_path, checkpoint):
    import shlex
    api = diagnostics()
    source = (ROOT / 'scripts/verify_cube_runtime.sh').read_text()
    command = next(line for line in source.splitlines() if 'node --test' in line and 'bigquery-package.test.js' in line)
    arguments = shlex.split(command)
    flags = arguments[arguments.index('node') + 1:-1]
    # The required explicit reporter keeps stderr marker prefixes stable across
    # image Node versions; no arbitrary TAP/comment parsing is introduced.
    assert flags == ['--test', '--test-reporter=spec']
    marker = {'kind': 'checkpoint', 'checkpoint': checkpoint, 'exit_status': 1}
    script = tmp_path / 'synthetic-package.test.cjs'
    script.write_text("const test=require('node:test');\n"
        "test('synthetic package boundary',()=>{\n"
        "  process.stderr.write(" + json.dumps(api.EVIDENCE_PREFIX + json.dumps(marker) + '\n') + ");\n"
        "  throw new Error('PRIVATE_PACKAGE_ERROR');\n});\n")
    result = subprocess.run(['node', *flags, str(script)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    private = tmp_path / 'package.log'; private.write_text(result.stdout + result.stderr); private.chmod(0o600)
    evidence = api.phase_failure_evidence(private, {'exit_status': 1, 'descendant_cleanup': True})
    assert evidence == {'reason': 'process_exit', 'details_status': 'available', 'markers': [marker]}
    assert 'PRIVATE_PACKAGE_ERROR' not in json.dumps(evidence)
    assert str(script) not in json.dumps(evidence)
    assert api.validate_phase_failure_evidence(evidence) == evidence


@pytest.mark.parametrize('marker', [
    {'kind': 'checkpoint', 'checkpoint': 'cube-bigquery-private-sentinel', 'exit_status': 1},
    {'kind': 'checkpoint', 'checkpoint': 'cube-bigquery-query', 'exit_status': 1, 'error': 'PRIVATE_SENTINEL'},
    {'kind': 'checkpoint', 'checkpoint': 'cube-bigquery-query', 'exit_status': True},
])
def test_bigquery_diagnostics_never_admit_dynamic_names_values_or_fields(tmp_path, marker):
    api = diagnostics()
    private = tmp_path / 'package.log'; private.write_text(api.EVIDENCE_PREFIX + json.dumps(marker) + '\n'); private.chmod(0o600)
    evidence = api.phase_failure_evidence(private, {'exit_status': 1, 'descendant_cleanup': True})
    assert evidence == {'reason': 'process_exit', 'details_status': 'invalid', 'markers': []}
    assert 'PRIVATE_SENTINEL' not in json.dumps(evidence)


def test_actual_bigquery_helper_vocabulary_and_receipts_match_private_parser(tmp_path):
    api = diagnostics()
    # Load the real helper and exercise its exported emitter. No package driver,
    # provider SDK, credentials, socket, or mocked response is needed here.
    program = """
const { STAGES, failureEvidence } = require('./tests/runtime/bigquery-probe-evidence.cjs');
const rows = STAGES.map(stage => {
  const lines = [];
  const receipt = failureEvidence(line => lines.push(line));
  receipt.stage(stage);
  receipt.emit();
  return { stage, lines };
});
process.stdout.write(JSON.stringify(rows));
"""
    result = subprocess.run(['node', '-e', program], cwd=ROOT,
        capture_output=True, text=True, timeout=5, check=True)
    rows = json.loads(result.stdout)
    assert isinstance(rows, list) and rows
    stages = [row['stage'] for row in rows]
    assert len(stages) == len(set(stages)) and set(stages) <= api.CHECKPOINTS
    for index, row in enumerate(rows):
        assert set(row) == {'stage', 'lines'} and len(row['lines']) == 1
        marker = {'kind': 'checkpoint', 'checkpoint': row['stage'], 'exit_status': 1}
        private = tmp_path / f'package-{index}.log'
        private.write_text('PRIVATE_PROVIDER_ERROR\n' + row['lines'][0])
        private.chmod(0o600)
        evidence = api.phase_failure_evidence(private, {'exit_status': 1, 'descendant_cleanup': True})
        assert evidence == {'reason': 'process_exit', 'details_status': 'available', 'markers': [marker]}
        assert api.validate_phase_failure_evidence(evidence) == evidence
        assert 'PRIVATE_PROVIDER_ERROR' not in json.dumps(evidence)


@pytest.mark.parametrize(('stage','error'), [('attestation','value'),('image_fixture_test','assertion'),('image_bootstrap','import'),('cleanup','timeout')])
def test_journey_failure_only_admits_fixed_stage_and_error(tmp_path,monkeypatch,stage,error):
    from types import SimpleNamespace
    api=diagnostics()
    node='tests/integration/test_bigquery_gateway.py::test_real_bigquery_author_fixture_publish_describe_worker_result'
    monkeypatch.setattr(api,'_collected_tests',{node})
    report=SimpleNamespace(nodeid=node,when='call',longrepr=SimpleNamespace(reprcrash=SimpleNamespace(
        message=f'tests.integration.source_journey_host.HostedJourneyFailure: Hosted synthetic BigQuery journey failed at {stage} ({error})')))
    marker=api.structured_journey_failure(report)
    assert marker=={'kind':'journey','stage':stage,'error':error}
    path=tmp_path/'private.log';path.write_text('PRIVATE_SENTINEL\n'+api.EVIDENCE_PREFIX+json.dumps(marker)+'\n');path.chmod(0o600)
    evidence=api.phase_failure_evidence(path,{'exit_status':1,'descendant_cleanup':True})
    assert evidence['markers']==[marker]
    assert api.validate_phase_failure_evidence(evidence)==evidence
    assert 'PRIVATE_SENTINEL' not in json.dumps(evidence)


@pytest.mark.parametrize('message',[
    'HostedJourneyFailure: Hosted synthetic BigQuery journey failed at PRIVATE_SENTINEL (value)',
    'HostedJourneyFailure: Hosted synthetic BigQuery journey failed at receipt (PRIVATE_SENTINEL)',
    'HostedJourneyFailure: Hosted synthetic BigQuery journey failed at receipt (value) PRIVATE_SENTINEL',
    'HostedJourneyFailure: Hosted synthetic BigQuery journey failed at receipt (value)\nPRIVATE_SENTINEL',
    'ValueError: Hosted synthetic BigQuery journey failed at receipt (value)',
    'HostedJourneyFailure: '+('PRIVATE_SENTINEL'*1000),
])
def test_journey_diagnostics_reject_unbounded_or_unrecognized_messages(monkeypatch,message):
    from types import SimpleNamespace
    api=diagnostics();node=api.JOURNEY_TEST
    monkeypatch.setattr(api,'_collected_tests',{node})
    report=SimpleNamespace(nodeid=node,when='call',longrepr=SimpleNamespace(reprcrash=SimpleNamespace(message=message)))
    assert api.structured_journey_failure(report) is None


def test_journey_diagnostics_are_scoped_to_exact_collected_call(monkeypatch):
    from types import SimpleNamespace
    api=diagnostics();node=api.JOURNEY_TEST
    report=SimpleNamespace(nodeid=node,when='setup',longrepr=SimpleNamespace(reprcrash=SimpleNamespace(message='HostedJourneyFailure: Hosted synthetic BigQuery journey failed at receipt (value)')))
    monkeypatch.setattr(api,'_collected_tests',{node})
    assert api.structured_journey_failure(report) is None
    report.when='call';monkeypatch.setattr(api,'_collected_tests',set())
    assert api.structured_journey_failure(report) is None


@pytest.mark.parametrize('marker',[
    {'kind':'journey','stage':'receipt','error':'value','raw':'PRIVATE_SENTINEL'},
    {'kind':'journey','stage':'PRIVATE_SENTINEL','error':'value'},
    {'kind':'journey','stage':'receipt','error':'PRIVATE_SENTINEL'},
    {'kind':'journey','stage':False,'error':'value'},
])
def test_journey_marker_schema_is_closed(marker):
    with pytest.raises(ValueError):diagnostics().validate_failure_marker(marker)


def test_actual_pytest_journey_failure_survives_closed_phase_summary(tmp_path):
    root=tmp_path/'source';folder=root/'tests/integration';folder.mkdir(parents=True)
    (folder/'test_bigquery_gateway.py').write_text('''
class HostedJourneyFailure(AssertionError): pass
HostedJourneyFailure.__module__ = 'tests.integration.source_journey_host'
def test_real_bigquery_author_fixture_publish_describe_worker_result():
    print('PRIVATE_PROVIDER_SENTINEL')
    raise HostedJourneyFailure('Hosted synthetic BigQuery journey failed at image_fixture_test (assertion)')
''')
    commit_fixture(root)
    (root/'journey_fixture_plugin.py').write_text('from pathlib import Path\nfrom scripts import pytest_runtime_diagnostics as api\napi.ROOT=Path('+repr(str(root))+')\napi._source_blobs=None\napi._source_commit=None\n')
    env={**os.environ,'PYTHONPATH':str(ROOT)+os.pathsep+str(root),'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'}
    result=subprocess.run([sys.executable,'-m','pytest','-q','-p','journey_fixture_plugin',
        '-p','scripts.pytest_runtime_diagnostics','--gateway-failure-evidence','tests/integration/test_bigquery_gateway.py'],
        cwd=root,env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==1
    api=diagnostics()
    selected=[line for line in result.stdout.splitlines() if line.startswith(api.EVIDENCE_PREFIX)
        and json.loads(line[len(api.EVIDENCE_PREFIX):]).get('kind')=='journey']
    assert len(selected)==1
    assert json.loads(selected[0][len(api.EVIDENCE_PREFIX):])=={'kind':'journey','stage':'image_fixture_test','error':'assertion'}
    private=tmp_path/'private.log';private.write_text('PRIVATE_PROVIDER_SENTINEL\n'+selected[0]+'\n');private.chmod(0o600)
    evidence=api.phase_failure_evidence(private,{'exit_status':1,'descendant_cleanup':True})
    assert evidence['details_status']=='available'
    assert evidence['markers']==[{'kind':'journey','stage':'image_fixture_test','error':'assertion'}]
    assert api.validate_phase_failure_evidence(evidence)==evidence
    assert 'PRIVATE_PROVIDER_SENTINEL' not in json.dumps(evidence)


def test_journey_diagnostic_vocabulary_matches_the_actual_host_bridge():
    from tests.integration import source_journey_host as host
    api=diagnostics()
    assert api.JOURNEY_STAGES==frozenset(host.HOST_STAGES)|frozenset('image_'+stage for stage in host.IMAGE_STAGES)
    assert api.JOURNEY_ERRORS==frozenset(host.FAILURE_ERRORS)
