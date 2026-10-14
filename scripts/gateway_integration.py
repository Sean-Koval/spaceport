"""Fixed integration phases with separate backend and composed-demo evidence.

This orchestrates only disposable synthetic resources. It never installs system
software, elevates privileges, transmits diagnostic files, or claims a blocked
observer as success. Full/diagnostic legacy harness semantics remain separate.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import secrets
import subprocess
import sys
import time

from scripts import demo_runtime as runtime
from scripts import verify_demo

ROOT = runtime.ROOT
BACKEND_SELECTION = ['-m', 'postgres or cube']
WORK_SECONDS = 1080
CLEANUP_SECONDS = 120
MAX_LOG_BYTES = 16 * 1024 * 1024


class Blocked(RuntimeError):
    pass


class CleanupExpired(BaseException):
    """Cannot be swallowed by a best-effort per-resource cleanup handler."""


class Budget:
    def __init__(self, seconds=WORK_SECONDS):
        self.deadline = time.monotonic() + seconds

    def remaining(self):
        return max(0, math.floor(self.deadline - time.monotonic()))

    def admit(self, seconds):
        if seconds > self.remaining():
            raise Blocked('Remaining phase budget is insufficient')
        return seconds


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for flag in ('backend', 'demo', 'record-backends', 'validate-backend-path',
                 'validate-demo-paths', 'fresh-state', 'cleanup-run'):
        modes.add_argument('--' + flag, dest='mode', action='store_const', const=flag)
    parser.add_argument('paths', nargs='+', type=Path)
    args = parser.parse_args(argv)
    count = 2 if args.mode in {'validate-demo-paths', 'fresh-state'} else 1
    if len(args.paths) != count:
        parser.error('Wrong number of exact state/evidence paths')
    return args


def validate_path(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or len(str(path)) > 4096:
        raise ValueError('Absolute bounded external path required')
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Symlink paths are not admitted')
    if path == ROOT or ROOT in path.parents:
        raise ValueError('State and evidence must remain outside the repository')
    return path


def validate_new_directory(path, *, private_parent=False):
    path = validate_path(path)
    if os.path.lexists(path) or not path.parent.is_dir():
        raise ValueError('A new directory with an existing parent is required')
    if private_parent:
        info = path.parent.stat()
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
            raise ValueError('An owner-controlled private parent is required')
    return path


def validate_demo_paths(state, receipts):
    state = runtime.load_state(validate_path(state))
    if state['source_inventory'] != runtime.source_inventory():
        raise ValueError('Prepared source changed')
    validate_new_directory(receipts, private_parent=True)
    return state


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def run_phase(name, command, output, budget, seconds, *, env=None):
    """Run a fixed command with a bounded log and exact descendant supervision."""
    from scripts.runtime_process import bounded_process
    row = {'phase': name, 'command': command, 'started_at': utc_now(),
        'status': 'BLOCKED', 'exit_status': None, 'timed_out': False,
        'output_limit_exceeded': False}
    try:
        budget.admit(seconds)
    except Blocked:
        return {**row, 'finished_at': utc_now(), 'reason': 'insufficient_remaining_phase_budget'}
    path = output / (name + '.log')
    result = bounded_process(command, path, timeout=seconds, env=env, cwd=ROOT, limit_bytes=MAX_LOG_BYTES)
    if result.get('descendant_cleanup') is not True:
        raise Blocked('Owned process cleanup is unverified; later phases must not start')
    return {**row, **result, 'finished_at': utc_now(),
        'status': 'PASS' if result['exit_status'] == 0 and result['descendant_cleanup'] else 'BLOCKED',
        'log': path.name, 'log_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def current_source():
    commit = runtime.run(['git', 'rev-parse', 'HEAD'], timeout=10).stdout.strip()
    tree = runtime.run(['git', 'rev-parse', 'HEAD^{tree}'], timeout=10).stdout.strip()
    if not re.fullmatch('[a-f0-9]{40}', commit) or not re.fullmatch('[a-f0-9]{40}', tree):
        raise ValueError('Exact Git source identity required')
    paths = ['scripts/verify_gateway_integration.sh', 'scripts/gateway_integration.py',
        'scripts/verify_cube_runtime.sh', 'scripts/verify_registry_postgres.sh', '.github/workflows/verify-cube.yml']
    return {'git_commit': commit, 'git_tree': tree,
        'source_digest': runtime.source_digest(runtime.source_inventory()),
        'harness_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths}}


def parse_collection(text):
    nodes = [line.strip() for line in text.splitlines() if line.startswith('tests/') and '::' in line]
    identities = []
    for node in nodes:
        parts = node.split('::')
        if not parts[0].endswith('.py') or any(not part for part in parts):
            raise ValueError('Invalid collected identity')
        identities.append((parts[0][:-3].replace('/', '.') + ('.' + '.'.join(parts[1:-1]) if len(parts) > 2 else ''), parts[-1]))
    if not identities or len(set(identities)) != len(identities):
        raise ValueError('Missing or duplicate collected identity')
    return set(identities)


def collect_test_identities(selection):
    text = verify_demo.collect_pytest([sys.executable, '-m', 'pytest', *selection, '--collect-only', '-q'])
    return parse_collection(text)


def collect_backend_identities():
    return collect_test_identities(BACKEND_SELECTION)


def validate_backend_junit(path, expected):
    count, identities = verify_demo.junit_details(path)
    if identities != expected:
        raise ValueError('Complete backend testcase identity mismatch')
    return count


def validate_backend_report(root, images):
    path = root / 'backends.json'
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError('Unbounded backend receipt')
    receipt = json.loads(path.read_text())
    if receipt.get('classification') != 'actual' or receipt.get('source') != current_source() or receipt.get('images') != images:
        raise ValueError('Backend source/image identity changed')
    junit = root / 'backends.xml'
    count = validate_backend_junit(junit, collect_backend_identities())
    if receipt.get('junit') != junit.name or hashlib.sha256(junit.read_bytes()).hexdigest() != receipt.get('sha256'):
        raise ValueError('Backend evidence bytes changed')
    if count != receipt.get('tests') or receipt.get('status') != 'PASS':
        raise ValueError('Incomplete backend gate')
    return receipt


def record_backends(root):
    validate_new_directory(root, private_parent=True)
    images = verify_demo.prerequisite_images(None)
    identities = verify_demo.validate_backend_endpoints(images)
    source = current_source()
    expected = collect_backend_identities()
    root.mkdir(mode=0o700)
    output = root / 'backends.xml'
    phase = run_phase('backends', [sys.executable, '-m', 'pytest', *BACKEND_SELECTION,
        '-q', '--junitxml=' + str(output)], root, Budget(580), 570, env=verify_demo.pytest_environment())
    if phase['status'] != 'PASS':
        runtime.private_json(root / 'blocked.json', phase)
        raise Blocked('Actual full backend gate failed')
    count = validate_backend_junit(output, expected)
    if current_source() != source or verify_demo.validate_backend_endpoints(images) != identities:
        raise ValueError('Source or live backend identity changed during verification')
    receipt = {'classification': 'actual', 'status': 'PASS', 'source': source,
        'images': images, 'containers': identities, 'tests': count,
        'junit': output.name, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
        'phase': phase, 'recorded_at': utc_now()}
    runtime.private_json(root / 'backends.json', receipt)
    return validate_backend_report(root, images)


def required_phase(rows, name, command, root, budget, seconds, *, env=None):
    row = run_phase(name, command, root, budget, seconds, env=env)
    rows.append(row)
    if row['status'] != 'PASS':
        raise Blocked('A required preparation/backend phase failed')
    return row


def preflight(mode):
    verify_demo.pytest_environment()
    # An exact reviewed checkout is required. Source modifications need a fresh
    # reviewed build/receipt, never reuse of an earlier immutable identity.
    if runtime.run(['git', 'status', '--porcelain', '--untracked-files=all'], timeout=10).stdout.strip():
        raise Blocked('A clean immutable checkout is required')
    runtime.require_docker()
    if runtime.run(['node', '--version'], timeout=10).stdout.strip() != 'v24.19.0':
        raise Blocked('Exact Node 24.19.0 is required before acquisition')
    if runtime.run(['npm', '--version'], timeout=10).stdout.strip() != '11.9.0':
        raise Blocked('Exact npm 11.9.0 is required before acquisition')
    if mode == 'demo':
        if os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'):
            raise Blocked('Browser overrides are not admitted for integration')
        from scripts.check_egress import observer_preflight
        return observer_preflight()
    return None


def register_compatibility(root, kind, project, label):
    root = runtime.validate_directory(validate_path(root))
    prefix = 'gateway-cube-' if kind == 'cube' else 'gateway-registry-test-'
    if kind not in {'cube', 'registry'} or not re.fullmatch(prefix + '[a-f0-9]{16}', project) or label != project + '-local':
        raise ValueError('Invalid private compatibility identity')
    runtime.private_json(root / (kind + '-cleanup.json'), {'kind': kind, 'project': project, 'run_label': label})


def cleanup_compatibility(root):
    failures = []
    for name, label_key in (('registry', 'gateway.registry.run'), ('cube', 'gateway.cube.run')):
        path = root / (name + '-cleanup.json')
        if not path.exists():
            continue
        try:
            if path.is_symlink() or path.stat().st_size > 4096:
                raise ValueError('Invalid compatibility cleanup record')
            value = json.loads(path.read_text())
            prefix = 'gateway-cube-' if name == 'cube' else 'gateway-registry-test-'
            if set(value) != {'kind', 'project', 'run_label'} or value['kind'] != name or not re.fullmatch(prefix + '[a-f0-9]{16}', value['project']) or value['run_label'] != value['project'] + '-local':
                raise ValueError('Invalid exact compatibility cleanup identity')
        except (OSError, ValueError, KeyError, TypeError):
            failures.append(name)
            continue
        for kind in (('container',) if name == 'registry' else ('container', 'network', 'volume', 'image')):
            flags = ['--all'] if kind == 'container' else []
            if kind != 'volume':
                flags += ['--no-trunc']
            listing = ['docker', kind, 'ls', *flags, '--quiet', '--filter', 'label=' + label_key + '=' + value['run_label']]
            try:
                for identifier in set(runtime.run(listing, timeout=15).stdout.split()):
                    try:
                        runtime.remove_exact(kind, identifier)
                    except Exception:
                        failures.append(name + ':' + kind)
                if runtime.run(listing, timeout=15).stdout.split():
                    failures.append(name + ':' + kind)
            except Exception:
                failures.append(name + ':' + kind)
    if failures:
        raise Blocked('Compatibility resources have unverified exact cleanup')


def cleanup_run(root):
    """Only predetermined state paths allocated by this exact integration run."""
    root = runtime.validate_directory(validate_path(root))
    records = []
    try:
        cleanup_compatibility(root)
    except Exception:
        records.append({'status': 'BLOCKED', 'state': 'compatibility'})
    # A owns the reused images, so B and both restore children go first.
    for name in ('state-b/restored', 'state-a/restored', 'state-b', 'state-a'):
        path = root / name
        if not path.exists():
            continue
        try:
            if not (path / 'cleanup.json').is_file():
                raise Blocked('Allocated state has no validated cleanup identity')
            records.append(runtime.cleanup(runtime.load_cleanup_state(validate_path(path))))
        except Exception:
            records.append({'status': 'BLOCKED', 'state': name})
    target = root / 'cleanup.json'
    if target.is_symlink() or (target.exists() and (not target.is_file() or target.stat().st_uid != os.getuid())):
        raise ValueError('Unsafe cleanup result target')
    temporary = root / ('cleanup-result-' + secrets.token_hex(8) + '.json')
    runtime.private_json(temporary, {'status': 'PASS' if all(row['status'] == 'PASS' for row in records) else 'BLOCKED', 'states': records, 'recorded_at': utc_now()})
    os.replace(temporary, target)
    if any(row['status'] != 'PASS' for row in records):
        raise Blocked('Exact cleanup has unverified resources')


def validate_demo_report(state):
    from scripts.demo_evidence import latest_evidence
    evidence = Path(state['state_directory']) / 'evidence'
    candidates = sorted(evidence.glob('verify-*/report.json'))
    if not candidates:
        raise ValueError('Mandatory integration report is missing')
    path = candidates[-1]
    if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Unbounded integration report')
    raw = json.loads(path.read_text())
    if raw.get('versions', {}).get('integration') is not True:
        raise ValueError('Mandatory integration mode was downgraded')
    summary = latest_evidence(evidence, state=state)
    return {'status': summary['status'], 'missing': summary.get('missing', ['unavailable_evidence']),
        'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'suites': summary.get('suites', [])}


def browser_evidence(root):
    # Only the exact locked Playwright browser is admitted. Launching it also
    # exposes missing OS libraries before any expensive Docker image build.
    code = """const { chromium } = require('./publisher/node_modules/playwright');
const pkg = require('./publisher/node_modules/playwright/package.json');
const versions = require('./publisher/node_modules/playwright-core/browsers.json');
(async () => { const browser = await chromium.launch({ headless: true, executablePath: chromium.executablePath() });
try { console.log(JSON.stringify({package: pkg.version, executable: chromium.executablePath(),
version: browser.version(), browsers: versions.browsers.filter(x => x.name === 'chromium' || x.name === 'chromium-headless-shell')})); }
finally { await browser.close(); } })().catch(() => process.exit(1));"""
    observed = json.loads(runtime.run(['node', '-e', code], timeout=30).stdout)
    chromium = [row for row in observed['browsers'] if row['name'] == 'chromium']
    if observed['package'] != '1.64.0' or len(chromium) != 1 or observed['version'] != chromium[0]['browserVersion'] or not chromium[0]['revision']:
        raise ValueError('Locked Playwright identity changed')
    executable = Path(observed['executable'])
    if not executable.is_file():
        raise ValueError('Prepared browser is missing')
    digest = hashlib.sha256()
    with executable.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    observed['executable_sha256'] = digest.hexdigest()
    observed['recorded_at'] = utc_now()
    runtime.private_json(root / 'browser.json', observed)
    return observed


def demo_cycles(root, rows, budget):
    offline = {**verify_demo.pytest_environment(), 'UV_OFFLINE': '1', 'npm_config_offline': 'true',
        'npm_config_audit': 'false', 'npm_config_fund': 'false', 'DO_NOT_TRACK': '1', 'OTEL_SDK_DISABLED': 'true'}
    state_a = root / 'state-a'
    receipts = root / 'prerequisites'
    required_phase(rows, 'publisher-dependencies', ['npm', '--prefix', 'publisher', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], root, budget, 180)
    required_phase(rows, 'browser-acquisition', ['node', 'publisher/node_modules/playwright/cli.js', 'install', 'chromium'], root, budget, 180)
    browser = browser_evidence(root)
    offline['PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'] = browser['executable']
    required_phase(rows, 'prepare-state-a', [sys.executable, '-m', 'scripts.demo_runtime', 'prepare', '--state-dir', str(state_a)], root, budget, 300)
    required_phase(rows, 'compatibility-prerequisites', ['bash', 'scripts/verify_cube_runtime.sh', '--demo-prerequisites', str(state_a), str(receipts)], root, budget, 360)
    # Compatibility is fully torn down before the independent edge claims 8080.
    cleanup_compatibility(root)
    prepared = runtime.load_state(state_a)
    runtime.verify_images(prepared)
    verify_demo.validate_prerequisites(receipts, prepared, verify_demo.prerequisite_images(prepared))
    for cycle, path in (('a', state_a), ('b', root / 'state-b')):
        if cycle == 'b':
            required_phase(rows, 'stop-state-a', [sys.executable, '-m', 'scripts.demo_runtime', 'down', '--state-dir', str(state_a)], root, budget, 90, env=offline)
            required_phase(rows, 'prepare-state-b-offline', [sys.executable, '-m', 'scripts.gateway_integration', '--fresh-state', str(state_a), str(path)], root, budget, 60, env=offline)
        required_phase(rows, 'start-state-' + cycle, [sys.executable, '-m', 'scripts.demo_runtime', 'up', '--state-dir', str(path)], root, budget, 180, env=offline)
        # A missing observer is deliberately nonzero but must not prevent the
        # independent business, security and operation suites from executing.
        rows.append(run_phase('verify-state-' + cycle, [sys.executable, '-m', 'scripts.verify_demo', '--integration', '--state-dir', str(path), '--prerequisites-dir', str(receipts)], root, budget, 330, env=offline))
        rows.append(run_phase('evidence-state-' + cycle, [sys.executable, '-m', 'scripts.demo_evidence', '--state-dir', str(path)], root, budget, 60, env=offline))
        rows.append({'phase': 'admit-state-' + cycle, **validate_demo_report(runtime.load_state(path))})


def job_budget():
    now = time.time()
    supplied = os.environ.get('GATEWAY_INTEGRATION_FIRST_STEP_EPOCH')
    if supplied is None:
        if os.environ.get('GITHUB_ACTIONS') == 'true':
            raise Blocked('CI first-step timestamp is required for bounded admission')
        started = now
    else:
        if not re.fullmatch(r'[0-9]{1,12}', supplied):
            raise Blocked('Invalid CI first-step timestamp')
        started = int(supplied)
        if started > now or now-started >= 1200:
            raise Blocked('CI first-step timestamp is stale or in the future')
    cleanup_deadline = started + 1200 - 60
    work = min(WORK_SECONDS, math.floor(cleanup_deadline-now-CLEANUP_SECONDS))
    if work <= 0:
        raise Blocked('Job cleanup reserve is already needed; no work may start')
    return {'work_seconds': work, 'cleanup_deadline_epoch': cleanup_deadline,
        'first_step_epoch': started, 'setup_elapsed_seconds': max(0, now-started),
        'runner_margin_seconds': 60, 'cleanup_reserve_seconds': CLEANUP_SECONDS,
        'assumption': 'First-step time is not exact job-start time; 60 seconds is reserved for runner/finalization overhead'}


def cleanup_remaining(root):
    path = root / 'budget.json'
    if not path.exists():
        if os.environ.get('GITHUB_ACTIONS') == 'true':
            raise Blocked('Missing persisted cleanup deadline')
        return CLEANUP_SECONDS
    if path.is_symlink() or path.stat().st_size > 4096:
        raise ValueError('Invalid bounded cleanup deadline')
    value = json.loads(path.read_text())
    if value['cleanup_deadline_epoch'] != value['first_step_epoch']+1140:
        raise ValueError('Cleanup deadline widened')
    remaining = min(CLEANUP_SECONDS, math.floor(value['cleanup_deadline_epoch']-time.time()))
    if remaining <= 10:
        raise Blocked('Shared cleanup deadline exhausted; cleanup is unverified')
    return remaining


def verify_units(root, rows, budget):
    selection = ['-m', 'unit or contract']
    expected = collect_test_identities(selection)
    output = root / 'units-contracts.xml'
    required_phase(rows, 'units-contracts', [sys.executable, '-m', 'pytest', *selection,
        '-q', '--junitxml=' + str(output)], root, budget, 120, env=verify_demo.pytest_environment())
    count = validate_backend_junit(output, expected)
    rows.append({'phase': 'admit-units-contracts', 'status': 'PASS', 'tests': count,
        'junit_sha256': hashlib.sha256(output.read_bytes()).hexdigest()})


def backend_phase_allowance(budget):
    # Run 11 on cb5c497e3a9e4765188a9216643afeb0abe24029 observed
    # 147.86 seconds before the registry child. 180 is an explicit planning
    # margin, not a guaranteed future bound; the child retains its own 600 cap.
    available = min(900, budget.remaining() - 10)
    if available < 600 + 180:
        raise Blocked('Insufficient budget for backend compatibility planning allowance')
    return available


def integrate(mode, root):
    validate_new_directory(root)
    root.mkdir(mode=0o700)
    rows = []
    allowance = job_budget()
    runtime.private_json(root / 'budget.json', allowance)
    budget = Budget(allowance['work_seconds'])
    report = {'schema_version': 1, 'mode': mode, 'classification': 'actual', 'status': 'BLOCKED',
        'started_at': utc_now(), 'phases': rows, 'work_budget_seconds': allowance['work_seconds'], 'job_budget': allowance,
        'cleanup_reserve_seconds': CLEANUP_SECONDS, 'separate_backend_gate_required': mode == 'demo',
        'limitations': {'live_bigquery': 'NOT RUN', 'studio': 'NOT TESTED', 'domo': 'NOT IMPLEMENTED', 'production': 'NOT APPROVED'}}
    previous_alarm = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(Blocked('Overall work budget exhausted')))
    signal.setitimer(signal.ITIMER_REAL, allowance['work_seconds'])
    try:
        report['observer_preflight'] = preflight(mode)
        report['source'] = current_source()
        report['tools'] = {'python': sys.version.split()[0], 'platform': sys.platform,
            'uv': runtime.run(['uv', '--version'], timeout=10).stdout.strip(),
            'docker': runtime.run(['docker', 'version', '--format', '{{.Server.Version}}'], timeout=15).stdout.strip()}
        report['tools'].update(node='24.19.0', npm='11.9.0')
        # TypeScript MCP contract tests require the locked SDK even in backend
        # mode. Acquire it once before the offline unit/contract selection.
        required_phase(rows, 'client-dependencies', ['npm', '--prefix', 'clients', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], root, budget, 180)
        verify_units(root, rows, budget)
        if mode == 'backend':
            seconds = backend_phase_allowance(budget)
            report['backend_planning'] = {'outer_cap_seconds': seconds, 'child_cap_seconds': 600,
                'compatibility_allowance_seconds': 180, 'observed_pre_child_seconds': 147.86,
                'observed_source_commit': 'cb5c497e3a9e4765188a9216643afeb0abe24029',
                'fit_guaranteed': False}
            required_phase(rows, 'backend-compatibility', ['bash', 'scripts/verify_cube_runtime.sh', '--backend-acceptance', str(root / 'backends')], root, budget, seconds, env=verify_demo.pytest_environment())
            cleanup_compatibility(root)
            receipt = validate_backend_report(root / 'backends', verify_demo.prerequisite_images(None))
            report['backend_tests'] = receipt['tests']
        else:
            demo_cycles(root, rows, budget)
        if current_source() != report['source']:
            raise Blocked('Source changed during integration')
        report['status'] = 'PASS' if all(row['status'] == 'PASS' for row in rows) else 'BLOCKED'
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        # Exact commands/timestamps/status are retained; subprocess output stays
        # private. Never print arbitrary exceptions containing environment data.
        report['blocked_reason'] = 'Required tools, source, phase budget, runtime gate or evidence admission failed'
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm)
        try:
            remaining = cleanup_remaining(root)
            cleanup = run_phase('exact-cleanup', [sys.executable, '-m', 'scripts.gateway_integration', '--cleanup-run', str(root)], root, Budget(remaining), remaining - 10)
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
            cleanup = {'phase': 'exact-cleanup', 'status': 'BLOCKED', 'exit_status': None,
                'reason': 'Shared cleanup deadline or exact process termination could not be verified'}
        rows.append(cleanup)
        if cleanup['status'] != 'PASS':
            report['status'] = 'BLOCKED'
        report['finished_at'] = utc_now()
        runtime.private_json(root / 'integration.json', report)
    return report


def main(argv=None):
    args = parse_args(argv)
    os.umask(0o077)
    if args.mode in {'backend', 'demo'}:
        def interrupted(*_):
            raise Blocked('Integration interrupted; exact cleanup required')
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
    try:
        if args.mode == 'validate-backend-path':
            validate_new_directory(args.paths[0], private_parent=True)
            return 0
        if args.mode == 'validate-demo-paths':
            validate_demo_paths(*args.paths)
            return 0
        if args.mode == 'record-backends':
            report = record_backends(args.paths[0])
        elif args.mode == 'fresh-state':
            source = runtime.load_state(validate_path(args.paths[0]))
            runtime.verify_images(source)
            validate_new_directory(args.paths[1], private_parent=True)
            runtime.prepare_restored_state(args.paths[1], source['port'], source)
            return 0
        elif args.mode == 'cleanup-run':
            remaining = cleanup_remaining(args.paths[0])
            def cleanup_expired(*_):
                raise CleanupExpired('Shared cleanup deadline exhausted')
            signal.signal(signal.SIGALRM, cleanup_expired)
            signal.setitimer(signal.ITIMER_REAL, remaining - 5)
            cleanup_run(args.paths[0])
            signal.setitimer(signal.ITIMER_REAL, 0)
            return 0
        else:
            report = integrate(args.mode, args.paths[0])
        print(json.dumps(report, sort_keys=True))
        return 0 if report['status'] == 'PASS' else 1
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, CleanupExpired):
        print(json.dumps({'status': 'BLOCKED', 'mode': args.mode, 'reason': 'Exact prerequisites, evidence or cleanup unavailable'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
