"""Offline harness/assertion regression probes, never actual Cube evidence."""
import itertools
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    "north": (
        {"day": "2024-03-09", "segment": "A", "revenue": "10.10"},
        {"day": "2024-03-10", "segment": None, "revenue": "20.20"},
        {"day": "2024-03-11", "segment": "A", "revenue": "9007199254740993.01"},
    ),
    "south": (
        {"day": "2024-03-09", "segment": "A", "revenue": "100.00"},
        {"day": "2024-03-10", "segment": "B", "revenue": "0.01"},
    ),
}
TOTALS = {"north": "9007199254741023.31", "south": "100.01"}


@pytest.mark.parametrize("phase", [0, 1], ids=["initial", "after-restart"])
def test_each_probe_command_uses_host_writer_identity(tmp_path, phase):
    # Exercise the real shell command construction; do not run Docker here.
    source = (ROOT / "scripts/verify_cube_runtime.sh").read_text()
    commands = [line for line in source.splitlines()
        if re.search(r'^bounded .*"\$\{COMPOSE\[@\]\}" run .*\bprobe\b', line)]
    assert len(commands) == 2
    capture = tmp_path / "arguments.json"
    recorder = tmp_path / "record.py"
    recorder.write_text("import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n")
    # Only the Docker boundary is substituted; shell id expansion remains real.
    code = ('set -euo pipefail\n'
        'bounded() { shift; "$@"; }\n'
        'COMPOSE=("$PYTHON" "$RECORDER")\n' + commands[phase] + '\n')
    import sys
    completed = subprocess.run(["bash", "-c", code], text=True, capture_output=True,
        env={**os.environ, "CAPTURE": str(capture), "RECORDER": str(recorder), "PYTHON": sys.executable},
        timeout=5)
    assert completed.returncode == 0, completed.stderr
    arguments = json.loads(capture.read_text())
    assert "--user" in arguments, "Probe root-owned evidence cannot be overwritten by the host runner"
    assert arguments[arguments.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert arguments.count("--user") == 1
    assert "--rm" in arguments and "--no-deps" in arguments
    assert "--privileged" not in arguments
    assert commands[phase].startswith(f"bounded {240 if phase == 0 else 90} ")


@pytest.fixture
def exercise_adapter_row_assertion(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tests/integration"))
    module = runpy.run_path(str(ROOT / "tests/integration/test_cube_adapter_runtime.py"))
    assertion = module["test_actual_adapter_exact_grain_and_lossless_totals"]

    def exercise(tenant, rows, *, row_count=None):
        async def recorded_rows(*args, **kwargs):
            return SimpleNamespace(rows=tuple(rows), row_count=len(rows) if row_count is None else row_count,
                cache_status="unknown", truncated=False, freshness=SimpleNamespace(status="fresh"))
        monkeypatch.setitem(assertion.__globals__, "execute", recorded_rows)
        assertion(object(), "consumer", tenant, TOTALS[tenant])

    return exercise


@pytest.mark.parametrize("tenant", ["north", "south"])
def test_adapter_exact_rows_accept_every_unsorted_permutation(exercise_adapter_row_assertion, tenant):
    for rows in itertools.permutations(EXPECTED[tenant]):
        exercise_adapter_row_assertion(tenant, rows)


@pytest.mark.parametrize("counterexample", ["wrong_day", "wrong_segment", "redistributed_revenue",
    "extra_field", "duplicate", "hidden_duplicate", "missing_row"])
def test_adapter_exact_rows_reject_full_row_or_cardinality_changes(exercise_adapter_row_assertion, counterexample):
    rows = [dict(row) for row in EXPECTED["north"]]
    row_count = None
    if counterexample == "wrong_day":
        rows[0]["day"] = "2024-03-08"
    elif counterexample == "wrong_segment":
        rows[0]["segment"] = "B"
    elif counterexample == "redistributed_revenue":
        rows[0]["revenue"], rows[2]["revenue"] = rows[2]["revenue"], rows[0]["revenue"]
    elif counterexample == "extra_field":
        rows[0]["unreviewed"] = "value"
    elif counterexample in {"duplicate", "hidden_duplicate"}:
        rows.append({**rows[0], "revenue": "0.00"})
        if counterexample == "hidden_duplicate":
            row_count = 3
    else:
        rows.pop()
    with pytest.raises(AssertionError):
        exercise_adapter_row_assertion("north", rows, row_count=row_count)
