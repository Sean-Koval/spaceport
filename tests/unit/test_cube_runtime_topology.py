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
    state = tmp_path / "state.json"
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
    (bin_dir / "uv").write_text(f"#!{sys.executable}\nimport os, sys\n"
        "assert sys.argv[1:5] == ['run', '--locked', '--offline', 'python']\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[5:]])\n")
    (bin_dir / "docker").write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
path = Path(os.environ["SYNTHETIC_DOCKER_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
current = state["after"] if state.get("restarted") else state["before"]
if args[0] == "compose":
    assert args[1:3] == ["--project-name", os.environ["SYNTHETIC_PROJECT"]]
    command = args[7:]
    if command == ["ps", "--all", "--quiet"]:
        print("\\n".join(current["containers"]))
    elif command == ["restart", "registry-db", "demo-db", "cube", "cube-test", "cube-reviewed", "cube-reviewed-test"]:
        state["restarted"] = True
        path.write_text(json.dumps(state))
    elif command[:1] == ["up"]:
        assert state.get("restarted")
    elif command[:1] == ["run"]:
        assert state.get("restarted")
    else:
        raise AssertionError(command)
elif args[:1] == ["inspect"]:
    print(json.dumps([current["containers"][args[1]]]))
elif args[:2] == ["network", "inspect"]:
    assert args[2] == os.environ["SYNTHETIC_PROJECT"] + "_private"
    print(json.dumps([current["network"]]))
else:
    raise AssertionError(args)
''')
    for executable in bin_dir.iterdir():
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SYNTHETIC_DOCKER_STATE", str(state))
    monkeypatch.setenv("SYNTHETIC_PROJECT", PROJECT)
    monkeypatch.setenv("GATEWAY_PROBE_IMAGE", PROJECT + "-probe:local")
    monkeypatch.setenv("GATEWAY_CUBE_RUN_LABEL", RUN_LABEL)
    monkeypatch.setenv("GATEWAY_RUNTIME_EVIDENCE_DIR", str(evidence))
    monkeypatch.setenv("GATEWAY_CUBE_TEST_HOSTS_FILE", str(evidence / "hosts.json"))
    module = runpy.run_path(str(ROOT / "tests/integration/test_cube_compatibility.py"))

    def run(after):
        state.write_text(json.dumps({"before": runtime_records(), "after": after}))
        result = subprocess.run(["bash", "-c", driver], cwd=ROOT,
            capture_output=True, text=True, timeout=20)
        return result, module["CubeStack"], evidence

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
