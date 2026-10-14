#!/usr/bin/env bash
# Explicit local integration entrypoint; no dispatch, elevation, or tool fallback.
set -euo pipefail
cd "$(dirname "$0")/.."
case "${1:-}" in
  --backend|--demo) (( $# == 2 )) || { printf 'Usage: %s (--backend|--demo) NEW_PRIVATE_RUN_DIR\n' "$0" >&2; exit 2; } ;;
  *) printf 'Usage: %s (--backend|--demo) NEW_PRIVATE_RUN_DIR\n' "$0" >&2; exit 2 ;;
esac
exec uv run --locked --offline python -m scripts.gateway_integration "$@"
