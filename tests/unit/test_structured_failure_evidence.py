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
