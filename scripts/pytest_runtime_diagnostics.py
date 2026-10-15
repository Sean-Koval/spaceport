#!/usr/bin/env python3
"""Explicit diagnostic-only pytest run with bounded, filtered log output.

Known run credentials come only from explicit private files. URL/JWT/token
patterns add defense in depth; they are not proof of universal secret removal.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys

LINE_BYTES = 8192
OUTPUT_BYTES = 262144
PREFIX = "pytest diagnostic: "
_failure_reported = False
_structured_evidence = False
_collected_tests = set()
_progress_evidence = False
_progress_collected = 0
_progress_completed = 0
_source_blobs = None
_source_commit = None
ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PREFIX = 'GATEWAY_FAILURE_EVIDENCE '
PHASE_PREFIX = 'GATEWAY_PHASE_FAILURE '
PROGRESS_PREFIX = 'GATEWAY_PRIVATE_PYTEST_PROGRESS '
MAX_PROGRESS_LINES = 10000
MAX_PROGRESS_BYTES = 4 * 1024 * 1024
CHECKPOINTS = frozenset('''source-runtime-wrapper source-runtime-control source-runtime-child
    source-config source-startup source-main source-revocation source-reclaim source-zero source-unknown source-cleanup
    source-journey-image source-image-build source-image-smoke source-gateway-image source-gateway-cli source-gateway-import source-create-secrets source-fixture-bootstrap cube-bigquery-serving cube-preflight cube-register-cleanup cube-create-secrets cube-images
    cube-secret-readability cube-probe-image cube-stack-readiness cube-role-initializers
    cube-runtime-versions cube-topology cube-node-security cube-auth-security cube-bigquery-package
    cube-bigquery-module-resolution cube-bigquery-package-versions cube-bigquery-ledger
    cube-bigquery-driver-construction cube-bigquery-query cube-bigquery-serialization
    cube-bigquery-result-hydration cube-bigquery-replay cube-bigquery-io-guard cube-bigquery-cleanup cube-egress
    cube-probe-tests cube-junit-admission cube-restart cube-restart-tests registry-gate
    cube-evidence-admission registry-preflight registry-register-cleanup registry-create-secret
    registry-image registry-start registry-readiness registry-identity registry-tests
    registry-junit-admission registry-acceptance backend-preflight backend-collection
    backend-tests backend-junit-admission backend-source-recheck backend-endpoint-recheck
    backend-receipt-write backend-report-admission'''.split())
PHASE_REASONS = frozenset(('process_exit', 'timeout', 'output_limit', 'interrupted',
    'orphaned_descendants', 'unverified_cleanup', 'phase_budget', 'supervision_exception',
    'work_interrupted', 'unavailable'))
EXCEPTIONS = frozenset(('AssertionError', 'RuntimeError', 'ValueError', 'TypeError', 'KeyError',
    'TimeoutError', 'ConnectionError', 'OperationalError', 'ProgrammingError', 'IntegrityError',
    'PermissionError', 'FileNotFoundError', 'CalledProcessError', 'Failed', 'other'))


def evidence_pytest_command(command, *, progress=False):
    if command[:3] == [sys.executable, '-m', 'pytest']:
        return [*command, '-p', 'scripts.pytest_runtime_diagnostics', '--gateway-failure-evidence',
            *(['--gateway-progress-evidence'] if progress else [])]
    return command


def freeze_source(commit=None, *, manifest=None):
    """Freeze committed blob identities before work; probe receives this privately."""
    global _source_blobs, _source_commit
    if manifest is not None:
        path = Path(manifest)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1024 * 1024): raise ValueError()
            value = json.loads(stream.read(1024 * 1024 + 1))
        if (type(value) is not dict or set(value) != {'commit', 'blobs'}
            or not isinstance(value['commit'], str) or not re.fullmatch('[a-f0-9]{40}', value['commit'])
            or type(value['blobs']) is not dict or not 1 <= len(value['blobs']) <= 2000): raise ValueError()
        for name, blob in value['blobs'].items():
            if (not re.fullmatch(r'(tests|scripts|gateway)/[A-Za-z0-9_/]+\.py', name)
                or not isinstance(blob, str) or not re.fullmatch('[a-f0-9]{40}', blob)): raise ValueError()
        _source_commit, _source_blobs = value['commit'], value['blobs']
    elif _source_blobs is None or commit is not None and commit != _source_commit:
        if commit is None:
            commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5).stdout.decode().strip()
        if not re.fullmatch('[a-f0-9]{40}', commit): raise ValueError()
        result = subprocess.run(['git', '-C', str(ROOT), 'ls-tree', '-r', '-z', commit, '--', 'tests', 'scripts', 'gateway'],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        if len(result.stdout) > 1024 * 1024: raise ValueError()
        blobs = {}
        for entry in result.stdout.split(b'\0'):
            if not entry: continue
            metadata, name = entry.decode('ascii').split('\t', 1)
            mode, kind, blob = metadata.split()
            if kind == 'blob' and mode in {'100644', '100755'} and re.fullmatch(r'(tests|scripts|gateway)/[A-Za-z0-9_/]+\.py', name):
                blobs[name] = blob
        if not 1 <= len(blobs) <= 2000: raise ValueError()
        _source_commit, _source_blobs = commit, blobs
    return {'commit': _source_commit, 'blobs': _source_blobs}


def source_file(name):
    """Admit source coordinates only; never echo a runtime path or source text."""
    if not isinstance(name, str) or len(name) > 512 or not re.fullmatch(r'(tests|scripts|gateway)/[A-Za-z0-9_/]+\.py', name):
        raise ValueError()
    freeze_source()
    if name not in _source_blobs: raise ValueError()
    path = ROOT / name
    if any(part.is_symlink() for part in (path, *path.parents)): raise ValueError()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024: raise ValueError()
        raw = stream.read(1024 * 1024 + 1)
    blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    if len(raw) > 1024 * 1024 or blob != _source_blobs[name]: raise ValueError()
    return raw.decode('utf-8')


def source_test_identity(node):
    try:
        address = node.partition('[')[0]
        parts = address.split('::')
        if not parts[0].startswith('tests/') or len(parts) < 2 or not parts[-1].startswith('test_'):
            raise ValueError()
        scope = ast.parse(source_file(parts[0])).body
        for index, name in enumerate(parts[1:]):
            expected = (ast.FunctionDef, ast.AsyncFunctionDef) if index == len(parts)-2 else (ast.ClassDef,)
            declaration = next(item for item in scope if isinstance(item, expected) and item.name == name)
            scope = declaration.body
        return address
    except (OSError, ValueError, TypeError, AttributeError, StopIteration, SyntaxError, subprocess.SubprocessError):
        return None


def validate_failure_marker(value):
    if type(value) is not dict: raise ValueError()
    if value.get('kind') == 'checkpoint':
        if (set(value) != {'kind', 'checkpoint', 'exit_status'} or value['checkpoint'] not in CHECKPOINTS
            or type(value['exit_status']) is not int or not 1 <= value['exit_status'] <= 255):
            raise ValueError()
    elif value.get('kind') == 'phase':
        if (set(value) != {'kind', 'phase', 'reason'} or value['phase'] != 'backends'
            or value['reason'] not in PHASE_REASONS):
            raise ValueError()
    elif value.get('kind') == 'progress':
        if (set(value) != {'kind', 'phase', 'test', 'stage', 'collected', 'completed'}
            or value['phase'] != 'backends' or value['test'] is None
            or source_test_identity(value['test']) != value['test']
            or value['stage'] not in {'setup', 'call', 'teardown', 'complete'}
            or type(value['collected']) is not int or not 1 <= value['collected'] <= 1000000
            or type(value['completed']) is not int or not 0 <= value['completed'] <= value['collected']
            or (value['stage'] == 'complete' and value['completed'] == 0)
            or (value['stage'] != 'complete' and value['completed'] == value['collected'])):
            raise ValueError()
    elif value.get('kind') == 'test':
        if (set(value) != {'kind', 'test', 'stage', 'exception', 'trace'}
            or source_test_identity(value['test']) != value['test'] or value['test'] is None
            or value['stage'] not in {'setup', 'call', 'teardown'} or value['exception'] not in EXCEPTIONS
            or type(value['trace']) is not list or len(value['trace']) > 4):
            raise ValueError()
        for frame in value['trace']:
            if (type(frame) is not dict or set(frame) != {'file', 'line'} or type(frame['line']) is not int
                or not 1 <= frame['line'] <= len(source_file(frame['file']).splitlines())):
                raise ValueError()
    else: raise ValueError()
    return value


def unique_marker_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ValueError()
        value[key] = item
    return value


def phase_failure_evidence(path, result, *, progress=False):
    """Select only enum/source-bound markers. Redaction is not a log-export license."""
    reason = next((name for key, name in (('timed_out', 'timeout'), ('output_limit_exceeded', 'output_limit'),
        ('interrupted', 'interrupted'), ('orphaned_descendants', 'orphaned_descendants')) if result.get(key) is True), 'process_exit')
    if result.get('descendant_cleanup') is not True: reason = 'unverified_cleanup'
    evidence = {'reason': reason, 'details_status': 'unavailable', 'markers': []}
    if reason == 'unverified_cleanup': return evidence
    try:
        path = Path(path)
        if any(part.is_symlink() for part in (path, *path.parents)): raise ValueError()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16 * 1024 * 1024): raise ValueError()
            consumed = 0
            encountered = 0
            progress_lines = progress_bytes = 0
            final_progress = None
            seen = set()
            while raw := stream.readline(LINE_BYTES + 1):
                consumed += len(raw)
                if consumed > 16 * 1024 * 1024: raise ValueError()
                if len(raw) > LINE_BYTES:
                    if (raw.startswith(EVIDENCE_PREFIX.encode())
                        or progress and raw.startswith(PROGRESS_PREFIX.encode())): raise ValueError()
                    while raw and not raw.endswith(b'\n'):
                        raw = stream.readline(LINE_BYTES + 1); consumed += len(raw)
                        if consumed > 16 * 1024 * 1024: raise ValueError()
                    continue
                if progress and raw.startswith(PROGRESS_PREFIX.encode()):
                    progress_lines += 1; progress_bytes += len(raw)
                    if progress_lines > MAX_PROGRESS_LINES or progress_bytes > MAX_PROGRESS_BYTES: raise ValueError()
                    final_progress = raw[len(PROGRESS_PREFIX):]
                    continue
                if not raw.startswith(EVIDENCE_PREFIX.encode()): continue
                encountered += 1
                if encountered > 32: raise ValueError()
                if raw in seen: continue
                seen.add(raw)
                marker = validate_failure_marker(json.loads(raw[len(EVIDENCE_PREFIX):], object_pairs_hook=unique_marker_object))
                if marker not in evidence['markers']:
                    if len(evidence['markers']) >= 8: raise ValueError()
                    evidence['markers'].append(marker)
            if final_progress is not None:
                # Earlier snapshots remain private. Source binding is checked
                # once here, rather than rereading/AST-parsing on every event.
                marker = validate_failure_marker(json.loads(final_progress, object_pairs_hook=unique_marker_object))
                if marker['kind'] != 'progress': raise ValueError()
                if marker not in evidence['markers']:
                    if len(evidence['markers']) >= 8: raise ValueError()
                    evidence['markers'].append(marker)
        if evidence['markers']: evidence['details_status'] = 'available'
    except FileNotFoundError: pass
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, subprocess.SubprocessError):
        evidence.update(details_status='invalid', markers=[])
    return evidence


def validate_phase_failure_evidence(value):
    reasons = PHASE_REASONS
    reason = value.get('reason') if type(value) is dict else None
    fallback = {'reason': reason if isinstance(reason, str) and reason in reasons else 'unavailable',
        'details_status': 'invalid', 'markers': []}
    try:
        if (type(value) is not dict or set(value) != {'reason', 'details_status', 'markers'} or reason not in reasons
            or value['details_status'] not in {'available', 'unavailable', 'invalid'}
            or type(value['markers']) is not list or len(value['markers']) > 8
            or bool(value['markers']) != (value['details_status'] == 'available')): raise ValueError()
        return {'reason': reason, 'details_status': value['details_status'],
            'markers': [validate_failure_marker(marker) for marker in value['markers']]}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError): return fallback


def emit_phase_failure(value, *, phase=None):
    value = validate_phase_failure_evidence(value)
    print(PHASE_PREFIX + json.dumps(value, sort_keys=True), file=sys.stderr, flush=True)
    # Nested phase/suite logs stay private. Re-emit selected, validated markers
    # so the supervising outer phase can retain them before runtime cleanup.
    for marker in value['markers']:
        print(EVIDENCE_PREFIX + json.dumps(marker, sort_keys=True), file=sys.stderr, flush=True)
    # The enclosing process can exit normally with code 1 after its own child
    # times out. Preserve that child's fixed classification without promoting it
    # to the enclosing process's reason or copying commands/private diagnostics.
    if phase == 'backends':
        marker = {'kind': 'phase', 'phase': phase, 'reason': value['reason']}
        print(EVIDENCE_PREFIX + json.dumps(validate_failure_marker(marker), sort_keys=True), file=sys.stderr, flush=True)


def pytest_addoption(parser):
    parser.addoption('--gateway-failure-evidence', action='store_true', default=False)
    parser.addoption('--gateway-failure-source', default=None)
    parser.addoption('--gateway-progress-evidence', action='store_true', default=False)


def pytest_configure(config):
    global _structured_evidence, _failure_reported, _collected_tests
    global _progress_evidence, _progress_collected, _progress_completed
    _structured_evidence = config.getoption('--gateway-failure-evidence')
    _failure_reported = False
    _collected_tests = set()
    _progress_evidence = _structured_evidence and config.getoption('--gateway-progress-evidence')
    _progress_collected = _progress_completed = 0
    if _structured_evidence:
        try: freeze_source(manifest=config.getoption('--gateway-failure-source'))
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError, subprocess.SubprocessError):
            global _source_blobs
            _source_blobs = {}


def pytest_collection_modifyitems(items):
    if _structured_evidence:
        _collected_tests.update(filter(None, (source_test_identity(item.nodeid) for item in items)))


def pytest_collection_finish(session):
    global _progress_collected
    # Parameterized cases count individually, after final marker selection.
    _progress_collected = len(session.items)


def private_progress(nodeid, stage):
    if not _progress_evidence: return
    identity = nodeid.partition('[')[0]
    value = {'kind': 'progress', 'phase': 'backends',
        'test': identity if identity in _collected_tests else None, 'stage': stage,
        'collected': _progress_collected, 'completed': _progress_completed}
    print('\n' + PROGRESS_PREFIX + json.dumps(value, sort_keys=True), flush=True)


def pytest_runtest_logstart(nodeid, location):
    private_progress(nodeid, 'setup')


def structured_test_failure(report):
    test = source_test_identity(report.nodeid)
    if test not in _collected_tests: return None
    trace = []
    representation = report.longrepr
    for entry in getattr(getattr(representation, 'reprtraceback', None), 'reprentries', ()):
        location = getattr(entry, 'reprfileloc', None)
        try:
            path = Path(location.path)
            name = path.relative_to(ROOT).as_posix() if path.is_absolute() else path.as_posix()
            if type(location.lineno) is int and 1 <= location.lineno <= len(source_file(name).splitlines()):
                trace.append({'file': name, 'line': location.lineno})
        except (OSError, ValueError, AttributeError): pass
    message = getattr(getattr(representation, 'reprcrash', None), 'message', '')
    exception = next((name for name in EXCEPTIONS if re.fullmatch(
        r'(?:[A-Za-z_]\w*\.)*' + name + r'(?::.*)?', message, re.S)), 'other')
    if exception == 'other' and message.startswith('assert '): exception = 'AssertionError'
    return {'kind': 'test', 'test': test, 'stage': report.when, 'exception': exception, 'trace': trace[-4:]}


def pytest_runtest_logreport(report):
    """Flush the first failure before pytest enters possibly slow teardown.

    Loaded only by the diagnostic child; stdout still passes through the
    parent's redaction, line limit, and total output budget.
    """
    global _failure_reported, _progress_completed
    if report.failed and not _failure_reported:
        _failure_reported = True
        if _structured_evidence:
            value = structured_test_failure(report)
            if value is not None:
                print('\n' + EVIDENCE_PREFIX + json.dumps(validate_failure_marker(value), sort_keys=True), flush=True)
        else:
            print(f"\nFIRST FAILURE ({report.when}): {report.nodeid}\n{report.longreprtext}", flush=True)
    if _progress_evidence and report.when in {'setup', 'call', 'teardown'}:
        if report.when == 'teardown': _progress_completed += 1
        stage = {'setup': 'call', 'call': 'teardown', 'teardown': 'complete'}[report.when]
        if report.when == 'setup' and not report.passed: stage = 'teardown'
        private_progress(report.nodeid, stage)


def read_secrets(paths):
    if not 1 <= len(paths) <= 20:
        raise ValueError("Invalid explicit diagnostic secret inputs")
    values = []
    for name in paths:
        path = Path(name)
        parent = path.parent.lstat()
        if (not path.is_absolute() or not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700 or parent.st_uid != os.getuid()):
            raise ValueError("Invalid explicit diagnostic secret inputs")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) not in {0o600, 0o444} or info.st_size not in {43, 44}):
                raise ValueError("Invalid explicit diagnostic secret inputs")
            value = stream.read(45).decode("ascii")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}\n?", value):
            raise ValueError("Invalid explicit diagnostic secret inputs")
        values.append(value.rstrip("\n"))
    return tuple(values)


def stream_output(stream, secrets, output):
    emitted = 0
    suppressed = False
    while raw := stream.readline(LINE_BYTES + 1):
        if len(raw) > LINE_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(LINE_BYTES + 1)
            text = "[oversized line omitted]"
        else:
            text = raw.decode("utf-8", errors="replace").rstrip("\n")
            for value in secrets:
                text = text.replace(value, "[REDACTED]")
            text = re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
            text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED JWT]", text)
            text = re.sub(r"(?<![A-Za-z0-9_:/.-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", "[REDACTED TOKEN]", text)
            text = "".join(c if c == "\t" or c.isprintable() else "?" for c in text)
        line = PREFIX + text + "\n"
        size = len(line.encode("utf-8"))
        if emitted + size <= OUTPUT_BYTES:
            output.write(line)
            output.flush()
            emitted += size
        elif not suppressed:
            output.write(PREFIX + "[remaining output omitted: byte limit exceeded]\n")
            output.flush()
            suppressed = True


def run_pytest(secrets):
    print("DIAGNOSTIC ONLY: first-failure limit; this is not full acceptance.", flush=True)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(Path(__file__).resolve().parent), environment.get("PYTHONPATH"))))
    process = subprocess.Popen([sys.executable, "-m", "pytest", "-vv", "--maxfail=1",
        "--tb=short", "--no-showlocals", "-p", "pytest_runtime_diagnostics"], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment)
    received = []
    def forward(signum, frame):
        received.append(signum)
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            pass
    previous = {signum: signal.signal(signum, forward) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        stream_output(process.stdout, secrets, sys.stdout)
        result = process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    result = -received[0] if received else result
    print("DIAGNOSTIC ONLY: pytest finished; full acceptance must run separately.", flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret-file", action="append", required=True)
    args = parser.parse_args()
    try:
        secrets = read_secrets(args.secret_file)
    except (OSError, ValueError, UnicodeError):
        print("Diagnostic run blocked: explicit private secret inputs were not verified", file=sys.stderr)
        raise SystemExit(2)
    result = run_pytest(secrets)
    if result < 0:
        if -result not in {signal.SIGKILL, signal.SIGSTOP}:
            signal.signal(-result, signal.SIG_DFL)
        os.kill(os.getpid(), -result)
    raise SystemExit(result)
