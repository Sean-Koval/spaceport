"""Fail-closed receipt tests for the mandatory portable Node source selection."""
import pytest
from tests.contract.test_bigquery_serving_controls import IDENTITIES, SUITES, admit_tap
pytestmark = [pytest.mark.unit, pytest.mark.contract]


def receipt():
    return '\n'.join(['TAP version 13',
        *[line for i, name in enumerate(IDENTITIES, 1)
            for line in ('# Subtest: '+name, f'ok {i} - {name}')],
        '1..27', '# tests 27', '# suites 0', '# pass 27', '# fail 0',
        '# cancelled 0', '# skipped 0', '# todo 0', '# duration_ms 10.0'])+'\n'


def test_complete_portable_inventory_is_explicit():
    assert SUITES == (
        'tests/node/bigquery-runtime-image.test.cjs',
        'tests/node/bigquery-serving-config.test.cjs',
        'tests/node/bigquery-serving-contract.test.cjs',
        'tests/node/bigquery-serving-ledger.test.cjs',
        'tests/node/bigquery-serving-security.test.cjs',
        'tests/node/bigquery-serving-timeout.test.cjs',
    )
    assert len(IDENTITIES) == len(set(IDENTITIES)) == 27
    assert admit_tap(receipt()) == 27


@pytest.mark.parametrize('kind', [
    'missing', 'duplicate', 'renamed', 'skipped', 'cancelled', 'todo', 'failed',
    'wrong_plan', 'wrong_count', 'duplicate_summary', 'no_version', 'extra_output', 'oversize', 'duplicate_duration', 'skip_directive', 'reordered', 'fail_count',
])
def test_incomplete_or_ambiguous_node_evidence_never_passes(kind):
    text = receipt()
    first = 'ok 1 - '+IDENTITIES[0]
    if kind == 'missing': text = text.replace(first+'\n', '')
    elif kind == 'duplicate': text += first+'\n'
    elif kind == 'renamed': text = text.replace(first, 'ok 1 - unreviewed case')
    elif kind in ('skipped', 'cancelled', 'todo'): text = text.replace('# '+kind+' 0', '# '+kind+' 1')
    elif kind == 'failed': text = text.replace(first, 'not '+first)
    elif kind == 'wrong_plan': text = text.replace('1..27', '1..26')
    elif kind == 'wrong_count': text = text.replace('# tests 27', '# tests 26')
    elif kind == 'duplicate_summary': text += '# fail 0\n'
    elif kind == 'no_version': text = text.replace('TAP version 13\n', '')
    elif kind == 'duplicate_duration': text += '# duration_ms 1.0\n'
    elif kind == 'skip_directive': text = text.replace(first, first+' # SKIP')
    elif kind == 'reordered': text = text.replace(first, 'ok 2 - '+IDENTITIES[0])
    elif kind == 'fail_count': text = text.replace('# fail 0', '# fail 1')
    elif kind == 'extra_output': text += 'untrusted diagnostic must not be admitted\n'
    elif kind == 'oversize': text += 'x'*65537
    with pytest.raises(ValueError): admit_tap(text)
