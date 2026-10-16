"""Bounded failure-only diagnostics. Nothing here creates acceptance outputs.

All file paths, phase names and schema choices belong to the trusted controller.
Private diagnostics are observations, not authority to select code or repair proof.
"""
from pathlib import Path
import os
import subprocess

ERRORS = (ValueError, KeyError, TypeError, OSError, RuntimeError, RecursionError, subprocess.SubprocessError)
MAX_BYTES = 16 * 1024
SUPERVISION = {'exit_status', 'timed_out', 'output_limit_exceeded', 'interrupted',
               'descendant_cleanup', 'orphaned_descendants'}


def require(value):
    if not value:
        raise ValueError('Invalid private diagnostic')


def cohort(driver):
    """Only executing environment plus the clean trusted Git checkout can bind a run."""
    env = os.environ
    driver.git_identity(driver.TRUSTED_ROOT, env['GITHUB_WORKFLOW_SHA'])
    identity = {'repository': env['GITHUB_REPOSITORY'], 'workflow_file': driver.WORKFLOW,
        'workflow_ref': env['GITHUB_WORKFLOW_REF'], 'workflow_sha': env['GITHUB_WORKFLOW_SHA'],
        'controller_digest': driver.controller_digest(), 'run_id': env['GITHUB_RUN_ID'],
        'run_attempt': env['GITHUB_RUN_ATTEMPT'], 'source_commit': env['REVIEWED_SOURCE_SHA'],
        'source_tree': '0' * 40, 'source_digest': '0' * 64}
    driver.contract()._identity(identity)
    pin = env['REVIEWED_MANIFEST_SHA256']
    require(driver.HASH64.fullmatch(pin) is not None)
    return {**{key: value for key, value in identity.items()
               if key not in ('source_tree', 'source_digest')}, 'manifest_sha256': pin}


def empty_report(job, who=None, reason='evidence_unavailable'):
    return {'schema_version': 1, 'kind': 'partition_failure', 'status': 'BLOCKED',
        'job_id': job, 'cohort': who, 'context_status': 'unavailable', 'source': None,
        'cleanup_status': 'unavailable', 'reason': reason, 'phases': []}


def emit(driver, report):
    encoded = driver.canonical(report)
    # On overflow no fragment of a large diagnostic is exported.
    if len(encoded) > MAX_BYTES:
        encoded = driver.canonical({'schema_version': 1, 'kind': 'diagnostic_unavailable',
                                    'status': 'BLOCKED', 'reason': 'output_bound'})
    print(encoded.decode('ascii'), flush=True)
    return 1


def phase_row(driver, path, name, cap):
    row = driver.read_private(path, 16 * 1024)
    require(type(row) is dict and set(row) == {'name', 'status', 'cap_seconds', 'elapsed_ms', 'supervision'})
    require(row['name'] == name and row['status'] in ('PASS', 'BLOCKED'))
    actual = row['cap_seconds']
    require(type(actual) is int and (780 <= actual <= cap if name == 'backend_stack' else actual == cap))
    require(type(row['elapsed_ms']) is int and 0 <= row['elapsed_ms'] <= actual * 1000 + 10000)
    control = row['supervision']
    require(type(control) is dict and set(control) == SUPERVISION)
    require(type(control['exit_status']) is int and -255 <= control['exit_status'] <= 255)
    require(all(type(control[key]) is bool for key in SUPERVISION - {'exit_status'}))
    passed = (control['exit_status'] == 0 and control['descendant_cleanup'] and
              not any(control[key] for key in SUPERVISION - {'exit_status', 'descendant_cleanup'}))
    require((row['status'] == 'PASS') == passed)
    return row


def cleanup_status(driver, root, identity, job):
    try:
        row = driver.read_private(root / 'cleanup-receipt.json', 16 * 1024)
        require(type(row) is dict and set(row) == {'identity', 'job_id', 'status', 'cap_seconds',
            'elapsed_ms', 'exact_resources_verified', 'descendants_verified'})
        require(row['identity'] == identity and row['job_id'] == job)
        require(row['status'] in ('PASS', 'BLOCKED') and type(row['cap_seconds']) is int and row['cap_seconds'] == 120)
        require(type(row['elapsed_ms']) is int and 0 <= row['elapsed_ms'] <= 130000)
        require(type(row['exact_resources_verified']) is bool and type(row['descendants_verified']) is bool)
        require((row['status'] == 'PASS') == (row['exact_resources_verified'] and row['descendants_verified']))
        return row['status']
    except FileNotFoundError:
        return 'unavailable'
    except ERRORS:
        return 'invalid'


def selected_nodes(driver, root, job, identity, pin):
    c = driver.contract()
    if job == 'source':
        require(phase_row(driver, root / 'source_collection.json', 'source_collection', 60)['status'] == 'PASS')
        inventory = driver.read_private(root / 'source_collection-nodes.json', 4 * 1024 * 1024)
        require(type(inventory) is list and 1 <= len(inventory) <= 10000)
        nodes = []
        for item in inventory:
            require(type(item) is dict and set(item) == {'nodeid', 'markers', 'fixtures'})
            c._node(item['nodeid'])
            require(len(item['nodeid'].encode()) <= 1024)
            for key in ('markers', 'fixtures'):
                require(type(item[key]) is list and len(item[key]) <= 256)
                require(all(type(value) is str and 0 < len(value) <= 256 and value.isidentifier() for value in item[key]))
                require(len(item[key]) == len(set(item[key])))
            require(bool({'unit', 'contract'} & set(item['markers'])))
            nodes.append(item['nodeid'])
        require(len(nodes) == len(set(nodes)))
        return nodes
    manifest = driver.read_private(root / 'manifest.json', 192 * 1024)
    c.validate_manifest(manifest, source=driver.source_from(identity),
        controller_digest=identity['controller_digest'], expected_sha256=pin)
    return manifest['partitions'][job]


def scoped_evidence(api, path, control, nodes, *, phase):
    progress = phase in ('backend_tests', 'backend_stack')
    value = api.phase_failure_evidence(path, control, progress=progress)
    selected = {node.split('[', 1)[0] for node in nodes}
    try:
        for marker in value['markers']:
            kind = marker['kind']
            require(kind in ({'test'} if phase == 'source_tests' else
                {'test', 'progress', 'journey'} if phase == 'backend_tests' else
                {'test', 'progress', 'journey', 'checkpoint', 'phase'} if phase == 'backend_stack' else set()))
            if kind in ('test', 'progress'):
                require(marker['test'] in selected)
            if kind == 'progress':
                require(progress and marker['collected'] == len(nodes) and 0 <= marker['completed'] <= len(nodes))
            if kind == 'journey':
                require(api.JOURNEY_TEST in selected)
        return value
    except ERRORS:
        return {'reason': value['reason'], 'details_status': 'invalid', 'markers': []}


def report(driver, candidate, root, job, who):
    result = empty_report(job, who)
    try:
        require(who is not None)
        root = driver.checked_directory(root)
        identity = driver.context(candidate, job_id=job)
        driver.contract()._identity(identity)
        ctx = driver.read_private(root / 'context.json', 16 * 1024)
        require(type(ctx) is dict and set(ctx) == {'candidate', 'identity', 'job_id', 'started', 'manifest_sha256'})
        require(ctx['candidate'] == str(candidate) and ctx['identity'] == identity and ctx['job_id'] == job)
        require(ctx['manifest_sha256'] == who['manifest_sha256'])
        require(type(ctx['started']) is int and ctx['started'] == int(os.environ['GATEWAY_INTEGRATION_FIRST_STEP_EPOCH']))
        require({key: identity[key] for key in who if key != 'manifest_sha256'} ==
                {key: value for key, value in who.items() if key != 'manifest_sha256'})
        result.update(context_status='verified', source=driver.source_from(identity),
                      cleanup_status=cleanup_status(driver, root, identity, job))
    except FileNotFoundError:
        return result
    except ERRORS:
        result['context_status'] = 'invalid'
        return result
    # The source manifest is not yet available before source tests. Its digest
    # above is a validated dispatch pin, not a claim about unavailable content.
    try:
        nodes = selected_nodes(driver, root, job, identity, who['manifest_sha256'])
        api = driver.trusted_module('pytest_runtime_diagnostics')
        api.ROOT = Path(candidate)
        api._source_blobs = api._source_commit = None
        api.freeze_source(identity['source_commit'])
    except ERRORS:
        nodes = None; api = None
    policy = driver.contract().SOURCE_PHASES if job == 'source' else driver.contract().BACKEND_PHASES
    for name, cap in policy:
        folder = root / 'backends' if name == 'backend_tests' else root
        row = {'name': name, 'status': 'unavailable', 'cap_seconds': None,
            'elapsed_ms': None, 'supervision': None,
            'failure_evidence': {'reason': 'unavailable', 'details_status': 'unavailable', 'markers': []}}
        try:
            row.update(phase_row(driver, folder / (name + '.json'), name, cap))
            if row['status'] == 'BLOCKED':
                # Fixed reasons do not depend on a log parser or selected tests.
                control = row['supervision']
                reason = next((reason for key, reason in (('timed_out', 'timeout'),
                    ('output_limit_exceeded', 'output_limit'), ('interrupted', 'interrupted'),
                    ('orphaned_descendants', 'orphaned_descendants')) if control[key]), 'process_exit')
                if not control['descendant_cleanup']: reason = 'unverified_cleanup'
                row['failure_evidence']['reason'] = reason
                if api is not None and nodes is not None and name in ('source_tests', 'backend_tests', 'backend_stack'):
                    row['failure_evidence'] = scoped_evidence(api, folder / (name + '.log'),
                        control, nodes, phase=name)
        except FileNotFoundError:
            pass
        except ERRORS:
            row.update(status='invalid', cap_seconds=None, elapsed_ms=None, supervision=None)
        result['phases'].append(row)
    # Source changes while reading private observations invalidate every marker.
    try:
        require(driver.context(candidate, job_id=job) == identity)
    except ERRORS:
        return {**empty_report(job, who), 'context_status': 'invalid'}
    return result


def aggregate_failure(driver, who, label, reason):
    require(label in ('context', 'source_manifest', 'source_receipt', *driver.contract().LANES))
    require(reason in ('context_invalid', 'transport_unavailable', 'transport_invalid', 'manifest_invalid'))
    return emit(driver, {'schema_version': 1, 'kind': 'aggregate_failure', 'status': 'FAIL',
        'cohort': who, 'input': label, 'reason': reason})
