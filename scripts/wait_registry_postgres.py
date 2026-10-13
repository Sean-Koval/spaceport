"""Bounded authenticated readiness through the exact published loopback port.

A Unix-socket-only image initialization server or an open non-PostgreSQL port
cannot satisfy this probe. Credentials are read from a synthetic private file;
errors and connection strings are never printed.
"""
import argparse
from pathlib import Path
import sys
import time

import psycopg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("password_file", type=Path)
    parser.add_argument("port", type=int)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    if not 0 < args.timeout <= 60 or not 0 < args.port < 65536:
        parser.error("Readiness requires a valid port and a timeout in (0,60] seconds")
    password = args.password_file.read_text()
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(host="127.0.0.1", port=args.port, user="gateway_test_admin",
                dbname="gateway_test_admin", password=password, connect_timeout=2,
                options="-c statement_timeout=1000") as connection:
                observed = connection.execute("SELECT 1,current_database(),current_user").fetchone()
                if observed == (1, "gateway_test_admin", "gateway_test_admin"):
                    print("Disposable PostgreSQL authenticated TCP readiness confirmed.")
                    return 0
        except psycopg.Error:
            pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.25, remaining))
    print("BLOCKED: Disposable PostgreSQL authenticated TCP readiness timed out.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
