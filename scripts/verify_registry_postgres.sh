#!/usr/bin/env bash
# Disposable synthetic PostgreSQL gate. Never accepts an existing database URL.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { printf 'BLOCKED: Docker is required for the real PostgreSQL gate.\n' >&2; exit 1; }
docker info --format '{{.ServerVersion}}' >/dev/null || { printf 'BLOCKED: Docker daemon unavailable.\n' >&2; exit 1; }
readonly POSTGRES_IMAGE='postgres:17.11-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826'
readonly RUN_ID="gateway-registry-test-$(uv run --locked --offline python -c 'import secrets; print(secrets.token_hex(8))')"
readonly TEMP_DIR="$(mktemp -d)"
readonly CONTAINER="$RUN_ID"
readonly RUN_LABEL="${GITHUB_RUN_ID:-$RUN_ID}-${GITHUB_RUN_ATTEMPT:-local}"
CONTAINER_ID=""
cleanup() {
  if [[ -n "$CONTAINER_ID" ]]; then docker rm --force --volumes "$CONTAINER_ID" >/dev/null 2>&1 || true; fi
  rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
uv run --locked --offline python - "$TEMP_DIR" <<'PY'
from pathlib import Path
import secrets
import sys
path=Path(sys.argv[1])
password=secrets.token_urlsafe(32)
(path/'postgres.env').write_text('POSTGRES_USER=gateway_test_admin\nPOSTGRES_DB=gateway_test_admin\nPOSTGRES_PASSWORD='+password+'\n')
(path/'password').write_text(password)
PY
# Pull preparation is explicit. No source credentials/secrets enter this container.
docker pull --platform linux/amd64 "$POSTGRES_IMAGE"
docker buildx imagetools inspect "$POSTGRES_IMAGE" --raw > "$TEMP_DIR/postgres-index.json"
uv run --locked --offline python - "$TEMP_DIR/postgres-index.json" <<'PYIMAGE'
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
CONTAINER_ID="$(docker run --detach --name "$CONTAINER" --platform linux/amd64 \
  --label gateway.registry.run="$RUN_LABEL" \
  --env-file "$TEMP_DIR/postgres.env" --publish 127.0.0.1::5432 \
  --memory 512m --cpus 1 --pids-limit 128 --security-opt no-new-privileges \
  "$POSTGRES_IMAGE")"
readonly PORT="$(docker inspect "$CONTAINER" --format '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}')"
[[ "$PORT" =~ ^[0-9]+$ ]]
if ! uv run --locked --offline python scripts/wait_registry_postgres.py "$TEMP_DIR/password" "$PORT" --timeout 60; then
  docker inspect "$CONTAINER" --format 'Container state={{.State.Status}}, exit={{.State.ExitCode}}' >&2
  exit 1
fi
docker exec "$CONTAINER" postgres --version
docker inspect "$CONTAINER" --format 'Actual port bindings={{json .HostConfig.PortBindings}}'
export GATEWAY_TEST_POSTGRES_AUTHORIZATION=disposable-local-postgres-only
export GATEWAY_TEST_POSTGRES_ADMIN_URL="$(uv run --locked --offline python - "$TEMP_DIR/password" "$PORT" <<'PY'
from pathlib import Path
import sys
from sqlalchemy.engine import URL
print(URL.create('postgresql+psycopg',username='gateway_test_admin',password=Path(sys.argv[1]).read_text(),host='127.0.0.1',port=int(sys.argv[2]),database='gateway_test_admin').render_as_string(hide_password=False))
PY
)"
# No connection URL, password, environment dump or database contents are logged.
uv run --locked --offline pytest tests/integration/test_registry.py -m postgres -q --junitxml "$TEMP_DIR/registry.xml"
uv run --locked --offline python - "$TEMP_DIR/registry.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET
suites=ET.parse(sys.argv[1]).getroot().findall('testsuite')
assert suites and sum(int(s.get('tests','0')) for s in suites)>0, 'PostgreSQL gate had no tests'
assert all(int(s.get(k,'0'))==0 for s in suites for k in ('failures','errors','skipped')), 'PostgreSQL gate must not skip or fail'
print('Real PostgreSQL gate: '+str(sum(int(s.get('tests','0')) for s in suites))+' tests, zero skips/errors/failures')
PY
# Full project verification catches regressions outside this task's file.
uv run --locked --offline pytest -q
