"""Serial, fail-closed migration setup worker for one disposable test cluster.

Only Python imports survive requests. Every request still migrates a fresh
database twice as its owner, using two new configs and disposed NullPool
connections, and returns both real identity snapshots. No migration result,
transaction, connection, database or role is cached for another request.
"""
from contextlib import redirect_stderr, redirect_stdout, suppress
import ctypes
import json
import os
import re
import secrets
import selectors
import signal
import subprocess
import sys
import time


def _bind_parent(expected_parent):
    """Kernel-enforced worker death even while blocked in a native DB call.

    Migrations in this worker create no subprocesses. This binding kills this
    process only; ordinary cleanup and the phase's existing pidfd/subreaper
    supervisor retain responsibility for any unexpected descendants.
    """
    if sys.platform != 'linux' or type(expected_parent) is not int or expected_parent <= 1:
        raise RuntimeError('Disposable worker requires Linux parent-death supervision')
    libc = ctypes.CDLL(None, use_errno=True)
    admitted = ctypes.c_int()
    if (libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0  # PR_SET_PDEATHSIG
        or libc.prctl(2, ctypes.byref(admitted), 0, 0, 0) != 0  # PR_GET_PDEATHSIG
        or admitted.value != signal.SIGKILL or os.getppid() != expected_parent):
        raise RuntimeError('Disposable worker parent supervision unavailable')


if __name__ == '__main__':
    # Arm before heavy imports or accepting any credential-bearing descriptor.
    try:
        _bind_parent(int(os.environ['GATEWAY_FIXTURE_PARENT_PID']))
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
    except Exception:
        raise SystemExit(1) from None

from sqlalchemy.engine import make_url
import pytest

from tests.integration.registry_migration_pair import (
    MAX_RECEIPT_BYTES, ROLE_KEYS, ROOT, TARGETS, collect_owned_pair,
    parse_receipt, unique_object,
)

AUTHORIZATION = 'disposable-local-postgres-only'
MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = MAX_RECEIPT_BYTES + 1024
PAIR_TIMEOUT = 60
CLEANUP_RESERVE = 1.0
# Shared across integration conftest imports and standalone security fixtures.
SESSION_WORKER_KEY = pytest.StashKey()


class MigrationWorkerError(RuntimeError):
    """Safe failure with no child bytes or descriptor details."""


def _object(wire, maximum):
    if type(wire) is not str or len(wire.encode('utf-8')) > maximum:
        raise ValueError()
    value = json.loads(wire, object_pairs_hook=unique_object)
    if type(value) is not dict:
        raise ValueError()
    return value


def _endpoint(host, port):
    if (type(host) is not str or host not in {'127.0.0.1', 'localhost'}
        or type(port) is not int or not 1 <= port <= 65535):
        raise ValueError()
    return host, port


def parse_request(wire, endpoint, sequence):
    """Validate closed, bounded private input before any database connection."""
    try:
        value = _object(wire, MAX_REQUEST_BYTES)
        if (set(value) != {'schema_version', 'sequence', 'nonce', 'database_url', 'roles', 'target'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['sequence']) is not int or value['sequence'] != sequence
            or sequence < 1 or type(value['nonce']) is not str
            or not re.fullmatch('[a-f0-9]{32}', value['nonce'])
            or type(value['database_url']) is not str or type(value['target']) is not str
            or value['target'] not in TARGETS or type(value['roles']) is not dict
            or set(value['roles']) != set(ROLE_KEYS)):
            raise ValueError()
        url = make_url(value['database_url'])
        token = re.fullmatch('gateway_test_([a-f0-9]{16})', url.database or '')
        if (url.drivername != 'postgresql+psycopg' or (url.host, url.port) != endpoint
            or not url.password or url.query or not token):
            raise ValueError()
        roles = {key: 'gwt_' + token[1] + '_' + key for key in ROLE_KEYS}
        if url.username != roles['owner'] or value['roles'] != roles:
            raise ValueError()
        return url, roles, value['target']
    except Exception:
        raise ValueError('Invalid disposable migration request') from None


def parse_response(wire, url, target, sequence, nonce):
    try:
        value = _object(wire, MAX_RESPONSE_BYTES)
        if (set(value) != {'schema_version', 'sequence', 'nonce', 'pair'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['sequence']) is not int or value['sequence'] != sequence
            or type(value['nonce']) is not str or value['nonce'] != nonce):
            raise ValueError()
        return parse_receipt(json.dumps(value['pair']), url, target)
    except Exception:
        raise ValueError('Invalid session migration evidence') from None


def _line(stream, maximum):
    wire = stream.readline(maximum + 1)
    if not wire:
        return None
    if not wire.endswith('\n') or len(wire.encode('utf-8')) > maximum:
        raise ValueError()
    return wire


def _perform(wire, endpoint, sequence):
    url, roles, target = parse_request(wire, endpoint, sequence)
    request = _object(wire, MAX_REQUEST_BYTES)
    # Do not retain library diagnostics, which might contain a connection URL.
    signal.setitimer(signal.ITIMER_REAL, PAIR_TIMEOUT)
    try:
        with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
            pair = collect_owned_pair(url, roles, target)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    response = json.dumps(dict(schema_version=1, sequence=sequence, nonce=request['nonce'], pair=pair))
    parse_response(response, url, target, sequence, request['nonce'])
    return response


def serve(source, destination):
    try:
        initial = _line(source, MAX_REQUEST_BYTES)
        if initial is None:
            return 0
        value = _object(initial, MAX_REQUEST_BYTES)
        if (set(value) != {'schema_version', 'authorization', 'host', 'port'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['authorization'] != AUTHORIZATION):
            raise ValueError()
        endpoint = _endpoint(value['host'], value['port'])
        sequence = 0
        while True:
            wire = _line(source, MAX_REQUEST_BYTES)
            if wire is None:
                return 0
            sequence += 1
            response = _perform(wire, endpoint, sequence)
            del wire
            destination.write(response + '\n')
            destination.flush()
            del response
    except Exception:
        # No exception strings, descriptors, partial receipts, or retries.
        return 1


class MigrationWorker:
    """One serial session owner; any ambiguous outcome permanently poisons it."""

    def __init__(self, host, port, *, timeout=PAIR_TIMEOUT):
        self._endpoint = _endpoint(host, port)
        if not 0 < timeout <= PAIR_TIMEOUT:
            raise ValueError('Invalid migration worker timeout')
        self._timeout = timeout
        self._process = None
        self._closed = False
        self._sequence = 0

    def _start(self):
        self._process = subprocess.Popen(
            [sys.executable, '-m', 'tests.integration.registry_migration_worker'], cwd=ROOT,
            env={'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                'GATEWAY_FIXTURE_PARENT_PID': str(os.getpid())},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, bufsize=0,
        )
        os.set_blocking(self._process.stdin.fileno(), False)
        os.set_blocking(self._process.stdout.fileno(), False)

    def _exchange(self, outgoing, deadline):
        process = self._process
        if process.poll() is not None:
            raise ValueError()
        # Reject unsolicited or stale bytes before sending another descriptor.
        try:
            os.read(process.stdout.fileno(), 1)
        except BlockingIOError:
            pass
        else:
            raise ValueError()
        written = 0
        incoming = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or process.poll() is not None:
                    raise ValueError()
                for key, _ in selector.select(min(remaining, 0.05)):
                    if key.fileobj is process.stdin:
                        written += os.write(process.stdin.fileno(), outgoing[written:])
                        if written == len(outgoing):
                            selector.unregister(process.stdin)
                    else:
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            raise ValueError()
                        incoming.extend(chunk)
                        if len(incoming) > MAX_RESPONSE_BYTES:
                            raise ValueError()
                        if b'\n' in incoming:
                            if (incoming.count(b'\n') != 1 or not incoming.endswith(b'\n')
                                or written != len(outgoing) or process.poll() is not None):
                                raise ValueError()
                            try:
                                os.read(process.stdout.fileno(), 1)
                            except BlockingIOError:
                                return incoming.decode('utf-8')
                            raise ValueError()

    def run(self, url, roles, target='head'):
        if self._closed:
            raise MigrationWorkerError('Disposable migration worker is closed')
        deadline = time.monotonic() + self._timeout
        exchange_deadline = deadline - min(CLEANUP_RESERVE, self._timeout / 2)
        try:
            self._sequence += 1
            nonce = secrets.token_hex(16)
            request = json.dumps(dict(schema_version=1, sequence=self._sequence, nonce=nonce,
                database_url=url.render_as_string(hide_password=False), roles=roles, target=target)) + '\n'
            parse_request(request, self._endpoint, self._sequence)
            outgoing = request
            if self._process is None:
                self._start()
                outgoing = json.dumps(dict(schema_version=1, authorization=AUTHORIZATION,
                    host=self._endpoint[0], port=self._endpoint[1])) + '\n' + request
            response = self._exchange(outgoing.encode('utf-8'), exchange_deadline)
            value = parse_response(response, url, target, self._sequence, nonce)
            return value['first'], value['second']
        except BaseException as error:
            self._close(deadline, expect_clean=False)
            if not isinstance(error, Exception):
                raise
            raise MigrationWorkerError('Disposable owner migration worker failed') from None

    def close(self):
        self._close(time.monotonic() + CLEANUP_RESERVE, expect_clean=True)

    def _observe_eof(self, deadline):
        process = self._process
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                for _, _ in selector.select(max(0, deadline - time.monotonic())):
                    if os.read(process.stdout.fileno(), 1):
                        return False
                    try:
                        return process.wait(timeout=max(0, deadline - time.monotonic())) == 0
                    except subprocess.TimeoutExpired:
                        return False
        return False

    def _close(self, deadline, *, expect_clean):
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is None:
            return
        clean = False
        with suppress(OSError):
            process.stdin.close()
        if expect_clean:
            # Observe EOF, no late output, and exit zero before terminating any
            # remaining owned descendants. Leave half the budget for cleanup.
            clean = self._observe_eof(time.monotonic() + max(0, deadline - time.monotonic()) / 2)
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=min(0.1, max(0, deadline - time.monotonic()) / 2))
        except subprocess.TimeoutExpired:
            pass
        finally:
            # Kill the owned group even if its leader exited but descendants
            # still hold pipes. Never signal another process's group.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=max(0, deadline - time.monotonic()))
            process.stdout.close()
        if expect_clean and not clean:
            raise MigrationWorkerError('Disposable migration worker did not close cleanly')


if __name__ == '__main__':
    raise SystemExit(serve(sys.stdin, sys.stdout))
