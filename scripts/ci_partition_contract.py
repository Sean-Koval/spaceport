"""Closed, pure-stdlib admission contract for the serial gateway CI workflow.

Load this file only from the reviewed bootstrap. It never reads environment,
files, candidate modules, or network state. A receipt is evidence to validate,
not authority to select policy or to define its own expected run identity.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET

SCHEMA_VERSION = 1
LANES = ('sql_owners', 'sql_execution', 'sql_publishing', 'sql_registry_results', 'runtime_process_socket')
SOURCE_JOB = 'source'
WORKFLOW_FILE = '.github/workflows/verify-gateway-partitions.yml'
SELECTION = ('-m', 'postgres or cube')
SOURCE_PHASES = (('client_dependencies', 180), ('source_collection', 60), ('source_tests', 180), ('backend_collection', 60))
BACKEND_PHASES = (('client_dependencies', 180), ('backend_collection', 60), ('backend_stack', 900), ('backend_tests', 570))
TOOL_PINS = {'python': '3.12.14', 'uv': '0.12.23', 'node': '24.19.0', 'npm': '11.9.0'}
MAX_NODES = 2048
MAX_SOURCE_NODES = 16384
MAX_NODE_LENGTH = 1024
MAX_XML_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 32 * 1024
# This reviewed policy is embedded deliberately, never read from the candidate.
# SQL identities are selected by exact full paths; runtime identities are fixed.
SQL_FILES = {
    'sql_owners': (
        'tests/integration/test_source_approval_final_clock.py',
        'tests/integration/test_source_approval_finalization.py',
        'tests/integration/test_source_approval_registry.py',
        'tests/integration/test_source_approval_startup.py',
        'tests/integration/test_source_approval_transactions.py',
        'tests/integration/test_source_fixture_owners.py',
        'tests/integration/test_source_manifest_canonical.py',
    ),
    'sql_execution': (
        'tests/integration/test_domo_gateway.py',
        'tests/integration/test_domo_preparation_registry.py',
        'tests/integration/test_execution_queue.py',
        'tests/integration/test_source_approval_execution_queue.py',
        'tests/integration/test_source_approval_lifecycle.py',
        'tests/integration/test_source_lifecycle.py',
    ),
    'sql_publishing': (
        'tests/integration/test_draft_discovery.py',
        'tests/integration/test_publishing.py',
        'tests/integration/test_reviewed_authoring_fixture.py',
        'tests/integration/test_source_approval_catalog_release.py',
        'tests/integration/test_source_approval_choice_release.py',
        'tests/integration/test_source_approval_fixture_runner.py',
        'tests/integration/test_source_approval_publishing_actions.py',
        'tests/integration/test_source_approval_publishing_synthetic.py',
        'tests/security/test_next_wave_crossflow.py',
        'tests/security/test_publisher_roles.py',
    ),
    'sql_registry_results': (
        'tests/integration/test_audit.py',
        'tests/integration/test_domo_lifecycle_edges.py',
        'tests/integration/test_identity_store.py',
        'tests/integration/test_operations.py',
        'tests/integration/test_pilot_bootstrap_postgres.py',
        'tests/integration/test_pilot_registry_options.py',
        'tests/integration/test_registry.py',
        'tests/integration/test_rest_services.py',
        'tests/integration/test_results.py',
        'tests/integration/test_source_approval_result_release.py',
        'tests/integration/test_source_pilot_bootstrap.py',
        'tests/integration/test_source_runtime_approval_seed.py',
        'tests/integration/test_source_runtime_composition.py',
        'tests/security/test_execution_reauthorization.py',
        'tests/security/test_policy_revocation.py',
        'tests/security/test_result_isolation.py',
    ),
}
RUNTIME_NODES = (
    'tests/integration/test_audit.py::test_destructive_audit_identity_downgrade_is_rejected_without_data_loss[registry_database0]',
    'tests/integration/test_bigquery_gateway.py::test_real_bigquery_author_fixture_publish_describe_worker_result',
    'tests/integration/test_cube_adapter_runtime.py::test_actual_adapter_dst_day_filter_preserves_daily_grain',
    'tests/integration/test_cube_adapter_runtime.py::test_actual_adapter_empty_query_keeps_recorded_load_watermark',
    'tests/integration/test_cube_adapter_runtime.py::test_actual_adapter_exact_grain_and_lossless_totals[north-9007199254741023.31-consumer]',
    'tests/integration/test_cube_adapter_runtime.py::test_actual_adapter_exact_grain_and_lossless_totals[north-9007199254741023.31-fixture_test]',
    'tests/integration/test_cube_adapter_runtime.py::test_actual_adapter_exact_grain_and_lossless_totals[south-100.01-consumer]',
    'tests/integration/test_cube_adapter_runtime.py::test_actual_adapter_exact_grain_and_lossless_totals[south-100.01-fixture_test]',
    'tests/integration/test_cube_adapter_runtime.py::test_actual_adapter_same_principal_entitlements_do_not_mix',
    'tests/integration/test_cube_compatibility.py::test_actual_cache_isolation_both_tenant_orders_and_entitlements',
    'tests/integration/test_cube_compatibility.py::test_authenticated_meta_records_actual_release_shape',
    'tests/integration/test_cube_compatibility.py::test_cold_bounded_fixture_observes_continue_wait_then_success',
    'tests/integration/test_cube_compatibility.py::test_daily_grain_and_exact_decimal_totals',
    'tests/integration/test_cube_compatibility.py::test_full_effective_compose_and_actual_topology_publish_only_loopback_edge',
    'tests/integration/test_cube_compatibility.py::test_half_open_dst_boundary_and_empty_result',
    'tests/integration/test_cube_compatibility.py::test_hostile_client_or_cannot_remove_mandatory_predicate',
    'tests/integration/test_cube_compatibility.py::test_invalid_context_is_denied[patch0]',
    'tests/integration/test_cube_compatibility.py::test_invalid_context_is_denied[patch1]',
    'tests/integration/test_cube_compatibility.py::test_invalid_context_is_denied[patch2]',
    'tests/integration/test_cube_compatibility.py::test_invalid_context_is_denied[patch3]',
    'tests/integration/test_cube_compatibility.py::test_invalid_context_is_denied[patch4]',
    'tests/integration/test_cube_compatibility.py::test_invalid_context_is_denied[patch5]',
    'tests/integration/test_cube_compatibility.py::test_invalid_context_is_denied[patch6]',
    'tests/integration/test_cube_compatibility.py::test_known_answer_lossless_rows[north-expected0]',
    'tests/integration/test_cube_compatibility.py::test_known_answer_lossless_rows[south-expected1]',
    'tests/integration/test_cube_compatibility.py::test_missing_context_is_denied[/cubejs-api/v1/load-cube-test]',
    'tests/integration/test_cube_compatibility.py::test_missing_context_is_denied[/cubejs-api/v1/load-cube]',
    'tests/integration/test_cube_compatibility.py::test_missing_context_is_denied[/cubejs-api/v1/meta-cube-test]',
    'tests/integration/test_cube_compatibility.py::test_missing_context_is_denied[/cubejs-api/v1/meta-cube]',
    'tests/integration/test_cube_compatibility.py::test_missing_explicit_cache_semantics_are_denied',
    'tests/integration/test_cube_compatibility.py::test_native_sql_and_websocket_ports_are_closed',
    'tests/integration/test_cube_compatibility.py::test_probe_outbound_is_blocked',
    'tests/integration/test_cube_compatibility.py::test_runtime_watermark_is_load_time_persisted_across_restart',
    'tests/integration/test_cube_compatibility.py::test_source_roles_are_read_only_separate_and_statement_timeout_is_server_enforced',
    'tests/integration/test_cube_compatibility.py::test_source_timeout_through_cube_observes_source_stop',
    'tests/integration/test_cube_compatibility.py::test_sql_graphql_and_dev_ingress_are_unavailable[/cubejs-api/graphql-body2]',
    'tests/integration/test_cube_compatibility.py::test_sql_graphql_and_dev_ingress_are_unavailable[/cubejs-api/v1/cubesql-body1]',
    'tests/integration/test_cube_compatibility.py::test_sql_graphql_and_dev_ingress_are_unavailable[/cubejs-api/v1/sql-body0]',
    'tests/integration/test_cube_compatibility.py::test_sql_graphql_and_dev_ingress_are_unavailable[/cubejs-system/v1/context-None]',
    'tests/integration/test_cube_compatibility.py::test_sql_graphql_and_dev_ingress_are_unavailable[/dev-None]',
    'tests/integration/test_cube_compatibility.py::test_sql_graphql_and_dev_ingress_are_unavailable[/playground-None]',
    'tests/integration/test_cube_reviewed_runtime.py::test_actual_reviewed_empty_result_cannot_claim_fresh_checkpoint',
    'tests/integration/test_cube_reviewed_runtime.py::test_actual_reviewed_prepare_poll_normalize_matches_same_statement_checkpoint[north-consumer]',
    'tests/integration/test_cube_reviewed_runtime.py::test_actual_reviewed_prepare_poll_normalize_matches_same_statement_checkpoint[north-fixture_test]',
    'tests/integration/test_cube_reviewed_runtime.py::test_actual_reviewed_prepare_poll_normalize_matches_same_statement_checkpoint[south-consumer]',
    'tests/integration/test_cube_reviewed_runtime.py::test_actual_reviewed_prepare_poll_normalize_matches_same_statement_checkpoint[south-fixture_test]',
    'tests/integration/test_cube_reviewed_runtime.py::test_actual_reviewed_same_tenant_group_scopes_do_not_mix',
    'tests/integration/test_cube_reviewed_runtime.py::test_reviewed_fixture_and_legacy_tokens_cannot_cross_endpoints[consumer]',
    'tests/integration/test_cube_reviewed_runtime.py::test_reviewed_fixture_and_legacy_tokens_cannot_cross_endpoints[fixture_test]',
    'tests/integration/test_cube_reviewed_runtime.py::test_reviewed_rows_and_checkpoint_survive_restart[consumer]',
    'tests/integration/test_cube_reviewed_runtime.py::test_reviewed_rows_and_checkpoint_survive_restart[fixture_test]',
    'tests/integration/test_cube_reviewed_runtime.py::test_reviewed_source_has_atomic_unique_checkpoint_and_separate_read_only_roles[consumer]',
    'tests/integration/test_cube_reviewed_runtime.py::test_reviewed_source_has_atomic_unique_checkpoint_and_separate_read_only_roles[fixture_test]',
    'tests/integration/test_execution_pending_replay.py::test_worker_replays_actual_continue_wait_then_live_orders_completes',
    'tests/integration/test_execution_queue.py::test_actual_worker_approved_orders_persists_lossless_complete_result',
    'tests/integration/test_execution_queue.py::test_real_worker_process_death_preserves_committed_attempt_for_recovery',
    'tests/integration/test_execution_queue.py::test_three_worker_processes_enforce_two_principal_ten_deployment',
    'tests/integration/test_mcp_services.py::test_real_mcp_rest_catalog_result_pagination_revocation_and_identity',
    'tests/integration/test_publishing.py::test_real_isolated_cube_fixture_run_publishes_exact_original_digest',
    'tests/integration/test_registry.py::test_authenticated_tcp_readiness_reaches_real_test_server',
    'tests/integration/test_rest_services.py::test_actual_pinned_cube_readiness_with_real_registry_and_fresh_revocation',
    'tests/integration/test_reviewed_execution_runtime.py::test_actual_reviewed_publication_worker_replay_and_reauthorized_stored_result',
    'tests/security/test_adversarial_matrix.py::test_actual_postgres_operator_and_service_authority_over_same_socket[operator]',
    'tests/security/test_adversarial_matrix.py::test_actual_postgres_operator_and_service_authority_over_same_socket[service]',
)
RUNTIME_EXCEPTIONS = (
    'tests/integration/test_audit.py::test_destructive_audit_identity_downgrade_is_rejected_without_data_loss[registry_database0]',
    'tests/integration/test_execution_queue.py::test_real_worker_process_death_preserves_committed_attempt_for_recovery',
    'tests/integration/test_execution_queue.py::test_three_worker_processes_enforce_two_principal_ten_deployment',
    'tests/integration/test_mcp_services.py::test_real_mcp_rest_catalog_result_pagination_revocation_and_identity',
    'tests/integration/test_registry.py::test_authenticated_tcp_readiness_reaches_real_test_server',
    'tests/security/test_adversarial_matrix.py::test_actual_postgres_operator_and_service_authority_over_same_socket[operator]',
    'tests/security/test_adversarial_matrix.py::test_actual_postgres_operator_and_service_authority_over_same_socket[service]',
)


class ContractError(ValueError):
    """A fixed, non-sensitive validation error safe for a bounded caller report."""

    def __init__(self):
        super().__init__('Invalid CI partition evidence')


def _require(condition):
    if not condition:
        raise ContractError()


def _keys(value, names):
    _require(type(value) is dict and set(value) == set(names))


def _integer(value, low, high):
    _require(type(value) is int and low <= value <= high)
    return value


def _hex(value, length=64):
    _require(type(value) is str and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is not None)
    return value


def _decimal(value, maximum, *, positive=False):
    _require(type(value) is str and len(value) <= 20 and re.fullmatch('0|[1-9][0-9]*', value) is not None)
    number = int(value)
    return _integer(number, 1 if positive else 0, maximum)


def _json_value(value, depth=0, budget=None):
    """Reject non-JSON Python values, cycles, huge numbers and parser depth bombs."""
    if budget is None:
        budget = [100000]
    budget[0] -= 1
    _require(depth <= 32 and budget[0] >= 0)
    kind = type(value)
    if value is None or kind is bool:
        return
    if kind is int:
        _integer(value, -(2**63), 2**63 - 1)
    elif kind is float:
        _require(math.isfinite(value))
    elif kind is str:
        _require(len(value) <= MAX_XML_BYTES and not any(0xD800 <= ord(c) <= 0xDFFF for c in value))
    elif kind is list:
        _require(len(value) <= 100000)
        for item in value:
            _json_value(item, depth + 1, budget)
    elif kind is dict:
        _require(len(value) <= 100000)
        for key, item in value.items():
            _require(type(key) is str)
            _json_value(key, depth + 1, budget)
            _json_value(item, depth + 1, budget)
    else:
        raise ContractError()


def canonical_bytes(value):
    """Return one strict compact, sorted, ASCII JSON representation."""
    _json_value(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ContractError() from None


def sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def load_json(raw, *, limit):
    """Load bounded UTF-8 JSON; transports additionally demand canonical bytes."""
    _integer(limit, 1, MAX_XML_BYTES)
    _require(type(raw) is bytes and 0 < len(raw) <= limit)

    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result)
            result[key] = value
        return result

    def integer(text):
        _require(len(text) <= 20)
        return _integer(int(text), -(2**63), 2**63 - 1)

    def nonfinite(_):
        raise ContractError()

    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_int=integer, parse_constant=nonfinite)
        _json_value(value)
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ContractError() from None


def _source(value):
    _keys(value, ('commit', 'tree', 'digest'))
    _hex(value['commit'], 40)
    _hex(value['tree'], 40)
    _hex(value['digest'])
    return value


def _node(value):
    _require(type(value) is str and 1 <= len(value) <= MAX_NODE_LENGTH and value.isprintable())
    # Parameters are opaque after the first '['. In particular, their '::',
    # slashes and brackets must never be interpreted as address delimiters.
    address, bracket, parameters = value.partition('[')
    parts = address.split('::')
    _require(len(parts) >= 2 and len(parts) <= 16)
    path = parts[0]
    _require(re.fullmatch(r'tests/(?:[A-Za-z_][A-Za-z0-9_]*/)*test_[A-Za-z0-9_]+\.py', path) is not None)
    _require(all(re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', p) is not None for p in parts[1:]))
    _require(not bracket or parameters.endswith(']'))
    return path


def _projection(node):
    _node(node)
    address, bracket, parameters = node.partition('[')
    parts = address.split('::')
    return (parts[0][:-3].replace('/', '.') + ('.' + '.'.join(parts[1:-1]) if len(parts) > 2 else ''), parts[-1] + bracket + parameters)


def _nodes(value, *, maximum=MAX_NODES, sorted_required=True):
    _require(type(value) is list and 0 < len(value) <= maximum)
    for node in value:
        _node(node)
    _require(len(set(value)) == len(value) and len({_projection(n) for n in value}) == len(value))
    _require(not sorted_required or value == sorted(value))
    return value


def _lane(node):
    path = _node(node)
    if node in RUNTIME_NODES:
        return 'runtime_process_socket'
    matches = [lane for lane in LANES[:-1] if path in SQL_FILES[lane]]
    _require(len(matches) == 1)
    return matches[0]


def _metadata_names(value):
    _require(type(value) is list and len(value) <= 128)
    for name in value:
        _require(type(name) is str and len(name) <= 128 and re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', name) is not None)
    _require(len(set(value)) == len(value))
    return set(value)


def build_manifest(source, controller_digest, inventory):
    """Partition a freshly collected, complete canonical backend inventory."""
    _source(source)
    _hex(controller_digest)
    _require(type(inventory) is list and 0 < len(inventory) <= MAX_NODES)
    partitions = {lane: [] for lane in LANES}
    for row in inventory:
        _keys(row, ('nodeid', 'markers', 'fixtures'))
        node = row['nodeid']
        lane = _lane(node)
        markers, fixtures = _metadata_names(row['markers']), _metadata_names(row['fixtures'])
        _require(bool(markers & {'postgres', 'cube'}))
        runtime_signal = 'cube' in markers or bool(fixtures & {'cube_stack', 'reviewed_stack'}) or node in RUNTIME_EXCEPTIONS
        _require(runtime_signal == (lane == 'runtime_process_socket'))
        partitions[lane].append(node)
    for nodes in partitions.values():
        nodes.sort()
    union = sorted(node for nodes in partitions.values() for node in nodes)
    result = {'schema_version': SCHEMA_VERSION, 'source': dict(source), 'controller_digest': controller_digest,
              'selection': list(SELECTION), 'inventory_sha256': sha256(union), 'partitions': partitions,
              'counts': {lane: len(partitions[lane]) for lane in LANES}, 'total': len(union)}
    return validate_manifest(result, source=source, controller_digest=controller_digest, expected_sha256=sha256(result))


def validate_manifest(value, *, source, controller_digest, expected_sha256):
    """Verify exact schema, policy, disjoint/full union, source and reviewed hash."""
    _source(source)
    _hex(controller_digest)
    _hex(expected_sha256)
    _keys(value, ('schema_version', 'source', 'controller_digest', 'selection', 'inventory_sha256', 'partitions', 'counts', 'total'))
    _integer(value['schema_version'], SCHEMA_VERSION, SCHEMA_VERSION)
    _source(value['source'])
    _require(value['source'] == source and value['controller_digest'] == controller_digest)
    _require(type(value['selection']) is list and value['selection'] == list(SELECTION))
    _keys(value['partitions'], LANES)
    _keys(value['counts'], LANES)
    union = []
    for lane in LANES:
        nodes = _nodes(value['partitions'][lane])
        _integer(value['counts'][lane], 1, MAX_NODES)
        _require(value['counts'][lane] == len(nodes) and all(_lane(node) == lane for node in nodes))
        union.extend(nodes)
    _nodes(sorted(union))
    _require(value['partitions']['runtime_process_socket'] == list(RUNTIME_NODES))
    _integer(value['total'], 1, MAX_NODES)
    _require(value['total'] == len(union) and value['inventory_sha256'] == sha256(sorted(union)))
    raw = canonical_bytes(value)
    _require(len(raw) <= 192 * 1024 and hashlib.sha256(raw).hexdigest() == expected_sha256)
    return value


def _transport_policy(kind):
    _require(type(kind) is str and kind in ('manifest', 'receipt'))
    return (8, 192 * 1024) if kind == 'manifest' else (3, 64 * 1024)


def encode_transport(value, *, kind):
    """Encode JSON into the fixed, bounded output slots, without compression."""
    slots, limit = _transport_policy(kind)
    raw = canonical_bytes(value)
    _require(len(raw) <= limit)
    encoded = base64.b64encode(raw).decode('ascii')
    count = (len(encoded) + CHUNK_BYTES - 1) // CHUNK_BYTES
    _require(0 < count <= slots)
    return {'count': str(count), 'encoded_length': str(len(encoded)), 'decoded_length': str(len(raw)),
            'sha256': hashlib.sha256(raw).hexdigest(),
            **{'chunk_' + str(i): encoded[i * CHUNK_BYTES:(i + 1) * CHUNK_BYTES] for i in range(slots)}}


def decode_transport(slots, *, kind):
    """Reject suppressed, rearranged, noncanonical or oversized job outputs."""
    slot_count, limit = _transport_policy(kind)
    _keys(slots, ('count', 'encoded_length', 'decoded_length', 'sha256', *('chunk_' + str(i) for i in range(slot_count))))
    _require(all(type(value) is str for value in slots.values()))
    count = _decimal(slots['count'], slot_count, positive=True)
    encoded_length = _decimal(slots['encoded_length'], ((limit + 2) // 3) * 4, positive=True)
    decoded_length = _decimal(slots['decoded_length'], limit, positive=True)
    _hex(slots['sha256'])
    for index in range(slot_count):
        chunk = slots['chunk_' + str(index)]
        if index >= count:
            _require(chunk == '')
        else:
            _require(0 < len(chunk) <= CHUNK_BYTES and chunk.isascii())
            _require(index == count - 1 or len(chunk) == CHUNK_BYTES)
    encoded = ''.join(slots['chunk_' + str(index)] for index in range(count))
    _require(len(encoded) == encoded_length and count == (encoded_length + CHUNK_BYTES - 1) // CHUNK_BYTES)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ContractError() from None
    _require(len(raw) == decoded_length and base64.b64encode(raw).decode('ascii') == encoded)
    _require(hashlib.sha256(raw).hexdigest() == slots['sha256'])
    value = load_json(raw, limit=limit)
    _require(canonical_bytes(value) == raw)
    return value


def _xml_counts(element, count):
    for name in ('tests', 'failures', 'errors', 'skipped'):
        number = _decimal(element.get(name), MAX_SOURCE_NODES)
        _require(number == (count if name == 'tests' else 0))


def junit_summary(raw, expected_nodes):
    """Admit exact zero-skip JUnit against locally collected pytest identities."""
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_XML_BYTES)
    _require(type(expected_nodes) is tuple)
    nodes = list(expected_nodes)
    _nodes(nodes, maximum=MAX_SOURCE_NODES, sorted_required=False)
    nodes.sort()
    expected = {_projection(node) for node in nodes}
    try:
        text = raw.decode('utf-8')
        _require('\x00' not in text and '<!DOCTYPE' not in text.upper() and '<!ENTITY' not in text.upper())
        parser = ET.XMLPullParser(events=('start', 'end'))
        depth = 0
        element_count = 0
        root = None
        # Enforce depth/element bounds while parsing, before building an entire
        # attacker-shaped tree. The byte cap applies to text/output as well.
        for offset in range(0, len(text), 16384):
            parser.feed(text[offset:offset + 16384])
            for event, element in parser.read_events():
                if event == 'start':
                    depth += 1
                    element_count += 1
                    _require(depth <= 32 and element_count <= 100000)
                    if root is None:
                        root = element
                else:
                    depth -= 1
        parser.close()
    except (ValueError, UnicodeError, ET.ParseError):
        raise ContractError() from None
    _require(root is not None and root.tag in ('testsuites', 'testsuite'))
    suites = list(root) if root.tag == 'testsuites' else [root]
    _require(bool(suites) and all(suite.tag == 'testsuite' for suite in suites))
    actual = []
    for suite in suites:
        _require(all(child.tag in ('testcase', 'properties', 'system-out', 'system-err') for child in suite))
        cases = [child for child in suite if child.tag == 'testcase']
        _require(bool(cases))
        _xml_counts(suite, len(cases))
        for case in cases:
            pair = (case.get('classname'), case.get('name'))
            _require(all(type(part) is str and 0 < len(part) <= MAX_NODE_LENGTH and part.isprintable() for part in pair))
            _require(all(child.tag in ('properties', 'system-out', 'system-err') for child in case))
            actual.append(pair)
    # Reject hidden cases/suites or outcome tags under property/output elements.
    _require(len(list(root.iter('testcase'))) == len(actual) and len(list(root.iter('testsuite'))) == len(suites))
    _require(not any(element.tag in ('failure', 'error', 'skipped') for element in root.iter()))
    _require(len(actual) == len(set(actual)) and set(actual) == expected)
    if root.tag == 'testsuites' and any(name in root.attrib for name in ('tests', 'failures', 'errors', 'skipped')):
        _xml_counts(root, len(actual))
    return {'count': len(nodes), 'nodes': nodes, 'identities_sha256': sha256(nodes), 'junit_sha256': hashlib.sha256(raw).hexdigest()}


def _identity(value):
    _keys(value, ('repository', 'source_commit', 'source_tree', 'source_digest', 'workflow_file', 'workflow_ref',
                  'workflow_sha', 'controller_digest', 'run_id', 'run_attempt'))
    repository = value['repository']
    _require(type(repository) is str and len(repository) <= 200 and re.fullmatch('[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository) is not None)
    _hex(value['source_commit'], 40)
    _hex(value['source_tree'], 40)
    _hex(value['source_digest'])
    _hex(value['workflow_sha'], 40)
    _hex(value['controller_digest'])
    _require(value['workflow_file'] == WORKFLOW_FILE)
    ref = value['workflow_ref']
    _require(type(ref) is str and len(ref) <= 512 and ref.startswith(repository + '/' + WORKFLOW_FILE + '@'))
    suffix = ref[len(repository + '/' + WORKFLOW_FILE + '@'):]
    _require(re.fullmatch(r'refs/(heads|tags)/[A-Za-z0-9_./-]+', suffix) is not None)
    _require('..' not in suffix and '//' not in suffix and not suffix.endswith(('/', '.')))
    _decimal(value['run_id'], 2**64 - 1, positive=True)
    _decimal(value['run_attempt'], 2**31 - 1, positive=True)
    return value


def _identity_source(identity):
    return {'commit': identity['source_commit'], 'tree': identity['source_tree'], 'digest': identity['source_digest']}


def _phases(value, job_id):
    policy = SOURCE_PHASES if job_id == SOURCE_JOB else BACKEND_PHASES
    _require(type(value) is list and len(value) == len(policy))
    for phase, (name, cap) in zip(value, policy):
        _keys(phase, ('name', 'status', 'cap_seconds', 'elapsed_ms', 'supervision'))
        _require(phase['name'] == name and phase['status'] == 'PASS')
        _integer(phase['cap_seconds'], 780 if name == 'backend_stack' else cap, cap)
        _integer(phase['elapsed_ms'], 0, phase['cap_seconds'] * 1000 + 10000)
        supervision = phase['supervision']
        _keys(supervision, ('exit_status', 'timed_out', 'output_limit_exceeded', 'interrupted', 'descendant_cleanup', 'orphaned_descendants'))
        _integer(supervision['exit_status'], 0, 0)
        _require(supervision['descendant_cleanup'] is True)
        _require(all(supervision[key] is False for key in ('timed_out', 'output_limit_exceeded', 'interrupted', 'orphaned_descendants')))


def validate_receipt(value, *, identity, manifest, job_id):
    """Accept only positive, complete proof tied to caller-owned expected context."""
    _identity(identity)
    _require(type(job_id) is str and job_id in (SOURCE_JOB, *LANES))
    manifest_digest = sha256(manifest)
    validate_manifest(manifest, source=_identity_source(identity), controller_digest=identity['controller_digest'], expected_sha256=manifest_digest)
    _keys(value, ('schema_version', 'identity', 'job_id', 'status', 'manifest_sha256', 'tools', 'test_summary', 'phases', 'cleanup', 'source_rechecked', 'controller_rechecked'))
    _integer(value['schema_version'], SCHEMA_VERSION, SCHEMA_VERSION)
    _identity(value['identity'])
    _require(value['identity'] == identity and value['job_id'] == job_id and value['status'] == 'PASS')
    _require(value['manifest_sha256'] == manifest_digest)
    _require(value['source_rechecked'] is True and value['controller_rechecked'] is True)
    tools = value['tools']
    _keys(tools, (*TOOL_PINS, 'docker', 'compose', 'runner_image'))
    for name, version in TOOL_PINS.items():
        _require(type(tools[name]) is str and tools[name] == version)
    for name in ('docker', 'compose', 'runner_image'):
        version = tools[name]
        _require(type(version) is str and re.fullmatch('[A-Za-z0-9v.+ _-]{1,128}', version) is not None and bool(version.strip()))
    summary = value['test_summary']
    _keys(summary, ('count', 'nodes', 'identities_sha256', 'junit_sha256', 'images_sha256', 'endpoints_sha256'))
    _integer(summary['count'], 1, MAX_SOURCE_NODES if job_id == SOURCE_JOB else MAX_NODES)
    _hex(summary['identities_sha256'])
    _hex(summary['junit_sha256'])
    if job_id == SOURCE_JOB:
        _require(type(summary['nodes']) is list and summary['nodes'] == [])
        _require(summary['images_sha256'] is None and summary['endpoints_sha256'] is None)
    else:
        nodes = _nodes(summary['nodes'])
        _require(nodes == manifest['partitions'][job_id] and summary['count'] == len(nodes))
        _require(summary['identities_sha256'] == sha256(nodes))
        _hex(summary['images_sha256'])
        _hex(summary['endpoints_sha256'])
    _phases(value['phases'], job_id)
    cleanup = value['cleanup']
    _keys(cleanup, ('identity', 'job_id', 'status', 'cap_seconds', 'elapsed_ms', 'exact_resources_verified', 'descendants_verified'))
    _identity(cleanup['identity'])
    _require(cleanup['identity'] == identity and cleanup['job_id'] == job_id and cleanup['status'] == 'PASS')
    _integer(cleanup['cap_seconds'], 120, 120)
    _integer(cleanup['elapsed_ms'], 0, 120000)
    _require(cleanup['exact_resources_verified'] is True and cleanup['descendants_verified'] is True)
    _require(len(canonical_bytes(value)) <= 64 * 1024)
    return value


def aggregate(manifest, source_receipt, lane_receipts, job_results, *, identity, expected_manifest_sha256):
    """Only a complete same-run/attempt cohort can emit full-backend PASS.

    Unsafe manifest/expected context raises ContractError. A safe context with
    incomplete job evidence returns FAIL with only bounded fixed failure enums.
    """
    _identity(identity)
    validate_manifest(manifest, source=_identity_source(identity), controller_digest=identity['controller_digest'], expected_sha256=expected_manifest_sha256)
    failures = []
    source_count = 0
    try:
        validate_receipt(source_receipt, identity=identity, manifest=manifest, job_id=SOURCE_JOB)
        source_count = source_receipt['test_summary']['count']
    except ContractError:
        failures.append('SOURCE_RECEIPT_INVALID')
    try:
        _require(type(lane_receipts) is list and len(lane_receipts) == len(LANES))
        seen = set()
        for receipt in lane_receipts:
            _require(type(receipt) is dict)
            job = receipt.get('job_id')
            _require(type(job) is str and job in LANES and job not in seen)
            validate_receipt(receipt, identity=identity, manifest=manifest, job_id=job)
            seen.add(job)
        _require(seen == set(LANES))
    except ContractError:
        failures.append('LANE_RECEIPTS_INVALID')
    try:
        _keys(job_results, (SOURCE_JOB, *LANES))
        _require(all(type(value) is str and value == 'success' for value in job_results.values()))
    except ContractError:
        failures.append('JOB_RESULTS_INVALID')
    return {'schema_version': SCHEMA_VERSION, 'identity': dict(identity), 'status': 'FAIL' if failures else 'PASS',
            'manifest_sha256': expected_manifest_sha256, 'source_count': source_count, 'backend_count': manifest['total'],
            'lane_counts': dict(manifest['counts']), 'lane_digests': {lane: sha256(manifest['partitions'][lane]) for lane in LANES},
            'failures': failures}
