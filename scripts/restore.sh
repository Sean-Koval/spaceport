#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --locked --offline python -m scripts.demo_backup restore "$@"
