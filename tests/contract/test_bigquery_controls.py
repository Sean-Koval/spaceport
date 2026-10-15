"""Offline executable controls: actual filesystem/processes, simulated provider.

The small superclass harness is not the installed Cube package. Neither this
suite nor its source assets establishes packaged compatibility or live access.
"""
from pathlib import Path
import subprocess

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]


def test_bigquery_offline_controls_with_real_storage_and_process_exclusivity():
    completed = subprocess.run(
        ["node", "--test", "--test-reporter=tap", *[f"tests/node/bigquery-{name}.test.cjs" for name in ("ledger", "driver", "assets")]],
        cwd=ROOT, text=True, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "# fail 0" in completed.stdout
    assert "# skipped 0" in completed.stdout


def test_candidate_has_no_runtime_or_gateway_activation():
    import json
    from gateway.adapters.bigquery import require_live_source_bounds
    from gateway.contracts.errors import GatewayError
    source = json.loads((ROOT / "fixtures/adapter/bigquery.json").read_text())["source_settings"]
    with pytest.raises(GatewayError, match="source cost and lifetime"):
        require_live_source_bounds(source)
    for relative in ("gateway/runtime.py", "gateway/pilot_runtime.py", "cube/cube.js"):
        assert "bigquery/bounded-driver" not in (ROOT / relative).read_text()
