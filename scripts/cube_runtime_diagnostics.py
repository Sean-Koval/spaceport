#!/usr/bin/env python3
"""Failure-only, run-private Cube startup status/log filtering. No artifacts."""
import argparse
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
CAPTURE_BYTES = 65536
OUTPUT_BYTES = 8192
# Read only fixed identity labels and selected state fields. Never request env,
# configuration, mounts, Docker error strings or healthcheck command/output.
FORMAT = ('{"name":{{json .Name}},"id":{{json .Id}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"run_label":{{json (index .Config.Labels "gateway.cube.run")}},'
    '"status":{{json .State.Status}},"running":{{json .State.Running}},'
    '"exit_code":{{json .State.ExitCode}},"oom_killed":{{json .State.OOMKilled}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}}}')


def secret_values(directory):
    if not directory.is_absolute():
        raise ValueError()
    for path in (directory.parent, directory):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
            raise ValueError()
    names = set(json.loads((ROOT / "infra/compose.yaml").read_text())["secrets"])
    if {path.name for path in directory.iterdir()} != names or not 1 <= len(names) <= 20:
        raise ValueError()
    values = []
    for name in sorted(names):
        path = directory / name
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o444 or
            info.st_uid != os.getuid() or info.st_size != 44):
            raise ValueError()
        contents = path.read_text()
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}\n", contents):
            raise ValueError()
        values.append(contents[:-1])
    return values


def capture(arguments):
    # Stream into a strictly bounded buffer, never capture_output/communicate or
    # an unbounded file. stderr is merged because startup exceptions use it.
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + 5
    raw = bytearray()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise subprocess.TimeoutExpired(arguments, 5)
            chunk = os.read(process.stdout.fileno(), min(4096, CAPTURE_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > CAPTURE_BYTES:
                # A byte cut can split a secret. Suppress the entire oversized
                # excerpt, including every unmatched prefix; never emit raw.
                return "[startup log omitted: byte limit exceeded]"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(arguments, 5)
        result = process.wait(timeout=remaining)
        if result != 0:
            raise subprocess.CalledProcessError(result, arguments)
        return raw.decode("utf-8", errors="replace")
    finally:
        selector.close()
        # Kill only this Popen diagnostic client. No docker stop/kill operation
        # or process-group action can touch a runtime/unrelated container.
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1)
        finally:
            process.stdout.close()


def diagnose(directory, project, run_label):
    try:
        if not re.fullmatch(r"gateway-cube-[a-f0-9]{16}", project):
            raise ValueError()
        if run_label != project + "-local" and not re.fullmatch(r"[0-9]{1,20}-[0-9]{1,10}", run_label):
            raise ValueError()
        values = secret_values(directory)
    except (OSError, ValueError):
        print("Cube startup diagnostics unavailable: private run identity/secrets were not verified")
        return
    for service in ("cube", "cube-test"):
        try:
            name = f"{project}-{service}-1"
            record = json.loads(capture(["docker", "inspect", "--format", FORMAT, name]))
            if (record["name"] != "/" + name or record["project"] != project or
                record["service"] != service or record["run_label"] != run_label or
                not re.fullmatch(r"[a-f0-9]{64}", record["id"]) or
                record["status"] not in {"created", "running", "paused", "restarting", "removing", "exited", "dead"} or
                type(record["running"]) is not bool or type(record["oom_killed"]) is not bool or
                type(record["exit_code"]) is not int or not 0 <= record["exit_code"] <= 255 or
                record["health"] not in {"none", "starting", "healthy", "unhealthy"}):
                raise ValueError()
            selected = {key: record[key] for key in ("status", "running", "exit_code", "oom_killed", "health")}
            selected["service"] = service
            print("Cube startup status: " + json.dumps(selected, sort_keys=True, separators=(",", ":")))
            text = capture(["docker", "logs", "--tail", "80", record["id"]])
            for value in values:
                text = text.replace(value, "[REDACTED]")
            text = "".join(c if c in "\n\t" or c.isprintable() else "?" for c in text)
            # Prefix every line to neutralize workflow command/control content.
            emitted = "\n".join(f"Cube startup log [{service}]: {line}" for line in text.splitlines()[:80])
            print(emitted.encode("utf-8")[:OUTPUT_BYTES].decode("utf-8", errors="ignore"))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            print(f"Cube startup diagnostics unavailable for verified service {service}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bounded private synthetic Cube startup diagnostics")
    parser.add_argument("--secret-dir", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-label", required=True)
    args = parser.parse_args()
    diagnose(args.secret_dir, args.project, args.run_label)
