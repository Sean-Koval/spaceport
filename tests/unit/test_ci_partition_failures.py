"""Failure-only disclosure controls, never successful acceptance evidence."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]
SENTINEL = 'PRIVATE_DIAGNOSTIC_SENTINEL'


def commit(root):
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c',
        'user.email=fixture@example.invalid', 'commit', '-qm', 'Frozen test'], check=True)
    return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()


@pytest.fixture(scope="module")
def frozen_case(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("frozen-diagnostic-case")
    from scripts import ci_partition_driver as d
    from scripts import ci_partition_contract as c
    from tests.unit.test_ci_partition_contract import inventory
    trusted = tmp_path / 'trusted'; trusted.mkdir()
    for name in (*d.CONTROLLERS, '.gitignore'):
        target = trusted / name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    trusted_sha = commit(trusted)
    candidate = tmp_path / 'candidate'; candidate.mkdir()
    for name in ('pyproject.toml', 'uv.lock', 'alembic.ini', '.dockerignore', 'docs/compatibility/openapi.json'):
        target = candidate / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_text('')
    tests = candidate / 'tests'; tests.mkdir()
    (tests / 'test_sample.py').write_text('def test_selected():\n    assert False\n\ndef test_outside():\n    pass\n')
    path = tests / 'integration/test_audit.py'; path.parent.mkdir()
    path.write_text('def test_partition_case():\n    pass\n')
    source_sha = commit(candidate)
    files = {str(p.relative_to(candidate)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in candidate.rglob('*') if p.is_file() and '.git' not in p.parts}
    source = {'commit': source_sha, 'tree': subprocess.check_output(['git', '-C', str(candidate),
        'rev-parse', 'HEAD^{tree}'], text=True).strip(), 'digest': d.digest(files)}
    controller = d.digest({name: hashlib.sha256((trusted / name).read_bytes()).hexdigest() for name in d.CONTROLLERS})
    manifest = c.build_manifest(source, controller, inventory(c))
    identity = {'repository': 'Sean-Koval/spaceport', 'source_commit': source_sha,
        'source_tree': source['tree'], 'source_digest': source['digest'], 'workflow_file': d.WORKFLOW,
        'workflow_ref': 'Sean-Koval/spaceport/' + d.WORKFLOW + '@refs/heads/main',
        'workflow_sha': trusted_sha, 'controller_digest': controller, 'run_id': '123', 'run_attempt': '1'}
    env = d.workload_environment()
    env.update(GITHUB_REPOSITORY=identity['repository'], GITHUB_WORKFLOW_REF=identity['workflow_ref'],
        GITHUB_WORKFLOW_SHA=trusted_sha, GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1',
        REVIEWED_SOURCE_SHA=source_sha, REVIEWED_MANIFEST_SHA256=d.digest(manifest),
        GATEWAY_INTEGRATION_FIRST_STEP_EPOCH=str(int(time.time())))
    return dict(driver=d, trusted=trusted, candidate=candidate, env=env,
        identity=identity, manifest=manifest)


@pytest.fixture
def case(frozen_case, tmp_path):
    value = {**frozen_case, 'env': dict(frozen_case['env']),
        'identity': dict(frozen_case['identity']),
        'manifest': json.loads(json.dumps(frozen_case['manifest']))}
    root = tmp_path / 'run'; root.mkdir(mode=0o700)
    output = tmp_path / 'github-output'; output.write_text('')
    value['env'].update(GITHUB_OUTPUT=str(output), GATEWAY_INTEGRATION_FIRST_STEP_EPOCH=str(int(time.time())))
    return {**value, 'root': root, 'output': output, 'started': int(value['env']['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH'])}


def cli(case, mode='finalize', job='source'):
    args = [sys.executable, '-I', '-S', str(case['trusted'] / 'scripts/ci_partition_driver.py'), mode]
    if mode == 'finalize':
        args += ['--candidate', str(case['candidate']), '--root', str(case['root']), '--job', job]
    result = subprocess.run(args, cwd=case['candidate'], env=case['env'], capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert 'Traceback' not in result.stderr
    assert SENTINEL not in result.stdout + result.stderr
    assert len(result.stdout.encode()) <= 16 * 1024
    assert case['output'].read_text() == ''
    return json.loads(result.stdout)


def seed_context(case):
    case['driver'].write_private(case['root'] / 'context.json', {'candidate': str(case['candidate']),
        'identity': case['identity'], 'job_id': 'source', 'started': case['started'],
        'manifest_sha256': case['env']['REVIEWED_MANIFEST_SHA256']})


def test_real_finalize_missing_context_has_bounded_failure_only(case):
    report = cli(case)
    assert report['kind'] == 'partition_failure'
    assert report['status'] == 'BLOCKED'
    assert report['context_status'] == 'unavailable'
    assert report['cohort']['run_id'] == '123'
    assert report['phases'] == []


def test_real_aggregate_missing_transport_has_fixed_fail(case):
    report = cli(case, 'aggregate')
    assert report['kind'] == 'aggregate_failure'
    assert report['status'] == 'FAIL'
    assert report['input'] == 'source_manifest'
    assert report['reason'] == 'transport_unavailable'
    assert report['cohort']['source_commit'] == case['identity']['source_commit']


def control(**changes):
    return {'exit_status': 0, 'timed_out': False, 'output_limit_exceeded': False,
        'interrupted': False, 'descendant_cleanup': True, 'orphaned_descendants': False, **changes}


def phase(case, name, cap, observed=None):
    observed = control() if observed is None else observed
    passed = observed == control()
    return {'name': name, 'status': 'PASS' if passed else 'BLOCKED', 'cap_seconds': cap,
        'elapsed_ms': 10, 'supervision': observed}


def selected(case):
    d = case['driver']; seed_context(case)
    d.write_private(case['root'] / 'source_collection.json', phase(case, 'source_collection', 60))
    d.write_private(case['root'] / 'source_collection-nodes.json', [{'nodeid': 'tests/test_sample.py::test_selected',
        'markers': ['unit'], 'fixtures': []}])


def put(case, name, value):
    path = case['root'] / name
    path.write_bytes(case['driver'].canonical(value)); path.chmod(0o600)


def failure_phase(case, marker=None, observed=None):
    put(case, 'source_tests.json', phase(case, 'source_tests', 180,
        control(exit_status=7) if observed is None else observed))
    log = case['root'] / 'source_tests.log'
    log.write_text(SENTINEL + '\n' + ('' if marker is None else 'GATEWAY_FAILURE_EVIDENCE ' + json.dumps(marker) + '\n'))
    log.chmod(0o600)


def test_missing_work_preserves_verified_failure_identity_without_acceptance(case):
    selected(case)
    marker = {'kind': 'test', 'test': 'tests/test_sample.py::test_selected', 'stage': 'call',
        'exception': 'AssertionError', 'trace': [{'file': 'tests/test_sample.py', 'line': 2}]}
    failure_phase(case, marker)
    report = cli(case)
    assert report['context_status'] == 'verified'
    assert report['source'] == {k: case['manifest']['source'][k] for k in ('commit', 'tree', 'digest')}
    row = next(row for row in report['phases'] if row['name'] == 'source_tests')
    assert row['failure_evidence'] == {'reason': 'process_exit', 'details_status': 'available', 'markers': [marker]}
    assert row['supervision']['exit_status'] == 7
    assert next(row for row in report['phases'] if row['name'] == 'client_dependencies')['failure_evidence']['reason'] == 'unavailable'


@pytest.mark.parametrize('damage', ['missing', 'malformed', 'duplicate', 'oversize', 'identity', 'job', 'pin', 'started', 'candidate'])
def test_context_damage_never_exports_private_markers(case, damage):
    selected(case); failure_phase(case, {'kind': 'checkpoint', 'checkpoint': 'cube-images', 'exit_status': 7})
    path = case['root'] / 'context.json'
    value = case['driver'].read_private(path)
    if damage == 'missing': path.unlink()
    elif damage == 'malformed': path.write_text(SENTINEL)
    elif damage == 'duplicate': path.write_text('{"identity":{},"identity":{}}')
    elif damage == 'oversize': path.write_text(SENTINEL * 2000)
    else:
        field = {'pin': 'manifest_sha256', 'job': 'job_id'}.get(damage, damage)
        value[field] = SENTINEL
        put(case, 'context.json', value)
    report = cli(case)
    assert report['context_status'] in ('invalid', 'unavailable')
    assert report['source'] is None and report['phases'] == []


@pytest.mark.parametrize('damage', ['extra', 'boolean_exit', 'nonfinite', 'negative_elapsed', 'huge_elapsed', 'wrong_cap', 'wrong_status', 'missing'])
def test_phase_schema_is_closed_and_finite(case, damage):
    selected(case); failure_phase(case)
    value = case['driver'].read_private(case['root'] / 'source_tests.json')
    if damage == 'extra': value['raw'] = SENTINEL
    if damage == 'boolean_exit': value['supervision']['exit_status'] = True
    if damage == 'negative_elapsed': value['elapsed_ms'] = -1
    if damage == 'huge_elapsed': value['elapsed_ms'] = 190001
    if damage == 'wrong_cap': value['cap_seconds'] = 181
    if damage == 'wrong_status': value['status'] = 'PASS'
    put(case, 'source_tests.json', value)
    if damage == 'nonfinite': (case['root'] / 'source_tests.json').write_text('{"elapsed_ms":NaN}')
    if damage == 'missing': (case['root'] / 'source_tests.json').unlink()
    report = cli(case)
    row = next(row for row in report['phases'] if row['name'] == 'source_tests')
    assert row['status'] == ('unavailable' if damage == 'missing' else 'invalid')
    assert row['supervision'] is None and row['cap_seconds'] is None
    assert row['failure_evidence']['markers'] == []


@pytest.mark.parametrize('damage', ['outside', 'parameter', 'unsafe', 'journey', 'oversize', 'encounters', 'duplicate'])
def test_marker_payloads_are_bounded_and_selection_bound(case, damage):
    selected(case)
    marker = {'kind': 'test', 'test': 'tests/test_sample.py::test_selected', 'stage': 'call',
        'exception': 'AssertionError', 'trace': []}
    if damage == 'outside': marker['test'] = 'tests/test_sample.py::test_outside'
    if damage == 'parameter': marker['test'] += '[' + SENTINEL + ']'
    if damage == 'unsafe': marker['raw'] = SENTINEL
    if damage == 'journey': marker = {'kind': 'journey', 'stage': 'author', 'error': 'assertion'}
    failure_phase(case, marker)
    log = case['root'] / 'source_tests.log'
    if damage == 'oversize': log.write_text('GATEWAY_FAILURE_EVIDENCE ' + SENTINEL * 1000 + '\n')
    if damage == 'encounters': log.write_text(('GATEWAY_FAILURE_EVIDENCE ' + json.dumps(marker) + '\n') * 33)
    if damage == 'duplicate': log.write_text('GATEWAY_FAILURE_EVIDENCE {"kind":"test","kind":"test"}\n')
    report = cli(case)
    evidence = next(row for row in report['phases'] if row['name'] == 'source_tests')['failure_evidence']
    assert evidence['details_status'] == 'invalid' and evidence['markers'] == []


@pytest.mark.parametrize('damage', ['collection_failed', 'duplicate_nodes', 'no_source_marker', 'bad_node', 'extra_key', 'missing_inventory'])
def test_source_markers_require_completed_exact_collection(case, damage):
    selected(case); failure_phase(case, {'kind': 'checkpoint', 'checkpoint': 'cube-images', 'exit_status': 7})
    if damage == 'collection_failed': put(case, 'source_collection.json', phase(case, 'source_collection', 60, control(exit_status=7)))
    else:
        value = case['driver'].read_private(case['root'] / 'source_collection-nodes.json')
        if damage == 'duplicate_nodes': value *= 2
        if damage == 'no_source_marker': value[0]['markers'] = ['postgres']
        if damage == 'bad_node': value[0]['nodeid'] = '../' + SENTINEL
        if damage == 'extra_key': value[0]['raw'] = SENTINEL
        put(case, 'source_collection-nodes.json', value)
        if damage == 'missing_inventory': (case['root'] / 'source_collection-nodes.json').unlink()
    report = cli(case)
    assert all(row['failure_evidence']['markers'] == [] for row in report['phases'])


@pytest.mark.parametrize('damage', ['valid', 'failed', 'wrong_job', 'wrong_attempt', 'extra', 'boolean_elapsed', 'contradictory'])
def test_cleanup_metadata_is_closed_and_cohort_bound(case, damage):
    selected(case)
    value = {'identity': dict(case['identity']), 'job_id': 'source', 'status': 'PASS', 'cap_seconds': 120,
        'elapsed_ms': 12, 'exact_resources_verified': True, 'descendants_verified': True}
    if damage == 'failed': value.update(status='BLOCKED', exact_resources_verified=False, descendants_verified=False)
    if damage == 'wrong_job': value['job_id'] = 'sql_owners'
    if damage == 'wrong_attempt': value['identity']['run_attempt'] = '2'
    if damage == 'extra': value['raw'] = SENTINEL
    if damage == 'boolean_elapsed': value['elapsed_ms'] = True
    if damage == 'contradictory': value['descendants_verified'] = False
    put(case, 'cleanup-receipt.json', value)
    assert cli(case)['cleanup_status'] == ('PASS' if damage == 'valid' else 'BLOCKED' if damage == 'failed' else 'invalid')


@pytest.mark.parametrize('field', ['GITHUB_WORKFLOW_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'REVIEWED_SOURCE_SHA', 'REVIEWED_MANIFEST_SHA256', 'GITHUB_WORKFLOW_REF'])
def test_invalid_executing_cohort_never_echoes_supplied_values(case, field):
    selected(case); failure_phase(case)
    case['env'][field] = SENTINEL
    report = cli(case)
    assert report['cohort'] is None and report['source'] is None and report['phases'] == []


@pytest.mark.parametrize('started', ['missing', 'invalid', 'expired', 'future', 'oversize', 'negative'])
def test_finalize_budget_failure_uses_no_fresh_reserve(case, started):
    selected(case); failure_phase(case)
    if started == 'missing': case['env'].pop('GATEWAY_INTEGRATION_FIRST_STEP_EPOCH')
    elif started == 'invalid': case['env']['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH'] = SENTINEL
    elif started == 'expired': case['env']['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH'] = str(int(time.time()) - 1201)
    elif started == 'oversize': case['env']['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH'] = '9' * 1000
    elif started == 'negative': case['env']['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH'] = '-1'
    else: case['env']['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH'] = str(int(time.time()) + 60)
    report = cli(case)
    assert report['reason'] == 'finalization_unavailable' and report['cohort'] is None
    assert report['phases'] == []


@pytest.mark.parametrize('name', ['client_dependencies', 'source_collection', 'source_tests', 'backend_collection'])
def test_source_phases_never_export_cube_checkpoint_or_backend_markers(case, name):
    selected(case)
    cap = 180 if name in ('client_dependencies', 'source_tests') else 60
    put(case, name + '.json', phase(case, name, cap, control(exit_status=7)))
    log = case['root'] / (name + '.log')
    log.write_text('GATEWAY_FAILURE_EVIDENCE {"kind":"checkpoint","checkpoint":"cube-images","exit_status":7}\n')
    log.chmod(0o600)
    assert all(not row['failure_evidence']['markers'] for row in cli(case)['phases'])


def seed_backend(case):
    job = 'sql_registry_results'
    seed_context(case)
    ctx = case['driver'].read_private(case['root'] / 'context.json'); ctx['job_id'] = job
    put(case, 'context.json', ctx); put(case, 'manifest.json', case['manifest'])
    (case['root'] / 'backends').mkdir(mode=0o700)
    return job


@pytest.mark.parametrize('damage', ['valid', 'foreign_manifest', 'wrong_source', 'wrong_controller', 'wrong_lane', 'wrong_count', 'checkpoint'])
def test_backend_progress_requires_reviewed_manifest_exact_lane_and_count(case, damage):
    job = seed_backend(case)
    nodes = case['manifest']['partitions'][job]
    marker = {'kind': 'progress', 'phase': 'backends', 'test': 'tests/integration/test_audit.py::test_partition_case',
        'stage': 'call', 'collected': len(nodes), 'completed': 0}
    manifest = json.loads(json.dumps(case['manifest']))
    if damage == 'foreign_manifest': manifest['schema_version'] = 9
    if damage == 'wrong_source': manifest['source']['commit'] = 'f' * 40
    if damage == 'wrong_controller': manifest['controller_digest'] = 'f' * 64
    if damage == 'wrong_lane': marker['test'] = 'tests/test_sample.py::test_selected'
    if damage == 'wrong_count': marker['collected'] += 1
    if damage == 'checkpoint': marker = {'kind': 'checkpoint', 'checkpoint': 'cube-images', 'exit_status': 7}
    put(case, 'manifest.json', manifest)
    case['driver'].write_private(case['root'] / 'backends/backend_tests.json',
        phase(case, 'backend_tests', 570, control(exit_status=124, timed_out=True)))
    log = case['root'] / 'backends/backend_tests.log'
    prefix = 'GATEWAY_FAILURE_EVIDENCE ' if damage == 'checkpoint' else 'GATEWAY_PRIVATE_PYTEST_PROGRESS '
    log.write_text(SENTINEL + '\n' + prefix + json.dumps(marker) + '\n'); log.chmod(0o600)
    report = cli(case, job=job)
    evidence = next(row for row in report['phases'] if row['name'] == 'backend_tests')['failure_evidence']
    assert evidence['reason'] == 'timeout'
    assert evidence['markers'] == ([marker] if damage == 'valid' else [])


@pytest.mark.parametrize('cap', [779, 780, 821, 900, 901])
def test_backend_stack_exports_its_actual_admitted_cap(case, cap):
    job = seed_backend(case)
    put(case, 'backend_stack.json', phase(case, 'backend_stack', cap, control(exit_status=7)))
    log = case['root'] / 'backend_stack.log'
    marker = {'kind': 'checkpoint', 'checkpoint': 'cube-images', 'exit_status': 7}
    log.write_text('GATEWAY_FAILURE_EVIDENCE ' + json.dumps(marker) + '\n'); log.chmod(0o600)
    report = cli(case, job=job)
    row = next(row for row in report['phases'] if row['name'] == 'backend_stack')
    valid = 780 <= cap <= 900
    assert row['cap_seconds'] == (cap if valid else None)
    assert row['failure_evidence']['markers'] == ([marker] if valid else [])


@pytest.mark.parametrize('damage', ['malformed', 'extra_chunk', 'foreign_source', 'missing_source_receipt'])
def test_aggregate_invalid_transport_has_bounded_exact_input(case, damage):
    from scripts import ci_partition_contract as c
    value = case['manifest']
    if damage == 'foreign_source': value['source']['commit'] = 'f' * 40
    slots = c.encode_transport(value, kind='manifest')
    case['env'].update({'SOURCE_MANIFEST_' + k.upper(): v for k, v in slots.items()})
    if damage == 'malformed': case['env']['SOURCE_MANIFEST_CHUNK_0'] = SENTINEL
    if damage == 'extra_chunk': case['env']['SOURCE_MANIFEST_CHUNK_8'] = SENTINEL
    report = cli(case, 'aggregate')
    assert report['status'] == 'FAIL'
    assert report['input'] == ('source_receipt' if damage == 'missing_source_receipt' else 'source_manifest')
    assert report['reason'] == ('manifest_invalid' if damage == 'foreign_source' else
        'transport_unavailable' if damage == 'missing_source_receipt' else 'transport_invalid')


def test_finalization_alarm_cannot_be_swallowed_into_a_new_budget(monkeypatch, capsys):
    from types import SimpleNamespace
    from scripts import ci_partition_driver as d, ci_partition_failures as f
    calls = []
    monkeypatch.setenv('GATEWAY_INTEGRATION_FIRST_STEP_EPOCH', str(int(time.time())))
    monkeypatch.setattr(d, 'trusted_module', lambda _: f)
    monkeypatch.setattr(d.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(d.signal, 'setitimer', lambda _, seconds: calls.append(seconds))
    monkeypatch.setattr(f, 'cohort', lambda _: {'run_id': '123'})
    monkeypatch.setattr(d, 'load_context', lambda *_: (_ for _ in ()).throw(ValueError()))
    monkeypatch.setattr(f, 'report', lambda *_: (_ for _ in ()).throw(d.FinalizationExpired()))
    assert d.finalize_command(Path('/candidate'), Path('/run'), 'source') == 1
    report = json.loads(capsys.readouterr().out)
    assert report['reason'] == 'finalization_budget' and report['phases'] == []
    assert calls == [60, 0]


def test_output_overflow_is_fixed_and_never_partial(capsys):
    from scripts import ci_partition_driver as d, ci_partition_failures as f
    assert f.emit(d, {'private': SENTINEL * 2000}) == 1
    report = json.loads(capsys.readouterr().out)
    assert report == {'schema_version': 1, 'kind': 'diagnostic_unavailable', 'status': 'BLOCKED', 'reason': 'output_bound'}


def test_real_pinned_pytest_timeout_exports_only_safe_selected_failure(tmp_path, monkeypatch):
    from scripts import ci_partition_driver as d, ci_partition_failures as f
    from scripts import pytest_runtime_diagnostics as api
    from scripts.runtime_process import bounded_process
    candidate = tmp_path / 'candidate'; (candidate / 'tests').mkdir(parents=True)
    (candidate / 'scripts').mkdir()
    shutil.copyfile(ROOT / 'scripts/pytest_runtime_diagnostics.py', candidate / 'scripts/pytest_runtime_diagnostics.py')
    parameter = 'PRIVATE_PARAMETER_SENTINEL'
    (candidate / 'tests/test_failure.py').write_text('import time\nfrom pathlib import Path\nimport pytest\n'
        '@pytest.fixture\ndef blocked():\n    yield\n    Path("teardown-entered").touch()\n    time.sleep(30)\n'
        '@pytest.mark.parametrize("value", [' + repr(parameter) + '])\n'
        'def test_selected(value, blocked):\n    local = "PRIVATE_LOCAL_SENTINEL"\n'
        '    raise RuntimeError("PRIVATE_MESSAGE_SENTINEL" + value)\n')
    sha = commit(candidate)
    monkeypatch.setattr(api, 'ROOT', candidate)
    monkeypatch.setattr(api, '_source_blobs', None); monkeypatch.setattr(api, '_source_commit', None)
    frozen = api.freeze_source(sha)
    pin = tmp_path / 'source.json'; d.write_private(pin, frozen)
    log = tmp_path / 'pytest.log'
    command = [sys.executable, '-m', 'pytest', '-q', '-p', 'scripts.pytest_runtime_diagnostics',
        '--gateway-failure-evidence', '--gateway-failure-source=' + str(pin)]
    observed = bounded_process(command, log, timeout=1.2, cwd=candidate, env=d.workload_environment())
    assert observed['timed_out'] and observed['descendant_cleanup']
    assert (candidate / 'teardown-entered').exists()
    node = 'tests/test_failure.py::test_selected'
    evidence = f.scoped_evidence(api, log, observed, [node + '[' + parameter + ']'], phase='source_tests')
    assert evidence['reason'] == 'timeout' and evidence['details_status'] == 'available'
    assert evidence['markers'][0]['test'] == node
    assert evidence['markers'][0]['exception'] == 'RuntimeError'
    assert evidence['markers'][0]['stage'] == 'call'
    encoded = json.dumps(evidence)
    assert all(secret not in encoded for secret in (parameter, 'PRIVATE_LOCAL_SENTINEL', 'PRIVATE_MESSAGE_SENTINEL'))
    assert 'GATEWAY_PRIVATE_PYTEST_PROGRESS ' not in log.read_text()


def test_driver_restores_source_failure_and_backend_progress_before_node_separator():
    source = (ROOT / 'scripts/ci_partition_driver.py').read_text()
    assert "*pytest_evidence(candidate,root,ctx['identity'])" in source
    assert "*pytest_evidence(candidate,target,ctx['identity'],progress=True),'--',*nodes]" in source
    workflow = (ROOT / '.github/workflows/verify-gateway-partitions.yml').read_text()
    for job in ('source', 'sql_owners', 'sql_execution', 'sql_publishing', 'sql_registry_results', 'runtime_process_socket'):
        assert 'finalize --candidate "$GITHUB_WORKSPACE/candidate" --root "$GATEWAY_INTEGRATION_ROOT" --job ' + job in workflow
