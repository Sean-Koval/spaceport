"""Partition mode selects its source protocol before any child executes."""
from types import SimpleNamespace
import sys

import pytest

from scripts import ci_partition_driver as driver

pytestmark = [pytest.mark.unit, pytest.mark.contract]


@pytest.fixture
def gate(monkeypatch, tmp_path):
    candidate = tmp_path / 'candidate'; candidate.mkdir()
    root = tmp_path / 'root'; root.mkdir()
    seen = []
    def main():
        seen.append(list(sys.argv))
        return 17
    module = SimpleNamespace(main=main)
    monkeypatch.setattr(driver, 'load_context', lambda *args: {})
    monkeypatch.setattr(driver, 'modules', lambda *args: (None, None, None))
    monkeypatch.setattr(driver, 'trusted_dependencies', lambda *args: None)
    monkeypatch.setattr(driver, 'trusted_module', lambda name: module)
    monkeypatch.setattr(sys, 'argv', ['original'])
    return candidate, root, seen


def test_partition_source_gate_is_explicitly_pinned_to_protocol_two(gate):
    candidate, root, seen = gate
    args = ['scripts/source_runtime_gate.py', '--project', 'gateway-cube-' + 'a'*16,
            '--run-label', '123-1']
    assert driver.helper(candidate, root, args) == 17
    assert seen == [[*args, '--protocol', '2']]
    assert len(args) == 5  # No mutation of caller input.


@pytest.mark.parametrize('tail', [
    ['--protocol', '1'], ['--protocol', '2'], ['--protocol=1'], ['--prot', '1'],
    ['--protocol', '2', '--protocol', '1'], ['--protocol=2'], ['--extra'],
], ids=['downgrade', 'supplied_two', 'equals_one', 'abbreviation', 'duplicate', 'equals_two', 'extra'])
def test_partition_gate_rejects_any_caller_protocol_or_extra_argument(gate, tail):
    candidate, root, seen = gate
    with pytest.raises(ValueError):
        driver.helper(candidate, root, ['scripts/source_runtime_gate.py', '--project',
            'gateway-cube-' + 'a'*16, '--run-label', '123-1', *tail])
    assert seen == []


@pytest.mark.parametrize('args', [
    ['scripts/source_runtime_gate.py'],
    ['scripts/source_runtime_gate.py', '--project', 'x', '--run-label'],
    ['scripts/source_runtime_gate.py', '--run-label', '123-1', '--project', 'x'],
    ['scripts/source_runtime_gate.py', '--proj', 'x', '--run-label', '123-1'],
    ['scripts/source_runtime_gate.py', '--project', 'x', '--run', '123-1'],
], ids=['missing', 'short', 'reordered', 'project_abbreviation', 'label_abbreviation'])
def test_partition_source_gate_requires_exact_original_shell_shape(gate, args):
    candidate, root, seen = gate
    with pytest.raises(ValueError): driver.helper(candidate, root, args)
    assert seen == []
