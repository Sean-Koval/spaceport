#!/usr/bin/env python3
"""Prepare container-readable disposable files behind private host traversal.

File-backed Compose secrets preserve bind-mount permissions. These synthetic
0444 files are readable by PostgreSQL UID999 after dropping privileges; both
host directories must remain owned by the runner and 0700. Values never print.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
MESSAGE = "Choose an empty private synthetic secret directory inside a private runner directory"


def prepare(directory: Path) -> int:
    if not directory.is_absolute():
        raise ValueError(MESSAGE)
    for path in (directory.parent, directory):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
            raise ValueError(MESSAGE)
    if any(directory.iterdir()):
        raise ValueError(MESSAGE)
    names = list(json.loads((ROOT / "infra/compose.yaml").read_text())["secrets"])
    if not names or len(names) > 20 or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) for name in names):
        raise ValueError("Invalid synthetic runtime secret declarations")
    written = []
    try:
        for name in names:
            path = directory / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            written.append(path)
            with os.fdopen(descriptor, "w") as file:
                file.write(secrets.token_urlsafe(32) + "\n")
                file.flush()
                os.fsync(file.fileno())
                # Individual Docker read-only bind mounts preserve this mode.
                # Host traversal is still blocked by the two verified 0700 dirs.
                os.fchmod(file.fileno(), 0o444)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return len(names)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare only disposable local synthetic runtime secrets")
    parser.add_argument("--secret-dir", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        count = prepare(arguments.secret_dir)
    except (OSError, ValueError):
        print(MESSAGE, file=sys.stderr)
        raise SystemExit(1)
    print(f"Prepared {count} disposable synthetic secret files behind private host directories")
