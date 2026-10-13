#!/usr/bin/env python3
"""Explicit diagnostic-only pytest run with bounded, filtered log output.

Known run credentials come only from explicit private files. URL/JWT/token
patterns add defense in depth; they are not proof of universal secret removal.
"""
import argparse
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys

LINE_BYTES = 8192
OUTPUT_BYTES = 262144
PREFIX = "pytest diagnostic: "


def read_secrets(paths):
    if not 1 <= len(paths) <= 20:
        raise ValueError("Invalid explicit diagnostic secret inputs")
    values = []
    for name in paths:
        path = Path(name)
        parent = path.parent.lstat()
        if (not path.is_absolute() or not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700 or parent.st_uid != os.getuid()):
            raise ValueError("Invalid explicit diagnostic secret inputs")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) not in {0o600, 0o444} or info.st_size not in {43, 44}):
                raise ValueError("Invalid explicit diagnostic secret inputs")
            value = stream.read(45).decode("ascii")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}\n?", value):
            raise ValueError("Invalid explicit diagnostic secret inputs")
        values.append(value.rstrip("\n"))
    return tuple(values)


def stream_output(stream, secrets, output):
    emitted = 0
    suppressed = False
    while raw := stream.readline(LINE_BYTES + 1):
        if len(raw) > LINE_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(LINE_BYTES + 1)
            text = "[oversized line omitted]"
        else:
            text = raw.decode("utf-8", errors="replace").rstrip("\n")
            for value in secrets:
                text = text.replace(value, "[REDACTED]")
            text = re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
            text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED JWT]", text)
            text = re.sub(r"(?<![A-Za-z0-9_:/.-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", "[REDACTED TOKEN]", text)
            text = "".join(c if c == "\t" or c.isprintable() else "?" for c in text)
        line = PREFIX + text + "\n"
        size = len(line.encode("utf-8"))
        if emitted + size <= OUTPUT_BYTES:
            output.write(line)
            output.flush()
            emitted += size
        elif not suppressed:
            output.write(PREFIX + "[remaining output omitted: byte limit exceeded]\n")
            output.flush()
            suppressed = True


def run_pytest(secrets):
    print("DIAGNOSTIC ONLY: five-failure limit; this is not full acceptance.", flush=True)
    process = subprocess.Popen([sys.executable, "-m", "pytest", "-vv", "--maxfail=5",
        "--tb=short", "--no-showlocals"], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    received = []
    def forward(signum, frame):
        received.append(signum)
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            pass
    previous = {signum: signal.signal(signum, forward) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        stream_output(process.stdout, secrets, sys.stdout)
        result = process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    result = -received[0] if received else result
    print("DIAGNOSTIC ONLY: pytest finished; full acceptance must run separately.", flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret-file", action="append", required=True)
    args = parser.parse_args()
    try:
        secrets = read_secrets(args.secret_file)
    except (OSError, ValueError, UnicodeError):
        print("Diagnostic run blocked: explicit private secret inputs were not verified", file=sys.stderr)
        raise SystemExit(2)
    result = run_pytest(secrets)
    if result < 0:
        if -result not in {signal.SIGKILL, signal.SIGSTOP}:
            signal.signal(-result, signal.SIG_DFL)
        os.kill(os.getpid(), -result)
    raise SystemExit(result)
