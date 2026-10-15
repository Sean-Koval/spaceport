"""Offline qualification of the credential-bearing, test-only Docker boundary."""
import copy
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import URL

pytestmark = [pytest.mark.unit, pytest.mark.contract]

CUBE = 'gateway-cube-' + 'a' * 16
REGISTRY = 'gateway-registry-test-' + 'b' * 16
IMAGE = 'sha256:' + 'c' * 64
RUNTIME = 'sha256:' + 'd' * 64
PROBE = 'sha256:' + 'e' * 64
POSTGRES = 'sha256:' + 'f' * 64
CID = '1' * 64
RUN_CID = '2' * 64
DIGEST = '3' * 64
ROLES = ('owner', 'app', 'audit_retention', 'source_reader')
PASSWORD = 'synthetic_private_password_' + 'z' * 15 + 'q'


def module():
    from tests.integration import source_journey_host
    return source_journey_host


def configuration():
    return {
        'GATEWAY_SOURCE_JOURNEY_IMAGE_ID': IMAGE,
        'GATEWAY_SOURCE_JOURNEY_IMAGE_TAG': CUBE + '-journey:local',
        'GATEWAY_SOURCE_JOURNEY_RUNTIME_IMAGE_ID': RUNTIME,
        'GATEWAY_SOURCE_JOURNEY_PROBE_IMAGE_ID': PROBE,
        'GATEWAY_SOURCE_JOURNEY_REGISTRY_CONTAINER': CID,
        'GATEWAY_SOURCE_JOURNEY_REGISTRY_RUN_LABEL': REGISTRY + '-local',
        'GATEWAY_CUBE_RUN_LABEL': CUBE + '-local',
    }


def database():
    token = '4' * 16
    roles = {key: 'gwt_' + token + '_' + key for key in ROLES}
    urls = {key: URL.create('postgresql+psycopg', username=role, password=PASSWORD,
        host='127.0.0.1', port=32543, database='gateway_test_' + token) for key, role in roles.items()}
    admin = URL.create('postgresql+psycopg', username='gateway_test_admin', password=PASSWORD,
        host='127.0.0.1', port=32543, database='gateway_test_admin')
    return SimpleNamespace(roles=roles, urls=urls, admin_url=admin)


def evidence():
    return {
        'schema_version': 1, 'kind': 'bigquery_gateway_journey', 'status': 'passed',
        'source_digest': DIGEST, 'image_id': IMAGE, 'checks': module().CHECKS,
        'counts': {'inserts': 10, 'metadata': 10000, 'results': 5, 'provider_errors': 0},
    }


def image_metadata():
    return {'id': IMAGE, 'tags': [CUBE + '-journey:local'], 'run_label': CUBE + '-local',
        'runtime_reference': RUNTIME, 'probe_reference': PROBE}


def registry_metadata():
    return {'id': CID, 'name': '/' + REGISTRY, 'image_id': POSTGRES,
        'configured_image': module().POSTGRES_IMAGE, 'run_label': REGISTRY + '-local',
        'running': True, 'ports': {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '32543'}]},
        'network_mode': 'bridge'}


def test_closed_success_is_bound_to_exact_source_and_image():
    m = module()
    assert m.receipt(m.PREFIX + json.dumps(evidence()) + '\n', DIGEST, IMAGE) == evidence()


@pytest.mark.parametrize('mutate', [
    lambda v: v.update(schema_version=True), lambda v: v.update(extra=True),
    lambda v: v.update(kind='other'), lambda v: v.update(status='failed'),
    lambda v: v.update(source_digest='0' * 64), lambda v: v.update(image_id=RUNTIME),
    lambda v: v.update(checks=v['checks'][::-1]), lambda v: v['counts'].update(inserts=True),
    lambda v: v['counts'].update(inserts=9), lambda v: v['counts'].update(metadata=-1),
    lambda v: v['counts'].update(metadata=0), lambda v: v['counts'].update(metadata=9),
    lambda v: v['counts'].update(metadata=10001), lambda v: v['counts'].update(results=4),
    lambda v: v['counts'].update(results=17), lambda v: v['counts'].update(provider_errors=1),
    lambda v: v['counts'].update(extra=0), lambda v: v['counts'].update(results=5.0),
])
def test_closed_success_rejects_wrong_or_extra_evidence(mutate):
    m = module(); value = copy.deepcopy(evidence()); mutate(value)
    with pytest.raises(ValueError): m.receipt(m.PREFIX + json.dumps(value), DIGEST, IMAGE)


@pytest.mark.parametrize('alter', [
    lambda line: '', lambda line: line + '\n' + line, lambda line: 'private-secret\n' + line,
    lambda line: line + '\nGATEWAY_FAILURE_EVIDENCE {}',
    lambda line: line.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'),
    lambda line: line.replace('"metadata": 10000', '"metadata": 10000, "metadata": 0'),
    lambda line: line + ' ' * 65536,
])
def test_receipt_rejects_chatter_duplicate_json_and_overflow(alter):
    m = module()
    with pytest.raises(ValueError): m.receipt(alter(m.PREFIX + json.dumps(evidence())), DIGEST, IMAGE)


def test_configuration_requires_all_exact_run_private_identities():
    m = module(); env = configuration()
    assert m.configuration(env).image_id == IMAGE
    for key in env:
        bad = dict(env); del bad[key]
        with pytest.raises(ValueError): m.configuration(bad)
    for key, value in [('GATEWAY_SOURCE_JOURNEY_IMAGE_ID', 'repo:latest'),
        ('GATEWAY_SOURCE_JOURNEY_IMAGE_TAG', 'other:latest'),
        ('GATEWAY_CUBE_RUN_LABEL', 'other'), ('GATEWAY_SOURCE_JOURNEY_REGISTRY_CONTAINER', 'container-name'),
        ('GATEWAY_SOURCE_JOURNEY_PROBE_IMAGE_ID', 'sha256:' + 'x' * 64)]:
        bad = {**env, key: value}
        with pytest.raises(ValueError): m.configuration(bad)


def test_descriptor_contains_only_exact_synthetic_database_authority():
    m = module(); db = database()
    value = json.loads(m.descriptor(db, DIGEST, IMAGE, 32543))
    assert set(value) == {'schema_version', 'source_digest', 'image_id', 'registry'}
    assert value['registry'] == {'host': '127.0.0.1', 'port': 32543,
        'database': 'gateway_test_' + '4' * 16, 'roles': db.roles,
        'passwords': {key: PASSWORD for key in ROLES}, 'admin_password': PASSWORD}


@pytest.mark.parametrize('change', [
    lambda db: setattr(db, 'admin_url', db.admin_url.set(host='localhost')),
    lambda db: setattr(db, 'admin_url', db.admin_url.set(database='production')),
    lambda db: setattr(db, 'admin_url', db.admin_url.set(username='postgres')),
    lambda db: setattr(db, 'admin_url', db.admin_url.set(password='short')),
    lambda db: db.urls.update(app=db.urls['app'].set(port=5432)),
    lambda db: db.urls.update(app=db.urls['app'].set(database='gateway_test_' + '5' * 16)),
    lambda db: db.urls.update(app=db.urls['app'].set(username='someone_else')),
    lambda db: db.urls.update(app=db.urls['app'].set(query={'options': '-c search_path=evil'})),
    lambda db: db.roles.update(owner='gwt_' + '5' * 16 + '_owner'),
    lambda db: db.roles.update(extra='role'),
])
def test_descriptor_rejects_unattested_or_non_synthetic_authority(change):
    m = module(); db = database(); change(db)
    with pytest.raises(ValueError): m.descriptor(db, DIGEST, IMAGE, 32543)


def test_descriptor_rejects_different_attested_port():
    with pytest.raises(ValueError): module().descriptor(database(), DIGEST, IMAGE, 5432)


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    """Only Docker's external responses are fake; launcher IO/processes are real."""
    m = module(); data = tmp_path / 'responses.json'; log = tmp_path / 'commands.jsonl'
    stdin = tmp_path / 'stdin'; pidfile = tmp_path / 'run.pid'; envfile = tmp_path / 'child-env.json'
    spec = {'image': image_metadata(), 'postgres': POSTGRES, 'registry': registry_metadata(),
        'receipt': m.PREFIX + json.dumps(evidence()), 'mode': 'success'}
    script = tmp_path / 'docker'
    script.write_text('#!' + sys.executable + '\n' + '''import json,os,sys,time
from pathlib import Path
base=Path(__file__).parent
spec=json.loads((base/'responses.json').read_text())
a=sys.argv[1:]
with (base/'commands.jsonl').open('a') as f:f.write(json.dumps(a)+'\\n')
if a[:2]==['image','inspect']:
 print(json.dumps(spec['image'] if a[-1].startswith('sha256:') else spec['postgres']))
elif a[:2]==['container','inspect']:print(json.dumps(spec['registry']))
elif a[0]=='run':
 if spec['mode']=='ambiguous_create':
  (base/'created-without-cid').write_text('2'*64)
  sys.exit(1)
 Path(a[a.index('--cidfile')+1]).write_text('2'*64)
 (base/'run.pid').write_text(str(os.getpid()))
 (base/'child-env.json').write_text(json.dumps(dict(os.environ)))
 mode=spec['mode']
 if mode=='stdin_stall':time.sleep(60)
 (base/'stdin').write_bytes(sys.stdin.buffer.read())
 if mode=='stall':time.sleep(60)
 if mode=='overflow':print('s'*70000,flush=True);time.sleep(60)
 print(spec['receipt'],flush=True)
 if mode=='nonzero':sys.exit(1)
elif a[0]=='rm':
 if spec['mode'] in ('rm_failure','remains'):sys.exit(1)
elif a[0]=='ps':
 if spec['mode']=='remains':print('2'*64)
else:sys.exit(2)
''')
    script.chmod(0o700)
    data.write_text(json.dumps(spec))
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('GATEWAY_TEST_POSTGRES_ADMIN_URL', 'postgresql://PRIVATE-ADMIN-SECRET')
    monkeypatch.setenv('GOOGLE_APPLICATION_CREDENTIALS', '/private/live-credentials')
    for key, value in configuration().items(): monkeypatch.setenv(key, value)
    monkeypatch.setattr(m, 'current_source_digest', lambda: DIGEST)
    return SimpleNamespace(m=m, spec=spec, data=data, log=log, stdin=stdin, pid=pidfile, env=envfile)


def commands(fake):
    return [json.loads(line) for line in fake.log.read_text().splitlines()]


def test_host_attests_before_stdin_and_runs_only_hardened_exact_image(fake_docker):
    f = fake_docker
    assert f.m.run_hosted_journey(database()) is None
    calls = commands(f); run = next(call for call in calls if call[0] == 'run')
    assert all(call[0] != 'run' for call in calls[:3])
    assert run[-1] == IMAGE
    for flag, value in [('--network', 'container:' + CID), ('--user', '65532:65532'),
        ('--memory', '1g'), ('--cpus', '1'), ('--pids-limit', '256'), ('--cap-drop', 'ALL'),
        ('--security-opt', 'no-new-privileges:true'), ('--pull', 'never')]:
        assert run[run.index(flag) + 1] == value
    assert '--read-only' in run and '--interactive' in run and '--rm' in run
    assert run[run.index('--mount') + 1].endswith(',dst=/app,readonly')
    assert run[run.index('--tmpfs') + 1] == '/tmp:rw,noexec,nosuid,nodev,size=256m'
    assert 'PRIVATE-ADMIN-SECRET' not in json.dumps(calls) and PASSWORD not in json.dumps(calls)
    assert 'GATEWAY_TEST_POSTGRES_ADMIN_URL' not in json.loads(f.env.read_text())
    assert 'GOOGLE_APPLICATION_CREDENTIALS' not in json.loads(f.env.read_text())
    assert json.loads(f.stdin.read_bytes())['registry']['admin_password'] == PASSWORD
    assert calls[-2:] == [['rm', '--force', RUN_CID],
        ['ps', '--all', '--no-trunc', '--filter', 'id=' + RUN_CID, '--format', '{{.ID}}']]
    assert not Path(run[run.index('--cidfile') + 1]).parent.exists()
    assert all('.Config.Env' not in ' '.join(call) for call in calls)


@pytest.mark.parametrize('area,key,value', [
    ('image', 'id', RUNTIME), ('image', 'tags', ['public:latest']), ('image', 'run_label', 'other'),
    ('image', 'runtime_reference', IMAGE), ('image', 'probe_reference', IMAGE),
    ('registry', 'id', RUN_CID), ('registry', 'name', '/existing-production'),
    ('registry', 'image_id', IMAGE), ('registry', 'configured_image', 'postgres:latest'),
    ('registry', 'run_label', 'other'), ('registry', 'running', False),
    ('registry', 'running', 1), ('registry', 'network_mode', 'host'),
    ('registry', 'ports', {'5432/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '32543'}]}),
    ('registry', 'ports', {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '32544'}]}),
    ('registry', 'ports', {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '32543'}, {'HostIp': '::1', 'HostPort': '32543'}]}),
])
def test_failed_attestation_never_starts_credential_bearing_container(fake_docker, area, key, value):
    f = fake_docker; f.spec[area][key] = value; f.data.write_text(json.dumps(f.spec))
    with pytest.raises(AssertionError): f.m.run_hosted_journey(database())
    assert not f.stdin.exists() and all(call[0] != 'run' for call in commands(f))


@pytest.mark.parametrize('mode', ['nonzero', 'overflow', 'stall', 'stdin_stall', 'remains'])
def test_failures_are_bounded_sanitized_and_cleanup_exact_container(fake_docker, monkeypatch, capsys, mode):
    f = fake_docker; f.spec['mode'] = mode; f.spec['receipt'] += '\nPRIVATE-CHILD-SECRET'
    f.data.write_text(json.dumps(f.spec))
    original = f.m._bounded
    def faster(command, **kwargs):
        if command[1] == 'run': kwargs['timeout'] = min(kwargs['timeout'], .3)
        return original(command, **kwargs)
    monkeypatch.setattr(f.m, '_bounded', faster)
    started = time.monotonic()
    with pytest.raises(AssertionError) as error: f.m.run_hosted_journey(database())
    assert time.monotonic() - started < 4
    assert 'PRIVATE' not in str(error.value) and PASSWORD not in str(error.value)
    assert capsys.readouterr() == ('', '')
    assert ['rm', '--force', RUN_CID] in commands(f)
    with pytest.raises(ProcessLookupError): os.kill(int(f.pid.read_text()), 0)


def test_failed_remove_is_admitted_only_when_absence_is_verified(fake_docker):
    f = fake_docker; f.spec['mode'] = 'rm_failure'; f.data.write_text(json.dumps(f.spec))
    f.m.run_hosted_journey(database())
    assert commands(f)[-1][0] == 'ps'


def test_real_exited_parent_with_stdout_holding_descendant_is_killed(tmp_path):
    m = module(); pidfile = tmp_path / 'descendant.pid'
    script = "import os,time\nfrom pathlib import Path\npid=os.fork()\nif pid:os._exit(0)\nPath(" + repr(str(pidfile)) + ").write_text(str(os.getpid()))\ntime.sleep(60)"
    start = time.monotonic()
    with pytest.raises(ValueError): m._bounded([sys.executable, '-c', script], timeout=.3)
    assert time.monotonic() - start < 2
    pid = int(pidfile.read_text())
    try:
        for _ in range(100):
            status = Path(f'/proc/{pid}/stat')
            if not status.exists() or status.read_text().split()[2] == 'Z': break
            time.sleep(.01)
        else: pytest.fail('Exited parent left its live descendant behind')
    finally:
        try: os.kill(pid, 9)
        except ProcessLookupError: pass


def test_real_child_output_and_stdin_writes_share_deadline():
    m = module(); started = time.monotonic()
    with pytest.raises(ValueError):
        m._bounded([sys.executable, '-c', 'import time;time.sleep(60)'], timeout=.2, payload=b'x' * 16384)
    assert time.monotonic() - started < 2


def test_combined_dockerfile_uses_pinned_upstream_and_prepared_locked_trees():
    root = Path(__file__).resolve().parents[2]
    text = (root / 'infra/SourceJourney.Dockerfile').read_text()
    runtime = (root / 'cube/bigquery-runtime/Dockerfile').read_text()
    # Separate synthetic composition has the same immutable installed Node
    # packages; image-reference labels are related evidence, not parent IDs.
    assert next(line for line in text.splitlines() if line.startswith('FROM ')) == runtime.splitlines()[0]
    assert '${RUNTIME_IMAGE}' not in text and '${PROBE_IMAGE}' not in text
    assert 'gateway.source-journey.runtime-reference=${RUNTIME_IMAGE_ID}' in text
    assert 'gateway.source-journey.probe-reference=${PROBE_IMAGE_ID}' in text
    assert 'COPY python /opt/python' in text
    assert 'COPY packages /opt/packages' in text
    assert 'COPY bigquery-runtime/*.cjs /cube/bigquery-runtime/' in text
    assert 'ENTRYPOINT ["/opt/python/bin/python3.12", "-m", "tests.integration.source_pilot_bigquery_image"]' in text
    assert not any(line.startswith(('RUN ', 'ADD ')) for line in text.splitlines())
    assert 'USER 65532:65532' in text and 'CMD []' in text


def test_combined_image_explicit_runtime_defaults_match_production():
    root = Path(__file__).resolve().parents[2]
    text = (root / 'infra/SourceJourney.Dockerfile').read_text()
    runtime = (root / 'cube/bigquery-runtime/Dockerfile').read_text()
    expected = next(line for line in runtime.splitlines() if line.startswith('ENV '))
    assert expected in text
    for setting in ['NODE_PATH=/cube/node_modules', 'GATEWAY_BIGQUERY_RUNTIME_DIR=/cube/bigquery-runtime',
        'NODE_ENV=production', 'CUBEJS_DEV_MODE=false', 'HOME=/tmp/spaceport-no-adc']:
        assert setting in expected
    assert 'ENV PYTHONPATH=/app:/opt/packages PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1' in text


@pytest.mark.parametrize('value', [None, {}, [], 'not-json', {'extra': True}])
def test_invalid_inspection_records_fail_before_any_secret_transmission(fake_docker, value):
    f = fake_docker; f.spec['image'] = value; f.data.write_text(json.dumps(f.spec))
    with pytest.raises(AssertionError): f.m.run_hosted_journey(database())
    assert all(call[0] != 'run' for call in commands(f)) and not f.stdin.exists()


@pytest.mark.parametrize('kind', ['missing', 'symlink', 'overlong', 'bad_id'])
def test_cleanup_never_targets_nonowned_or_malformed_identity(tmp_path, kind):
    m = module(); cidfile = tmp_path / 'container-id'
    if kind == 'symlink':
        target = tmp_path / 'target'; target.write_text(RUN_CID); cidfile.symlink_to(target)
    elif kind == 'overlong': cidfile.write_text(RUN_CID + 'x' * 100)
    elif kind == 'bad_id': cidfile.write_text('registry-production')
    with pytest.raises(ValueError): m._cleanup(cidfile, time.monotonic() + 2)


def test_total_command_deadline_reserves_exact_container_cleanup(fake_docker, monkeypatch):
    f = fake_docker; f.spec['mode'] = 'stall'; f.data.write_text(json.dumps(f.spec))
    monkeypatch.setattr(f.m, 'OVERALL_SECONDS', 3)
    monkeypatch.setattr(f.m, 'CLEANUP_RESERVE', 1.6)
    started = time.monotonic()
    with pytest.raises(AssertionError): f.m.run_hosted_journey(database())
    assert time.monotonic() - started < 3
    assert ['rm', '--force', RUN_CID] in commands(f)
    assert commands(f)[-1][0] == 'ps'


def test_normal_exited_parent_cannot_leave_detached_output_descendant(tmp_path):
    m = module(); pidfile = tmp_path / 'descendant.pid'
    script = ('import os,time\nfrom pathlib import Path\npid=os.fork()\n'
        'if pid:\n time.sleep(.05)\n os._exit(0)\n'
        'os.close(1);os.close(2)\nPath(' + repr(str(pidfile)) + ').write_text(str(os.getpid()))\ntime.sleep(60)')
    assert m._bounded([sys.executable, '-c', script], timeout=2) == ''
    pid = int(pidfile.read_text())
    try:
        for _ in range(100):
            status = Path(f'/proc/{pid}/stat')
            if not status.exists() or status.read_text().split()[2] == 'Z': break
            time.sleep(.01)
        else: pytest.fail('Successful parent left its live descendant behind')
    finally:
        try: os.kill(pid, 9)
        except ProcessLookupError: pass


def test_numeric_run_labels_must_identify_the_same_hosted_run():
    m = module(); env = configuration()
    both = {**env, 'GATEWAY_CUBE_RUN_LABEL': '123-2',
        'GATEWAY_SOURCE_JOURNEY_REGISTRY_RUN_LABEL': '123-2'}
    assert m.configuration(both).cube_run_label == '123-2'
    for cube, registry in [('123-2', '124-2'), ('123-2', env['GATEWAY_SOURCE_JOURNEY_REGISTRY_RUN_LABEL']),
        (env['GATEWAY_CUBE_RUN_LABEL'], '123-2')]:
        with pytest.raises(ValueError):
            m.configuration({**env, 'GATEWAY_CUBE_RUN_LABEL': cube,
                'GATEWAY_SOURCE_JOURNEY_REGISTRY_RUN_LABEL': registry})


def test_ambiguous_create_without_cid_fails_without_guessing_cleanup_target(fake_docker):
    f = fake_docker; f.spec['mode'] = 'ambiguous_create'; f.data.write_text(json.dumps(f.spec))
    with pytest.raises(AssertionError, match='failed at cleanup'):
        f.m.run_hosted_journey(database())
    assert (f.data.parent / 'created-without-cid').read_text() == RUN_CID
    assert not f.stdin.exists()
    # Only the outer registered run-label cleanup may reconcile this ambiguity.
    # The launcher cannot infer container absence or remove a guessed name/CID.
    assert not any(call[0] in ('rm', 'ps') for call in commands(f))


def test_docker_default_bridge_mode_is_admitted_for_existing_registry_command(fake_docker):
    f = fake_docker; f.spec['registry']['network_mode'] = 'default'
    f.data.write_text(json.dumps(f.spec))
    f.m.run_hosted_journey(database())
    assert next(call for call in commands(f) if call[0] == 'run')[-1] == IMAGE
