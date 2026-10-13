"""Real child-pytest diagnostics, separate from private-runtime verification."""
import io
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/pytest_runtime_diagnostics.py"


def load_diagnostics():
    assert SCRIPT.exists(), "Missing bounded diagnostic-only pytest runner"
    return runpy.run_path(str(SCRIPT))


def test_secret_inputs_are_explicit_bounded_files_only(tmp_path):
    diagnostics = load_diagnostics()
    path = tmp_path / "password"
    path.write_text("a" * 43)
    path.chmod(0o600)
    assert diagnostics["read_secrets"]([str(path)]) == ("a" * 43,)
    for bad_paths in [[], [str(path)] * 21, ["relative"], [str(tmp_path)]]:
        with pytest.raises((OSError, ValueError)):
            diagnostics["read_secrets"](bad_paths)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises((OSError, ValueError)):
        diagnostics["read_secrets"]([str(link)])
    path.write_text("a" * 10000)
    with pytest.raises((OSError, ValueError)):
        diagnostics["read_secrets"]([str(path)])


def test_stream_redacts_known_secrets_urls_jwts_and_neutralizes_control():
    diagnostics = load_diagnostics()
    secret = "runtime-known-secret"
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJuIn0.signature"
    raw = f"::error:: {secret} https://person:other-password@example.test/path {token}\n\x1b[31mnormal trace\n".encode()
    output = io.StringIO()
    diagnostics["stream_output"](io.BytesIO(raw), (secret,), output)
    text = output.getvalue()
    assert all(value not in text for value in (secret, "other-password", token, "\x1b"))
    assert all(line.startswith("pytest diagnostic: ") for line in text.splitlines())
    assert "normal trace" in text


def test_oversized_line_is_omitted_whole_without_secret_prefix_leak():
    diagnostics = load_diagnostics()
    limit = diagnostics["LINE_BYTES"]
    secret = "x" * 43
    raw = b"a" * (limit - 20) + secret.encode() + b"\nuseful next line\n"
    output = io.StringIO()
    diagnostics["stream_output"](io.BytesIO(raw), (secret,), output)
    text = output.getvalue()
    assert "omitted" in text and "useful next line" in text
    assert "x" * 10 not in text and "a" * 10 not in text


def test_output_budget_drains_stream_without_logging_overflow():
    diagnostics = load_diagnostics()
    stream = io.BytesIO(b"line\n" * diagnostics["OUTPUT_BYTES"])
    output = io.StringIO()
    diagnostics["stream_output"](stream, (), output)
    assert len(output.getvalue().encode()) <= diagnostics["OUTPUT_BYTES"] + 128
    assert stream.read() == b""


@pytest.mark.parametrize("failure", [False, True])
def test_actual_pytest_limits_only_failure_runs_and_labels_diagnostics(tmp_path, monkeypatch, capfd, failure):
    diagnostics = load_diagnostics()
    monkeypatch.chdir(tmp_path)
    cases = []
    for index in range(7):
        statement = 'raise RuntimeError("synthetic root cause")' if failure else 'assert True'
        cases.append(f"def test_diagnostic_case_{index}():\n    {statement}\n")
    (tmp_path / "test_cases.py").write_text("\n".join(cases))
    result = diagnostics["run_pytest"](())
    output = capfd.readouterr().out
    assert result == (1 if failure else 0)
    assert "DIAGNOSTIC ONLY" in output
    assert "full acceptance" in output
    assert "test_cases.py::test_diagnostic_case_0" in output
    if failure:
        assert "5 failed" in output and "synthetic root cause" in output
        assert "test_cases.py::test_diagnostic_case_5" not in output
    else:
        assert "7 passed" in output and "test_cases.py::test_diagnostic_case_6" in output


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGKILL])
def test_child_signal_is_preserved_by_cli(tmp_path, signum):
    diagnostics = load_diagnostics()
    secret = tmp_path / "password"
    secret.write_text("s" * 43)
    secret.chmod(0o600)
    (tmp_path / "test_signal.py").write_text(
        f"import os\ndef test_child_signal():\n    os.kill(os.getpid(), {signum})\n")
    result = subprocess.run([sys.executable, str(SCRIPT), "--secret-file", str(secret)],
        cwd=tmp_path, text=True, capture_output=True, timeout=10)
    assert result.returncode == -signum
    assert "DIAGNOSTIC ONLY" in result.stdout
    assert secret.read_text() not in result.stdout + result.stderr


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_helper_forwards_parent_signal_and_preserves_it(tmp_path, signum):
    load_diagnostics()
    import time
    secret = tmp_path / "password"
    secret.write_text("p" * 43)
    secret.chmod(0o600)
    marker = tmp_path / "started"
    (tmp_path / "test_wait.py").write_text(
        "import os, time\nfrom pathlib import Path\ndef test_wait():\n"
        "    Path('started').write_text(str(os.getpid()))\n    time.sleep(30)\n")
    child = subprocess.Popen([sys.executable, str(SCRIPT), "--secret-file", str(secret)],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "Diagnostic child did not start"
        child.send_signal(signum)
        output, _ = child.communicate(timeout=5)
        assert child.returncode == -signum
        assert "DIAGNOSTIC ONLY" in output
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


def test_explicit_fifo_secret_input_is_rejected_without_blocking(tmp_path):
    load_diagnostics()
    path = tmp_path / "fifo"
    os.mkfifo(path, 0o600)
    result = subprocess.run([sys.executable, str(SCRIPT), "--secret-file", str(path)],
        cwd=tmp_path, text=True, capture_output=True, timeout=1)
    assert result.returncode == 2
    assert "inputs were not verified" in result.stderr


def test_token_defense_keeps_exact_43_character_test_node_names():
    diagnostics = load_diagnostics()
    node = "tests/test_runtime.py::test_" + "n" * 38
    secret = "z" * 43
    output = io.StringIO()
    diagnostics["stream_output"](io.BytesIO(f"{node} FAILED\npassword='{secret}'\n".encode()), (), output)
    assert node in output.getvalue()
    assert secret not in output.getvalue()
