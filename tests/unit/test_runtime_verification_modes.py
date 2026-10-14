"""Offline command/diagnostic regressions; never private-runtime evidence."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]


def shell_function(path, name):
    source = (ROOT / path).read_text()
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}", source, re.MULTILINE | re.DOTALL)
    assert match, f"Missing explicit {name} mode dispatcher"
    return match.group(0)


@pytest.mark.parametrize("script", ["verify_cube_runtime.sh", "verify_registry_postgres.sh"])
@pytest.mark.parametrize("arguments", [["--unknown"], ["--diagnostic", "extra"], ["--demo-prerequisites"], ["--backend-acceptance"], ["--backend-acceptance", "relative"], ["--demo-prerequisites", "relative", "also-relative"]])
def test_harness_rejects_unknown_modes_before_any_docker_call(tmp_path, script, arguments):
    marker = tmp_path / "docker-called"
    docker = tmp_path / "docker"
    docker.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 77\n')
    docker.chmod(0o755)
    result = subprocess.run(["bash", str(ROOT / "scripts" / script), *arguments],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        text=True, capture_output=True, timeout=5)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Usage:" in result.stderr
    assert not marker.exists(), "Invalid mode reached runtime initialization"


def invoke_dispatcher(tmp_path, script, function, mode, boundary, status=0):
    output = tmp_path / "arguments.json"
    recorder = tmp_path / "record.py"
    recorder.write_text("import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
        f"raise SystemExit({status})\n")
    code = ('set -euo pipefail\n' + shell_function(script, function) + '\n'
        f'{boundary}() {{ "$PYTHON" "$RECORDER" "$@"; }}\n'
        f'VERIFY_MODE="$MODE"\nTEMP_DIR="$TEMP"\nINTEGRATION_STATE="$TEMP/state"\nINTEGRATION_RECEIPTS="$TEMP/receipts"\nunset GATEWAY_RUNTIME_SECRET_DIR\n{function}\n')
    result = subprocess.run(["bash", "-c", code], text=True, capture_output=True,
        env={**os.environ, "CAPTURE": str(output), "RECORDER": str(recorder),
            "PYTHON": sys.executable, "MODE": mode, "TEMP": str(tmp_path)}, timeout=5)
    return result, json.loads(output.read_text())


@pytest.mark.parametrize("mode", ["full", "diagnostic"])
def test_cube_mode_forwarding_keeps_360_second_bound(tmp_path, mode):
    result, arguments = invoke_dispatcher(tmp_path, "scripts/verify_cube_runtime.sh",
        "run_registry_verification", mode, "bounded")
    assert result.returncode == 0
    assert arguments == ["360", "bash", "scripts/verify_registry_postgres.sh"] + (
        ["--diagnostic"] if mode == "diagnostic" else [])


@pytest.mark.parametrize("mode", ["full", "diagnostic"])
@pytest.mark.parametrize("status", [0, 1, 2])
def test_project_mode_preserves_full_command_and_exit_status(tmp_path, mode, status):
    result, arguments = invoke_dispatcher(tmp_path, "scripts/verify_registry_postgres.sh",
        "run_project_verification", mode, "uv", status)
    assert result.returncode == status
    assert arguments == ["run", "--locked", "--offline"] + (
        ["python", "scripts/pytest_runtime_diagnostics.py", "--secret-file", str(tmp_path / "password")] if mode == "diagnostic" else ["pytest", "-q"])
    if mode == "diagnostic":
        assert "DIAGNOSTIC ONLY" in result.stdout
    else:
        assert "DIAGNOSTIC ONLY" not in result.stdout


def workflow_runtime_step():
    source = (ROOT / ".github/workflows/verify-cube.yml").read_text()
    assert re.search(r"verification_mode:\n\s+description:.*\n\s+required: true\n\s+default: full\n\s+type: choice\n\s+options:\n\s+- full\n\s+- diagnostic", source)
    assert "timeout-minutes: 20" in source and "contents: read" in source
    step = source.split("      - name: Verify pinned real Cube synthetic boundary\n", 1)[1].split("      - name:", 1)[0]
    assert "VERIFICATION_MODE: ${{ inputs.verification_mode }}" in step
    assert "run: |\n" in step
    code = step.split("run: |\n", 1)[1]
    assert "${{" not in code, "Workflow input must not interpolate into shell source"
    return "\n".join(line[10:] for line in code.splitlines())


@pytest.mark.parametrize("mode,expected", [("full", 0), ("diagnostic", 0), ("invalid", 2), ("$(touch injected)", 2)])
def test_workflow_mode_is_explicit_and_strict(tmp_path, mode, expected):
    script = workflow_runtime_step()
    result = subprocess.run(["bash", "-c", 'bash() { printf "SCRIPT:%s\\n" "$*"; }\n' + script],
        cwd=tmp_path, env={**os.environ, "VERIFICATION_MODE": mode}, text=True, capture_output=True, timeout=5)
    assert result.returncode == expected
    assert not (tmp_path / "injected").exists()
    if expected == 0:
        assert result.stdout.strip() == "SCRIPT:scripts/verify_cube_runtime.sh" + (
            " --diagnostic" if mode == "diagnostic" else "")
    else:
        assert "SCRIPT:" not in result.stdout


@pytest.mark.parametrize("mode", ["demo-prerequisites", "backend-acceptance"])
def test_cube_explicit_integration_modes_keep_bound_and_exact_paths(tmp_path, mode):
    result, arguments = invoke_dispatcher(tmp_path, "scripts/verify_cube_runtime.sh",
        "run_registry_verification", mode, "bounded")
    assert result.returncode == 0
    assert arguments == ["600" if mode == "backend-acceptance" else "360", "bash", "scripts/verify_registry_postgres.sh", "--" + mode] + (
        [str(tmp_path / "state"), str(tmp_path / "receipts")] if mode == "demo-prerequisites" else [str(tmp_path / "receipts")])


@pytest.mark.parametrize("mode", ["demo-prerequisites", "backend-acceptance"])
@pytest.mark.parametrize("status", [0, 1, 2])
def test_registry_explicit_integration_modes_do_not_fall_through_to_full_pytest(tmp_path, mode, status):
    result, arguments = invoke_dispatcher(tmp_path, "scripts/verify_registry_postgres.sh",
        "run_project_verification", mode, "uv", status)
    assert result.returncode == status
    prefix = ["run", "--locked", "--offline", "python", "-m"]
    assert arguments == prefix + (
        ["scripts.verify_demo", "--record-prerequisites", "--state-dir", str(tmp_path / "state"), "--prerequisites-dir", str(tmp_path / "receipts")]
        if mode == "demo-prerequisites" else ["scripts.gateway_integration", "--record-backends", str(tmp_path / "receipts")])
