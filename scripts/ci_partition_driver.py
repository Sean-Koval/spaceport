"""Trusted, closed serial-CI controller. Candidate code is only the test workload.

Invoke controllers using isolated Python with -I -S and an absolute trusted
path. No imports, cleanup programs or phase commands are chosen by candidate data.
Legacy integration commands keep their existing behavior.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import types

TRUSTED_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = '.github/workflows/verify-gateway-partitions.yml'
SOURCE_SECONDS = 180
BACKEND_SECONDS = 570
CHILD_SECONDS = 600
CLEANUP_SECONDS = 120
JOB_SECONDS = 1200
FINALIZATION_SECONDS = 60
MAX_LOG_BYTES = 16 * 1024 * 1024
CONTROLLERS = (
    WORKFLOW, 'scripts/ci_partition_driver.py', 'scripts/ci_partition_contract.py',
    'scripts/verify_cube_runtime.sh', 'scripts/verify_registry_postgres.sh',
    'scripts/gateway_integration.py', 'scripts/demo_runtime.py', 'scripts/verify_demo.py',
    'scripts/runtime_process.py', 'scripts/pytest_runtime_diagnostics.py',
    'scripts/cube_runtime_secrets.py', 'scripts/source_runtime_secrets.py',
    'scripts/cube_runtime_diagnostics.py', 'scripts/source_runtime_gate.py',
    'scripts/wait_registry_postgres.py', 'tests/integration/source_journey_host.py',
)
HASH40 = re.compile(r'[0-9a-f]{40}')
HASH64 = re.compile(r'[0-9a-f]{64}')
BLOCKED = 'BLOCKED: trusted partition evidence or cleanup unavailable'


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON key')
        value[key] = item
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def checked_directory(path):
    path = Path(path)
    if (not path.is_absolute() or '..' in path.parts or len(str(path)) > 4096
            or any(p.is_symlink() for p in (path, *path.parents)) or not path.is_dir()):
        raise ValueError('Exact directory required')
    return path


def require_separate_roots(candidate, trusted=TRUSTED_ROOT):
    candidate, trusted = checked_directory(candidate), checked_directory(trusted)
    if candidate == trusted or candidate in trusted.parents or trusted in candidate.parents:
        raise ValueError('Separate trusted and candidate roots required')
    return candidate


def private_bytes(path, limit):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Private file required')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > limit):
        raise ValueError('Bounded private file required')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Bounded private file required')
    return raw


def read_private(path, limit=1024 * 1024):
    return json.loads(private_bytes(path, limit), object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def write_private(path, value):
    path = Path(path)
    if any(p.is_symlink() for p in (path.parent, *path.parents)):
        raise ValueError('Private output required')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())


def git_identity(root, expected):
    root = checked_directory(root)
    if not HASH40.fullmatch(expected):
        raise ValueError('Exact source SHA required')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args],
            timeout=15, text=True, stderr=subprocess.DEVNULL).strip()
    commit, tree = git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD^{tree}')
    if commit != expected or not HASH40.fullmatch(tree) or git('status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Clean exact source required')
    return {'commit': commit, 'tree': tree}


def controller_digest():
    values = {}
    for name in CONTROLLERS:
        path = TRUSTED_ROOT / name
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
            raise ValueError('Exact trusted controller closure required')
        values[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest(values)


def pin_trusted_namespace(name, relative):
    path = TRUSTED_ROOT / relative
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        sys.modules[name] = module
    elif list(getattr(module, '__path__', ())) != [str(path)]:
        raise ValueError('Shadowed trusted package namespace')


def trusted_module(name):
    allowed = {Path(p).stem for p in CONTROLLERS if p.startswith('scripts/') and p.endswith('.py')}
    if name not in allowed:
        raise ValueError('Unknown trusted helper')
    # -I -S supplies only the interpreter's stdlib paths; the sole project path
    # added here is the executing trusted bootstrap, never the workload cwd.
    if str(TRUSTED_ROOT) not in sys.path:
        sys.path.insert(0, str(TRUSTED_ROOT))
    pin_trusted_namespace('scripts','scripts')
    module = importlib.import_module('scripts.' + name)
    if Path(module.__file__).resolve() != TRUSTED_ROOT / 'scripts' / (name + '.py'):
        raise ValueError('Shadowed trusted controller')
    return module


def contract():
    return trusted_module('ci_partition_contract')


def trusted_dependencies(candidate):
    candidate = require_separate_roots(candidate)
    site = TRUSTED_ROOT / '.venv/lib/python3.12/site-packages'
    if not site.is_dir() or site.is_symlink():
        raise ValueError('Separate locked bootstrap dependencies required')
    resolved = site.resolve()
    if resolved == candidate or candidate in resolved.parents:
        raise ValueError('Candidate dependencies cannot supply controller imports')
    if str(resolved) not in sys.path:
        sys.path.append(str(resolved))
    return resolved


def check_environment_budget(env):
    size = 0
    for key, value in env.items():
        if type(key) is not str or type(value) is not str:
            raise ValueError('Invalid environment')
        encoded = (key + '=' + value).encode('utf-8')
        if len(encoded) >= 128 * 1024:
            raise ValueError('Single environment value exceeds platform bound')
        size += len(encoded) + 1 + 8
    if size >= min(1024 * 1024, os.sysconf('SC_ARG_MAX') - 128 * 1024):
        raise ValueError('Aggregate environment exceeds platform bound')


def workload_environment(value=None):
    env = dict(os.environ if value is None else value)
    if any(env.get(key) for key in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS')):
        raise ValueError('Ambient pytest controls rejected')
    for name in tuple(env):
        if (name.startswith(('PYTHON', 'ACTIONS_', 'INPUT_')) or name in {
                'GITHUB_OUTPUT', 'GITHUB_ENV', 'GITHUB_PATH', 'GITHUB_STEP_SUMMARY',
                'GITHUB_STATE', 'GITHUB_TOKEN', 'GH_TOKEN'}):
            env.pop(name)
    env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    check_environment_budget(env)
    return env


def work_remaining(started, *, now=None):
    now = time.time() if now is None else now
    remaining = math.floor(started + JOB_SECONDS - CLEANUP_SECONDS - FINALIZATION_SECONDS - now)
    if now < started or remaining <= 0:
        raise ValueError('Work budget exhausted')
    return remaining


def cleanup_remaining(started, *, now=None):
    now = time.time() if now is None else now
    remaining = min(CLEANUP_SECONDS, math.floor(started + JOB_SECONDS - FINALIZATION_SECONDS - now))
    if now < started or remaining <= 5:
        raise ValueError('Cleanup budget exhausted')
    return remaining


def finalization_remaining(started, *, now=None):
    now = time.time() if now is None else now
    remaining = min(FINALIZATION_SECONDS, math.floor(started + JOB_SECONDS - now))
    if now < started or remaining <= 0:
        raise ValueError('Finalization budget exhausted')
    return remaining


def modules(candidate):
    candidate = require_separate_roots(candidate)
    runtime = trusted_module('demo_runtime'); runtime.ROOT = candidate
    verify = trusted_module('verify_demo'); verify.ROOT = candidate
    harness = trusted_module('gateway_integration'); harness.ROOT = candidate
    diagnostics = trusted_module('pytest_runtime_diagnostics'); diagnostics.ROOT = candidate
    pin_trusted_namespace('tests','tests'); pin_trusted_namespace('tests.integration','tests/integration')
    journey = importlib.import_module('tests.integration.source_journey_host')
    if Path(journey.__file__).resolve() != TRUSTED_ROOT/'tests/integration/source_journey_host.py':
        raise ValueError('Shadowed trusted journey helper')
    journey.ROOT = candidate
    return runtime, verify, harness


def source_identity(candidate, expected):
    identity = git_identity(candidate, expected)
    runtime, _, _ = modules(candidate)
    identity['digest'] = runtime.source_digest(runtime.source_inventory())
    return identity


def context(candidate, *, job_id):
    candidate = require_separate_roots(candidate)
    env = os.environ
    git_identity(TRUSTED_ROOT, env['GITHUB_WORKFLOW_SHA'])
    source = source_identity(candidate, env['REVIEWED_SOURCE_SHA'])
    repository = env['GITHUB_REPOSITORY']; ref = env['GITHUB_WORKFLOW_REF']
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or not ref.startswith(repository + '/' + WORKFLOW + '@')
            or not re.fullmatch(r'[A-Za-z0-9_./@-]{1,512}', ref)
            or not HASH64.fullmatch(env['REVIEWED_MANIFEST_SHA256'])
            or any(not re.fullmatch(r'[1-9][0-9]{0,19}', env[key])
                   for key in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'))):
        raise ValueError('Exact executing context required')
    if job_id not in ('source', *contract().LANES):
        raise ValueError('Unknown job')
    return {'repository': repository, 'source_commit': source['commit'],
        'source_tree': source['tree'], 'source_digest': source['digest'],
        'workflow_file': WORKFLOW, 'workflow_ref': ref, 'workflow_sha': env['GITHUB_WORKFLOW_SHA'],
        'controller_digest': controller_digest(), 'run_id': env['GITHUB_RUN_ID'],
        'run_attempt': env['GITHUB_RUN_ATTEMPT']}


def source_from(identity):
    return {'commit': identity['source_commit'], 'tree': identity['source_tree'],
            'digest': identity['source_digest']}


def observed_tools(candidate):
    def observed(command, pattern):
        value = subprocess.check_output(command, cwd=TRUSTED_ROOT, timeout=2,
            text=True, stderr=subprocess.DEVNULL).strip()
        if not re.fullmatch(pattern, value):
            raise ValueError('Unexpected actual tool identity')
        return value
    observed([str(TRUSTED_ROOT/'.venv/bin/python'),'--version'], r'Python 3\.12\.14')
    observed([str(candidate_python(candidate)),'--version'], r'Python 3\.12\.14')
    version = r'[A-Za-z0-9v.+ _-]{1,128}'
    runner = os.environ['ImageVersion']
    if not re.fullmatch(version, runner):raise ValueError('Invalid runner image')
    return {'python':'3.12.14', 'uv':observed(['uv','self','version','--short'],r'0\.12\.23'),
        'node':observed(['node','--version'],r'v24\.19\.0')[1:],
        'npm':observed(['npm','--version'],r'11\.9\.0'),
        'docker':observed(['docker','version','--format','{{.Client.Version}} {{.Server.Version}}'],version),
        'compose':observed(['docker','compose','version','--short'],version), 'runner_image':runner}


def candidate_python(candidate):
    path = candidate / '.venv/bin/python'
    # The interpreter is a locked installed workload prerequisite, not a controller.
    if not path.is_file():
        raise ValueError('Pinned candidate interpreter missing')
    return path


def phase(name, command, root, candidate, cap, started):
    if cap > work_remaining(started):
        raise ValueError('Insufficient phase budget')
    began = time.monotonic()
    supervision = trusted_module('runtime_process').bounded_process(command,
        root / (name + '.log'), timeout=cap, env=workload_environment(), cwd=candidate,
        limit_bytes=MAX_LOG_BYTES)
    _, verify, _ = modules(candidate)
    row = {'name': name, 'status': 'PASS' if verify.process_passed(supervision) else 'BLOCKED',
        'cap_seconds': cap, 'elapsed_ms': math.ceil((time.monotonic() - began) * 1000),
        'supervision': supervision}
    write_private(root / (name + '.json'), row)
    if row['status'] != 'PASS':
        raise ValueError('Supervised phase did not pass')
    return row


def collect(candidate, root, name, selection, started):
    output = root / (name + '-nodes.json')
    row = phase(name, [str(candidate_python(candidate)), '-I', str(Path(__file__).resolve()),
        'workload-collect', '--candidate', str(candidate), '--output', str(output),
        '--selection', selection], root, candidate, 60, started)
    inventory = read_private(output, 4 * 1024 * 1024)
    if type(inventory) is not list or not inventory or len(inventory) > 10000:
        raise ValueError('Bounded nonempty inventory required')
    return inventory, row


def workload_collect(args):
    # This is deliberately a candidate pytest subprocess, not a controller.
    candidate = checked_directory(args.candidate)
    os.chdir(candidate)
    sys.path.insert(0, str(candidate))
    import pytest
    class Inventory:
        def pytest_collection_finish(self, session):
            self.rows = [{'nodeid': item.nodeid,
                'markers': sorted({marker.name for marker in item.iter_markers()}),
                'fixtures': sorted(item.fixturenames)} for item in session.items]
    inventory = Inventory()
    selected = 'unit or contract' if args.selection == 'source' else 'postgres or cube'
    code = pytest.main(['-m', selected, '--collect-only', '-q', '-p', 'no:cacheprovider'], plugins=[inventory])
    if code != 0:
        return int(code)
    write_private(args.output, inventory.rows)
    return 0


def input_transport(prefix, kind):
    count = 8 if kind == 'manifest' else 3
    fields = ('count', 'encoded_length', 'decoded_length', 'sha256',
              *(f'chunk_{index}' for index in range(count)))
    allowed = {prefix + '_' + field.upper() for field in fields}
    if any(name.startswith(prefix+'_') and name not in allowed for name in os.environ):
        raise ValueError('Unexpected transport field')
    slots = {field: os.environ.get(prefix + '_' + field.upper(), '') for field in fields}
    return contract().decode_transport(slots, kind=kind)


def export_transport(value, kind, prefix):
    slots = contract().encode_transport(value, kind=kind)
    path = Path(os.environ['GITHUB_OUTPUT'])
    if path.is_symlink() or not path.is_absolute():
        raise ValueError('Fixed output channel required')
    with path.open('a', encoding='utf-8') as stream:
        for field, value in slots.items():
            if not re.fullmatch(r'[A-Za-z0-9+/=]*', value) or len(value) > 32768:
                raise ValueError('Bounded output slot required')
            stream.write(prefix + '_' + field + '=' + value + '\n')


def initialize(args):
    candidate = require_separate_roots(args.candidate)
    parent = checked_directory(Path(args.root).parent)
    if (stat.S_IMODE(parent.stat().st_mode) != 0o700 or parent.stat().st_uid != os.getuid()
            or parent == candidate or candidate in parent.parents
            or parent == TRUSTED_ROOT or TRUSTED_ROOT in parent.parents):
        raise ValueError('Private external run parent required')
    root = Path(args.root)
    if root.exists() or root.is_symlink():
        raise ValueError('New private run required')
    started = int(os.environ['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH'])
    work_remaining(started)
    identity = context(candidate, job_id=args.job)
    root.mkdir(mode=0o700)
    value = {'candidate': str(candidate), 'identity': identity, 'job_id': args.job,
        'started': started, 'manifest_sha256': os.environ['REVIEWED_MANIFEST_SHA256']}
    write_private(root / 'context.json', value)
    # Reuse the existing cleanup deadline semantics without extending the job.
    write_private(root / 'budget.json', {'cleanup_deadline_epoch': started + 1140, 'first_step_epoch': started})
    return candidate, root, value


def load_context(candidate, root):
    candidate = require_separate_roots(candidate); root = checked_directory(root)
    value = read_private(root / 'context.json')
    if (set(value) != {'candidate','identity','job_id','started','manifest_sha256'}
            or value['candidate'] != str(candidate)
            or context(candidate, job_id=value['job_id']) != value['identity']):
        raise ValueError('Context/source changed')
    return value


def admit_source(candidate, ctx):
    manifest = input_transport('SOURCE_MANIFEST', 'manifest')
    source_receipt = input_transport('SOURCE_RECEIPT', 'receipt')
    contract().validate_manifest(manifest, source=source_from(ctx['identity']),
        controller_digest=ctx['identity']['controller_digest'], expected_sha256=ctx['manifest_sha256'])
    contract().validate_receipt(source_receipt, identity=ctx['identity'], manifest=manifest, job_id='source')
    return manifest


def admit_lane(candidate, job):
    identity = context(candidate, job_id=job)
    admit_source(candidate, {'identity':identity, 'manifest_sha256':os.environ['REVIEWED_MANIFEST_SHA256']})
    return 0


def run_work(args):
    candidate, root, ctx = initialize(args)
    rows = []
    try:
        runtime, verify, harness = modules(candidate)
        if args.job != 'source': manifest = admit_source(candidate, ctx)
        tools = observed_tools(candidate)
        harness.preflight('backend')
        rows.append(phase('client_dependencies', ['npm','--prefix','clients','ci','--ignore-scripts',
            '--no-audit','--no-fund'], root, candidate, 180, ctx['started']))
        if args.job == 'source':
            inventory, row = collect(candidate, root, 'source_collection', 'source', ctx['started'])
            rows.append(row)
            nodes = tuple(sorted(item['nodeid'] for item in inventory))
            output = root / 'source.xml'
            rows.append(phase('source_tests', [str(candidate_python(candidate)), '-m','pytest',
                '-m','unit or contract','-q','--junitxml='+str(output)], root, candidate,
                SOURCE_SECONDS, ctx['started']))
            summary = contract().junit_summary(private_bytes(output, MAX_LOG_BYTES), nodes)
            summary.update(nodes=[], images_sha256=None, endpoints_sha256=None)
        inventory, row = collect(candidate, root, 'backend_collection', 'backend', ctx['started'])
        rows.append(row)
        fresh = contract().build_manifest(source_from(ctx['identity']), ctx['identity']['controller_digest'], inventory)
        contract().validate_manifest(fresh, source=source_from(ctx['identity']),
            controller_digest=ctx['identity']['controller_digest'], expected_sha256=ctx['manifest_sha256'])
        if args.job != 'source' and fresh != manifest:
            raise ValueError('Collection changed')
        manifest = fresh
        write_private(root / 'manifest.json', manifest)
        if args.job != 'source':
            cap = min(900, work_remaining(ctx['started']) - 10)
            if cap < 780:
                raise ValueError('Backend admission allowance unavailable')
            rows.append(phase('backend_stack', ['bash', str(TRUSTED_ROOT/'scripts/verify_cube_runtime.sh'),
                '--partition-acceptance', str(candidate), str(root)], root, candidate, cap, ctx['started']))
            backend = read_private(root / 'backends' / 'lane-evidence.json')
            rows.append(backend['phase']); summary = backend['test_summary']
        if context(candidate, job_id=args.job) != ctx['identity']:
            raise ValueError('Source/controller changed')
        write_private(root / 'work.json', {'phases': rows, 'test_summary': summary, 'tools': tools})
        return 0
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, KeyError, TypeError):
        print(BLOCKED, file=sys.stderr)
        return 1


def cleanup(candidate, root):
    # Cleanup validates frozen trusted identity but must not need a still-valid
    # candidate source/work receipt: source mutation cannot strand owned resources.
    candidate = require_separate_roots(candidate); root = checked_directory(root)
    ctx = read_private(root / 'context.json')
    if ctx['candidate'] != str(candidate) or ctx['identity']['controller_digest'] != controller_digest():
        raise ValueError('Trusted cleanup identity changed')
    remaining = cleanup_remaining(ctx['started'])
    began = time.monotonic()
    command = [sys.executable, '-I', '-S', str(Path(__file__).resolve()), 'cleanup-child',
        '--candidate', str(candidate), '--root', str(root)]
    result = trusted_module('runtime_process').bounded_process(command, root/'cleanup.log',
        timeout=remaining-5, cwd=TRUSTED_ROOT, env=workload_environment(), limit_bytes=MAX_LOG_BYTES)
    _, verify, _ = modules(candidate)
    passed = verify.process_passed(result)
    receipt = {'identity': ctx['identity'], 'job_id':ctx['job_id'],
        'status':'PASS' if passed else 'BLOCKED', 'cap_seconds':120,
        'elapsed_ms':math.ceil((time.monotonic()-began)*1000),
        'exact_resources_verified':passed, 'descendants_verified':result.get('descendant_cleanup') is True}
    write_private(root/'cleanup-receipt.json',receipt)
    return 0 if passed else 1


def cleanup_child(candidate, root):
    _, _, harness = modules(candidate)
    harness.cleanup_run(Path(root))
    return 0


def finalize(candidate, root):
    ctx = load_context(candidate, root); root = Path(root)
    work = read_private(root/'work.json'); cleanup_value = read_private(root/'cleanup-receipt.json')
    manifest = read_private(root/'manifest.json',192*1024)
    receipt = {'schema_version':1,'identity':ctx['identity'],'job_id':ctx['job_id'],
        'status':'PASS','manifest_sha256':ctx['manifest_sha256'],
        'test_summary':work['test_summary'],'phases':work['phases'],'cleanup':cleanup_value,
        'tools':work['tools'],
        'source_rechecked':True,'controller_rechecked':True}
    if observed_tools(Path(candidate)) != work['tools']:raise ValueError('Actual tools changed')
    contract().validate_manifest(manifest,source=source_from(ctx['identity']),
        controller_digest=ctx['identity']['controller_digest'],expected_sha256=ctx['manifest_sha256'])
    contract().validate_receipt(receipt,identity=ctx['identity'],manifest=manifest,job_id=ctx['job_id'])
    if ctx['job_id']=='source': export_transport(manifest,'manifest','manifest')
    export_transport(receipt,'receipt','receipt')
    print('PASS: exact '+ctx['job_id']+' partition receipt; full backend requires aggregate')
    return 0


def trusted_inline_sources():
    sources = set()
    for name in ('verify_cube_runtime.sh','verify_registry_postgres.sh'):
        lines = (TRUSTED_ROOT/'scripts'/name).read_text().splitlines(keepends=True)
        index = 0
        while index < len(lines):
            match = re.search(r"<<'([A-Z][A-Z0-9_]*)'",lines[index])
            if match:
                delimiter=match.group(1);begin=index+1;index=begin
                while index<len(lines) and lines[index].rstrip('\n')!=delimiter:index+=1
                if index==len(lines):raise ValueError('Unterminated trusted helper')
                sources.add(''.join(lines[begin:index]))
            index+=1
    return sources


def check_inline_source(code):
    if code not in trusted_inline_sources():raise ValueError('Untrusted inline helper')


def python_layout(candidate, code):
    # Fixed workload packaging metadata, never an import path for controllers.
    if code == 'import sysconfig; print(sysconfig.get_path("purelib"))':
        return str(checked_directory(candidate/'.venv/lib/python3.12/site-packages'))
    if code == 'import sys; print(sys.base_prefix)':
        value = subprocess.check_output([str(candidate_python(candidate)), '-I', '-S', '-c', code],
            cwd=TRUSTED_ROOT, env=workload_environment(), text=True, timeout=10).strip()
        return str(checked_directory(Path(value)))
    raise ValueError('Unknown interpreter layout query')


def helper(candidate, root, argv):
    candidate=require_separate_roots(candidate);root=checked_directory(root)
    ctx=load_context(candidate,root)
    runtime, verify, harness=modules(candidate)
    # Only trusted locked dependencies may be imported by controller helpers.
    trusted_dependencies(candidate)
    if argv[:2]==['-m','scripts.gateway_integration']:
        tail=argv[2:]
        if len(tail)==2 and tail[0]=='--validate-backend-path':
            if Path(tail[1])!=root/'backends':raise ValueError('Wrong backend output')
            harness.validate_new_directory(root/'backends',private_parent=True);return 0
        if len(tail)==2 and tail[0]=='--record-backends' and Path(tail[1])==root/'backends':
            return record_lane(candidate,root,ctx)
        raise ValueError('Unknown controller helper')
    if argv[:2]==['-m','tests.integration.source_runtime_image_gate']:
        os.execve(str(candidate_python(candidate)),[str(candidate_python(candidate)),*argv],workload_environment())
    if argv[:1]==['-c']:
        if len(argv)==2 and argv[1] in ('import sys; print(sys.base_prefix)',
                'import sysconfig; print(sysconfig.get_path("purelib"))'):
            print(python_layout(candidate,argv[1]));return 0
        if argv!=['-c','import secrets; print(secrets.token_hex(8))']:raise ValueError('Unknown inline helper')
        import secrets
        print(secrets.token_hex(8));return 0
    if argv[:1]==['-']:
        code=sys.stdin.read(65537)
        check_inline_source(code);sys.argv=argv
        exec(compile(code,'<trusted-shell-helper>','exec'),{'__name__':'__main__'})
        return 0
    if not argv:raise ValueError('Missing trusted helper')
    mapping={'scripts/cube_runtime_secrets.py':'cube_runtime_secrets',
        'scripts/source_runtime_secrets.py':'source_runtime_secrets',
        'scripts/cube_runtime_diagnostics.py':'cube_runtime_diagnostics',
        'scripts/source_runtime_gate.py':'source_runtime_gate',
        'scripts/wait_registry_postgres.py':'wait_registry_postgres'}
    if argv[0] not in mapping:raise ValueError('Unknown trusted helper')
    module=trusted_module(mapping[argv[0]])
    if hasattr(module,'ROOT'):module.ROOT=candidate
    sys.argv=argv
    if argv[0].endswith(('cube_runtime_secrets.py','source_runtime_secrets.py')):
        parser=argparse.ArgumentParser();parser.add_argument('--secret-dir',type=Path,required=True)
        parsed=parser.parse_args(argv[1:]);module.prepare(parsed.secret_dir);return 0
    if argv[0].endswith('cube_runtime_diagnostics.py'):
        parser=argparse.ArgumentParser();parser.add_argument('--secret-dir',type=Path,required=True)
        parser.add_argument('--project',required=True);parser.add_argument('--run-label',required=True)
        parsed=parser.parse_args(argv[1:]);module.diagnose(parsed.secret_dir,parsed.project,parsed.run_label);return 0
    return module.main()


def record_lane(candidate,root,ctx):
    runtime,verify,harness=modules(candidate)
    target=root/'backends';harness.validate_new_directory(target,private_parent=True)
    manifest=read_private(root/'manifest.json',192*1024)
    contract().validate_manifest(manifest,source=source_from(ctx['identity']),
        controller_digest=ctx['identity']['controller_digest'],expected_sha256=ctx['manifest_sha256'])
    nodes=tuple(manifest['partitions'][ctx['job_id']])
    images=verify.prerequisite_images(None);endpoints=verify.validate_backend_endpoints(images)
    target.mkdir(mode=0o700);output=target/'backends.xml'
    row=phase('backend_tests',[str(candidate_python(candidate)),'-m','pytest','-q',
        '--junitxml='+str(output),'--',*nodes],target,candidate,BACKEND_SECONDS,ctx['started'])
    summary=contract().junit_summary(private_bytes(output,MAX_LOG_BYTES),nodes)
    if context(candidate,job_id=ctx['job_id'])!=ctx['identity'] or verify.validate_backend_endpoints(images)!=endpoints:
        raise ValueError('Backend source/endpoint changed')
    summary.update(images_sha256=digest(images),endpoints_sha256=digest(endpoints))
    write_private(target/'lane-evidence.json',{'phase':row,'test_summary':summary})
    return 0


def manifest_command(candidate,output):
    candidate=require_separate_roots(candidate)
    source=source_identity(candidate,os.environ['REVIEWED_SOURCE_SHA'])
    root=checked_directory(Path(output).parent)
    if (stat.S_IMODE(root.stat().st_mode)!=0o700 or root.stat().st_uid!=os.getuid()
            or root==candidate or candidate in root.parents
            or root==TRUSTED_ROOT or TRUSTED_ROOT in root.parents):
        raise ValueError('Private external manifest directory required')
    git_identity(TRUSTED_ROOT,os.environ['REVIEWED_CONTROLLER_SHA'])
    controller=controller_digest()
    # Regeneration is bounded collection only, not source/backend acceptance.
    inventory,_=collect(candidate,root,'backend_collection','backend',int(time.time()))
    if source_identity(candidate,os.environ['REVIEWED_SOURCE_SHA'])!=source or controller_digest()!=controller:
        raise ValueError('Source/controller changed during collection')
    git_identity(TRUSTED_ROOT,os.environ['REVIEWED_CONTROLLER_SHA'])
    result=contract().build_manifest(source,controller,inventory)
    write_private(output,result)
    print(digest(result));return 0


def aggregate_command():
    # The aggregate never imports/executes candidate code or needs candidate deps.
    check_environment_budget(os.environ)
    c=contract();manifest=input_transport('SOURCE_MANIFEST','manifest')
    env=os.environ
    controller=controller_digest()
    # The reviewed digest and fixed schema authorize source metadata. A receipt
    # is never the source of expected run identity, even before validation.
    if type(manifest) is not dict:raise ValueError('Manifest object required')
    c.validate_manifest(manifest,source=manifest.get('source'),controller_digest=controller,
        expected_sha256=env['REVIEWED_MANIFEST_SHA256'])
    identity={'repository':env['GITHUB_REPOSITORY'],'workflow_file':WORKFLOW,
        'workflow_ref':env['GITHUB_WORKFLOW_REF'],'workflow_sha':env['GITHUB_WORKFLOW_SHA'],
        'controller_digest':controller,'run_id':env['GITHUB_RUN_ID'],
        'run_attempt':env['GITHUB_RUN_ATTEMPT'],'source_commit':env['REVIEWED_SOURCE_SHA'],
        'source_tree':manifest['source']['tree'],'source_digest':manifest['source']['digest']}
    if identity['source_commit']!=manifest['source']['commit']:raise ValueError('Foreign manifest source')
    git_identity(TRUSTED_ROOT,identity['workflow_sha'])
    source=input_transport('SOURCE_RECEIPT','receipt')
    lanes=[input_transport(lane.upper()+'_RECEIPT','receipt') for lane in c.LANES]
    results={job:env.get(job.upper()+'_RESULT','') for job in ('source',*c.LANES)}
    report=c.aggregate(manifest,source,lanes,results,identity=identity,
        expected_manifest_sha256=env['REVIEWED_MANIFEST_SHA256'])
    print(json.dumps(report,sort_keys=True))
    return 0 if report['status']=='PASS' else 1


def parse_args(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    modes=parser.add_subparsers(dest='mode',required=True)
    for name in ('run','cleanup','cleanup-child','finalize','helper'):
        sub=modes.add_parser(name);sub.add_argument('--candidate',type=Path,required=True)
        sub.add_argument('--root',type=Path,required=True)
        if name=='run':sub.add_argument('--job',required=True)
        if name=='helper':sub.add_argument('helper_args',nargs=argparse.REMAINDER)
    sub=modes.add_parser('admit-lane');sub.add_argument('--candidate',type=Path,required=True);sub.add_argument('--job',required=True)
    sub=modes.add_parser('workload-collect');sub.add_argument('--candidate',type=Path,required=True)
    sub.add_argument('--output',type=Path,required=True);sub.add_argument('--selection',choices=('source','backend'),required=True)
    sub=modes.add_parser('manifest');sub.add_argument('--candidate',type=Path,required=True);sub.add_argument('--output',type=Path,required=True)
    modes.add_parser('aggregate');modes.add_parser('controller-check')
    return parser.parse_args(argv)


def main(argv=None):
    os.umask(0o077);args=parse_args(argv)
    if args.mode=='workload-collect':return workload_collect(args)
    if not sys.flags.isolated or not sys.flags.no_site:
        raise ValueError('Controllers require isolated no-site Python')
    if args.mode=='controller-check':
        trusted_module('runtime_process');print('trusted-controller-ok');return 0
    if args.mode=='admit-lane':return admit_lane(args.candidate,args.job)
    if args.mode=='run':
        def expired(*_):raise ValueError('Fixed work deadline reached')
        signal.signal(signal.SIGALRM,expired)
        signal.setitimer(signal.ITIMER_REAL,work_remaining(int(os.environ['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH'])))
        try:return run_work(args)
        finally:signal.setitimer(signal.ITIMER_REAL,0)
    if args.mode=='cleanup':return cleanup(args.candidate,args.root)
    if args.mode=='cleanup-child':return cleanup_child(args.candidate,args.root)
    if args.mode=='finalize':
        started=read_private(args.root/'context.json')['started']
        def expired(*_):raise ValueError('Fixed finalization deadline reached')
        signal.signal(signal.SIGALRM,expired)
        signal.setitimer(signal.ITIMER_REAL,finalization_remaining(started))
        try:return finalize(args.candidate,args.root)
        finally:signal.setitimer(signal.ITIMER_REAL,0)
    if args.mode=='helper':
        tail=args.helper_args
        if tail[:1]==['--']:tail=tail[1:]
        return helper(args.candidate,args.root,tail)
    if args.mode=='manifest':return manifest_command(args.candidate,args.output)
    if args.mode=='aggregate':return aggregate_command()
    raise ValueError('Unknown mode')


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,KeyError,TypeError,OSError,RuntimeError,RecursionError,subprocess.SubprocessError):
        print(BLOCKED,file=sys.stderr);raise SystemExit(1)
