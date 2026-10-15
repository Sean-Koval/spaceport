"""Test-only bridge to the exact local BigQuery image and registry namespace.

No credentials enter Docker arguments, inherited environment, mounts or logs.
The original host fixture retains ownership of the database and its four roles.
All Docker metadata is deliberately projected; never inspect Config.Env.
The mandatory outer registered run-label cleanup remains the recovery owner if
Docker creates a container but dies before recording its exact private CID.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
POSTGRES_IMAGE = 'postgres:17.11-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826'
PREFIX = 'GATEWAY_BQ_GATEWAY_JOURNEY '
CHECKS = ['package_bootstrap', 'fixture_publish', 'catalog_describe', 'consumer_result',
    'durable_settlement', 'provider_guard', 'teardown']
ROLES = ('owner', 'app', 'audit_retention', 'source_reader')
MAXIMUM_OUTPUT = 65536
MAXIMUM_DESCRIPTOR = 16384
OVERALL_SECONDS = 120
CLEANUP_RESERVE = 10
RUN_SECONDS = 95
IMAGE_FORMAT = ('{"id":{{json .Id}},"tags":{{json .RepoTags}},'
    '"run_label":{{json (index .Config.Labels "gateway.cube.run")}},'
    '"runtime_reference":{{json (index .Config.Labels "gateway.source-journey.runtime-reference")}},'
    '"probe_reference":{{json (index .Config.Labels "gateway.source-journey.probe-reference")}}}')
REGISTRY_FORMAT = ('{"id":{{json .Id}},"name":{{json .Name}},"image_id":{{json .Image}},'
    '"configured_image":{{json .Config.Image}},'
    '"run_label":{{json (index .Config.Labels "gateway.registry.run")}},'
    '"running":{{json .State.Running}},"ports":{{json .NetworkSettings.Ports}},'
    '"network_mode":{{json .HostConfig.NetworkMode}}}')


# Diagnostics are enums, never exception strings or arbitrary child output.
IMAGE_STAGES = frozenset(('bootstrap', 'source', 'descriptor', 'database', 'package_bootstrap',
    'cube_startup', 'fixture_test', 'publish', 'catalog', 'query', 'worker', 'result',
    'settlement', 'teardown', 'counts'))
HOST_STAGES = frozenset(('configuration', 'attestation', 'source', 'descriptor', 'execution', 'receipt', 'cleanup'))
FAILURE_ERRORS = frozenset(('permission', 'missing_file', 'import', 'timeout', 'assertion', 'value',
    'type', 'unexpected', 'output_limit', 'process_exit', 'io', 'encoding'))


def failure_reason(error):
    for kind, reason in ((PermissionError, 'permission'), (FileNotFoundError, 'missing_file'),
            (ImportError, 'import'), (TimeoutError, 'timeout'), (AssertionError, 'assertion'),
            (UnicodeError, 'encoding'), (ValueError, 'value'), (TypeError, 'type'), (OSError, 'io')):
        if isinstance(error, kind):
            return reason
    return 'unexpected'


class HostedJourneyFailure(AssertionError):
    def __init__(self, stage, error):
        if (type(stage) is not str or stage not in HOST_STAGES | {'image_' + value for value in IMAGE_STAGES}
                or type(error) is not str or error not in FAILURE_ERRORS):
            raise ValueError('Invalid journey failure evidence')
        self.journey_stage, self.journey_error = stage, error
        super().__init__('Hosted synthetic BigQuery journey failed at ' + stage + ' (' + error + ')')


class _CommandFailure(ValueError):
    def __init__(self, error, image_stage=None):
        if (type(error) is not str or error not in FAILURE_ERRORS
                or image_stage is not None and (type(image_stage) is not str or image_stage not in IMAGE_STAGES)):
            raise ValueError('Invalid journey command evidence')
        self.journey_error, self.image_stage = error, image_stage
        super().__init__('Journey command failed (' + error + ')')


@dataclass(frozen=True)
class Configuration:
    image_id: str
    image_tag: str
    runtime_image_id: str
    probe_image_id: str
    registry_container: str
    registry_run_label: str
    cube_run_label: str


def _matches(pattern, value):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def configuration(environment=None):
    env = os.environ if environment is None else environment
    keys = {
        'image_id': 'GATEWAY_SOURCE_JOURNEY_IMAGE_ID',
        'image_tag': 'GATEWAY_SOURCE_JOURNEY_IMAGE_TAG',
        'runtime_image_id': 'GATEWAY_SOURCE_JOURNEY_RUNTIME_IMAGE_ID',
        'probe_image_id': 'GATEWAY_SOURCE_JOURNEY_PROBE_IMAGE_ID',
        'registry_container': 'GATEWAY_SOURCE_JOURNEY_REGISTRY_CONTAINER',
        'registry_run_label': 'GATEWAY_SOURCE_JOURNEY_REGISTRY_RUN_LABEL',
        'cube_run_label': 'GATEWAY_CUBE_RUN_LABEL',
    }
    if any(key not in env for key in keys.values()):
        raise ValueError('Complete hosted journey configuration required')
    value = Configuration(**{key: env[name] for key, name in keys.items()})
    if (any(not _matches(r'sha256:[a-f0-9]{64}', getattr(value, key))
            for key in ('image_id', 'runtime_image_id', 'probe_image_id'))
            or not _matches(r'gateway-cube-[a-f0-9]{16}-journey:local', value.image_tag)
            or not _matches(r'[a-f0-9]{64}', value.registry_container)
            or not _matches(r'(?:gateway-registry-test-[a-f0-9]{16}-local|[0-9]+-[0-9]+)', value.registry_run_label)):
        raise ValueError('Exact run-private journey identities required')
    project = value.image_tag.removesuffix('-journey:local')
    if value.cube_run_label != project + '-local' and not _matches(r'[0-9]+-[0-9]+', value.cube_run_label):
        raise ValueError('Exact run-private journey label required')
    if ((_matches(r'[0-9]+-[0-9]+', value.cube_run_label)
            or _matches(r'[0-9]+-[0-9]+', value.registry_run_label))
            and value.cube_run_label != value.registry_run_label):
        raise ValueError('Hosted registry and journey must belong to the same run')
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate journey evidence')
        result[key] = value
    return result


def _json(raw):
    def invalid_constant(_):
        raise ValueError('Invalid journey evidence')
    try:
        return json.loads(raw, object_pairs_hook=_unique, parse_constant=invalid_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('Invalid journey evidence') from None


def failure_receipt(output):
    # Only a complete, single, bounded child failure is eligible. Chatter,
    # duplicate keys, extra data and success receipts yield no diagnostics.
    if type(output) is not str or len(output.encode('utf-8')) > 4096:
        raise ValueError('Invalid journey failure receipt')
    lines = output.splitlines()
    if len(lines) != 1 or not lines[0].startswith(PREFIX):
        raise ValueError('Invalid journey failure receipt')
    value = _json(lines[0][len(PREFIX):])
    if (type(value) is not dict or set(value) != {'schema_version', 'kind', 'status', 'stage', 'error'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['kind'] != 'bigquery_gateway_journey' or value['status'] != 'failed'
            or type(value['stage']) is not str or value['stage'] not in IMAGE_STAGES
            or type(value['error']) is not str or value['error'] not in FAILURE_ERRORS):
        raise ValueError('Invalid journey failure receipt')
    return value['stage'], value['error']


def receipt(output, source_digest, image_id):
    if type(output) is not str or len(output.encode('utf-8')) > MAXIMUM_OUTPUT:
        raise ValueError('Invalid journey receipt')
    lines = output.splitlines()
    if len(lines) != 1 or not lines[0].startswith(PREFIX) or len(lines[0]) > 4096:
        raise ValueError('A single closed journey receipt is required')
    value = _json(lines[0][len(PREFIX):])
    if (type(value) is not dict or set(value) != {'schema_version', 'kind', 'status', 'source_digest', 'image_id', 'checks', 'counts'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['kind'] != 'bigquery_gateway_journey' or value['status'] != 'passed'
            or value['source_digest'] != source_digest or value['image_id'] != image_id
            or value['checks'] != CHECKS or type(value['counts']) is not dict
            or set(value['counts']) != {'inserts', 'metadata', 'results', 'provider_errors'}):
        raise ValueError('Invalid journey receipt')
    counts = value['counts']
    if (any(type(number) is not int for number in counts.values()) or counts['inserts'] != 10
            or not 10 <= counts['metadata'] <= 10000 or not 5 <= counts['results'] <= 16 or counts['provider_errors'] != 0):
        raise ValueError('Invalid journey counts')
    return value


def _docker_environment():
    # Docker must not inherit the host fixture's admin URL or arbitrary cloud
    # credentials. Preserve only CLI location and explicitly configured daemon
    # transport; every attestation, run and cleanup uses this same environment.
    allowed = ('PATH', 'HOME', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')
    return {**{key: os.environ[key] for key in allowed if key in os.environ}, 'LANG': 'C.UTF-8'}


def _bounded(command, *, timeout=5, maximum=MAXIMUM_OUTPUT, payload=None, admit_failure=False):
    """One deadline covers nonblocking stdin, bounded output and parent exit.

    Always kill the owned process group, even when the direct parent exited:
    descendants may still hold a pipe or continue after apparent success.
    """
    if timeout <= 0 or (payload is not None and (type(payload) is not bytes or len(payload) > MAXIMUM_DESCRIPTOR)):
        raise ValueError('Invalid bounded journey command')
    process = None
    deadline = time.monotonic() + timeout
    data = bytearray()
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, env=_docker_environment())
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, 'output')
            offset = 0
            stdin_closed_early = False
            if payload is not None:
                os.set_blocking(process.stdin.fileno(), False)
                if payload:
                    selector.register(process.stdin, selectors.EVENT_WRITE, 'input')
                else:
                    process.stdin.close()
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _CommandFailure('timeout')
                for key, _ in selector.select(min(.05, remaining)):
                    if key.data == 'input':
                        try:
                            offset += os.write(key.fd, payload[offset:])
                        except BrokenPipeError:
                            # Bootstrap can reject before reading the descriptor.
                            # Keep draining bounded output to retain its closed
                            # failure receipt, but never admit a partial write as
                            # a successful command.
                            stdin_closed_early = True
                        if stdin_closed_early or offset == len(payload):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fd, min(8192, maximum + 1 - len(data)))
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            data.extend(chunk)
                            if len(data) > maximum:
                                raise _CommandFailure('output_limit')
            if process.returncode != 0:
                image_stage, reason = None, 'process_exit'
                if admit_failure:
                    try:
                        image_stage, reason = failure_receipt(data.decode('utf-8'))
                    except (ValueError, UnicodeError):
                        pass
                raise _CommandFailure(reason, image_stage)
            if stdin_closed_early:
                raise _CommandFailure('io')
        return data.decode('utf-8')
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        raise _CommandFailure(failure_reason(error)) from None
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Never communicate() here: it would collect unlimited descendant
            # output. Close our bounded pipes, then reap only our direct child.
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                raise _CommandFailure('timeout') from None


def _command(command, deadline, *, timeout=5, maximum=MAXIMUM_OUTPUT, payload=None, admit_failure=False):
    # Reserve the direct-child reap bound inside the caller's absolute budget.
    remaining = deadline - time.monotonic() - .5
    if remaining <= 0:
        raise _CommandFailure('timeout')
    return _bounded(command, timeout=min(timeout, remaining), maximum=maximum, payload=payload, admit_failure=admit_failure)


def _attest(config, db, deadline):
    # Related qualified image IDs are reference links, not ancestry claims.
    # The fixed digest-pinned Dockerfile and original locked build trees are
    # the combined image's build inputs.
    image = _json(_command(['docker', 'image', 'inspect', '--format', IMAGE_FORMAT, config.image_id], deadline))
    if (type(image) is not dict or set(image) != {'id', 'tags', 'run_label', 'runtime_reference', 'probe_reference'}
            or image['id'] != config.image_id or type(image['tags']) is not list
            or config.image_tag not in image['tags'] or any(type(tag) is not str for tag in image['tags'])
            or image['run_label'] != config.cube_run_label or image['runtime_reference'] != config.runtime_image_id
            or image['probe_reference'] != config.probe_image_id):
        raise ValueError('Journey image identity attestation failed')
    postgres = _json(_command(['docker', 'image', 'inspect', '--format', '{{json .Id}}', POSTGRES_IMAGE], deadline))
    if not _matches(r'sha256:[a-f0-9]{64}', postgres):
        raise ValueError('Pinned registry image attestation failed')
    registry = _json(_command(['docker', 'container', 'inspect', '--format', REGISTRY_FORMAT, config.registry_container], deadline))
    fields = {'id', 'name', 'image_id', 'configured_image', 'run_label', 'running', 'ports', 'network_mode'}
    if (type(registry) is not dict or set(registry) != fields or registry['id'] != config.registry_container
            or not _matches(r'/gateway-registry-test-[a-f0-9]{16}', registry['name'])
            or registry['image_id'] != postgres or registry['configured_image'] != POSTGRES_IMAGE
            or registry['run_label'] != config.registry_run_label or registry['running'] is not True
            or registry['network_mode'] not in ('bridge', 'default')):
        raise ValueError('Disposable registry identity attestation failed')
    if (config.registry_run_label != registry['name'][1:] + '-local'
            and not _matches(r'[0-9]+-[0-9]+', config.registry_run_label)):
        raise ValueError('Disposable registry run attestation failed')
    port = getattr(db.admin_url, 'port', None)
    if type(port) is not int or not 1 <= port <= 65535 or registry['ports'] != {
            '5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(port)}]}:
        raise ValueError('Disposable registry endpoint attestation failed')
    return port


def descriptor(db, source_digest, image_id, attested_port):
    from sqlalchemy.engine import URL
    try:
        admin = db.admin_url
        if (not isinstance(admin, URL) or admin.drivername != 'postgresql+psycopg' or admin.host != '127.0.0.1'
                or type(attested_port) is not int or not 1 <= attested_port <= 65535 or admin.port != attested_port
                or admin.username != 'gateway_test_admin' or admin.database != 'gateway_test_admin' or admin.query
                or not _matches(r'[A-Za-z0-9_-]{43}', admin.password)
                or not _matches(r'[a-f0-9]{64}', source_digest) or not _matches(r'sha256:[a-f0-9]{64}', image_id)
                or type(db.roles) is not dict or set(db.roles) != set(ROLES)
                or type(db.urls) is not dict or set(db.urls) != set(ROLES)):
            raise ValueError
        database = db.urls['owner'].database
        if not _matches(r'gateway_test_[a-f0-9]{16}', database):
            raise ValueError
        token = database.removeprefix('gateway_test_')
        passwords = {}
        for role in ROLES:
            url = db.urls[role]
            if (db.roles[role] != 'gwt_' + token + '_' + role or not isinstance(url, URL)
                    or url.drivername != 'postgresql+psycopg' or url.host != admin.host or url.port != attested_port
                    or url.database != database or url.username != db.roles[role] or url.query
                    or not _matches(r'[A-Za-z0-9_-]{43}', url.password)):
                raise ValueError
            passwords[role] = url.password
        value = {'schema_version': 1, 'source_digest': source_digest, 'image_id': image_id,
            'registry': {'host': '127.0.0.1', 'port': attested_port, 'database': database,
                'roles': dict(db.roles), 'passwords': passwords, 'admin_password': admin.password}}
        raw = json.dumps(value, separators=(',', ':'), sort_keys=True).encode('ascii')
        if len(raw) > MAXIMUM_DESCRIPTOR:
            raise ValueError
        return raw
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError('Invalid synthetic journey authority') from None


def current_source_digest():
    from scripts.demo_runtime import source_digest, source_inventory
    return source_digest(source_inventory())


def _cleanup(cidfile, deadline):
    try:
        info = cidfile.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65:
            raise ValueError
        identifier = cidfile.read_text(encoding='ascii').strip()
        if not _matches(r'[a-f0-9]{64}', identifier):
            raise ValueError
    except (OSError, UnicodeError, ValueError):
        # Docker may have created the container before losing its CID write.
        # Fail closed; only mandatory outer registered run-label cleanup can
        # reconcile this case. Never claim absence or guess a removal target.
        raise ValueError('Journey cleanup requires its private exact container identity') from None
    # CID came only from this docker run's fresh file in our private directory.
    # Never remove registry, image tags, label matches or arbitrary names.
    try:
        _command(['docker', 'rm', '--force', identifier], deadline, timeout=3, maximum=8192)
    except ValueError:
        pass
    remaining = _command(['docker', 'ps', '--all', '--no-trunc', '--filter', 'id=' + identifier,
        '--format', '{{.ID}}'], deadline, timeout=3, maximum=1024)
    if remaining.strip():
        raise ValueError('Journey container cleanup did not complete')


def run_image_smoke(image_id, run_label):
    """Check exact-image imports without a registry, stdin or any credentials.

    The mandatory outer registered run-label cleanup owns Docker containers if
    the CLI loses contact after creation. This helper never guesses a CID.
    """
    if (not _matches(r'sha256:[a-f0-9]{64}', image_id)
            or not _matches(r'(?:gateway-cube-[a-f0-9]{16}-local|[1-9][0-9]{0,19}-[1-9][0-9]{0,19})', run_label)
            or any(char in str(ROOT) for char in (',', '\n', '\r'))):
        raise ValueError('Exact run-private image smoke identity required')
    command = ['docker', 'run', '--rm', '--pull', 'never', '--platform', 'linux/amd64',
        '--network', 'none', '--label', 'gateway.cube.run=' + run_label,
        '--read-only', '--user', '65532:65532', '--workdir', '/app', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges:true', '--memory', '256m', '--cpus', '1', '--pids-limit', '64',
        '--mount', 'type=bind,src=' + str(ROOT) + ',dst=/app,readonly', image_id, '--smoke']
    try:
        output = _bounded(command, timeout=15, maximum=4096, admit_failure=True)
    except _CommandFailure as error:
        stage = 'execution' if error.image_stage is None else 'image_' + error.image_stage
        raise HostedJourneyFailure(stage, error.journey_error) from None
    if output != 'GATEWAY_BQ_GATEWAY_JOURNEY_SMOKE {"schema_version":1,"status":"passed"}\n':
        raise ValueError('Exact image import smoke receipt required')


def run_hosted_journey(db):
    """Run once, with a 120-second total subprocess budget, or fail closed."""
    deadline = time.monotonic() + OVERALL_SECONDS
    stage = 'configuration'
    try:
        config = configuration()
        stage = 'attestation'
        port = _attest(config, db, deadline - CLEANUP_RESERVE)
        stage = 'source'
        digest = current_source_digest()
        stage = 'descriptor'
        payload = descriptor(db, digest, config.image_id, port)
        if any(char in str(ROOT) for char in (',', '\n', '\r')):
            raise ValueError('Unsupported source mount path')
        with tempfile.TemporaryDirectory(prefix='source-journey-') as directory:
            cidfile = Path(directory) / 'container-id'
            command = ['docker', 'run', '--rm', '--interactive', '--pull', 'never',
                '--platform', 'linux/amd64', '--network', 'container:' + config.registry_container,
                '--cidfile', str(cidfile), '--label', 'gateway.cube.run=' + config.cube_run_label,
                '--read-only', '--user', '65532:65532', '--workdir', '/app',
                '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=256m', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges:true', '--memory', '1g', '--cpus', '1', '--pids-limit', '256',
                '--mount', 'type=bind,src=' + str(ROOT) + ',dst=/app,readonly',
                '--env', 'GATEWAY_SOURCE_TEST_AUTHORIZATION=private-synthetic-source-only', config.image_id]
            stage = 'execution'
            try:
                output = _command(command, deadline - CLEANUP_RESERVE, timeout=RUN_SECONDS, payload=payload, admit_failure=True)
                stage = 'receipt'
                receipt(output, digest, config.image_id)
            finally:
                stage_before_cleanup = stage
                stage = 'cleanup'
                _cleanup(cidfile, deadline)
                stage = stage_before_cleanup
    except Exception as error:
        # Pytest gets only allowlisted stage/error attributes. No exception text,
        # child output or credential-bearing object representation is exposed.
        reason = failure_reason(error)
        if isinstance(error, _CommandFailure):
            reason = error.journey_error
            if stage == 'execution' and error.image_stage is not None:
                stage = 'image_' + error.image_stage
        raise HostedJourneyFailure(stage, reason) from None
