"""Pre-phase observations cannot supply successful acceptance evidence."""
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess

import pytest

from scripts import ci_partition_driver as driver
from scripts import ci_partition_failures as failures
from tests.unit.test_ci_partition_failures import frozen_case, case, cli, seed_context, SENTINEL

pytestmark = [pytest.mark.unit, pytest.mark.contract]


def context(case):
    return {'identity': case['identity'], 'job_id': 'source',
            'manifest_sha256': case['env']['REVIEWED_MANIFEST_SHA256']}


def test_prephase_failure_is_separate_and_cohort_bound(case):
    seed_context(case)
    journal = failures.PrephaseJournal(driver, case['root'], context(case))
    error = subprocess.TimeoutExpired(['private-command'], 2, output=SENTINEL)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        with journal('module_binding'):
            raise error
    assert caught.value is error
    value = cli(case)
    assert value['prephase'] == {'status': 'available', 'stage': 'module_binding',
        'state': 'blocked', 'category': 'timeout', 'exit_status': None}


def write_rows(case, rows):
    path = case['root'] / 'prephase.jsonl'
    path.write_bytes(b''.join(driver.canonical(row) + b'\n' for row in rows))
    path.chmod(0o600)
    return path


def rows(case, *, job='source'):
    result = []
    for stage in failures.PREPHASE_STAGES:
        if job == 'source' and stage == 'source_admission':
            continue
        for state in ('entered', 'complete'):
            result.append({'schema_version': 1, **context(case), 'job_id': job,
                'sequence': len(result) + 1, 'stage': stage, 'state': state,
                'category': None, 'exit_status': None})
    return result


def read(case, *, job='source'):
    return failures.prephase_status(driver, case['root'], case['identity'], job,
                                   case['env']['REVIEWED_MANIFEST_SHA256'])


@pytest.mark.parametrize('job,count', [('source', 42), ('sql_execution', 44)])
def test_complete_grammar_has_bounded_records_and_reports_only_checkpoint(case, job, count):
    value = rows(case, job=job)
    assert len(value) == count <= failures.PREPHASE_RECORDS
    path = write_rows(case, value)
    assert path.stat().st_size <= failures.PREPHASE_LIMIT
    assert read(case, job=job) == {'status': 'available', 'stage': 'phase_receipt_write',
        'state': 'complete', 'category': None, 'exit_status': None}


@pytest.mark.parametrize('length', range(1, 45))
def test_every_complete_prefix_is_a_checkpoint_or_unfinished_observation(case, length):
    values = rows(case, job='sql_execution')[:length]
    write_rows(case, values)
    value = read(case, job='sql_execution')
    assert value['status'] == 'available'
    assert value['stage'] == values[-1]['stage']
    assert value['category'] == ('unfinished' if length % 2 else None)
    assert value['exit_status'] is None


@pytest.mark.parametrize('damage', [
    'partial', 'empty', 'malformed', 'duplicate_key', 'too_many', 'oversize', 'extra',
    'wrong_source', 'wrong_tree', 'wrong_digest', 'wrong_controller', 'wrong_run', 'wrong_attempt',
    'wrong_repository', 'wrong_workflow', 'wrong_job', 'wrong_pin', 'boolean_sequence',
    'gap_sequence', 'boolean_schema', 'unsafe_stage', 'skip_stage', 'repeat_stage',
    'missing_enter', 'mismatched_end', 'after_blocked', 'nonfinite', 'boolean_exit',
    'zero_exit', 'large_exit', 'false_timeout_exit', 'unsafe_category', 'unsafe_state',
    'extra_identity', 'symlink', 'public_mode',
])
def test_full_journal_validation_rejects_foreign_or_malformed_data(case, damage):
    values = rows(case)[:4]
    if damage == 'extra': values[-1]['extra'] = SENTINEL
    elif damage.startswith('wrong_') and damage not in ('wrong_job', 'wrong_pin'):
        key = {'source': 'source_commit', 'tree': 'source_tree', 'digest': 'source_digest',
            'controller': 'controller_digest', 'run': 'run_id', 'attempt': 'run_attempt',
            'repository': 'repository', 'workflow': 'workflow_sha'}[damage[6:]]
        values[-1]['identity'] = {**case['identity'], key: SENTINEL}
    elif damage == 'wrong_job': values[-1]['job_id'] = 'sql_owners'
    elif damage == 'wrong_pin': values[-1]['manifest_sha256'] = '0' * 64
    elif damage == 'boolean_sequence': values[0]['sequence'] = True
    elif damage == 'gap_sequence': values[-1]['sequence'] += 1
    elif damage == 'boolean_schema': values[0]['schema_version'] = True
    elif damage == 'unsafe_stage': values[-1]['stage'] = SENTINEL
    elif damage == 'skip_stage': values[2]['stage'] = 'tool_workload_python'
    elif damage == 'repeat_stage': values[2]['stage'] = 'module_binding'
    elif damage == 'missing_enter': values[0]['state'] = 'complete'
    elif damage == 'mismatched_end': values[-1]['stage'] = 'module_binding'
    elif damage == 'after_blocked': values[1].update(state='blocked', category='rejected')
    elif damage == 'boolean_exit': values[-1].update(state='blocked', category='nonzero_exit', exit_status=True)
    elif damage == 'zero_exit': values[-1].update(state='blocked', category='nonzero_exit', exit_status=0)
    elif damage == 'large_exit': values[-1].update(state='blocked', category='nonzero_exit', exit_status=256)
    elif damage == 'false_timeout_exit': values[-1].update(state='blocked', category='timeout', exit_status=1)
    elif damage == 'unsafe_category': values[-1].update(state='blocked', category=SENTINEL)
    elif damage == 'unsafe_state': values[-1]['state'] = SENTINEL
    elif damage == 'extra_identity': values[-1]['identity'] = {**case['identity'], SENTINEL: SENTINEL}
    path = write_rows(case, values)
    if damage == 'partial': path.write_bytes(path.read_bytes()[:-1])
    elif damage == 'empty': path.write_bytes(b'')
    elif damage == 'malformed': path.write_bytes((SENTINEL + '\n').encode())
    elif damage == 'duplicate_key': path.write_bytes(b'{"sequence":1,"sequence":1}\n')
    elif damage == 'too_many': path.write_bytes((driver.canonical(values[0]) + b'\n') * 65)
    elif damage == 'oversize': path.write_bytes(b'x' * (65536 + 1))
    elif damage == 'nonfinite': path.write_bytes(b'{"sequence":NaN}\n')
    elif damage == 'public_mode': path.chmod(0o644)
    elif damage == 'symlink':
        other = path.with_suffix('.data'); path.rename(other); path.symlink_to(other)
    value = read(case)
    assert value == failures.prephase_unavailable('unavailable' if damage in ('partial', 'empty') else 'invalid')
    assert SENTINEL not in driver.canonical(value).decode()


@pytest.mark.parametrize('error,category,code', [
    (subprocess.TimeoutExpired([SENTINEL], 2, output=SENTINEL), 'timeout', None),
    (subprocess.CalledProcessError(7, [SENTINEL], output=SENTINEL), 'nonzero_exit', 7),
    (subprocess.CalledProcessError(-9, [SENTINEL], stderr=SENTINEL), 'nonzero_exit', -9),
    (subprocess.CalledProcessError(True, [SENTINEL]), 'rejected', None),
    (OSError(SENTINEL), 'io_error', None), (RuntimeError(SENTINEL), 'rejected', None),
], ids=['timeout', 'exit', 'signal', 'invalid_exit', 'io', 'rejected'])
def test_only_typed_exceptions_supply_closed_reason(case, error, category, code):
    journal = failures.PrephaseJournal(driver, case['root'], context(case))
    with pytest.raises(type(error)) as caught:
        with journal('module_binding'):
            raise error
    assert caught.value is error
    assert read(case) == {'status': 'available', 'stage': 'module_binding',
        'state': 'blocked', 'category': category, 'exit_status': code}
    assert SENTINEL.encode() not in (case['root'] / 'prephase.jsonl').read_bytes()


@pytest.mark.parametrize('error', [KeyboardInterrupt(), SystemExit(7)], ids=['interrupt', 'exit'])
def test_baseexception_is_never_caught_or_mislabelled(case, error):
    journal = failures.PrephaseJournal(driver, case['root'], context(case))
    with pytest.raises(type(error)) as caught:
        with journal('module_binding'):
            raise error
    assert caught.value is error
    assert read(case)['category'] == 'unfinished'


def test_io_observation_failure_never_replaces_workload_result_or_exception(case, monkeypatch):
    journal = failures.PrephaseJournal(driver, case['root'], context(case))
    def broken(*args, **kwargs): raise OSError(SENTINEL)
    monkeypatch.setattr(failures.os, 'open', broken)
    with journal('module_binding'):
        result = 37
    assert result == 37
    error = ValueError('original')
    with pytest.raises(ValueError) as caught:
        with journal('tool_bootstrap_python'):
            raise error
    assert caught.value is error
    assert journal.disabled is True
    assert read(case) == failures.prephase_unavailable()


def test_real_cli_rejects_forged_journal_without_repairing_acceptance(case):
    seed_context(case)
    value = rows(case)[:2]
    value[-1]['identity'] = {**case['identity'], 'run_attempt': '2'}
    write_rows(case, value)
    report = cli(case)
    assert report['context_status'] == 'verified'
    assert report['prephase'] == failures.prephase_unavailable('invalid')
    assert not (case['root'] / 'work.json').exists()


def test_real_cli_missing_context_never_attributes_journal(case):
    write_rows(case, rows(case)[:2])
    assert cli(case)['prephase'] == failures.prephase_unavailable()


@pytest.fixture
def staged_run(case, monkeypatch):
    from types import SimpleNamespace
    from scripts import gateway_integration as harness
    from scripts import demo_runtime as runtime
    ctx = {**context(case), 'job_id': 'sql_execution', 'started': 1}
    commands = []
    control = dict(exit_status=0, timed_out=False, output_limit_exceeded=False,
        interrupted=False, descendant_cleanup=True, orphaned_descendants=False)
    def version(command, **kwargs):
        commands.append((tuple(command), dict(kwargs)))
        if command[-1] == '--version' and 'python' in str(command[0]): return 'Python 3.12.14\n'
        return {'uv': '0.12.23', 'node': 'v24.19.0', 'npm': '11.9.0',
                'docker': '28.0.4 28.0.4' if command[1] == 'version' else '2.38.2'}[command[0]]
    def run(command, **kwargs):
        commands.append((tuple(command), dict(kwargs)))
        return SimpleNamespace(stdout={'node': 'v24.19.0', 'npm': '11.9.0'}.get(command[0], ''))
    def supervised(command, path, **kwargs):
        commands.append((tuple(command), dict(kwargs)))
        return control
    def collection(*args): raise ValueError('stop after original first phase')
    monkeypatch.setattr(driver, 'initialize', lambda args: (case['candidate'], case['root'], ctx))
    monkeypatch.setattr(driver, 'modules', lambda candidate: (runtime,
        SimpleNamespace(process_passed=lambda result: result == control), harness))
    monkeypatch.setattr(driver, 'admit_source', lambda *args: case['manifest'])
    monkeypatch.setattr(driver, 'candidate_python', lambda candidate: candidate / '.venv/bin/python')
    monkeypatch.setattr(driver, 'work_remaining', lambda started: 1000)
    monkeypatch.setattr(driver, 'collect', collection)
    monkeypatch.setattr(driver, 'trusted_module', lambda name: failures if name == 'ci_partition_failures'
        else SimpleNamespace(bounded_process=supervised))
    monkeypatch.setattr(driver.subprocess, 'check_output', version)
    monkeypatch.setattr(runtime, 'run', run)
    monkeypatch.setattr(runtime.shutil, 'which', lambda name: '/usr/bin/docker')
    monkeypatch.setattr(harness.verify_demo, 'pytest_environment', lambda: None)
    monkeypatch.setenv('ImageVersion', '20261012.334.1')
    return SimpleNamespace(args=SimpleNamespace(job='sql_execution'), commands=commands,
                           control=control, ctx=ctx)


@pytest.mark.parametrize('stage', failures.PREPHASE_STAGES)
def test_actual_run_wiring_stops_at_each_closed_boundary(case, staged_run, monkeypatch, stage, capsys):
    from contextlib import contextmanager
    original = failures.PrephaseJournal
    seen = []
    class Injected(original):
        @contextmanager
        def __call__(self, name):
            seen.append(name)
            with super().__call__(name):
                if name == stage:
                    raise ValueError(SENTINEL)
                yield
    monkeypatch.setattr(failures, 'PrephaseJournal', Injected)
    assert driver.run_work(staged_run.args) == 1
    assert seen == list(failures.PREPHASE_STAGES[:failures.PREPHASE_STAGES.index(stage) + 1])
    assert read(case, job='sql_execution') == {'status': 'available', 'stage': stage,
        'state': 'blocked', 'category': 'rejected', 'exit_status': None}
    assert SENTINEL not in str(capsys.readouterr())
    assert not (case['root'] / 'work.json').exists()


def test_first_phase_success_has_original_commands_caps_and_receipt(case, staged_run):
    assert driver.run_work(staged_run.args) == 1  # explicit test stop at later collection
    value = read(case, job='sql_execution')
    assert value['stage'] == 'phase_receipt_write' and value['state'] == 'complete'
    assert value['category'] is None
    actual = staged_run.commands
    assert len(actual) == 13
    assert [entry[1]['timeout'] for entry in actual] == [2] * 7 + [10, 15, 15, 10, 10, 180]
    assert [entry[0] for entry in actual[2:]] == [
        ('uv', 'self', 'version', '--short'), ('node', '--version'), ('npm', '--version'),
        ('docker', 'version', '--format', '{{.Client.Version}} {{.Server.Version}}'),
        ('docker', 'compose', 'version', '--short'),
        ('git', 'status', '--porcelain', '--untracked-files=all'),
        ('docker', 'info', '--format', '{{.ServerVersion}}'), ('docker', 'compose', 'version'),
        ('node', '--version'), ('npm', '--version'),
        ('npm', '--prefix', 'clients', 'ci', '--ignore-scripts', '--no-audit', '--no-fund')]
    row = driver.read_private(case['root'] / 'client_dependencies.json')
    assert set(row) == {'name', 'status', 'cap_seconds', 'elapsed_ms', 'supervision'}
    assert row['status'] == 'PASS' and row['cap_seconds'] == 180
    assert row['supervision'] == staged_run.control


def test_legacy_preflight_default_and_observer_have_equal_calls(staged_run):
    from scripts import gateway_integration as harness
    from contextlib import contextmanager
    harness.preflight('backend')
    expected = list(staged_run.commands)
    staged_run.commands.clear()
    seen = []
    @contextmanager
    def observed(stage):
        seen.append(stage)
        yield
    harness.preflight('backend', observer=observed)
    assert staged_run.commands == expected
    assert seen == list(failures.PREPHASE_STAGES[10:17])


@pytest.mark.parametrize('mode,category,code', [('timeout', 'timeout', None), ('exit', 'nonzero_exit', 7)])
def test_isolated_actual_tool_probe_retains_two_second_cap_and_no_output(case, tmp_path, mode, category, code):
    import sys
    from pathlib import Path
    fake_root = tmp_path / 'tools'; executable = fake_root / '.venv/bin/python'
    executable.parent.mkdir(parents=True)
    body = 'import time; time.sleep(3)' if mode == 'timeout' else 'raise SystemExit(7)'
    executable.write_text('#!' + sys.executable + '\nprint(' + repr(SENTINEL) + ', flush=True)\n' + body + '\n')
    executable.chmod(0o700)
    input_path = tmp_path / 'probe-context.json'
    driver.write_private(input_path, context(case))
    # Actual isolated helper execution and subprocess.check_output, no fake timeout.
    script = '''import importlib.util, json, pathlib, sys
spec=importlib.util.spec_from_file_location('probe_driver',sys.argv[1])
d=importlib.util.module_from_spec(spec);sys.modules[spec.name]=d;spec.loader.exec_module(d)
f=d.trusted_module('ci_partition_failures')
ctx=json.loads(pathlib.Path(sys.argv[3]).read_text())
j=f.PrephaseJournal(d,pathlib.Path(sys.argv[2]),ctx)
with j('module_binding'):pass
d.TRUSTED_ROOT=pathlib.Path(sys.argv[4])
try:d.observed_tools(pathlib.Path(sys.argv[4]),observer=j)
except Exception:raise SystemExit(1)
raise SystemExit(99)
'''
    result = subprocess.run([sys.executable, '-I', '-S', '-c', script, driver.__file__,
        str(case['root']), str(input_path), str(fake_root)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and result.stdout == result.stderr == ''
    assert read(case) == {'status': 'available', 'stage': 'tool_bootstrap_python',
        'state': 'blocked', 'category': category, 'exit_status': code}
    seed_context(case)
    assert cli(case)['prephase'] == read(case)


@pytest.mark.parametrize('damage', ['existing', 'symlink', 'replacement', 'oversize'])
def test_writer_is_bounded_and_never_overwrites_unowned_existing_data(case, damage):
    path = case['root'] / 'prephase.jsonl'
    journal = failures.PrephaseJournal(driver, case['root'], context(case))
    if damage == 'replacement':
        journal.write('module_binding', 'entered')
        path.rename(path.with_suffix('.original'))
    if damage in ('existing', 'replacement'):
        path.write_text(SENTINEL); path.chmod(0o600)
    elif damage == 'symlink':
        target = case['root'] / 'unowned'; target.write_text(SENTINEL)
        path.symlink_to(target)
    elif damage == 'oversize':
        journal.binding['identity'] = {'unsafe': SENTINEL * 10000}
    with journal('module_binding'):
        value = 'original-success'
    assert value == 'original-success' and journal.disabled
    if damage != 'oversize': assert path.read_text() == SENTINEL
    else: assert path.stat().st_size <= failures.PREPHASE_LIMIT


def test_success_authority_function_unchanged():
    import ast
    import hashlib
    tree = ast.parse(Path(driver.__file__).read_text())
    value = ast.dump(next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'finalize'))
    # AST from independently reviewed trusted 00ce538a (same as 6dcc775 tree).
    assert hashlib.sha256(value.encode()).hexdigest() == '5468425fb6704fecedd7a517a9a90aba3528f74ddb86196fbb79e7aa63f5a450'


BASELINE_PREFLIGHT = "def preflight(mode):\n    verify_demo.pytest_environment()\n    # An exact reviewed checkout is required. Source modifications need a fresh\n    # reviewed build/receipt, never reuse of an earlier immutable identity.\n    if runtime.run(['git', 'status', '--porcelain', '--untracked-files=all'], timeout=10).stdout.strip():\n        raise Blocked('A clean immutable checkout is required')\n    runtime.require_docker()\n    if runtime.run(['node', '--version'], timeout=10).stdout.strip() != 'v24.19.0':\n        raise Blocked('Exact Node 24.19.0 is required before acquisition')\n    if runtime.run(['npm', '--version'], timeout=10).stdout.strip() != '11.9.0':\n        raise Blocked('Exact npm 11.9.0 is required before acquisition')\n    if mode == 'demo':\n        if os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'):\n            raise Blocked('Browser overrides are not admitted for integration')\n        from scripts.check_egress import observer_preflight\n        return observer_preflight()\n    return None\n"


BASELINE_DOCKER = "def require_docker():\n    if shutil.which('docker') is None: raise RuntimeError('BLOCKED: Docker is required for actual demo operations')\n    run(['docker','info','--format','{{.ServerVersion}}'],timeout=15)\n    run(['docker','compose','version'],timeout=15)\n"


@pytest.mark.parametrize('path,name,baseline,sha256', [
    ('scripts/gateway_integration.py', 'preflight', BASELINE_PREFLIGHT,
     'cf5e87cbc84e63c49f55b1080bf8c8ce2a2cfbbde412714181acda1eedc362e4'),
    ('scripts/demo_runtime.py', 'require_docker', BASELINE_DOCKER, '07a73c508d4956869fd3565e90cb31bb3fa6504d096ac8f9bb0beb95803d4dc3'),
], ids=['preflight', 'docker'])
def test_legacy_bytes_outside_inert_observer_wrappers_are_unchanged(path, name, baseline, sha256):
    import ast
    import hashlib
    text = (Path(__file__).resolve().parents[2] / path).read_text()
    node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == name)
    lines = text.splitlines(keepends=True)
    restored = ''.join(lines[:node.lineno - 1]) + baseline + ''.join(lines[node.end_lineno:])
    restored = restored.replace('from contextlib import nullcontext\n', '', 1)
    assert hashlib.sha256(restored.encode()).hexdigest() == sha256


def test_successful_tool_values_and_phase_evidence_do_not_depend_on_journal(case, staged_run, monkeypatch):
    expected = driver.observed_tools(case['candidate'])
    journal = failures.PrephaseJournal(driver, case['root'], context(case))
    journal.disabled = True  # Diagnostic I/O unavailable does not affect work.
    assert driver.observed_tools(case['candidate'], observer=journal) == expected
    assert expected == {'python': '3.12.14', 'uv': '0.12.23', 'node': '24.19.0',
        'npm': '11.9.0', 'docker': '28.0.4 28.0.4', 'compose': '2.38.2',
        'runner_image': '20261012.334.1'}
    monkeypatch.setattr(driver.time, 'monotonic', lambda: 100.0)
    first = case['root'] / 'first'; first.mkdir()
    second = case['root'] / 'second'; second.mkdir()
    command = ['npm', '--prefix', 'clients', 'ci', '--ignore-scripts', '--no-audit', '--no-fund']
    expected_row = driver.phase('client_dependencies', command, first, case['candidate'], 180, 1)
    actual_row = driver.phase('client_dependencies', command, second, case['candidate'], 180, 1, observer=journal)
    assert actual_row == expected_row
    assert (first / 'client_dependencies.json').read_bytes() == (second / 'client_dependencies.json').read_bytes()


def test_writer_never_exceeds_record_bound(case):
    journal = failures.PrephaseJournal(driver, case['root'], context(case))
    for _ in range(100): journal.write('module_binding', 'entered')
    assert journal.disabled
    assert len((case['root'] / 'prephase.jsonl').read_bytes().splitlines()) == 64
