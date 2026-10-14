"""Offline workflow controls, never evidence of hosted or runtime acceptance."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import time

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / '.github/workflows/verify-gateway.yml'


def source():
    assert WORKFLOW.is_file(), 'Separate manual gateway workflow is missing'
    return WORKFLOW.read_text()


def step(name):
    marker = '      - name: ' + name + '\n'
    assert marker in source(), 'Required workflow step is missing: ' + name
    text = source().split(marker, 1)[1].split('      - name:', 1)[0]
    code = text.split('        run: |\n', 1)[1]
    assert '${{' not in code, 'Inputs must never interpolate into shell source'
    return '\n'.join(line[10:] for line in code.splitlines())


def environment(tmp_path, **overrides):
    runner = tmp_path / 'runner'; runner.mkdir(exist_ok=True)
    workspace = tmp_path / 'checkout'; workspace.mkdir(exist_ok=True)
    return {**os.environ, 'RUNNER_TEMP': str(runner), 'GITHUB_WORKSPACE': str(workspace),
        'GITHUB_ENV': str(tmp_path / 'github-env'), 'GITHUB_ACTIONS': 'true',
        'GITHUB_WORKFLOW_REF': 'owner/repo/.github/workflows/verify-gateway.yml@refs/heads/main',
        'GITHUB_WORKFLOW_SHA': 'b' * 40, 'GITHUB_REPOSITORY': 'owner/repo',
        'GITHUB_RUN_ID': '1234', 'GITHUB_RUN_ATTEMPT': '1', 'ImageVersion': '20261012.1.0',
        'REVIEWED_SOURCE_SHA': 'a' * 40, 'INTEGRATION_MODE': 'backend', **overrides}


def run(name, env, *, prefix=''):
    return subprocess.run(['bash', '-c', prefix + '\n' + step(name)], env=env,
        cwd=env['GITHUB_WORKSPACE'], text=True, capture_output=True, timeout=5)


def initialize(tmp_path, **overrides):
    env = environment(tmp_path, **overrides)
    result = run('Validate inputs and initialize private CI identity', env)
    assert result.returncode == 0, result.stdout + result.stderr
    env.update(dict(line.split('=', 1) for line in Path(env['GITHUB_ENV']).read_text().splitlines()))
    return env


def test_manual_only_fixed_runner_permissions_pins_and_unchanged_legacy():
    text = source()
    trigger = text.split('\non:\n', 1)[1].split('\npermissions:', 1)[0]
    assert re.findall(r'^  ([\w_]+):', trigger, re.M) == ['workflow_dispatch']
    assert re.findall(r'^      ([\w_]+):', trigger, re.M) == ['source_sha', 'integration_mode']
    assert re.findall(r'^          - (\w+)$', trigger, re.M) == ['backend', 'demo']
    assert 'default: backend' in trigger
    assert '\npermissions:\n  contents: read\n' in text
    assert 'group: data-product-gateway-manual-verification\n  cancel-in-progress: false' in text
    assert text.count('runs-on: ubuntu-24.04') == text.count('timeout-minutes: 20') == 1
    assert re.findall(r'uses: (\S+)', text) == [
        'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
        'astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884',
        'actions/setup-node@820762786026740c76f36085b0efc47a31fe5020']
    assert 'ref: ${{ inputs.source_sha }}' in text and 'persist-credentials: false' in text
    assert "version: '0.12.23'" in text and "python-version: '3.12.14'" in text
    assert "node-version: '24.19.0'" in text
    assert 'enable-cache: false' in text and 'package-manager-cache: false' in text
    assert not re.search(r'^\s+(?:cache|secrets|environment|services|container):', text, re.M)
    assert not any(token in text for token in ('continue-on-error', '|| true', 'upload-artifact',
        'actions/cache', 'secrets.', 'registry-url:', 'sudo ', 'apt-get ', 'install-deps',
        '--with-deps', 'npm audit', 'docker system prune', 'docker login', 'npx '))
    assert hashlib.sha256((ROOT / '.github/workflows/verify-cube.yml').read_bytes()).hexdigest() == (
        '6e0e8b8bb85788c869a15e5c4b6d885ed2e68517ded79566b13bb70065ac935e')


@pytest.mark.parametrize('key,value', [('REVIEWED_SOURCE_SHA', ''), ('REVIEWED_SOURCE_SHA', 'A'*40),
    ('REVIEWED_SOURCE_SHA', 'a'*39), ('REVIEWED_SOURCE_SHA', '$(touch injected)'),
    ('INTEGRATION_MODE', 'full'), ('INTEGRATION_MODE', '$(touch injected)')])
def test_invalid_input_is_rejected_before_private_root_or_runtime(tmp_path, key, value):
    env = environment(tmp_path, **{key: value})
    result = run('Validate inputs and initialize private CI identity', env)
    assert result.returncode != 0
    assert not Path(env['GITHUB_ENV']).exists()
    assert not list(Path(env['RUNNER_TEMP']).iterdir())
    assert not (Path(env['GITHUB_WORKSPACE']) / 'injected').exists()


def test_private_external_parent_new_child_and_first_step_timestamp(tmp_path):
    before = int(time.time()); env = initialize(tmp_path); after = int(time.time())
    parent = Path(env['GATEWAY_CI_PARENT']); root = Path(env['GATEWAY_INTEGRATION_ROOT'])
    assert parent.parent == Path(env['RUNNER_TEMP'])
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert root == parent / 'run' and not root.exists()
    assert Path(env['PLAYWRIGHT_BROWSERS_PATH']) == parent / 'browsers'
    assert before <= int(env['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH']) <= after
    receipt = parent / 'ci-identity.json'
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    value = json.loads(receipt.read_text())
    assert value['executing_workflow']['sha'] == 'b'*40
    assert value['requested_source_sha'] == 'a'*40
    assert value['first_step_epoch'] == int(env['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH'])


@pytest.mark.parametrize('bad_path', ['relative', 'inside_checkout', 'symlink'])
def test_temp_root_cannot_be_relative_inside_checkout_or_symlink(tmp_path, bad_path):
    env = environment(tmp_path)
    if bad_path == 'relative': env['RUNNER_TEMP'] = 'relative'
    elif bad_path == 'inside_checkout': env['RUNNER_TEMP'] = env['GITHUB_WORKSPACE']
    else:
        link = tmp_path/'link'; link.symlink_to(env['RUNNER_TEMP'], target_is_directory=True)
        env['RUNNER_TEMP'] = str(link)
    assert run('Validate inputs and initialize private CI identity', env).returncode != 0
    assert not Path(env['GITHUB_ENV']).exists()


def test_both_modes_prepare_exact_node_and_private_npm_without_scripts_or_audit():
    text = source(); code = step('Prepare exact run-private npm')
    assert 'if: inputs.integration_mode' not in text
    assert text.index('name: Set up exact Node') < text.index('name: Prepare exact run-private npm')
    assert text.index('name: Prepare exact run-private npm') < text.index('name: Record checked-out source and actual tools')
    assert text.index('name: Record checked-out source and actual tools') < text.index('name: Run explicit integration')
    assert 'npm install --global --prefix "$GATEWAY_CI_PARENT/npm-tool"' in code
    assert '--ignore-scripts --no-audit --no-fund npm@11.9.0' in code
    assert '== v24.19.0' in code and '== 11.9.0' in code
    assert '"$GITHUB_PATH"' in code
    assert 'npm_config_audit: \'false\'' in text and 'npm_config_fund: \'false\'' in text
    assert not any(command in text for command in ('npm --prefix publisher ci', 'npm --prefix clients ci',
        'playwright/cli.js install', 'pytest -m'))


@pytest.mark.parametrize('mode', ['backend', 'demo'])
@pytest.mark.parametrize('status', [0, 1, 2, 124])
def test_explicit_runtime_mode_preserves_exit_status(tmp_path, mode, status):
    env = initialize(tmp_path, INTEGRATION_MODE=mode)
    result = run('Run explicit integration', env,
        prefix=f'bash() {{ printf "%s\\n" "$*" > "$GATEWAY_CI_PARENT/call"; return {status}; }}')
    assert result.returncode == status
    assert (Path(env['GATEWAY_CI_PARENT'])/'call').read_text().strip() == (
        f'scripts/verify_gateway_integration.sh --{mode} {env["GATEWAY_INTEGRATION_ROOT"]}')


@pytest.mark.parametrize('problem', ['mode', 'existing_root', 'wrong_root', 'expired'])
def test_runtime_rejects_nonadmitted_mode_path_or_job_budget(tmp_path, problem):
    env = initialize(tmp_path)
    if problem == 'mode': env['INTEGRATION_MODE'] = 'diagnostic'
    if problem == 'existing_root': Path(env['GATEWAY_INTEGRATION_ROOT']).mkdir()
    if problem == 'wrong_root': env['GATEWAY_INTEGRATION_ROOT'] = '/tmp/other-run'
    if problem == 'expired': env['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH'] = str(int(time.time())-1020)
    result = run('Run explicit integration', env,
        prefix='bash() { printf CALLED; }')
    assert result.returncode != 0
    assert 'CALLED' not in result.stdout


def test_always_cleanup_uses_exact_registered_root_and_shared_deadline(tmp_path):
    env = initialize(tmp_path); Path(env['GATEWAY_INTEGRATION_ROOT']).mkdir()
    text = source().split('      - name: Always verify exact registered cleanup\n', 1)[1]
    assert text.startswith('        if: always()\n')
    result = run('Always verify exact registered cleanup', env,
        prefix='timeout() { printf "%s\\n" "$*"; return 7; }')
    assert result.returncode == 7
    assert '--signal=TERM --kill-after=5s 110s uv run --locked --offline python -m scripts.gateway_integration --cleanup-run ' + env['GATEWAY_INTEGRATION_ROOT'] in result.stdout
    attempt = json.loads((Path(env['GATEWAY_CI_PARENT'])/'ci-cleanup-attempt.json').read_text())
    assert attempt['exit_status'] == 7 and attempt['finished_epoch'] >= attempt['started_epoch']
    env['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH'] = str(int(time.time())-1130)
    result = run('Always verify exact registered cleanup', env,
        prefix='timeout() { printf "%s\\n" "$*"; }')
    assert result.returncode == 0
    assert re.search(r'--kill-after=5s [1-5]s uv run', result.stdout)
    env['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH'] = str(int(time.time())-1140)
    assert run('Always verify exact registered cleanup', env,
        prefix='timeout() { printf CALLED; }').returncode != 0


def test_cleanup_skips_never_created_root_and_rejects_forged_root(tmp_path):
    env = initialize(tmp_path)
    assert run('Always verify exact registered cleanup', env).returncode == 0
    env['GATEWAY_INTEGRATION_ROOT'] = env['GATEWAY_CI_PARENT']
    assert run('Always verify exact registered cleanup', env).returncode != 0


def fake_tool(tmp_path, name, body):
    tool = tmp_path / name
    tool.write_text('#!/bin/sh\n' + body + '\n')
    tool.chmod(0o755)



def uv_tool(bindir, version, semantic_version='0.12.23'):
    # Real workflow shell/Python validation runs against controlled CLI output.
    # Dependency preparation is inert so these tests never acquire packages.
    fake_tool(bindir, 'uv', 'case "$*" in\n'
        '"python install 3.12.14"|"sync --locked --dev") :;;\n'
        f'"--version") printf "%s\\n" {shlex.quote(version)};;\n'
        f'"self version --short") printf "%s\\n" {shlex.quote(semantic_version)};;\n'
        '"run --locked --offline python --version") echo "Python 3.12.14";;\n'
        '*) exit 71;; esac')


UV_RELEASE_VERSIONS = [
    'uv 0.12.23',
    'uv 0.12.23 (x86_64-unknown-linux-gnu)',
    'uv 0.12.23 (abcdef123 2026-10-13 x86_64-unknown-linux-gnu)',
]
UV_INVALID_VERSIONS = [
    '', '0.12.23', 'uv 0.12.22', 'uv 0.12.230', 'uv 0.12.23rc1',
    'uv 0.12.23+custom', 'uv 0.12.23 arbitrary', 'uv 0.12.23 (arbitrary)',
    'uv 0.12.23 (abcdef123 2026-10-13)',
    'uv 0.12.23 (aarch64-unknown-linux-gnu)',
    'uv 0.12.23+1 (abcdef123 2026-10-13 x86_64-unknown-linux-gnu)',
    'uv 0.12.23 (x86_64-unknown-linux-gnu) extra',
    'uv 0.12.23 (abcdef123 2026-10-13) extra',
    'uv 0.12.23 (abcdef123 2026-10-13\nINJECTED)',
    'uv 0.12.23\nINJECTED', ' uv 0.12.23', 'uv 0.12.23 ',
    'uv 0.12.23 (' + 'a'*129 + ')',
]


@pytest.mark.parametrize('version', UV_RELEASE_VERSIONS)
def test_python_preparation_accepts_exact_uv_release_with_known_metadata(tmp_path, version):
    env = source_tools(tmp_path, environment(tmp_path), uv_version=version)
    result = run('Prepare exact Python dependencies', env)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('version', UV_INVALID_VERSIONS)
def test_python_preparation_rejects_wrong_uv_or_unrecognized_output(tmp_path, version):
    env = source_tools(tmp_path, environment(tmp_path), uv_version=version)
    assert run('Prepare exact Python dependencies', env).returncode != 0


@pytest.mark.parametrize('step_name', [
    'Prepare exact Python dependencies', 'Record checked-out source and actual tools'])
@pytest.mark.parametrize('semantic_version', ['0.12.22', '0.12.230', '0.12.23rc1', '0.12.23 extra'])
def test_uv_semantic_version_must_independently_match_exact_pin(tmp_path, step_name, semantic_version):
    env = source_tools(tmp_path, initialize(tmp_path), uv_semantic_version=semantic_version)
    assert run(step_name, env).returncode != 0
    assert not (Path(env['GATEWAY_CI_PARENT'])/'ci-source.json').exists()


@pytest.mark.parametrize('version', UV_RELEASE_VERSIONS)
def test_source_receipt_preserves_exact_uv_release_with_known_metadata(tmp_path, version):
    env = source_tools(tmp_path, initialize(tmp_path), uv_version=version)
    result = run('Record checked-out source and actual tools', env)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((Path(env['GATEWAY_CI_PARENT'])/'ci-source.json').read_text())
    assert receipt['tools']['uv'] == version


@pytest.mark.parametrize('version', UV_INVALID_VERSIONS)
def test_source_receipt_rejects_wrong_uv_or_unrecognized_output(tmp_path, version):
    env = source_tools(tmp_path, initialize(tmp_path), uv_version=version)
    assert run('Record checked-out source and actual tools', env).returncode != 0
    assert not (Path(env['GATEWAY_CI_PARENT'])/'ci-source.json').exists()


@pytest.mark.parametrize('version', UV_RELEASE_VERSIONS)
def test_summary_preserves_full_uv_release_metadata_without_widening_other_tools(tmp_path, version):
    env = source_tools(tmp_path, initialize(tmp_path))
    assert run('Record checked-out source and actual tools', env).returncode == 0
    receipt_path = Path(env['GATEWAY_CI_PARENT'])/'ci-source.json'
    receipt = json.loads(receipt_path.read_text())
    receipt['tools'].update(uv=version, docker='28.5.1 (unapproved metadata)')
    receipt_path.write_text(json.dumps(receipt))
    result = run('Publish only allowlisted CI summary', env)
    assert result.returncode == 1  # No runtime report is fabricated.
    summary = json.loads(result.stdout)
    assert summary['tools']['uv'] == version
    assert 'docker' not in summary['tools']


@pytest.mark.parametrize('version', UV_INVALID_VERSIONS)
def test_summary_excludes_unrecognized_uv_output(tmp_path, version):
    env = source_tools(tmp_path, initialize(tmp_path))
    assert run('Record checked-out source and actual tools', env).returncode == 0
    receipt_path = Path(env['GATEWAY_CI_PARENT'])/'ci-source.json'
    receipt = json.loads(receipt_path.read_text())
    receipt['tools']['uv'] = version
    receipt_path.write_text(json.dumps(receipt))
    result = run('Publish only allowlisted CI summary', env)
    assert result.returncode == 1
    assert 'uv' not in json.loads(result.stdout)['tools']


@pytest.mark.parametrize('step_name', [
    'Prepare exact Python dependencies', 'Record checked-out source and actual tools'])
def test_invalid_uv_version_diagnostic_escapes_control_characters(tmp_path, step_name):
    env = source_tools(tmp_path, initialize(tmp_path), uv_version='uv 0.12.23\nINJECTED')
    result = run(step_name, env)
    assert result.returncode != 0
    assert 'INJECTED' in result.stdout + result.stderr
    assert '\nINJECTED' not in result.stdout + result.stderr


@pytest.mark.parametrize('step_name', [
    'Prepare exact Python dependencies', 'Record checked-out source and actual tools'])
def test_invalid_uv_version_diagnostic_is_bounded(tmp_path, step_name):
    env = source_tools(tmp_path, initialize(tmp_path), uv_version='uv ' + 'x'*16384)
    result = run(step_name, env)
    assert result.returncode != 0
    assert len(result.stdout + result.stderr) < 512


def source_tools(tmp_path, env, *, commit='a'*40, dirty=False,
    uv_version='uv 0.12.23', uv_semantic_version='0.12.23'):
    bindir = tmp_path/'bin'; bindir.mkdir()
    fake_tool(bindir, 'git', 'case "$*" in\n'
        f'"rev-parse HEAD") echo {commit};;\n'
        '"rev-parse HEAD^{tree}") echo ' + 'c'*40 + ';;\n'
        + ('"status --porcelain --untracked-files=all") echo changed;;\n' if dirty else
           '"status --porcelain --untracked-files=all") :;;\n') + '*) exit 71;; esac')
    uv_tool(bindir, uv_version, uv_semantic_version)
    fake_tool(bindir, 'docker', 'case "$*" in "compose version --short") echo 2.40.3;; '
        '*) echo "28.5.1 28.5.1";; esac')
    fake_tool(bindir, 'node', 'echo v24.19.0')
    fake_tool(bindir, 'npm', 'echo 11.9.0')
    return {**env, 'PATH': str(bindir) + ':' + os.environ['PATH']}


@pytest.mark.parametrize('mode', ['backend', 'demo'])
def test_source_and_executing_workflow_identity_are_separate_and_actual_tools_recorded(tmp_path, mode):
    env = source_tools(tmp_path, initialize(tmp_path, INTEGRATION_MODE=mode))
    result = run('Record checked-out source and actual tools', env)
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads((Path(env['GATEWAY_CI_PARENT'])/'ci-source.json').read_text())
    assert value['source'] == {'commit': 'a'*40, 'tree': 'c'*40}
    assert value['tools']['node'] == 'v24.19.0'
    assert value['tools']['npm'] == '11.9.0'
    assert value['tools']['docker'] == '28.5.1 28.5.1'
    assert value['runner_image'] == '20261012.1.0'
    assert value['setup_elapsed_seconds'] >= 0
    assert value['work_seconds_remaining'] <= 1020
    assert 'b'*40 not in json.dumps(value)


@pytest.mark.parametrize('mode', ['backend', 'demo'])
@pytest.mark.parametrize('tool,version', [('node', 'v24.18.0'), ('npm', '11.17.0')])
def test_both_modes_reject_nonexact_node_or_npm_before_runtime(tmp_path, mode, tool, version):
    env = source_tools(tmp_path, initialize(tmp_path, INTEGRATION_MODE=mode))
    fake_tool(tmp_path/'bin', tool, 'echo ' + version)
    assert run('Record checked-out source and actual tools', env).returncode != 0
    assert not (Path(env['GATEWAY_CI_PARENT'])/'ci-source.json').exists()


@pytest.mark.parametrize('problem', ['commit', 'dirty', 'expired'])
def test_source_recording_fails_closed_before_runtime(tmp_path, problem):
    env = source_tools(tmp_path, initialize(tmp_path),
        commit='d'*40 if problem == 'commit' else 'a'*40, dirty=problem == 'dirty')
    if problem == 'expired': env['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH'] = str(int(time.time())-1020)
    assert run('Record checked-out source and actual tools', env).returncode != 0
    assert not (Path(env['GATEWAY_CI_PARENT'])/'ci-source.json').exists()


def test_summary_is_allowlisted_hashes_reports_and_preserves_blocked_packet_gate(tmp_path):
    env = source_tools(tmp_path, initialize(tmp_path, INTEGRATION_MODE='demo'))
    assert run('Record checked-out source and actual tools', env).returncode == 0
    root = Path(env['GATEWAY_INTEGRATION_ROOT']); root.mkdir(mode=0o700)
    integration = {'status': 'BLOCKED', 'mode': 'demo', 'source': {'git_commit': 'a'*40, 'git_tree': 'c'*40},
        'started_at': '2026-10-14T00:00:00+00:00', 'finished_at': '2026-10-14T00:05:00+00:00',
        'observer_preflight': {'status': 'BLOCKED', 'reason': 'approved_scoped_observer_not_available'},
        'raw': 'PRIVATE_SENTINEL', 'phases': [{'phase': 'admit-state-a', 'status': 'BLOCKED',
            'suites': [{'suite': 'consumer_rest', 'classification': 'actual', 'status': 'PASS', 'tests': 12,
                'evidence_paths': ['PRIVATE_SENTINEL'], 'private': 'PRIVATE_SENTINEL'},
                {'suite': 'acceptance_security', 'classification': 'actual', 'status': 'PASS', 'tests': 8},
                {'suite': 'offline_egress', 'classification': 'actual', 'status': 'BLOCKED', 'tests': None},
                {'suite': 'PRIVATE_SENTINEL', 'classification': 'actual', 'status': 'PASS', 'tests': 1}]},
            {'phase': 'exact-cleanup', 'status': 'PASS', 'exit_status': 0, 'timed_out': False,
                'started_at': '2026-10-14T00:04:00+00:00', 'finished_at': '2026-10-14T00:05:00+00:00',
                'command': ['PRIVATE_SENTINEL']}]}
    for name, data in [('integration', integration), ('cleanup', {'status': 'PASS', 'private': 'PRIVATE_SENTINEL'})]:
        path = root/(name+'.json'); path.write_text(json.dumps(data)); path.chmod(0o600)
    result = run('Publish only allowlisted CI summary', env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'PRIVATE_SENTINEL' not in result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert value['status'] == 'BLOCKED' and value['cleanup_status'] == 'PASS'
    assert value['executing_workflow']['sha'] == 'b'*40 and value['source']['commit'] == 'a'*40
    assert value['packet_gate'] == {'status': 'BLOCKED', 'reason': 'approved_scoped_observer_not_available'}
    assert value['cycles']['a'] == [
        {'suite': 'consumer_rest', 'classification': 'actual', 'status': 'PASS', 'tests': 12},
        {'suite': 'acceptance_security', 'classification': 'actual', 'status': 'PASS', 'tests': 8},
        {'suite': 'offline_egress', 'classification': 'actual', 'status': 'BLOCKED', 'tests': None}]
    assert value['report_sha256']['integration'] == hashlib.sha256((root/'integration.json').read_bytes()).hexdigest()
    assert value['report_sha256']['cleanup'] == hashlib.sha256((root/'cleanup.json').read_bytes()).hexdigest()
    assert value['phases'][-1] == {'phase': 'exact-cleanup', 'status': 'PASS', 'exit_status': 0,
        'tests': None, 'timed_out': False, 'started_at': '2026-10-14T00:04:00+00:00',
        'finished_at': '2026-10-14T00:05:00+00:00'}
    assert value['limitations']['production'] == 'NOT APPROVED'


@pytest.mark.parametrize('problem', ['missing', 'symlink', 'oversized', 'wrong_source'])
def test_summary_never_promotes_missing_or_untrusted_reports(tmp_path, problem):
    env = source_tools(tmp_path, initialize(tmp_path))
    assert run('Record checked-out source and actual tools', env).returncode == 0
    root = Path(env['GATEWAY_INTEGRATION_ROOT']); root.mkdir(mode=0o700)
    path = root/'integration.json'
    if problem == 'symlink': path.symlink_to(Path(env['GATEWAY_CI_PARENT'])/'ci-identity.json')
    elif problem == 'oversized': path.write_bytes(b'x'*(1024*1024+1)); path.chmod(0o600)
    elif problem == 'wrong_source':
        path.write_text(json.dumps({'status': 'PASS', 'source': {'git_commit': 'd'*40, 'git_tree': 'c'*40}}))
        path.chmod(0o600)
    result = run('Publish only allowlisted CI summary', env)
    assert result.returncode != 0
    value = json.loads(result.stdout)
    assert value['status'] == 'BLOCKED'
    assert 'Traceback' not in result.stderr
