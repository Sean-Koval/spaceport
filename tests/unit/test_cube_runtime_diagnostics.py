"""Diagnostic filtering only; fake Docker I/O never proves real startup behavior."""
import json
from pathlib import Path
import runpy
import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts/cube_runtime_diagnostics.py"
PROJECT = "gateway-cube-" + "a" * 16
RUN_LABEL = "38454938437-1"


def load():
    assert MODULE.exists(), "Missing safe run-private Cube startup diagnostics"
    return runpy.run_path(str(MODULE))


def secrets(tmp_path):
    directory = tmp_path / "secrets"
    directory.mkdir(mode=0o700)
    runpy.run_path(str(ROOT / "scripts/cube_runtime_secrets.py"))["prepare"](directory)
    return directory, [file.read_text().strip() for file in directory.iterdir()]


def docker_io(monkeypatch, module, log, alter=None):
    calls = []

    def capture(arguments):
        calls.append(arguments)
        assert arguments[0] == "docker"
        if arguments[1] == "inspect":
            assert arguments[2] == "--format"
            assert ".Config.Env" not in arguments[3] and ".Mounts" not in arguments[3]
            service = arguments[-1].removeprefix(PROJECT + "-").removesuffix("-1")
            assert service in {"cube", "cube-test", "cube-reviewed", "cube-reviewed-test"}
            assert arguments[-1] == f"{PROJECT}-{service}-1"
            record = {"name": f"/{PROJECT}-{service}-1", "project": PROJECT,
                "service": service, "run_label": RUN_LABEL,
                "id": {"cube": "b", "cube-test": "c", "cube-reviewed": "d", "cube-reviewed-test": "e"}[service] * 64,
                "status": "exited", "running": False, "exit_code": 1,
                "oom_killed": False, "health": "unhealthy"}
            if alter:
                record.update(alter)
            return json.dumps(record)
        else:
            assert arguments[1:4] == ["logs", "--tail", "80"]
            assert arguments[-1] in {letter * 64 for letter in "bcde"}
            return log

    monkeypatch.setitem(module["diagnose"].__globals__, "capture", capture)
    return calls


def assert_no_values(output, values):
    if any(value in output for value in values):
        pytest.fail("Disposable synthetic secret appeared in diagnostic output", pytrace=False)


def test_diagnostics_emit_only_bounded_filtered_logs_and_selected_status(monkeypatch, tmp_path, capsys):
    module = load()
    directory, values = secrets(tmp_path)
    calls = docker_io(monkeypatch, module, "Fatal startup: " + " ".join(values) +
        "\n\x1b[31m::error::untrusted log text\x1b[0m\n")
    module["diagnose"](directory, PROJECT, RUN_LABEL)
    output = capsys.readouterr().out
    assert_no_values(output, values)
    assert "[REDACTED]" in output and "Fatal startup" in output
    assert "\x1b" not in output
    assert not any(line.startswith("::") for line in output.splitlines())
    assert output.count('"exit_code":1') == 4
    assert len([call for call in calls if call[1] == "logs"]) == 4
    assert {call[-1] for call in calls if call[1] == "inspect"} == {
        f"{PROJECT}-{service}-1" for service in ("cube", "cube-test", "cube-reviewed", "cube-reviewed-test")}


@pytest.mark.parametrize("alter", [{"project": "other"}, {"run_label": "stale"},
    {"service": "registry-db"}, {"name": "/stale-cube-1"}])
def test_diagnostics_refuse_logs_from_mismatched_container_identity(monkeypatch, tmp_path, capsys, alter):
    module = load()
    directory, values = secrets(tmp_path)
    calls = docker_io(monkeypatch, module, "must not be read", alter)
    module["diagnose"](directory, PROJECT, RUN_LABEL)
    output = capsys.readouterr().out
    assert "must not be read" not in output
    assert all(call[1] == "inspect" for call in calls)
    assert_no_values(output, values)


def test_diagnostics_fail_closed_without_a_verified_private_secret_directory(monkeypatch, tmp_path, capsys):
    module = load()
    directory, values = secrets(tmp_path)
    calls = docker_io(monkeypatch, module, "must not be read")
    directory.chmod(0o755)
    try:
        module["diagnose"](directory, PROJECT, RUN_LABEL)
    finally:
        directory.chmod(0o700)
    assert calls == []
    assert_no_values(capsys.readouterr().out, values)


@pytest.mark.parametrize("defect", ["missing", "malformed"])
def test_diagnostics_refuse_incomplete_or_malformed_secret_inventory(monkeypatch, tmp_path, capsys, defect):
    module = load()
    directory, values = secrets(tmp_path)
    calls = docker_io(monkeypatch, module, "must not be read")
    file = next(directory.iterdir())
    if defect == "missing":
        file.unlink()
    else:
        file.chmod(0o600)
        file.write_text(file.read_text() + "\n")
        file.chmod(0o444)
    module["diagnose"](directory, PROJECT, RUN_LABEL)
    assert calls == []
    assert_no_values(capsys.readouterr().out, values)


def test_capture_stops_its_client_and_suppresses_a_secret_crossing_the_byte_bound(tmp_path):
    # This exercises a real local subprocess/pipe, not mocked Docker behavior.
    module = load()
    directory, values = secrets(tmp_path)
    log = tmp_path / "synthetic-log"
    first = "Known failure\n"
    log.write_text(first + "x" * (module["CAPTURE_BYTES"] - 20 - len(first)) + values[0] + "\n")
    command = [sys.executable, "-c", "import pathlib,sys,time; "
        "sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes()); "
        "sys.stdout.buffer.flush(); time.sleep(30)", str(log)]
    started = time.monotonic()
    output = module["capture"](command)
    assert time.monotonic() - started < 3
    assert output == "[startup log omitted: byte limit exceeded]"
    if values[0][:20] in output:
        pytest.fail("Partial disposable secret appeared in captured output", pytrace=False)
    assert_no_values(output, values)


def test_capture_terminates_only_its_client_and_discards_partial_secret_at_deadline(tmp_path):
    module = load()
    directory, values = secrets(tmp_path)
    partial = tmp_path / "partial-synthetic-log"
    partial.write_text(values[0][:20])
    command = [sys.executable, "-c", "import pathlib,sys,time; "
        "sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes()); "
        "sys.stdout.buffer.flush(); time.sleep(30)", str(partial)]
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        module["capture"](command)
    assert time.monotonic() - started < 7
    assert caught.value.output is None and caught.value.stderr is None


@pytest.mark.parametrize("failure", ["timeout", "read"])
def test_diagnostics_emit_only_fixed_status_for_incomplete_capture(monkeypatch, tmp_path, capsys, failure):
    module = load()
    directory, values = secrets(tmp_path)
    docker_io(monkeypatch, module, "unused")
    normal = module["diagnose"].__globals__["capture"]

    def incomplete(arguments):
        if arguments[1] != "logs":
            return normal(arguments)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(arguments, 5, output=values[0][:20])
        raise OSError(values[0][:20])

    monkeypatch.setitem(module["diagnose"].__globals__, "capture", incomplete)
    module["diagnose"](directory, PROJECT, RUN_LABEL)
    output = capsys.readouterr().out
    assert "Cube startup diagnostics unavailable" in output
    if values[0][:20] in output:
        pytest.fail("Incomplete disposable secret appeared in diagnostic output", pytrace=False)
    assert_no_values(output, values)
