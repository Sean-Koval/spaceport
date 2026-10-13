#!/usr/bin/env python3
"""Generate synthetic demo accounts locally. Never create production credentials.

Writes secrets only to a newly created private directory outside the repository.
Does not connect to a database; an operator applies the token-free bootstrap SQL.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys


REPOSITORY = Path(__file__).resolve().parents[1]
ACCOUNTS = (
    ("demo-north", "north", ("consumer",)),
    ("demo-south", "south", ("consumer",)),
    ("demo-author", "north", ("author",)),
    ("demo-publisher", "north", ("publisher",)),
)


def generate(output_dir: Path) -> None:
    if not output_dir.is_absolute():
        raise ValueError("Choose an absolute local output directory outside the repository")
    # Detect dangling final symlinks too, before resolution could redirect
    # exclusive creation to their nonexistent target.
    if os.path.lexists(output_dir):
        raise ValueError("Demo output path already exists")
    output = output_dir.resolve()
    if output == REPOSITORY or REPOSITORY in output.parents:
        raise ValueError("Demo tokens must be written outside the repository")
    # Existing paths are never reused/overwritten, including symbolic links.
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    written = []
    try:
        tokens = {principal: "dpg_demo_" + secrets.token_urlsafe(32) for principal, _, _ in ACCOUNTS}
        hashes = {"version": 1, "credentials": [{"digest": hashlib.sha256(tokens[principal].encode("ascii")).hexdigest(),
            "principal_id": principal, "kind": "demo", "active": True} for principal, _, _ in ACCOUNTS]}
        sql = ["-- Synthetic demo identities only. Apply as the registry owner/operator.",
               "-- The owner must first create the reviewed revenue-access policy.", "BEGIN;"]
        for principal, tenant, roles in ACCOUNTS:
            sql.append(f"INSERT INTO gateway.principals (id, subject, kind) VALUES ('{principal}', 'synthetic-{principal}', 'demo');")
            sql.append("INSERT INTO gateway.principal_grants (principal_id, policy_id, tenant_id, roles, entitlements) "
                f"VALUES ('{principal}', 'revenue-access', '{tenant}', '{json.dumps(roles)}', '{{\"groups\":[\"{tenant}\"]}}');")
        sql.append("COMMIT;")
        files = {"demo-tokens.json": json.dumps(tokens, indent=2) + "\n",
                 "credential-hashes.json": json.dumps(hashes, indent=2) + "\n",
                 "bootstrap.sql": "\n".join(sql) + "\n"}
        for name, content in files.items():
            path = output / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            written.append(path)
            with os.fdopen(fd, "w") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        output.rmdir()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Create isolated synthetic demo accounts without printing tokens")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        generate(args.output_dir)
    except (OSError, ValueError):
        print("Choose a new private local output directory outside the repository.", file=sys.stderr)
        return 1
    print("Synthetic demo accounts created locally. Apply bootstrap.sql as the registry owner; mount credential-hashes.json read-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
