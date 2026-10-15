"""Pure offline admission checks; these never prove a runtime backend pass."""
import base64
import copy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]
SOURCE = {'commit': 'a' * 40, 'tree': 'b' * 40, 'digest': 'c' * 64}
CONTROLLER = 'd' * 64


@pytest.fixture
def contract():
    path = ROOT / 'scripts/ci_partition_contract.py'
    assert path.is_file(), 'Pure CI partition contract is missing'
    return importlib.import_module('scripts.ci_partition_contract')


def inventory(c):
    rows = [{'nodeid': node, 'markers': ['cube'], 'fixtures': ['cube_stack']}
            for node in c.RUNTIME_NODES]
    for lane, paths in c.SQL_FILES.items():
        rows += [{'nodeid': path + '::test_partition_case', 'markers': ['postgres'], 'fixtures': []}
                 for path in paths]
    return rows


def manifest(c):
    return c.build_manifest(SOURCE, CONTROLLER, inventory(c))


def identity(c):
    return {'repository': 'Sean-Koval/spaceport', 'source_commit': SOURCE['commit'],
            'source_tree': SOURCE['tree'], 'source_digest': SOURCE['digest'],
            'workflow_file': c.WORKFLOW_FILE,
            'workflow_ref': 'Sean-Koval/spaceport/' + c.WORKFLOW_FILE + '@refs/heads/main',
            'workflow_sha': 'e' * 40, 'controller_digest': CONTROLLER,
            'run_id': '123456789', 'run_attempt': '1'}


def receipt(c, m, job):
    who = identity(c)
    nodes = [] if job == 'source' else m['partitions'][job]
    return {'schema_version': 1, 'identity': who, 'job_id': job, 'status': 'PASS',
            'manifest_sha256': c.sha256(m),
            'tools': {'python': '3.12.14', 'uv': '0.12.23', 'node': '24.19.0', 'npm': '11.9.0',
                      'docker': '28.5.1', 'compose': 'v2.40.0', 'runner_image': 'ubuntu24_20261008.1'},
            'test_summary': {'count': 3925 if job == 'source' else len(nodes), 'nodes': list(nodes),
                             'identities_sha256': c.sha256(['locally checked source identities']) if job == 'source' else c.sha256(nodes),
                             'junit_sha256': 'f' * 64,
                             'images_sha256': None if job == 'source' else '1' * 64,
                             'endpoints_sha256': None if job == 'source' else '2' * 64},
            'phases': [{'name': name, 'status': 'PASS', 'cap_seconds': cap, 'elapsed_ms': 1,
                        'supervision': {'exit_status': 0, 'timed_out': False, 'output_limit_exceeded': False,
                                        'interrupted': False, 'descendant_cleanup': True, 'orphaned_descendants': False}}
                       for name, cap in (c.SOURCE_PHASES if job == 'source' else c.BACKEND_PHASES)],
            'cleanup': {'identity': copy.deepcopy(who), 'job_id': job, 'status': 'PASS', 'cap_seconds': 120,
                        'elapsed_ms': 1, 'exact_resources_verified': True, 'descendants_verified': True},
            'source_rechecked': True, 'controller_rechecked': True}


def validate(c, value, m, job='sql_owners'):
    return c.validate_receipt(value, identity=identity(c), manifest=m, job_id=job)


def junit(nodes):
    suites = ET.Element('testsuites')
    suite = ET.SubElement(suites, 'testsuite', tests=str(len(nodes)), failures='0', errors='0', skipped='0')
    for node in nodes:
        address, bracket, parameters = node.partition('[')
        parts = address.split('::')
        classname = parts[0][:-3].replace('/', '.') + ('.' + '.'.join(parts[1:-1]) if len(parts) > 2 else '')
        ET.SubElement(suite, 'testcase', classname=classname, name=parts[-1] + bracket + parameters, time='0.001')
    return ET.tostring(suites)


def slots_for_raw(c, raw, kind='manifest'):
    encoded = base64.b64encode(raw).decode('ascii')
    count = (len(encoded) + 32767) // 32768
    return {'count': str(count), 'encoded_length': str(len(encoded)), 'decoded_length': str(len(raw)),
            'sha256': hashlib.sha256(raw).hexdigest(),
            **{'chunk_' + str(i): encoded[i * 32768:(i + 1) * 32768]
               for i in range(8 if kind == 'manifest' else 3)}}


def test_canonical_json_and_hash_are_deterministic(contract):
    c = contract
    assert c.canonical_bytes({'z': '雪', 'a': [1, True, None]}) == b'{"a":[1,true,null],"z":"\\u96ea"}'
    assert c.sha256({'b': 2, 'a': 1}) == hashlib.sha256(b'{"a":1,"b":2}').hexdigest()
    assert c.load_json(b' {"a":1} ', limit=20) == {'a': 1}


@pytest.mark.parametrize('raw', [b'{"a":1,"a":2}', b'{"a":{"b":1,"b":2}}', b'NaN', b'Infinity', b'-Infinity',
                               b'1e9999', b'"\\ud800"', b'\xff', b'[] trailing', b'9' * 100, b'[' * 34 + b']' * 34])
def test_json_rejects_ambiguous_unbounded_or_nonfinite_values(contract, raw):
    with pytest.raises(contract.ContractError, match='^Invalid CI partition evidence$'):
        contract.load_json(raw, limit=4096)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), {1: 'a'}, {'x': {1, 2}}, ('a',), b'a'])
def test_canonical_rejects_non_json_types(contract, value):
    with pytest.raises(contract.ContractError):
        contract.canonical_bytes(value)


def test_json_exact_limits_and_cycles(contract):
    assert contract.load_json(b'{}', limit=2) == {}
    for raw, limit in [(b'{}', 1), (b'{}', True), ('{}', 2), (b'{}', -1)]:
        with pytest.raises(contract.ContractError):
            contract.load_json(raw, limit=limit)
    value = []; value.append(value)
    with pytest.raises(contract.ContractError):
        contract.canonical_bytes(value)


def test_manifest_determinism_full_union_and_fixed_lanes(contract):
    c = contract; rows = inventory(c); m = manifest(c)
    assert c.build_manifest(SOURCE, CONTROLLER, list(reversed(rows))) == m
    assert tuple(m['partitions']) == c.LANES
    assert m['selection'] == ['-m', 'postgres or cube']
    nodes = sorted(row['nodeid'] for row in rows)
    assert m['total'] == len(nodes) == sum(m['counts'].values())
    assert m['inventory_sha256'] == c.sha256(nodes)
    assert m['partitions']['runtime_process_socket'] == sorted(c.RUNTIME_NODES)
    assert c.validate_manifest(m, source=SOURCE, controller_digest=CONTROLLER, expected_sha256=c.sha256(m)) == m


@pytest.mark.parametrize('mutation', ['duplicate', 'unknown_file', 'same_basename', 'empty_lane', 'unknown_runtime',
                                     'missing_runtime', 'missing_runtime_marker', 'bad_marker', 'extra_row_key', 'missing_postgres'])
def test_manifest_rejects_unreviewed_or_ambiguous_inventory(contract, mutation):
    c = contract; rows = inventory(c)
    if mutation == 'duplicate': rows.append(copy.deepcopy(rows[0]))
    if mutation == 'unknown_file': rows.append({'nodeid': 'tests/integration/test_surprise.py::test_a', 'markers': ['postgres'], 'fixtures': []})
    if mutation == 'same_basename': rows[-1]['nodeid'] = rows[-1]['nodeid'].replace('tests/', 'tests/other/', 1)
    if mutation == 'empty_lane': rows = [r for r in rows if r['nodeid'].split('::')[0] not in c.SQL_FILES['sql_owners']]
    if mutation == 'unknown_runtime': rows[-1]['markers'] = ['cube']
    if mutation == 'missing_runtime': rows.pop(0)
    if mutation == 'missing_runtime_marker':
        row = next(r for r in rows if r['nodeid'] not in c.RUNTIME_EXCEPTIONS)
        row.update(markers=['postgres'], fixtures=[])
    if mutation == 'bad_marker': rows[-1]['markers'] = ['postgres', 'bad\nmarker']
    if mutation == 'extra_row_key': rows[-1]['command'] = 'echo secret'
    if mutation == 'missing_postgres': rows[-1]['markers'] = []
    with pytest.raises(c.ContractError): c.build_manifest(SOURCE, CONTROLLER, rows)


@pytest.mark.parametrize('suffix', ['\nsecret', '\x00', '\r', '\x1f', '\x7f'])
def test_manifest_rejects_node_control_characters(contract, suffix):
    rows = inventory(contract); rows[-1]['nodeid'] += suffix
    with pytest.raises(contract.ContractError): contract.build_manifest(SOURCE, CONTROLLER, rows)


@pytest.mark.parametrize('node', ['/tests/integration/test_registry.py::test_a', '../tests/integration/test_registry.py::test_a',
                                 'tests/../integration/test_registry.py::test_a', '-tests/integration/test_registry.py::test_a',
                                 'tests/integration/test_registry.py', 'tests/integration/test_registry.py[::1]',
                                 'tests/integration/test_registry.py::', 'tests/integration/test_registry.py::test_a[' + 'x' * 1024 + ']'],
                         ids=['absolute', 'parent-prefix', 'parent-component', 'option-prefix',
                              'no-test', 'parameter-before-test', 'empty-test', 'oversized-parameter'])
def test_manifest_rejects_unsafe_addresses(contract, node):
    rows = inventory(contract); rows[-1]['nodeid'] = node
    with pytest.raises(contract.ContractError): contract.build_manifest(SOURCE, CONTROLLER, rows)


def test_parameter_suffix_is_preserved_as_one_node(contract):
    c = contract; rows = inventory(c)
    node = 'tests/integration/test_registry.py::TestCases::test_value[quote"[\'::]tail-雪-/../--option]'
    rows.append({'nodeid': node, 'markers': ['postgres'], 'fixtures': []})
    m = c.build_manifest(SOURCE, CONTROLLER, rows)
    assert node in m['partitions']['sql_registry_results']
    assert c.junit_summary(junit([node]), (node,))['nodes'] == [node]


@pytest.mark.parametrize('field,value', [('schema_version', True), ('selection', ['-m', 'postgres']), ('total', True),
                                       ('controller_digest', 'f' * 64), ('inventory_sha256', 'f' * 64), ('extra', 'secret')])
def test_manifest_exact_schema_types_and_context(contract, field, value):
    c = contract; m = manifest(c); m[field] = value
    with pytest.raises(c.ContractError): c.validate_manifest(m, source=SOURCE, controller_digest=CONTROLLER, expected_sha256=c.sha256(m))


def test_manifest_rechecks_partition_membership_counts_hash_and_source(contract):
    c = contract; good = manifest(c)
    for change in ('overlap', 'wrong_lane', 'count', 'bool_count', 'unsorted', 'duplicate', 'source', 'unknown_lane', 'missing_lane', 'missing_runtime'):
        m = copy.deepcopy(good)
        if change == 'overlap': m['partitions']['sql_execution'].append(m['partitions']['sql_owners'][0])
        if change == 'wrong_lane': m['partitions']['sql_execution'].append(m['partitions']['sql_owners'].pop())
        if change == 'count': m['counts']['sql_owners'] += 1
        if change == 'bool_count': m['counts']['sql_owners'] = True
        if change == 'unsorted': m['partitions']['sql_owners'].reverse()
        if change == 'duplicate': m['partitions']['sql_owners'].append(m['partitions']['sql_owners'][0])
        if change == 'source': m['source']['commit'] = '1' * 40
        if change == 'unknown_lane': m['partitions']['extra'] = []
        if change == 'missing_lane': del m['partitions']['sql_owners']
        if change == 'missing_runtime': m['partitions']['runtime_process_socket'].pop()
        with pytest.raises(c.ContractError): c.validate_manifest(m, source=SOURCE, controller_digest=CONTROLLER, expected_sha256=c.sha256(m))
    with pytest.raises(c.ContractError): c.validate_manifest(good, source=SOURCE, controller_digest=CONTROLLER, expected_sha256='0' * 64)


@pytest.mark.parametrize('kind', ['manifest', 'receipt'])
def test_transport_exact_chunk_roundtrip(contract, kind):
    c = contract; value = manifest(c) if kind == 'manifest' else receipt(c, manifest(c), 'sql_owners')
    encoded = c.encode_transport(value, kind=kind)
    assert c.decode_transport(encoded, kind=kind) == value
    assert set(encoded) == {'count', 'encoded_length', 'decoded_length', 'sha256'} | {'chunk_' + str(i) for i in range(8 if kind == 'manifest' else 3)}
    assert all(type(v) is str for v in encoded.values())
    assert all(len(encoded['chunk_' + str(i)]) == 32768 for i in range(int(encoded['count']) - 1))


@pytest.mark.parametrize('mutation', ['extra', 'missing', 'unused', 'bool_count', 'leading_zero', 'negative', 'count_overflow',
                                     'length', 'decoded_length', 'hash', 'invalid_base64', 'newline', 'truncated', 'reordered'])
def test_transport_rejects_missing_duplicate_reordered_or_malformed_slots(contract, mutation):
    c = contract; value = {'payload': 'x' * 40000}; s = c.encode_transport(value, kind='manifest')
    if mutation == 'extra': s['chunk_8'] = ''
    if mutation == 'missing': del s['chunk_7']
    if mutation == 'unused': s['chunk_7'] = 'YQ=='
    if mutation == 'bool_count': s['count'] = True
    if mutation == 'leading_zero': s['count'] = '02'
    if mutation == 'negative': s['decoded_length'] = '-1'
    if mutation == 'count_overflow': s['count'] = '9'
    if mutation == 'length': s['encoded_length'] = '1'
    if mutation == 'decoded_length': s['decoded_length'] = '1'
    if mutation == 'hash': s['sha256'] = '0' * 64
    if mutation == 'invalid_base64': s['chunk_0'] = '!' + s['chunk_0'][1:]
    if mutation == 'newline': s['chunk_0'] = '\n' + s['chunk_0'][1:]
    if mutation == 'truncated': s['chunk_0'] = s['chunk_0'][:-1]
    if mutation == 'reordered': s['chunk_0'], s['chunk_1'] = s['chunk_1'], s['chunk_0']
    with pytest.raises(c.ContractError): c.decode_transport(s, kind='manifest')


@pytest.mark.parametrize('raw', [b'{"b":2,"a":1}', b'{"a": 1}', b'{"a":1}\n', b'{"a":1,"a":1}', b'{"x":"\xc3\xa9"}', b'NaN'])
def test_transport_rejects_noncanonical_json_even_with_valid_hash(contract, raw):
    with pytest.raises(contract.ContractError): contract.decode_transport(slots_for_raw(contract, raw), kind='manifest')


def test_transport_caps_kinds_and_noncanonical_base64(contract):
    c = contract
    for kind, cap in [('manifest', 192 * 1024), ('receipt', 64 * 1024)]:
        value = {'x': 'a' * (cap - 8)}
        assert len(c.canonical_bytes(value)) == cap
        assert c.decode_transport(c.encode_transport(value, kind=kind), kind=kind) == value
        with pytest.raises(c.ContractError): c.encode_transport({'x': 'a' * cap}, kind=kind)
        with pytest.raises(c.ContractError): c.decode_transport(slots_for_raw(c, c.canonical_bytes({'x': 'a' * cap}), kind), kind=kind)
    for bad in ['unknown', None, True, []]:
        with pytest.raises(c.ContractError): c.encode_transport({}, kind=bad)
        with pytest.raises(c.ContractError): c.decode_transport({}, kind=bad)
    s = slots_for_raw(c, b'{}'); assert s['chunk_0'] == 'e30='
    s['chunk_0'] = 'e31='  # Same decoded bytes, nonzero pad bits.
    with pytest.raises(c.ContractError): c.decode_transport(s, kind='manifest')


def test_junit_exact_nodes_digest_and_class_parameter_projection(contract):
    c = contract
    nodes = ('tests/unit/test_example.py::TestCases::test_one[a::b]', 'tests/unit/test_example.py::test_two[é::雪]')
    raw = junit(nodes); out = c.junit_summary(raw, nodes)
    assert out == {'count': 2, 'nodes': list(nodes), 'identities_sha256': c.sha256(list(nodes)), 'junit_sha256': hashlib.sha256(raw).hexdigest()}


@pytest.mark.parametrize('mutation', ['zero', 'duplicate', 'missing', 'extra', 'skipped', 'failure', 'error', 'suite_count',
                                     'suite_skip', 'missing_counts', 'nested_suite', 'wrong_root', 'dtd', 'entity', 'oversize',
                                     'root_count', 'unexpected_case_parent', 'missing_identity', 'duplicate_expected'])
def test_junit_rejects_any_nonexact_or_unsafe_evidence(contract, mutation):
    c = contract; nodes = ('tests/unit/test_example.py::test_one', 'tests/unit/test_example.py::test_two')
    root = ET.fromstring(junit(nodes)); suite = root[0]
    raw = None
    if mutation == 'zero': raw = junit([])
    if mutation == 'duplicate': suite.append(copy.deepcopy(suite[0])); suite.set('tests', '3')
    if mutation == 'missing': suite.remove(suite[0]); suite.set('tests', '1')
    if mutation == 'extra': ET.SubElement(suite, 'testcase', classname='tests.unit.test_example', name='test_extra'); suite.set('tests', '3')
    if mutation in ('skipped', 'failure', 'error'): ET.SubElement(suite[0], mutation)
    if mutation == 'suite_count': suite.set('tests', '3')
    if mutation == 'suite_skip': suite.set('skipped', '1')
    if mutation == 'missing_counts': del suite.attrib['failures']
    if mutation == 'nested_suite': ET.SubElement(suite, 'testsuite', tests='0', failures='0', errors='0', skipped='0')
    if mutation == 'wrong_root': root.tag = 'receipt'
    if mutation == 'dtd': raw = b'<!DOCTYPE testsuites><testsuites/>'
    if mutation == 'entity': raw = b'<!DOCTYPE testsuites [<!ENTITY x "secret">]><testsuites>&x;</testsuites>'
    if mutation == 'oversize': raw = b' ' * (16 * 1024 * 1024 + 1)
    if mutation == 'root_count': root.set('tests', '100')
    if mutation == 'unexpected_case_parent': root.append(copy.deepcopy(suite[0]))
    if mutation == 'missing_identity': del suite[0].attrib['name']
    if mutation == 'duplicate_expected': nodes = (nodes[0], nodes[0])
    with pytest.raises(c.ContractError): c.junit_summary(ET.tostring(root) if raw is None else raw, nodes)


@pytest.mark.parametrize('job', ['source', 'sql_owners', 'sql_execution', 'sql_publishing', 'sql_registry_results', 'runtime_process_socket'])
def test_valid_receipt_has_all_same_cohort_evidence(contract, job):
    c = contract; m = manifest(c); r = receipt(c, m, job)
    assert validate(c, r, m, job) == r


@pytest.mark.parametrize('field', ['repository', 'source_commit', 'source_tree', 'source_digest', 'workflow_file', 'workflow_ref',
                                  'workflow_sha', 'controller_digest', 'run_id', 'run_attempt'])
def test_receipt_rejects_each_cross_cohort_identity_field(contract, field):
    c = contract; m = manifest(c); r = receipt(c, m, 'sql_owners')
    r['identity'][field] += 'x'
    with pytest.raises(c.ContractError): validate(c, r, m)
    r = receipt(c, m, 'sql_owners'); r['cleanup']['identity'][field] += 'x'
    with pytest.raises(c.ContractError): validate(c, r, m)


@pytest.mark.parametrize('mutation', ['extra', 'missing', 'schema_bool', 'wrong_job', 'status_fail', 'status_blocked', 'wrong_manifest',
                                     'no_source_recheck', 'no_controller_recheck', 'count_bool', 'count_wrong', 'nodes_missing',
                                     'nodes_duplicate', 'nodes_extra', 'nodes_unsorted', 'identities_digest', 'junit_digest',
                                     'images_null', 'endpoints_null', 'phase_missing', 'phase_extra', 'phase_reorder', 'phase_fail',
                                     'cap_increased', 'elapsed_negative', 'elapsed_overflow', 'elapsed_bool', 'exit_bool', 'exit_nonzero',
                                     'timed_out', 'output_limit_exceeded', 'interrupted', 'descendant_cleanup', 'orphaned_descendants',
                                     'cleanup_missing', 'cleanup_job', 'cleanup_fail', 'cleanup_cap', 'cleanup_elapsed',
                                     'cleanup_resources', 'cleanup_descendants'])
def test_receipt_rejects_incomplete_failed_or_forged_proof(contract, mutation):
    c = contract; m = manifest(c); r = receipt(c, m, 'sql_owners'); s = r['test_summary']; p = r['phases'][0]; clean = r['cleanup']
    if mutation == 'extra': r['raw_log'] = 'secret'
    if mutation == 'missing': del r['status']
    if mutation == 'schema_bool': r['schema_version'] = True
    if mutation == 'wrong_job': r['job_id'] = 'source'
    if mutation == 'status_fail': r['status'] = 'FAIL'
    if mutation == 'status_blocked': r['status'] = 'BLOCKED'
    if mutation == 'wrong_manifest': r['manifest_sha256'] = '0' * 64
    if mutation == 'no_source_recheck': r['source_rechecked'] = False
    if mutation == 'no_controller_recheck': r['controller_rechecked'] = 1
    if mutation == 'count_bool': s['count'] = True
    if mutation == 'count_wrong': s['count'] += 1
    if mutation == 'nodes_missing': s['nodes'].pop()
    if mutation == 'nodes_duplicate': s['nodes'].append(s['nodes'][0])
    if mutation == 'nodes_extra': s['nodes'].append('tests/integration/test_registry.py::test_extra')
    if mutation == 'nodes_unsorted': s['nodes'].reverse()
    if mutation == 'identities_digest': s['identities_sha256'] = '0' * 64
    if mutation == 'junit_digest': s['junit_sha256'] = 'secret'
    if mutation == 'images_null': s['images_sha256'] = None
    if mutation == 'endpoints_null': s['endpoints_sha256'] = None
    if mutation == 'phase_missing': r['phases'].pop()
    if mutation == 'phase_extra': r['phases'].append(copy.deepcopy(p))
    if mutation == 'phase_reorder': r['phases'].reverse()
    if mutation == 'phase_fail': p['status'] = 'FAIL'
    if mutation == 'cap_increased': p['cap_seconds'] += 1
    if mutation == 'elapsed_negative': p['elapsed_ms'] = -1
    if mutation == 'elapsed_overflow': p['elapsed_ms'] = p['cap_seconds'] * 1000 + 10001
    if mutation == 'elapsed_bool': p['elapsed_ms'] = True
    if mutation == 'exit_bool': p['supervision']['exit_status'] = False
    if mutation == 'exit_nonzero': p['supervision']['exit_status'] = 1
    if mutation in ('timed_out', 'output_limit_exceeded', 'interrupted', 'orphaned_descendants'): p['supervision'][mutation] = True
    if mutation == 'descendant_cleanup': p['supervision']['descendant_cleanup'] = False
    if mutation == 'cleanup_missing': del r['cleanup']
    if mutation == 'cleanup_job': clean['job_id'] = 'sql_execution'
    if mutation == 'cleanup_fail': clean['status'] = 'FAIL'
    if mutation == 'cleanup_cap': clean['cap_seconds'] = 121
    if mutation == 'cleanup_elapsed': clean['elapsed_ms'] = 120001
    if mutation == 'cleanup_resources': clean['exact_resources_verified'] = False
    if mutation == 'cleanup_descendants': clean['descendants_verified'] = False
    with pytest.raises(c.ContractError): validate(c, r, m)


def test_source_receipt_local_summary_and_admitted_stack_caps(contract):
    c = contract; m = manifest(c)
    for field, value in [('count', 0), ('count', True), ('nodes', ['test']), ('images_sha256', '1' * 64), ('endpoints_sha256', '2' * 64)]:
        r = receipt(c, m, 'source'); r['test_summary'][field] = value
        with pytest.raises(c.ContractError): validate(c, r, m, 'source')
    for cap in (780, 840, 900):
        r = receipt(c, m, 'sql_owners'); r['phases'][2]['cap_seconds'] = cap
        assert validate(c, r, m) == r
    for cap in (779, 901, True):
        r = receipt(c, m, 'sql_owners'); r['phases'][2]['cap_seconds'] = cap
        with pytest.raises(c.ContractError): validate(c, r, m)


def test_aggregate_accepts_exact_six_same_run_successes(contract):
    c = contract; m = manifest(c); src = receipt(c, m, 'source'); lanes = [receipt(c, m, j) for j in c.LANES]
    out = c.aggregate(m, src, list(reversed(lanes)), {j: 'success' for j in ('source', *c.LANES)}, identity=identity(c), expected_manifest_sha256=c.sha256(m))
    assert out == {'schema_version': 1, 'identity': identity(c), 'status': 'PASS', 'manifest_sha256': c.sha256(m),
                   'source_count': 3925, 'backend_count': m['total'], 'lane_counts': m['counts'],
                   'lane_digests': {j: c.sha256(m['partitions'][j]) for j in c.LANES}, 'failures': []}


@pytest.mark.parametrize('mutation', ['missing_lane', 'duplicate_lane', 'unknown_lane', 'map_instead_of_list', 'stale_attempt',
                                     'source_fail', 'cleanup_fail', 'missing_job', 'extra_job', 'skipped', 'cancelled', 'failure', 'unknown_result'])
def test_aggregate_fails_closed_for_all_incomplete_cohorts(contract, mutation):
    c = contract; m = manifest(c); src = receipt(c, m, 'source'); lanes = [receipt(c, m, j) for j in c.LANES]
    results = {j: 'success' for j in ('source', *c.LANES)}
    if mutation == 'missing_lane': lanes.pop()
    if mutation == 'duplicate_lane': lanes[-1] = copy.deepcopy(lanes[0])
    if mutation == 'unknown_lane': lanes[-1]['job_id'] = 'secret_unknown'
    if mutation == 'map_instead_of_list': lanes = {r['job_id']: r for r in lanes}
    if mutation == 'stale_attempt': lanes[-1]['identity']['run_attempt'] = '2'
    if mutation == 'source_fail': src['status'] = 'FAIL'
    if mutation == 'cleanup_fail': lanes[-1]['cleanup']['status'] = 'FAIL'
    if mutation == 'missing_job': del results['source']
    if mutation == 'extra_job': results['other'] = 'success'
    if mutation in ('skipped', 'cancelled', 'failure', 'unknown_result'): results['sql_owners'] = mutation
    out = c.aggregate(m, src, lanes, results, identity=identity(c), expected_manifest_sha256=c.sha256(m))
    assert out['status'] == 'FAIL' and out['failures']
    assert set(out['failures']) <= {'SOURCE_RECEIPT_INVALID', 'LANE_RECEIPTS_INVALID', 'JOB_RESULTS_INVALID'}
    assert 'secret_unknown' not in c.canonical_bytes(out).decode()


def test_aggregate_cannot_take_expected_identity_from_receipts(contract):
    c = contract; m = manifest(c); who = identity(c); who['run_attempt'] = '2'
    out = c.aggregate(m, receipt(c, m, 'source'), [receipt(c, m, j) for j in c.LANES],
                      {j: 'success' for j in ('source', *c.LANES)}, identity=who, expected_manifest_sha256=c.sha256(m))
    assert out['status'] == 'FAIL'
    assert out['identity']['run_attempt'] == '2'


def test_contract_loads_under_isolated_stdlib_without_candidate_imports(contract, tmp_path):
    for name in ('scripts', 'json', 'hashlib', 'base64', 'sitecustomize'):
        (tmp_path / (name + '.py')).write_text('raise RuntimeError("candidate shadow executed")\n')
    code = 'import importlib.util; s=importlib.util.spec_from_file_location("trusted_contract",' + repr(str(ROOT / 'scripts/ci_partition_contract.py')) + '); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); assert m.sha256({})'
    result = subprocess.run([sys.executable, '-I', '-S', '-c', code], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_junit_malformed_expected_node_types_have_fixed_errors(contract):
    raw = junit(['tests/unit/test_example.py::test_one'])
    for expected in [('tests/unit/test_example.py::test_one', 1), ([],), (None,), (), ['tests/unit/test_example.py::test_one']]:
        with pytest.raises(contract.ContractError, match='^Invalid CI partition evidence$'):
            contract.junit_summary(raw, expected)


@pytest.mark.parametrize('bad', [None, True, 0, 'secret', [], {}])
def test_public_validators_reject_wrong_top_level_types(contract, bad):
    c = contract; m = manifest(c)
    with pytest.raises(c.ContractError): c.build_manifest(SOURCE, CONTROLLER, bad)
    with pytest.raises(c.ContractError): c.validate_manifest(bad, source=SOURCE, controller_digest=CONTROLLER, expected_sha256=c.sha256(m))
    with pytest.raises(c.ContractError): c.decode_transport(bad, kind='manifest')
    with pytest.raises(c.ContractError): validate(c, bad, m)


@pytest.mark.parametrize('path', [(), ('identity',), ('tools',), ('test_summary',), ('cleanup',), ('cleanup', 'identity'),
                                 ('phases', 0), ('phases', 0, 'supervision')])
@pytest.mark.parametrize('mode', ['missing', 'extra'])
def test_receipt_every_nested_schema_is_closed(contract, path, mode):
    c = contract; m = manifest(c); value = receipt(c, m, 'sql_owners'); target = value
    for key in path: target = target[key]
    if mode == 'missing': del target[next(iter(target))]
    else: target['unknown_private_value'] = 'secret'
    with pytest.raises(c.ContractError, match='^Invalid CI partition evidence$'): validate(c, value, m)


@pytest.mark.parametrize('field,value', [('run_id', '0'), ('run_id', '01'), ('run_id', True), ('run_attempt', '0'),
                                       ('run_attempt', '2' * 30), ('repository', 'owner/repo\nsecret'),
                                       ('workflow_ref', 'Sean-Koval/spaceport/.github/workflows/other.yml@refs/heads/main'),
                                       ('workflow_sha', 'A' * 40), ('source_commit', 'a' * 39)])
def test_aggregate_rejects_malformed_own_context_instead_of_exporting_it(contract, field, value):
    c = contract; m = manifest(c); who = identity(c); who[field] = value
    with pytest.raises(c.ContractError):
        c.aggregate(m, receipt(c, m, 'source'), [receipt(c, m, j) for j in c.LANES],
                    {j: 'success' for j in ('source', *c.LANES)}, identity=who, expected_manifest_sha256=c.sha256(m))


def test_collection_limits_duplicates_and_all_runtime_requirements(contract):
    c = contract; rows = inventory(c)
    for node in c.RUNTIME_NODES:
        with pytest.raises(c.ContractError):
            c.build_manifest(SOURCE, CONTROLLER, [r for r in rows if r['nodeid'] != node])
    for key, value in [('markers', ['postgres', 'postgres']), ('fixtures', ['request', 'request']),
                       ('markers', ['a'] * 129), ('fixtures', [None]), ('fixtures', {})]:
        changed = copy.deepcopy(rows); changed[-1][key] = value
        with pytest.raises(c.ContractError): c.build_manifest(SOURCE, CONTROLLER, changed)
    with pytest.raises(c.ContractError): c.build_manifest(SOURCE, CONTROLLER, rows * 100)


def test_junit_multiple_suites_and_suite_root_are_supported(contract):
    c = contract; nodes = ('tests/unit/test_example.py::test_one', 'tests/unit/test_example.py::test_two')
    root = ET.Element('testsuites', tests='2', failures='0', errors='0', skipped='0')
    root.append(ET.fromstring(junit([nodes[0]]))[0]); root.append(ET.fromstring(junit([nodes[1]]))[0])
    assert c.junit_summary(ET.tostring(root), nodes)['count'] == 2
    assert c.junit_summary(ET.tostring(root[0]), nodes[:1])['count'] == 1


def test_junit_rejects_hidden_outcomes_utf16_and_depth_bombs(contract):
    c = contract; nodes = ('tests/unit/test_example.py::test_one',)
    tree = ET.fromstring(junit(nodes)); props = ET.SubElement(tree[0][0], 'properties'); ET.SubElement(props, 'skipped')
    payloads = [ET.tostring(tree), junit(nodes).decode().encode('utf-16'), b'<testsuites>' + b'<x>' * 34 + b'</x>' * 34 + b'</testsuites>']
    for raw in payloads:
        with pytest.raises(c.ContractError): c.junit_summary(raw, nodes)


def test_embedded_reviewed_policy_is_complete_and_uses_full_paths(contract):
    c = contract
    policy = {'sql_files': c.SQL_FILES, 'runtime_nodes': c.RUNTIME_NODES, 'runtime_exceptions': c.RUNTIME_EXCEPTIONS}
    # Reviewed 726-node proposal, with the whole-file Domo lifecycle rebalance.
    serialized = json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()
    assert hashlib.sha256(serialized).hexdigest() == '983a165e542c7ba9cf30526dc3b325a84eec9f003132bbd67c7dfb6501bbe031'
    assert len(c.RUNTIME_NODES) == 64 and len(c.RUNTIME_EXCEPTIONS) == 7
    paths = [path for paths in c.SQL_FILES.values() for path in paths]
    assert len(paths) == len(set(paths)) == 39
    assert all(path.startswith(('tests/integration/', 'tests/security/')) for path in paths)


def test_bad_nested_json_types_never_escape_as_raw_exceptions(contract):
    c = contract; m = manifest(c); good = receipt(c, m, 'sql_owners')

    def paths(value, prefix=()):
        if type(value) is dict:
            for key, child in value.items():
                yield prefix + (key,)
                yield from paths(child, prefix + (key,))
        elif type(value) is list:
            for index, child in enumerate(value):
                yield prefix + (index,)
                yield from paths(child, prefix + (index,))

    for path in paths(good):
        for wrong in [None, True, -1, {}, [], 'untrusted raw content\n']:
            value = copy.deepcopy(good); target = value
            for key in path[:-1]: target = target[key]
            target[path[-1]] = wrong
            try:
                validate(c, value, m)
            except c.ContractError as error:
                assert str(error) == 'Invalid CI partition evidence'
            else:
                assert value == good


@pytest.mark.parametrize('field', ['python', 'uv', 'node', 'npm'])
def test_receipt_requires_exact_reviewed_tool_pins(contract, field):
    c = contract; m = manifest(c)
    for version in ['0.0.0', '', True, None, 'secret\nvalue', 'x' * 129]:
        value = receipt(c, m, 'sql_owners'); value['tools'][field] = version
        with pytest.raises(c.ContractError): validate(c, value, m)


@pytest.mark.parametrize('field', ['docker', 'compose', 'runner_image'])
@pytest.mark.parametrize('version', ['', ' ', True, None, 1, [], {}, 'x' * 129, 'version\nsecret', 'release/secret', 'release;secret', 'release雪'])
def test_receipt_rejects_unbounded_or_unsafe_runner_tool_versions(contract, field, version):
    c = contract; m = manifest(c); value = receipt(c, m, 'sql_owners'); value['tools'][field] = version
    with pytest.raises(c.ContractError): validate(c, value, m)


def test_receipt_runner_tools_are_bounded_but_not_cross_job_identity(contract):
    c = contract; m = manifest(c); src = receipt(c, m, 'source'); lanes = [receipt(c, m, j) for j in c.LANES]
    for index, value in enumerate([src, *lanes]):
        value['tools'].update(docker='v28.0.' + str(index), compose='v2.40.' + str(index), runner_image='a' * 127 + str(index))
    out = c.aggregate(m, src, lanes, {j: 'success' for j in ('source', *c.LANES)}, identity=identity(c), expected_manifest_sha256=c.sha256(m))
    assert out['status'] == 'PASS'
    assert 'tools' not in out['identity']


def test_domo_lifecycle_file_moves_whole_to_registry_results(contract):
    c = contract; path = 'tests/integration/test_domo_lifecycle_edges.py'
    rows = inventory(c)
    names = ['test_one_domo_post_survives_reclaim_and_late_exact_observation',
             'test_revocation_cannot_turn_started_send_into_no_io_proof[False]',
             'test_revocation_cannot_turn_started_send_into_no_io_proof[True]',
             'test_domo_preparation_is_owner_bound_and_atomic_with_session',
             'test_unused_original_permit_can_prove_no_io_after_actual_deadline',
             'test_domo_arm_honors_lower_retained_bigquery_global_cap']
    nodes = [path + '::' + name for name in names]
    rows += [{'nodeid': node, 'markers': ['postgres'], 'fixtures': []} for node in nodes]
    m = c.build_manifest(SOURCE, CONTROLLER, rows)
    assert path not in c.SQL_FILES['sql_execution']
    assert path in c.SQL_FILES['sql_registry_results']
    assert set(nodes) <= set(m['partitions']['sql_registry_results'])
    assert not set(nodes) & set(m['partitions']['sql_execution'])
    assert m['partitions']['runtime_process_socket'] == list(c.RUNTIME_NODES)


def test_own_collected_identities_fit_receipt_contract(request, contract):
    own = [item.nodeid for item in request.session.items if Path(str(item.path)) == Path(__file__)]
    assert own
    assert all(len(node.encode('utf-8')) <= contract.MAX_NODE_LENGTH for node in own)
