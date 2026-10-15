"""Offline harness admission probes. Simulated Docker is never runtime evidence."""
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]
PROJECT = "gateway-cube-" + "a" * 16
RUN_LABEL = "38474646900-1"
NETWORK = PROJECT + "_private"
SERVICES = ("cube", "cube-test", "demo-db", "registry-db", "edge", "cube-reviewed", "cube-reviewed-test")


def runtime_records():
    records = {}
    for number, service in enumerate(SERVICES, 1):
        identifier = str(number) * 64
        records[identifier] = {
            "Config": {"Labels": {"com.docker.compose.service": service,
                "com.docker.compose.project": PROJECT, "gateway.cube.run": RUN_LABEL}},
            "Name": f"/{PROJECT}-{service}-1",
            "State": {"Running": True, "Health": {"Status": "healthy"}},
            "NetworkSettings": {"Networks": {NETWORK: {"IPAddress": f"172.23.0.{number + 1}"}}},
            "HostConfig": {"PortBindings": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]} if service == "edge" else {}},
        }
    network = {"Internal": True, "Labels": {"com.docker.compose.project": PROJECT,
        "gateway.cube.run": RUN_LABEL}}
    return {"containers": records, "network": network}


@pytest.fixture
def topology_harness(monkeypatch, tmp_path):
    """Run the real capture/restart shell sections with deterministic Docker I/O."""
    bin_dir = tmp_path / "bin"
    evidence = tmp_path / "evidence"
    bin_dir.mkdir()
    evidence.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    calls = tmp_path / "calls.txt"
    source = (ROOT / "scripts/verify_cube_runtime.sh").read_text()
    capture = source.split("# Record safe topology only.", 1)[1].split("# Real release Node", 1)[0]
    # Retain the complete first comment line when cutting at a comment prefix.
    capture = "# Record safe topology only." + capture
    restart = "# Restart the actual databases" + source.split("# Restart the actual databases", 1)[1].split("# Existing registry verifier", 1)[0]
    bounded = source.split("bounded() {", 1)[1].split("\n}", 1)[0]
    startup = source.split("start_private_stack() {", 1)[1].split("\n}", 1)[0]
    driver = ("set -euo pipefail\n" + "bounded() {" + bounded + "\n}\n" +
        "start_private_stack() {" + startup + "\n}\n" +
        f"COMPOSE=(docker compose --project-name {shlex.quote(PROJECT)} -f infra/compose.yaml -f infra/compose.test.yaml)\n" +
        capture + restart)
    # Only these deterministic command responders are synthetic. Keep every
    # invocation/process boundary; do not spend an interpreter startup and JSON
    # parse on each canned Docker response. The real shell and inline admission
    # Python still execute twice, with their original bounded wrappers.
    log_command = r'''{
  printf '%s' "${0##*/}"
  printf '\t%s' "$@"
  printf '\n'
} >> "$SYNTHETIC_COMMAND_CALLS"
'''
    (bin_dir / "uv").write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + log_command + r'''
[[ $# -ge 4 && "$1" == run && "$2" == --locked && "$3" == --offline && "$4" == python ]]
shift 4
exec "$SYNTHETIC_PYTHON" "$@"
''')
    (bin_dir / "docker").write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + log_command + r'''
current="$SYNTHETIC_DOCKER_STATE/before"
if [[ -e "$SYNTHETIC_DOCKER_STATE/restarted" ]]; then
  current="$SYNTHETIC_DOCKER_STATE/after"
fi
case "${1:-}" in
  compose)
    [[ $# -ge 8 && "$2" == --project-name && "$3" == "$SYNTHETIC_PROJECT" &&
       "$4" == -f && "$5" == infra/compose.yaml && "$6" == -f && "$7" == infra/compose.test.yaml ]]
    shift 7
    case "$1" in
      ps)
        [[ $# == 3 && "$2" == --all && "$3" == --quiet ]]
        printf '%s\n' "$(<"$current/ids")"
        ;;
      restart)
        [[ $# == 7 && "$2" == registry-db && "$3" == demo-db && "$4" == cube &&
           "$5" == cube-test && "$6" == cube-reviewed && "$7" == cube-reviewed-test ]]
        : > "$SYNTHETIC_DOCKER_STATE/restarted"
        ;;
      up|run) [[ -e "$SYNTHETIC_DOCKER_STATE/restarted" ]] ;;
      *) exit 1 ;;
    esac
    ;;
  inspect)
    [[ $# == 2 && "$2" =~ ^[a-f0-9]{64}$ && -f "$current/$2.json" ]]
    printf '%s\n' "$(<"$current/$2.json")"
    ;;
  network)
    [[ $# == 3 && "$2" == inspect && "$3" == "${SYNTHETIC_PROJECT}_private" ]]
    printf '%s\n' "$(<"$current/network.json")"
    ;;
  *) exit 1 ;;
esac
''')
    for executable in bin_dir.iterdir():
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SYNTHETIC_DOCKER_STATE", str(state))
    monkeypatch.setenv("SYNTHETIC_PYTHON", sys.executable)
    monkeypatch.setenv("SYNTHETIC_COMMAND_CALLS", str(calls))
    monkeypatch.setenv("SYNTHETIC_PROJECT", PROJECT)
    monkeypatch.setenv("GATEWAY_PROBE_IMAGE", PROJECT + "-probe:local")
    monkeypatch.setenv("GATEWAY_CUBE_RUN_LABEL", RUN_LABEL)
    monkeypatch.setenv("GATEWAY_RUNTIME_EVIDENCE_DIR", str(evidence))
    monkeypatch.setenv("GATEWAY_CUBE_TEST_HOSTS_FILE", str(evidence / "hosts.json"))
    module = runpy.run_path(str(ROOT / "tests/integration/test_cube_compatibility.py"))

    def prepare(after):
        calls.write_text("")
        (state / "restarted").unlink(missing_ok=True)
        for name, records in (("before", runtime_records()), ("after", after)):
            snapshot = state / name
            snapshot.mkdir(exist_ok=True)
            for response in snapshot.iterdir():
                response.unlink()
            (snapshot / "ids").write_text("\n".join(records["containers"]))
            (snapshot / "network.json").write_text(json.dumps([records["network"]]))
            for identifier, record in records["containers"].items():
                (snapshot / f"{identifier}.json").write_text(json.dumps([record]))

    def command(*arguments):
        return subprocess.run(["docker", *arguments], cwd=ROOT, capture_output=True, text=True, timeout=5)

    def run(after):
        prepare(after)
        result = subprocess.run(["bash", "-c", driver], cwd=ROOT,
            capture_output=True, text=True, timeout=20)
        return result, module["CubeStack"], evidence

    run.calls = calls
    run.evidence = evidence
    run.prepare = prepare
    run.command = command
    return run


@pytest.mark.parametrize("rotate_addresses", [False, True])
def test_runtime_harness_admits_verified_post_restart_addresses(topology_harness, rotate_addresses):
    after = runtime_records()
    if rotate_addresses:
        # Restart preserves the container identities, but bridge addresses move.
        for number, record in enumerate(after["containers"].values(), 1):
            record["NetworkSettings"]["Networks"][NETWORK]["IPAddress"] = f"172.23.0.{20 + number}"
    result, stack_class, evidence = topology_harness(after)
    assert result.returncode == 0, result.stdout + result.stderr
    stack = stack_class()
    try:
        expected = {record["Config"]["Labels"]["com.docker.compose.service"]:
            record["NetworkSettings"]["Networks"][NETWORK]["IPAddress"]
            for record in after["containers"].values()
            if record["Config"]["Labels"]["com.docker.compose.service"] in {"cube", "cube-test", "demo-db", "cube-reviewed", "cube-reviewed-test"}}
        assert stack.host_mode is True
        assert stack.hosts == expected
        assert json.loads((evidence / "hosts.json").read_text())["hosts"] == expected
    finally:
        stack.client.close()


@pytest.mark.parametrize("defect", ["service", "project", "run_label", "name", "stopped",
    "unhealthy", "extra_network", "published_port", "external_network", "network_project", "network_run", "public_address", "replaced_container"])
@pytest.mark.parametrize("service", ["cube", "cube-reviewed", "cube-reviewed-test"])
def test_runtime_harness_still_rejects_widened_or_untrusted_restart(topology_harness, defect, service):
    after = runtime_records()
    identifier = str(SERVICES.index(service) + 1) * 64
    cube = after["containers"][identifier]
    if defect in {"service", "project", "run_label"}:
        key = {"service": "com.docker.compose.service", "project": "com.docker.compose.project", "run_label": "gateway.cube.run"}[defect]
        cube["Config"]["Labels"][key] = "untrusted"
    elif defect == "name":
        cube["Name"] = "/untrusted-cube-1"
    elif defect == "stopped":
        cube["State"]["Running"] = False
    elif defect == "unhealthy":
        cube["State"]["Health"]["Status"] = "unhealthy"
    elif defect == "extra_network":
        cube["NetworkSettings"]["Networks"]["untrusted"] = {"IPAddress": "172.24.0.2"}
    elif defect == "published_port":
        cube["HostConfig"]["PortBindings"] = {"4000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "4000"}]}
    elif defect == "public_address":
        cube["NetworkSettings"]["Networks"][NETWORK]["IPAddress"] = "1.1.1.1"
    elif defect == "replaced_container":
        after["containers"]["a" * 64] = after["containers"].pop(identifier)
    elif defect == "external_network":
        after["network"]["Internal"] = False
    else:
        key = "com.docker.compose.project" if defect == "network_project" else "gateway.cube.run"
        after["network"]["Labels"][key] = "untrusted"
    result, stack_class, _ = topology_harness(after)
    if result.returncode == 0:
        with pytest.raises(ValueError):
            stack_class()
    else:
        assert result.returncode == 1, result.stdout + result.stderr


@pytest.mark.parametrize("service", ["cube-reviewed", "cube-reviewed-test"])
def test_missing_reviewed_service_cannot_satisfy_required_backend_topology(topology_harness, service):
    after = runtime_records()
    after["containers"].pop(str(SERVICES.index(service) + 1) * 64)
    result, _, _ = topology_harness(after)
    assert result.returncode == 1


def test_topology_fixture_runs_both_full_capture_passes_through_real_shell(topology_harness):
    result, _, _ = topology_harness(runtime_records())
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [line.split("\t") for line in topology_harness.calls.read_text().splitlines()]
    docker = [call[1:] for call in calls if call[0] == "docker"]
    uv = [call[1:] for call in calls if call[0] == "uv"]
    reads = [call for call in docker if call[0] != "compose"]
    identifiers = list(runtime_records()["containers"])
    capture = [command for identifier in identifiers for command in (
        ["inspect", identifier], ["network", "inspect", NETWORK], ["network", "inspect", NETWORK])]
    capture += [["inspect", identifier] for identifier in identifiers]
    assert reads == capture * 2
    assert len(docker) == 60 and len(uv) == 2
    assert all(call[:4] == ["run", "--locked", "--offline", "python"] for call in uv)
    compose = [call[7:] for call in docker if call[0] == "compose"]
    assert [call[0] for call in compose] == ["ps", "restart", "up", "run"]
    assert compose[1] == ["restart", "registry-db", "demo-db", "cube", "cube-test", "cube-reviewed", "cube-reviewed-test"]
    prefix = ["compose", "--project-name", PROJECT, "-f", "infra/compose.yaml", "-f", "infra/compose.test.yaml"]
    assert all(call[:7] == prefix for call in docker if call[0] == "compose")
    assert uv[0] == uv[1]
    assert uv[0] == ["run", "--locked", "--offline", "python", "-", str(topology_harness.evidence / "topology.json"), *identifiers]
    assert calls == [["docker", *prefix, "ps", "--all", "--quiet"], ["uv", *uv[0]],
        *[["docker", *call] for call in capture], ["docker", *prefix, *compose[1]],
        ["docker", *prefix, *compose[2]], ["uv", *uv[1]],
        *[["docker", *call] for call in capture], ["docker", *prefix, *compose[3]]]
    assert result.stdout.count("Observed private topology:") == 2


@pytest.mark.parametrize("arguments", [
    ("inspect", "1" * 64, "unexpected"),
    ("inspect", "a" * 64),
    ("network", "inspect", "untrusted"),
    ("network", "inspect", NETWORK, "unexpected"),
    ("version",),
    ("compose", "--project-name", "untrusted", "-f", "infra/compose.yaml", "-f", "infra/compose.test.yaml", "ps", "--all", "--quiet"),
    ("compose", "--project-name", PROJECT, "-f", "untrusted.yaml", "-f", "infra/compose.test.yaml", "ps", "--all", "--quiet"),
])
def test_topology_fixture_rejects_unknown_or_inexact_read_commands(topology_harness, arguments):
    topology_harness.prepare(runtime_records())
    assert topology_harness.command(*arguments).returncode != 0


def test_topology_fixture_changes_snapshot_only_after_exact_restart(topology_harness):
    after = runtime_records()
    identifier = "1" * 64
    after["containers"][identifier]["State"]["Running"] = False
    topology_harness.prepare(after)
    compose = ("compose", "--project-name", PROJECT, "-f", "infra/compose.yaml", "-f", "infra/compose.test.yaml")
    for verb in ("up", "run"):
        assert topology_harness.command(*compose, verb).returncode != 0
    assert json.loads(topology_harness.command("inspect", identifier).stdout)[0]["State"]["Running"] is True
    assert topology_harness.command(*compose, "restart", "cube").returncode != 0
    assert json.loads(topology_harness.command("inspect", identifier).stdout)[0]["State"]["Running"] is True
    assert topology_harness.command(*compose, "restart", "registry-db", "demo-db", "cube", "cube-test", "cube-reviewed", "cube-reviewed-test").returncode == 0
    assert json.loads(topology_harness.command("inspect", identifier).stdout)[0]["State"]["Running"] is False
    # Reusing the fixture must neither keep restart state nor stale responses.
    after["containers"].pop(identifier)
    topology_harness.prepare(after)
    assert json.loads(topology_harness.command("inspect", identifier).stdout)[0]["State"]["Running"] is True
    assert topology_harness.command(*compose, "restart", "registry-db", "demo-db", "cube", "cube-test", "cube-reviewed", "cube-reviewed-test").returncode == 0
    assert topology_harness.command("inspect", identifier).returncode != 0
