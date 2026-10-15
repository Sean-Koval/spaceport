"""Bounded serial migration IPC controls; real backend acceptance stays separate."""
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from tests.unit.test_registry_migration_pair import ROLES, SNAPSHOT, URL_VALUE

pytestmark = [pytest.mark.unit, pytest.mark.contract]
NONCE = 'b' * 32


def process_running(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except (FileNotFoundError, ProcessLookupError):
        return False


def api():
    path = Path(__file__).resolve().parents[2] / 'tests/integration/registry_migration_worker.py'
    assert path.is_file(), 'Missing bounded session migration worker'
    return importlib.import_module('tests.integration.registry_migration_worker')


def descriptor(**changes):
    return dict(schema_version=1, sequence=1, nonce=NONCE,
        database_url=URL_VALUE.render_as_string(hide_password=False), roles=ROLES,
        target='head', **changes)


def response(**changes):
    value = dict(schema_version=1, sequence=1, nonce=NONCE,
        pair=dict(schema_version=1, database=URL_VALUE.database, owner=ROLES['owner'],
            target='head', first=SNAPSHOT, second=SNAPSHOT))
    value.update(changes)
    return value


def test_worker_request_binds_reviewed_target_and_four_roles_to_one_cluster():
    module = api()
    url, roles, target = module.parse_request(json.dumps(descriptor()), ('127.0.0.1', 54321), 1)
    assert url == URL_VALUE and roles == ROLES and target == 'head'


@pytest.mark.parametrize('change', ['endpoint', 'database', 'owner', 'roles', 'extra_role',
    'target', 'sequence', 'nonce', 'schema', 'extra_key', 'duplicate', 'oversized', 'partial'])
def test_worker_request_rejects_foreign_or_ambiguous_descriptors_before_connection(change):
    module = api(); value = descriptor()
    if change == 'endpoint': value['database_url'] = URL_VALUE.set(port=54322).render_as_string(hide_password=False)
    if change == 'database': value['database_url'] = URL_VALUE.set(database='gateway_test_admin').render_as_string(hide_password=False)
    if change == 'owner': value['database_url'] = URL_VALUE.set(username='gateway_test_admin').render_as_string(hide_password=False)
    if change == 'roles': value['roles'] = {**ROLES, 'app': 'gwt_' + 'c' * 16 + '_app'}
    if change == 'extra_role': value['roles'] = {**ROLES, 'admin': 'gateway_test_admin'}
    if change == 'target': value['target'] = '0002_audit_policy_id'
    if change == 'sequence': value['sequence'] = True
    if change == 'nonce': value['nonce'] = 'old'
    if change == 'schema': value['schema_version'] = True
    if change == 'extra_key': value['extra'] = 'PRIVATE_ERROR'
    wire = json.dumps(value)
    if change == 'duplicate': wire = wire.replace('"schema_version": 1', '"schema_version": 2, "schema_version": 1')
    if change == 'oversized': wire += ' ' * module.MAX_REQUEST_BYTES
    if change == 'partial': wire = wire[:-1]
    with pytest.raises(ValueError) as caught:
        module.parse_request(wire, ('127.0.0.1', 54321), 1)
    assert 'PRIVATE_' not in str(caught.value)


@pytest.mark.parametrize('change', ['sequence', 'nonce', 'schema', 'extra', 'duplicate', 'trailing', 'partial'])
def test_worker_receipt_rejects_stale_or_ambiguous_evidence(change):
    module = api(); value = response()
    if change == 'sequence': value['sequence'] = 0
    if change == 'nonce': value['nonce'] = 'c' * 32
    if change == 'schema': value['schema_version'] = True
    if change == 'extra': value['extra'] = 'PRIVATE_ERROR'
    wire = json.dumps(value)
    if change == 'duplicate': wire = wire.replace('"sequence": 1', '"sequence": 0, "sequence": 1')
    if change == 'trailing': wire += '\n' + wire
    if change == 'partial': wire = wire[:-1]
    with pytest.raises(ValueError): module.parse_response(wire, URL_VALUE, 'head', 1, NONCE)


def test_worker_eof_finishes_without_partial_receipt_or_private_errors(monkeypatch):
    module = api()
    init = json.dumps(dict(schema_version=1, authorization=module.AUTHORIZATION, host='127.0.0.1', port=54321))
    events = []
    def pair(url, roles, target):
        events.append((url, roles, target))
        print('PRIVATE_PASSWORD PRIVATE_ERROR')
        return response()['pair']
    monkeypatch.setattr(module, 'collect_owned_pair', pair)
    output = io.StringIO()
    assert module.serve(io.StringIO(init + '\n' + json.dumps(descriptor()) + '\n'), output) == 0
    assert json.loads(output.getvalue()) == response()
    assert events == [(URL_VALUE, ROLES, 'head')]


def test_worker_failed_request_stops_without_running_following_request(monkeypatch):
    module = api(); calls = []
    init = json.dumps(dict(schema_version=1, authorization=module.AUTHORIZATION, host='127.0.0.1', port=54321))
    def fail(*args): calls.append(True); raise RuntimeError('PRIVATE_PASSWORD PRIVATE_ERROR')
    monkeypatch.setattr(module, 'collect_owned_pair', fail)
    output = io.StringIO()
    assert module.serve(io.StringIO(init + '\n' + json.dumps(descriptor()) + '\n' + json.dumps(descriptor()) + '\n'), output) == 1
    assert calls == [True] and output.getvalue() == ''


CHILD = '''import json,sys,os,time
initial = json.loads(sys.stdin.readline())
for line in sys.stdin:
 request = json.loads(line)
 from urllib.parse import urlsplit
 url = urlsplit(request['database_url'])
 pair = dict(schema_version=1,database=url.path[1:],owner=url.username,target=request['target'],first=SNAPSHOT,second=SNAPSHOT)
 value = dict(schema_version=1,sequence=request['sequence'],nonce=request['nonce'],pair=pair)
 ACTION
'''.replace('SNAPSHOT', repr(SNAPSHOT))


def child_factory(monkeypatch, module, action):
    real_popen = subprocess.Popen; calls = []
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return real_popen([sys.executable, '-u', '-c', CHILD.replace('ACTION', action)], **kwargs)
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    return calls


def test_client_reuses_only_interpreter_and_keeps_credentials_off_argv_and_env(monkeypatch):
    module = api()
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_ADMIN_URL', 'PRIVATE_ADMIN')
    monkeypatch.setenv('GOOGLE_APPLICATION_CREDENTIALS', 'PRIVATE_PROVIDER')
    calls = child_factory(monkeypatch, module, "print(json.dumps(value),flush=True)")
    worker = module.MigrationWorker('127.0.0.1', 54321)
    try:
        first = worker.run(URL_VALUE, ROLES)
        other_roles = {key: role.replace('a' * 16, 'c' * 16) for key, role in ROLES.items()}
        other_url = URL_VALUE.set(database='gateway_test_' + 'c' * 16, username=other_roles['owner'])
        second = worker.run(other_url, other_roles, '0001_registry')
        assert first == second == (SNAPSHOT, SNAPSHOT)
        assert len(calls) == 1
        command, options = calls[0]
        assert command == [sys.executable, '-m', 'tests.integration.registry_migration_worker']
        assert options['start_new_session'] is True and options['stderr'] is subprocess.DEVNULL
        assert options['env'] == {'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1',
            'GATEWAY_FIXTURE_PARENT_PID': str(os.getpid())}
        assert 'PRIVATE_' not in repr(command) + repr(options)
    finally: worker.close()
    assert worker._process.poll() is not None


@pytest.mark.parametrize('action', [
    "value['sequence']-=1; print(json.dumps(value),flush=True)",
    "value['nonce']='c'*32; print(json.dumps(value),flush=True)",
    "print(json.dumps(value)+'\\n'+json.dumps(value),flush=True)",
    "print('PRIVATE_ERROR',flush=True)",
    "print('x'*300000,flush=True)",
    "sys.stdout.write(json.dumps(value)[:-1]); sys.stdout.flush(); time.sleep(5)",
    "sys.exit(1)",
    "time.sleep(5)",
])
def test_client_failure_poisons_worker_without_replay_or_private_output(monkeypatch, action):
    module = api(); calls = child_factory(monkeypatch, module, action)
    worker = module.MigrationWorker('127.0.0.1', 54321, timeout=0.3)
    for _ in range(2):
        with pytest.raises(module.MigrationWorkerError) as caught: worker.run(URL_VALUE, ROLES)
        assert 'PRIVATE_' not in str(caught.value)
    assert len(calls) == 1 and worker._process.poll() is not None


def test_client_cross_cluster_request_poisoned_before_any_process(monkeypatch):
    module = api(); calls = child_factory(monkeypatch, module, "raise AssertionError()")
    worker = module.MigrationWorker('127.0.0.1', 54322)
    with pytest.raises(module.MigrationWorkerError): worker.run(URL_VALUE, ROLES)
    assert calls == []


def test_child_exit_with_pipe_holding_descendant_is_detected_and_group_killed(monkeypatch, tmp_path):
    module = api(); pid_path = tmp_path / 'descendant.pid'
    action = ("child=os.fork(); "
        f"open({str(pid_path)!r},'w').write(str(child)) if child else None; "
        "time.sleep(10) if not child else sys.exit(1)")
    child_factory(monkeypatch, module, action)
    worker = module.MigrationWorker('127.0.0.1', 54321, timeout=3)
    started = time.monotonic()
    with pytest.raises(module.MigrationWorkerError): worker.run(URL_VALUE, ROLES)
    assert time.monotonic() - started < 2
    pid = int(pid_path.read_text())
    # A reparented zombie has stopped executing and holds no descriptors.
    deadline = time.monotonic() + 0.5
    while process_running(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not process_running(pid)


def test_normal_pair_explicitly_uses_session_worker_without_starting_legacy_child(monkeypatch):
    module = api(); fixture = importlib.import_module('tests.integration.conftest')
    calls = []
    class Worker:
        def run(self, url, roles, target): calls.append((url, roles, target)); return SNAPSHOT, SNAPSHOT
    monkeypatch.setattr(fixture.subprocess, 'run', lambda *a, **k: pytest.fail('Unexpected standalone migration'))
    assert fixture.run_migration_pair(URL_VALUE, ROLES, worker=Worker()) == (SNAPSHOT, SNAPSHOT)
    assert calls == [(URL_VALUE, ROLES, 'head')]


def test_session_fixture_always_closes_worker(monkeypatch):
    module = api(); fixture = importlib.import_module('tests.integration.conftest')
    assert hasattr(fixture, 'session_migration_worker')
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_AUTHORIZATION', module.AUTHORIZATION)
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_ADMIN_URL', URL_VALUE.set(
        username='gateway_test_admin', database='gateway_test_admin').render_as_string(hide_password=False))
    calls = []
    class Worker:
        def __init__(self, host, port): self._endpoint = (host, port); calls.append((host, port))
        def close(self): calls.append('close')
    monkeypatch.setattr(module, 'MigrationWorker', Worker)
    finalizers = []
    request = SimpleNamespace(session=SimpleNamespace(stash={}, addfinalizer=finalizers.append))
    worker = fixture.session_migration_worker(request)
    assert isinstance(worker, Worker)
    assert fixture.session_migration_worker(request) is worker
    # pytest loads a conftest under its own name, while security modules also
    # import registry_database directly. Both must own exactly one session worker.
    spec = importlib.util.spec_from_file_location('separate_fixture_import', fixture.__file__)
    separate = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, separate)
    spec.loader.exec_module(separate)
    assert separate.session_migration_worker(request) is worker
    assert len(finalizers) == 1
    finalizers[0]()
    assert calls == [('127.0.0.1', 54321), 'close']


def test_session_revalidates_cluster_before_reusing_worker(monkeypatch):
    module = api(); fixture = importlib.import_module('tests.integration.conftest')
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_AUTHORIZATION', module.AUTHORIZATION)
    admin = URL_VALUE.set(username='gateway_test_admin', database='gateway_test_admin')
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_ADMIN_URL', admin.render_as_string(hide_password=False))
    request = SimpleNamespace(session=SimpleNamespace(stash={}, addfinalizer=lambda finalizer: None))
    worker = fixture.session_migration_worker(request)
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_ADMIN_URL', admin.set(port=54322).render_as_string(hide_password=False))
    with pytest.raises(pytest.fail.Exception): fixture.session_migration_worker(request)
    assert worker._closed is True and worker._process is None


def test_worker_failure_still_drops_only_its_fresh_database_and_four_roles(monkeypatch):
    module = api(); fixture = importlib.import_module('tests.integration.conftest')
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_AUTHORIZATION', module.AUTHORIZATION)
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_ADMIN_URL', URL_VALUE.set(
        username='gateway_test_admin', database='gateway_test_admin').render_as_string(hide_password=False))
    monkeypatch.setattr(fixture.secrets, 'token_hex', lambda count: 'a' * 16)
    queries = []; closed = []
    class Admin:
        def execute(self, statement): queries.append(statement.as_string())
        def close(self): closed.append(True)
    class Worker:
        def run(self, *args): raise module.MigrationWorkerError('PRIVATE_ERROR')
    monkeypatch.setattr(fixture.psycopg, 'connect', lambda **kwargs: Admin())
    monkeypatch.setattr(fixture, 'session_migration_worker', lambda request: Worker())
    with pytest.raises(pytest.fail.Exception) as caught:
        next(fixture.registry_database.__wrapped__(SimpleNamespace(param={})))
    assert 'PRIVATE_' not in str(caught.value)
    assert [query for query in queries if query.startswith('DROP ')] == [
        'DROP DATABASE "' + URL_VALUE.database + '" WITH (FORCE)',
        *['DROP ROLE "' + role + '"' for role in reversed(list(ROLES.values()))]]
    assert closed == [True]


def test_imported_database_fixture_has_no_unexported_fixture_dependency():
    api(); fixture = importlib.import_module('tests.integration.conftest')
    import inspect
    assert tuple(inspect.signature(fixture.registry_database.__wrapped__).parameters) == ('request',)


def test_successful_session_close_kills_descendants_and_is_idempotent(monkeypatch, tmp_path):
    module = api(); pid_path = tmp_path / 'descendant.pid'
    action = ("child=os.fork(); "
        f"open({str(pid_path)!r},'w').write(str(child)) if child else None; "
        "time.sleep(10) if not child else print(json.dumps(value),flush=True)")
    child_factory(monkeypatch, module, action)
    worker = module.MigrationWorker('127.0.0.1', 54321, timeout=3)
    assert worker.run(URL_VALUE, ROLES) == (SNAPSHOT, SNAPSHOT)
    pid = int(pid_path.read_text())
    with pytest.raises(module.MigrationWorkerError): worker.close()
    worker.close()
    with pytest.raises(module.MigrationWorkerError): worker.run(URL_VALUE, ROLES)
    assert worker._process.poll() is not None
    deadline = time.monotonic() + 0.5
    while process_running(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not process_running(pid)


def test_failure_cleanup_is_inside_pair_deadline_even_when_term_is_ignored(monkeypatch):
    module = api()
    child_factory(monkeypatch, module,
        "__import__('signal').signal(15,__import__('signal').SIG_IGN); time.sleep(5)")
    worker = module.MigrationWorker('127.0.0.1', 54321, timeout=0.3)
    started = time.monotonic()
    with pytest.raises(module.MigrationWorkerError): worker.run(URL_VALUE, ROLES)
    assert time.monotonic() - started < 0.4
    assert worker._process.poll() is not None


@pytest.mark.parametrize('late', ["print('PRIVATE_LATE_OUTPUT',flush=True)", "sys.exit(2)"])
def test_normal_session_close_rejects_late_output_or_nonzero_exit(monkeypatch, late):
    module = api()
    child_factory(monkeypatch, module, "print(json.dumps(value),flush=True); time.sleep(0.05); " + late)
    worker = module.MigrationWorker('127.0.0.1', 54321)
    assert worker.run(URL_VALUE, ROLES) == (SNAPSHOT, SNAPSHOT)
    with pytest.raises(module.MigrationWorkerError) as caught: worker.close()
    assert 'PRIVATE_' not in str(caught.value)
    assert worker._process.poll() is not None


def test_normal_session_close_observes_clean_eof_exit(monkeypatch):
    module = api()
    child_factory(monkeypatch, module, "print(json.dumps(value),flush=True)")
    worker = module.MigrationWorker('127.0.0.1', 54321)
    worker.run(URL_VALUE, ROLES)
    worker.close()
    assert worker._process.returncode == 0


@pytest.mark.parametrize('stage', ['blocked_request', 'before_arming'])
def test_worker_dies_when_pytest_parent_is_killed_even_during_blocked_request(tmp_path, stage):
    api()
    ready = tmp_path / 'worker.pid'; release = tmp_path / 'release'
    before = (f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n") if stage == 'before_arming' else ''
    child = "import os,time,runpy\nfrom pathlib import Path\n" + before + f'''
import tests.integration.registry_migration_pair as pair
def blocked(*args):
 Path({str(ready)!r}).write_text(str(os.getpid()))
 time.sleep(30)
pair.collect_owned_pair=blocked
runpy.run_module('tests.integration.registry_migration_worker',run_name='__main__')
'''
    parent = f'''
import subprocess,sys
from tests.integration import registry_migration_worker as worker
from tests.unit.test_registry_migration_pair import URL_VALUE,ROLES
real_popen=subprocess.Popen
def popen(command,**options):
 return real_popen([sys.executable,'-c',{child!r}],**options)
worker.subprocess.Popen=popen
worker.MigrationWorker('127.0.0.1',54321).run(URL_VALUE,ROLES)
'''
    process = subprocess.Popen([sys.executable, '-c', parent], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    worker_pid = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline: time.sleep(0.01)
        assert ready.exists(), 'Worker never reached requested lifecycle stage'
        worker_pid = int(ready.read_text())
        process.kill(); process.wait(timeout=1)
        release.touch()
        deadline = time.monotonic() + 2
        while process_running(worker_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not process_running(worker_pid)
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=1)
        if worker_pid is not None:
            try: os.killpg(worker_pid, 9)
            except ProcessLookupError: pass


def test_worker_has_its_own_hard_request_alarm():
    api()
    child = '''
import io,json,sys,time
from tests.integration import registry_migration_worker as worker
from tests.unit.test_registry_migration_worker import descriptor
worker.PAIR_TIMEOUT=0.1
worker.collect_owned_pair=lambda *args: time.sleep(5)
initial=dict(schema_version=1,authorization=worker.AUTHORIZATION,host='127.0.0.1',port=54321)
worker.serve(io.StringIO(json.dumps(initial)+'\\n'+json.dumps(descriptor())+'\\n'),sys.stdout)
'''
    process = subprocess.Popen([sys.executable, '-c', child], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert process.wait(timeout=2) == -14
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=1)


def test_stdin_eof_during_blocked_request_remains_bounded_and_cannot_replay(monkeypatch, tmp_path):
    module = api(); ready = tmp_path / 'ready'
    calls = child_factory(monkeypatch, module, f"open({str(ready)!r},'w').close(); time.sleep(5)")
    worker = module.MigrationWorker('127.0.0.1', 54321, timeout=0.6)
    def end_input():
        deadline = time.monotonic() + 0.25
        while not ready.exists() and time.monotonic() < deadline: time.sleep(0.005)
        if ready.exists(): worker._process.stdin.close()
    closer = threading.Thread(target=end_input)
    closer.start()
    started = time.monotonic()
    with pytest.raises(module.MigrationWorkerError): worker.run(URL_VALUE, ROLES)
    closer.join(timeout=1)
    assert ready.exists() and not closer.is_alive()
    assert time.monotonic() - started < 0.7 and worker._process.poll() is not None
    with pytest.raises(module.MigrationWorkerError): worker.run(URL_VALUE, ROLES)
    assert len(calls) == 1


def test_existing_phase_supervisor_reaps_worker_and_escaped_descendant_after_parent_kill(tmp_path):
    module = api()
    from scripts.runtime_process import bounded_process
    worker_pid = tmp_path / 'worker.pid'; descendant_pid = tmp_path / 'descendant.pid'
    child = f'''
import os,time,runpy
from pathlib import Path
import tests.integration.registry_migration_pair as pair
def blocked(*args):
 if os.fork()==0:
  os.setsid()
  Path({str(descendant_pid)!r}).write_text(str(os.getpid()))
  time.sleep(30)
 else:
  Path({str(worker_pid)!r}).write_text(str(os.getpid()))
  while not Path({str(descendant_pid)!r}).exists(): time.sleep(0.01)
  os.kill(os.getppid(),9)
  time.sleep(30)
pair.collect_owned_pair=blocked
runpy.run_module('tests.integration.registry_migration_worker',run_name='__main__')
'''
    parent = f'''
import subprocess,sys
from tests.integration import registry_migration_worker as worker
from tests.unit.test_registry_migration_pair import URL_VALUE,ROLES
real_popen=subprocess.Popen
worker.subprocess.Popen=lambda command,**options: real_popen([sys.executable,'-c',{child!r}],**options)
worker.MigrationWorker('127.0.0.1',54321).run(URL_VALUE,ROLES)
'''
    result = bounded_process([sys.executable, '-c', parent], tmp_path / 'phase.log',
        timeout=5, cwd=module.ROOT)
    assert result['exit_status'] != 0 and result['descendant_cleanup'] is True
    assert worker_pid.exists() and descendant_pid.exists()
    for path in (worker_pid, descendant_pid):
        assert not Path('/proc', path.read_text()).exists(), 'Owned worker descendant survived phase cleanup'


@pytest.mark.parametrize('failed_upgrade', [None, 0, 1])
def test_owned_pair_uses_fresh_config_connection_and_disposes_every_engine(monkeypatch, failed_upgrade):
    api(); module = importlib.import_module('tests.integration.registry_migration_pair')
    assert hasattr(module, 'collect_owned_pair')
    events = []; configs = []; engines = []
    class Connection:
        def __enter__(self): events.append('connect'); return self
        def __exit__(self, *args): events.append('close')
    class Engine:
        def connect(self): return Connection()
        def dispose(self): events.append('dispose')
    def engine(url, **options):
        assert url == URL_VALUE and options['poolclass'] is module.NullPool
        assert options['hide_parameters'] is True and options['echo'] is False
        value = Engine(); engines.append(value); return value
    def upgrade(config, target):
        events.append('upgrade'); configs.append(config)
        assert config.attributes['registry_roles'] == ROLES
        assert config.attributes['registry_roles'] is not ROLES
        assert target == 'head'
        if len(configs) - 1 == failed_upgrade: raise RuntimeError('private failure')
    monkeypatch.setattr(module, 'create_engine', engine)
    monkeypatch.setattr(module.command, 'upgrade', upgrade)
    monkeypatch.setattr(module, 'migration_snapshot', lambda url: events.append('snapshot') or SNAPSHOT)
    if failed_upgrade is None:
        assert module.collect_owned_pair(URL_VALUE, ROLES, 'head') == response()['pair']
        assert configs[0] is not configs[1]
        assert configs[0].attributes['connection'] is not configs[1].attributes['connection']
        assert engines[0] is not engines[1]
        assert events == ['connect', 'upgrade', 'close', 'dispose', 'snapshot'] * 2
    else:
        with pytest.raises(RuntimeError): module.collect_owned_pair(URL_VALUE, ROLES, 'head')
        assert events[-2:] == ['close', 'dispose']
