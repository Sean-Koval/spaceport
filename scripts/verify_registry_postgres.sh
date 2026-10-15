#!/usr/bin/env bash
# Disposable synthetic PostgreSQL gate. Never accepts an existing database URL.
set -euo pipefail
VERIFY_MODE=full
PARTITION_MODE=false
PARTITION_CANDIDATE=""
PARTITION_ROOT=""
readonly TRUSTED_SCRIPT_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
GATE_PYTHON=(uv run --locked --offline python)
INTEGRATION_STATE=""
INTEGRATION_RECEIPTS=""
CHECKPOINT=registry-preflight
failure_checkpoint() {
  local result="$1"
  if (( result != 0 )); then
    printf 'GATEWAY_FAILURE_EVIDENCE {"kind":"checkpoint","checkpoint":"%s","exit_status":%d}\n' "$CHECKPOINT" "$result" >&2
  fi
}
trap 'failure_checkpoint "$?"' EXIT
usage() {
  printf 'Usage: %s [--diagnostic | --backend-acceptance RECEIPTS_DIR | --demo-prerequisites STATE_DIR RECEIPTS_DIR]\n' "$0" >&2
  exit 2
}
case "${1:-}" in
  '') (( $# == 0 )) || usage ;;
  --diagnostic) (( $# == 1 )) || usage; VERIFY_MODE=diagnostic ;;
  --partition-acceptance)
    (( $# == 3 )) && [[ "$2" == /* && "$3" == /* ]] || usage
    PARTITION_MODE=true; PARTITION_CANDIDATE="$2"; PARTITION_ROOT="$3"
    VERIFY_MODE=backend-acceptance; INTEGRATION_RECEIPTS="$3/backends" ;;
  --backend-acceptance)
    (( $# == 2 )) && [[ "$2" == /* ]] || usage
    VERIFY_MODE=backend-acceptance; INTEGRATION_RECEIPTS="$2" ;;
  --demo-prerequisites)
    (( $# == 3 )) && [[ "$2" == /* && "$3" == /* ]] || usage
    VERIFY_MODE=demo-prerequisites; INTEGRATION_STATE="$2"; INTEGRATION_RECEIPTS="$3" ;;
  *) usage ;;
esac
readonly VERIFY_MODE INTEGRATION_STATE INTEGRATION_RECEIPTS PARTITION_MODE PARTITION_CANDIDATE PARTITION_ROOT
umask 077
if [[ "$PARTITION_MODE" == true ]]; then
  GATE_PYTHON=("$TRUSTED_SCRIPT_ROOT/.venv/bin/python" -I -S "$TRUSTED_SCRIPT_ROOT/scripts/ci_partition_driver.py"
    helper --candidate "$PARTITION_CANDIDATE" --root "$PARTITION_ROOT" --)
  cd "$PARTITION_CANDIDATE"
else
  cd "$TRUSTED_SCRIPT_ROOT"
fi
if [[ "$VERIFY_MODE" == backend-acceptance ]]; then
  "${GATE_PYTHON[@]}" -m scripts.gateway_integration --validate-backend-path "$INTEGRATION_RECEIPTS" || usage
elif [[ "$VERIFY_MODE" == demo-prerequisites ]]; then
  "${GATE_PYTHON[@]}" -m scripts.gateway_integration --validate-demo-paths "$INTEGRATION_STATE" "$INTEGRATION_RECEIPTS" || usage
fi
command -v docker >/dev/null || { printf 'BLOCKED: Docker is required for the real PostgreSQL gate.\n' >&2; exit 1; }
docker info --format '{{.ServerVersion}}' >/dev/null || { printf 'BLOCKED: Docker daemon unavailable.\n' >&2; exit 1; }
readonly POSTGRES_IMAGE='postgres:17.11-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826'
readonly RUN_ID="gateway-registry-test-$("${GATE_PYTHON[@]}" -c 'import secrets; print(secrets.token_hex(8))')"
readonly TEMP_DIR="$(mktemp -d)"
readonly CONTAINER="$RUN_ID"
RUN_LABEL="$RUN_ID-local"
if [[ "$VERIFY_MODE" == backend-acceptance || "$VERIFY_MODE" == demo-prerequisites ]]; then
  CHECKPOINT=registry-register-cleanup
  "${GATE_PYTHON[@]}" - "${INTEGRATION_RECEIPTS%/*}" "$RUN_ID" "$RUN_LABEL" <<'PYCLEANUP'
from pathlib import Path
import sys
from scripts.gateway_integration import register_compatibility
register_compatibility(Path(sys.argv[1]), 'registry', sys.argv[2], sys.argv[3])
PYCLEANUP
fi
readonly RUN_LABEL
CONTAINER_ID=""
cleanup() {
  failure_checkpoint "$?"
  if [[ -n "$CONTAINER_ID" ]]; then docker rm --force --volumes "$CONTAINER_ID" >/dev/null 2>&1 || true; fi
  rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
CHECKPOINT=registry-create-secret
"${GATE_PYTHON[@]}" - "$TEMP_DIR" <<'PY'
from pathlib import Path
import secrets
import sys
path=Path(sys.argv[1])
password=secrets.token_urlsafe(32)
(path/'postgres.env').write_text('POSTGRES_USER=gateway_test_admin\nPOSTGRES_DB=gateway_test_admin\nPOSTGRES_PASSWORD='+password+'\n')
(path/'password').write_text(password)
PY
# Pull preparation is explicit. No source credentials/secrets enter this container.
CHECKPOINT=registry-image
docker pull --platform linux/amd64 "$POSTGRES_IMAGE"
docker buildx imagetools inspect "$POSTGRES_IMAGE" --raw > "$TEMP_DIR/postgres-index.json"
"${GATE_PYTHON[@]}" - "$TEMP_DIR/postgres-index.json" <<'PYIMAGE'
import json
from pathlib import Path
import sys
index=json.loads(Path(sys.argv[1]).read_text())
children=[m['digest'] for m in index['manifests'] if m.get('platform',{}).get('os')=='linux' and m.get('platform',{}).get('architecture')=='amd64']
assert children==['sha256:66aafa11cf15800a3c94763f7e11d1e7b2e37e5e84bc4ce027cb6e1e4bfaf4df'], 'Unexpected PostgreSQL amd64 manifest'
print('Verified PostgreSQL linux/amd64 manifest: '+children[0])
PYIMAGE
printf 'PostgreSQL requested index: %s\n' "$POSTGRES_IMAGE"
docker image inspect "$POSTGRES_IMAGE" --format 'Pulled image ID: {{.Id}}; repo digests: {{json .RepoDigests}}'
CHECKPOINT=registry-start
CONTAINER_ID="$(docker run --detach --name "$CONTAINER" --platform linux/amd64 \
  --label gateway.registry.run="$RUN_LABEL" \
  --env-file "$TEMP_DIR/postgres.env" --publish 127.0.0.1::5432 \
  --memory 512m --cpus 1 --pids-limit 128 --security-opt no-new-privileges \
  "$POSTGRES_IMAGE")"
# The source journey launcher independently attests this exact disposable registry.
if [[ -n "${GATEWAY_SOURCE_JOURNEY_IMAGE_ID+x}" ]]; then
  export GATEWAY_SOURCE_JOURNEY_REGISTRY_CONTAINER="$CONTAINER_ID"
  export GATEWAY_SOURCE_JOURNEY_REGISTRY_RUN_LABEL="$RUN_LABEL"
fi
readonly PORT="$(docker inspect "$CONTAINER" --format '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}')"
[[ "$PORT" =~ ^[0-9]+$ ]]
CHECKPOINT=registry-readiness
if ! "${GATE_PYTHON[@]}" scripts/wait_registry_postgres.py "$TEMP_DIR/password" "$PORT" --timeout 60; then
  docker inspect "$CONTAINER" --format 'Container state={{.State.Status}}, exit={{.State.ExitCode}}' >&2
  exit 1
fi
CHECKPOINT=registry-identity
docker exec "$CONTAINER" postgres --version
docker inspect "$CONTAINER" --format 'Actual port bindings={{json .HostConfig.PortBindings}}'
export GATEWAY_TEST_POSTGRES_AUTHORIZATION=disposable-local-postgres-only
export GATEWAY_TEST_POSTGRES_ADMIN_URL="$("${GATE_PYTHON[@]}" - "$TEMP_DIR/password" "$PORT" <<'PY'
from pathlib import Path
import sys
from sqlalchemy.engine import URL
print(URL.create('postgresql+psycopg',username='gateway_test_admin',password=Path(sys.argv[1]).read_text(),host='127.0.0.1',port=int(sys.argv[2]),database='gateway_test_admin').render_as_string(hide_password=False))
PY
)"
# No connection URL, password, environment dump or database contents are logged.
# Explicit integration recorders run the complete selection themselves.
if [[ "$VERIFY_MODE" == full || "$VERIFY_MODE" == diagnostic ]]; then
CHECKPOINT=registry-tests
uv run --locked --offline pytest tests/integration/test_registry.py -m postgres -q --junitxml "$TEMP_DIR/registry.xml"
CHECKPOINT=registry-junit-admission
"${GATE_PYTHON[@]}" - "$TEMP_DIR/registry.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET
suites=ET.parse(sys.argv[1]).getroot().findall('testsuite')
assert suites and sum(int(s.get('tests','0')) for s in suites)>0, 'PostgreSQL gate had no tests'
assert all(int(s.get(k,'0'))==0 for s in suites for k in ('failures','errors','skipped')), 'PostgreSQL gate must not skip or fail'
print('Real PostgreSQL gate: '+str(sum(int(s.get('tests','0')) for s in suites))+' tests, zero skips/errors/failures')
PY
fi
# Full acceptance stays the default. Diagnostic runs are never acceptance.
run_project_verification() {
  if [[ "$VERIFY_MODE" == diagnostic ]]; then
    printf 'DIAGNOSTIC ONLY: full acceptance must run separately.\n'
    local secret_arguments=(--secret-file "$TEMP_DIR/password")
    if [[ -n "${GATEWAY_RUNTIME_SECRET_DIR:-}" ]]; then
      local name
      for name in registry_admin demo_admin registry_owner registry_app registry_audit_retention registry_source_reader demo_reader fixture_reader fixture_observer cube_signing_key cube_test_signing_key; do
        secret_arguments+=(--secret-file "$GATEWAY_RUNTIME_SECRET_DIR/$name")
      done
    fi
    "${GATE_PYTHON[@]}" scripts/pytest_runtime_diagnostics.py "${secret_arguments[@]}"
  elif [[ "$VERIFY_MODE" == demo-prerequisites ]]; then
    "${GATE_PYTHON[@]}" -m scripts.verify_demo --record-prerequisites --state-dir "$INTEGRATION_STATE" --prerequisites-dir "$INTEGRATION_RECEIPTS"
  elif [[ "$VERIFY_MODE" == backend-acceptance ]]; then
    "${GATE_PYTHON[@]}" -m scripts.gateway_integration --record-backends "$INTEGRATION_RECEIPTS"
  else
    uv run --locked --offline pytest -q
  fi
}
CHECKPOINT=registry-acceptance
run_project_verification
