"""Real descendant ownership and cleanup regression controls."""
import sys

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]


@pytest.mark.parametrize('finished', [True, False], ids=['adopted-zombie', 'live-orphan'])
def test_orphan_classification_reaps_finished_children_but_rejects_live_ones(tmp_path, finished):
    from scripts import runtime_process as process
    target = tmp_path / 'descendant-pid'
    # WNOWAIT leaves a known-finished child for the subreaper to adopt. The
    # live control closes its output descriptors so only ownership detects it.
    code = f'''
import os, signal
from pathlib import Path
reader, writer = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(reader)
    with open(os.devnull, 'wb') as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    Path({str(target)!r}).write_text(str(os.getpid()))
    os.write(writer, b'1')
    os.close(writer)
    if {finished!r}:
        os._exit(0)
    while True:
        signal.pause()
os.close(writer)
assert os.read(reader, 1) == b'1'
os.close(reader)
if {finished!r}:
    os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
'''
    result = process.bounded_process([sys.executable, '-c', code], tmp_path / 'output', timeout=3)
    assert result['descendant_cleanup'] is True
    assert process.process_info(int(target.read_text())) is None
    assert result['exit_status'] == (0 if finished else 126)
    assert result['orphaned_descendants'] is (not finished)
    assert result['timed_out'] is False
    assert result['output_limit_exceeded'] is False
    assert result['interrupted'] is False

