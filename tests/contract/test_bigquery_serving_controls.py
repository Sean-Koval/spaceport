"""Mandatory portable source contracts; no installed SDK or live provider claim."""
from pathlib import Path
import re
import shutil
import tempfile

import pytest

from scripts.runtime_process import bounded_process

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]
SUITES = (
    'tests/node/bigquery-runtime-image.test.cjs',
    'tests/node/bigquery-serving-config.test.cjs',
    'tests/node/bigquery-serving-contract.test.cjs',
    'tests/node/bigquery-serving-ledger.test.cjs',
    'tests/node/bigquery-serving-security.test.cjs',
    'tests/node/bigquery-serving-timeout.test.cjs',
)
IDENTITIES = (
    'runtime image derives exact reviewed Cube without installation or mutable base overrides',
    'profile parser mirrors exact Python descriptor with computed physical capacity and fixed limits',
    'bundle hashing pins static file bytes and rejects symlinks, executable models and context interpolation',
    'server and observer envelopes separate signing/materialized models from metadata-only credentials',
    'retained bundle snapshot is compiled from its checked bytes after on-disk replacement',
    'observer identity must be distinct from the execution principal',
    'serving contract provides separate versioned strict validators',
    'metadata admits documented defaults and response-only anonymous destination with exact provenance',
    'metadata mismatches never produce terminal evidence including arbitrary hidden permanent destinations',
    'metadata redacts diagnostics, distinguishes pending and error terminal, bounds parsed input',
    'rows preserve exact strings and forbid partial, oversize and unsupported values',
    'environment refuses ambient credentials, alternate metadata or proxy endpoints even when empty',
    'immutable snapshots detach and recursively freeze callback-visible configuration',
    'SQL adapter exposes only fixed mapped function names and confirms COMMIT before returning permits',
    'ambiguous COMMIT never hands out an admission permit or retries a mutation',
    'normal and observation authority use distinct fixed SQL functions',
    'runtime error sealing carries the exact attempt rather than an unguarded session ID',
    'failed durable-commit pin never invokes an authority or admission function',
    'new JWT schema validates source context and rewrites retained independent scope',
    'raw-body digest and every retained source identity fail closed on mismatch',
    'queue and cache identities partition generations while compiler key only uses static immutable identities',
    'HTTP request ID rejects repeated/comma-joined headers and exact mismatched generations',
    'valid signed outer scope cannot be swapped onto another retained source authority',
    'newly signed body fingerprint cannot replace the DB-retained BigQuery preparation',
    'unknown freshness supports time plus measure with only the two hidden scope members',
    'first intent leaves six seconds of wall and monotonic admission headroom',
    'first intent rejects insufficient retained time instead of admitting a refreshed timeout',
)
MAX_OUTPUT = 65536


def admit_tap(output):
    """Admit every exact portable case once; never expose child diagnostics."""
    if type(output) is not str or len(output.encode('utf-8')) > MAX_OUTPUT:
        raise ValueError('Invalid portable source evidence')
    cases = []
    subtests = []
    summaries = {}
    version = plan = duration = 0
    for line in output.splitlines():
        if line == 'TAP version 13':
            version += 1
        elif line == '1..' + str(len(IDENTITIES)):
            plan += 1
        elif match := re.fullmatch(r'ok ([1-9][0-9]*) - (.+)', line):
            cases.append((int(match[1]), match[2]))
        elif line.startswith('# Subtest: '):
            subtests.append(line.removeprefix('# Subtest: '))
        elif match := re.fullmatch(r'# (tests|suites|pass|fail|cancelled|skipped|todo) ([0-9]+)', line):
            if match[1] in summaries:
                raise ValueError('Duplicate portable source evidence')
            summaries[match[1]] = int(match[2])
        elif re.fullmatch(r'# duration_ms [0-9]+(?:\.[0-9]+)?', line):
            duration += 1
        elif line in ('  ---', "  type: 'test'", '  ...') or re.fullmatch(r'  duration_ms: [0-9]+(?:\.[0-9]+)?', line):
            continue
        else:
            raise ValueError('Unexpected portable source evidence')
    if (version != 1 or plan != 1 or duration != 1
            or cases != list(enumerate(IDENTITIES, 1)) or tuple(subtests) != IDENTITIES
            or summaries != {'tests':len(IDENTITIES), 'suites':0, 'pass':len(IDENTITIES),
                'fail':0, 'cancelled':0, 'skipped':0, 'todo':0}):
        raise ValueError('Incomplete portable source evidence')
    return len(cases)


def test_all_portable_bigquery_source_controls_are_required():
    node = shutil.which('node')
    if node is None:
        pytest.fail('Node is required for portable source contracts', pytrace=False)
    # No ambient Node preloads, SDK search path, provider endpoints, credentials,
    # or personal HOME enter these stdlib-only validators and fake-ledger tests.
    with tempfile.TemporaryDirectory(prefix='source-portable-gate-') as directory:
        output = Path(directory)/'node.tap'
        env = {'PATH':str(Path(node).parent), 'HOME':directory, 'TMPDIR':directory,
            'NODE_ENV':'production', 'TZ':'UTC'}
        result = bounded_process([node, '--test', '--test-reporter=tap', *SUITES],
            output, timeout=20, env=env, cwd=ROOT, limit_bytes=MAX_OUTPUT)
        if result != {'exit_status':0, 'timed_out':False, 'output_limit_exceeded':False,
                'interrupted':False, 'descendant_cleanup':True, 'orphaned_descendants':False}:
            pytest.fail('Portable source contract process or cleanup did not pass', pytrace=False)
        try:
            count = admit_tap(output.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pytest.fail('Required portable source contract evidence did not pass', pytrace=False)
        assert count == len(IDENTITIES)
