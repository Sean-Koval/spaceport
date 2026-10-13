#!/usr/bin/env bash
# Run-private synthetic gate. Never accepts an existing URL/database/secrets.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { printf 'BLOCKED: Docker is required for real Cube verification.\n' >&2; exit 1; }
docker info --format '{{.ServerVersion}}' >/dev/null || { printf 'BLOCKED: Docker daemon unavailable.\n' >&2; exit 1; }
readonly TEMP_DIR="$(mktemp -d)"
readonly PROJECT="gateway-cube-$(uv run --locked --offline python -c 'import secrets; print(secrets.token_hex(8))')"
export GATEWAY_RUNTIME_SECRET_DIR="$TEMP_DIR/secrets"
export GATEWAY_RUNTIME_EVIDENCE_DIR="$TEMP_DIR/evidence"
export GATEWAY_PROBE_IMAGE="$PROJECT-probe:local"
export GATEWAY_CUBE_RUN_LABEL="${GITHUB_RUN_ID:-$PROJECT}-${GITHUB_RUN_ATTEMPT:-local}"
COMPOSE=(docker compose --project-name "$PROJECT" -f infra/compose.yaml -f infra/compose.test.yaml)
cleanup() {
  "${COMPOSE[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker image rm "$GATEWAY_PROBE_IMAGE" >/dev/null 2>&1 || true
  rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir "$GATEWAY_RUNTIME_SECRET_DIR" "$GATEWAY_RUNTIME_EVIDENCE_DIR" "$TEMP_DIR/build"
uv run --locked --offline python scripts/cube_runtime_secrets.py --secret-dir "$GATEWAY_RUNTIME_SECRET_DIR"
# Preparation happens before the internally isolated services start. Exact
# official images only; this process has no registry login or external secrets.
uv run --locked --offline python - "$TEMP_DIR/images.txt" <<'PY'
import json
from pathlib import Path
import re
import sys
config=json.loads(Path('infra/compose.yaml').read_text())
images=sorted({service['image'] for service in config['services'].values()})
assert all(re.fullmatch(r'(postgres:17\.11-bookworm|cubejs/cube:v1\.8\.5|nginx:1\.30\.5-trixie)@sha256:[a-f0-9]{64}', image) for image in images)
Path(sys.argv[1]).write_text('\n'.join(images)+'\n')
PY
while IFS= read -r image; do
  docker pull --platform linux/amd64 "$image"
  docker image inspect "$image" --format 'Prepared image={{json .RepoDigests}}'
  docker buildx imagetools inspect "$image" --raw > "$TEMP_DIR/image-index.json"
  uv run --locked --offline python - "$image" "$TEMP_DIR/image-index.json" <<'PYIMAGE'
import json
from pathlib import Path
import sys
expected={
 'cubejs/cube':'sha256:eb5cd9cfb257df0f9cf6dfab3aef394c3958f8f5faa0e55fdbd1ae49afb52b8c',
 'postgres':'sha256:66aafa11cf15800a3c94763f7e11d1e7b2e37e5e84bc4ce027cb6e1e4bfaf4df',
 'nginx':'sha256:6e2a172ae5503c0df64cd3294b57c86394ced402016a8cc45436086fa51f5d02',
}
index=json.loads(Path(sys.argv[2]).read_text())
children=[m['digest'] for m in index['manifests'] if m.get('platform',{}).get('os')=='linux' and m.get('platform',{}).get('architecture')=='amd64']
assert children==[expected[sys.argv[1].split(':',1)[0]]], 'Unexpected platform manifest'
print('Verified prepared linux/amd64 manifest: '+children[0])
PYIMAGE
done < "$TEMP_DIR/images.txt"
# File-backed Compose secrets retain source permissions. Verify the actual
# intended non-root UID against individual read-only mounts before initialization.
for service in registry-db demo-db; do
  readarray -t names < <(uv run --locked --offline python - "$service" <<'PYNAMES'
import json
from pathlib import Path
import sys
print('\n'.join(json.loads(Path('infra/compose.yaml').read_text())['services'][sys.argv[1]]['secrets']))
PYNAMES
  )
  mounts=()
  for name in "${names[@]}"; do
    mounts+=(--mount "type=bind,source=$GATEWAY_RUNTIME_SECRET_DIR/$name,target=/run/secrets/$name,readonly")
  done
  docker run --rm --name "$PROJECT-read-$service" --label "gateway.cube.run=$GATEWAY_CUBE_RUN_LABEL" \
    --network none --user 999:999 --read-only --cap-drop ALL --security-opt no-new-privileges \
    --memory 64m --cpus 1 --pids-limit 32 "${mounts[@]}" --entrypoint sh \
    'postgres:17.11-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826' -c '
      set -eu
      test "$(id -u)" = 999
      for file in /run/secrets/*; do
        test -r "$file"
        LC_ALL=C grep -Eq "^[A-Za-z0-9_-]{43}$" "$file"
      done
      printf "Observed actual non-root mounted synthetic secret readability\n"
    '
done
# Copy the exact already-installed uv-managed Python and locked package set.
# No pip, curl, apt or mutable image/dependency resolution occurs in the build.
python_root=$(uv run --locked --offline python -c 'import sys; print(sys.base_prefix)')
packages=$(uv run --locked --offline python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
cp -a "$python_root" "$TEMP_DIR/build/python"
cp -a "$packages" "$TEMP_DIR/build/packages"
cp infra/Probe.Dockerfile "$TEMP_DIR/build/Dockerfile"
docker build --network none --label "gateway.cube.run=$GATEWAY_CUBE_RUN_LABEL" --tag "$GATEWAY_PROBE_IMAGE" "$TEMP_DIR/build"
docker run --rm --network none --entrypoint /opt/python/bin/python3.12 "$GATEWAY_PROBE_IMAGE" --version
"${COMPOSE[@]}" config --format json > "$GATEWAY_RUNTIME_EVIDENCE_DIR/compose.json"
"${COMPOSE[@]}" up -d --wait --wait-timeout 150 registry-db demo-db cube cube-test edge
# Initialization must succeed after the official entrypoint drops to UID999.
# Read-only per-service mounts are unchanged; no contents/passwords are logged.
for service in registry-db demo-db; do
  "${COMPOSE[@]}" exec -T --user 999:999 "$service" sh -c '
    set -eu
    test "$(id -u)" = 999
    for file in /run/secrets/*; do test -r "$file"; done
    printf "Observed initialized PostgreSQL non-root secret reads\n"
  '
done
registry_roles=$("${COMPOSE[@]}" exec -T --user 999:999 registry-db psql --no-psqlrc -U synthetic_admin -d gateway_registry -Atqc "SELECT count(*) FROM pg_roles WHERE rolname IN ('gateway_owner','gateway_app','gateway_audit_retention','gateway_source_reader') AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls")
test "$registry_roles" = 4
demo_roles=$("${COMPOSE[@]}" exec -T --user 999:999 demo-db psql --no-psqlrc -U synthetic_admin -d gateway_demo -Atqc "SELECT count(*) FROM pg_roles WHERE rolname IN ('source_owner','demo_reader','fixture_reader','fixture_observer') AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls")
test "$demo_roles" = 4
printf 'Observed both private PostgreSQL initializers with four non-elevated synthetic roles each\n'
"${COMPOSE[@]}" exec -T cube node -p 'require("@cubejs-backend/server-core/package.json").version'
"${COMPOSE[@]}" exec -T cube node -p 'require("jsonwebtoken/package.json").version'
"${COMPOSE[@]}" exec -T demo-db postgres --version
"${COMPOSE[@]}" exec -T edge nginx -v
"${COMPOSE[@]}" exec -T cube sh -c 'test "$(node -p '\''require("@cubejs-backend/server-core/package.json").version'\'')" = "1.8.5"'
# Record safe topology only. Never dump container env, mounts or credentials.
container_ids=$("${COMPOSE[@]}" ps --all --quiet)
uv run --locked --offline python - "$GATEWAY_RUNTIME_EVIDENCE_DIR/topology.json" $container_ids <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys
project=os.environ["GATEWAY_PROBE_IMAGE"].removesuffix("-probe:local")
run_label=os.environ["GATEWAY_CUBE_RUN_LABEL"]
records=[]
instances={}
for identifier in sys.argv[2:]:
    raw=json.loads(subprocess.check_output(['docker','inspect',identifier]))[0]
    name=raw['Config']['Labels']['com.docker.compose.service']
    assert name in {'cube','cube-test','demo-db','registry-db','edge'} and name not in instances
    assert raw['Config']['Labels']['com.docker.compose.project']==project
    assert raw['Config']['Labels']['gateway.cube.run']==run_label
    assert raw['Name']=='/'+project+'-'+name+'-1'
    assert raw['State']['Running']
    if name!='edge': assert raw['State']['Health']['Status']=='healthy'
    networks=list(raw['NetworkSettings']['Networks'])
    assert networks==[project+'_private']
    network=json.loads(subprocess.check_output(['docker','network','inspect',networks[0]]))[0]
    assert network['Labels']['com.docker.compose.project']==project
    assert network['Labels']['gateway.cube.run']==run_label and network['Internal']
    instances[name]=identifier
    internal=all(json.loads(subprocess.check_output(['docker','network','inspect',network]))[0]['Internal'] for network in networks)
    records.append({'service':raw['Config']['Labels']['com.docker.compose.service'],
        'ports':raw['HostConfig']['PortBindings'] or {}, 'internal_networks_only':bool(networks) and internal})
Path(sys.argv[1]).write_text(json.dumps(records,sort_keys=True,indent=2)+'\n')
print('Observed private topology: '+', '.join(sorted(row['service'] for row in records)))
assert set(instances)=={'cube','cube-test','demo-db','registry-db','edge'}
hosts={}
for identifier in sys.argv[2:]:
    raw=json.loads(subprocess.check_output(['docker','inspect',identifier]))[0]
    name=raw['Config']['Labels']['com.docker.compose.service']
    if name in ('cube','cube-test','demo-db'):
        assert raw['Config']['Labels']['gateway.cube.run']
        attachments=list(raw['NetworkSettings']['Networks'].values())
        assert len(attachments)==1
        hosts[name]=attachments[0]['IPAddress']
assert set(hosts)=={'cube','cube-test','demo-db'}
Path(sys.argv[1]).with_name('hosts.json').write_text(json.dumps({'kind':'private-synthetic-cube-only','hosts':hosts,'run_label':run_label,'project':project,'container_ids':{name:instances[name] for name in hosts}})+'\n')
PY
# Real release Node process executes the same security tests, without substitutes.
"${COMPOSE[@]}" exec -T -e GATEWAY_SECURITY_MODULE=/cube/conf/security.js cube node --test tests/runtime/context.test.js
"${COMPOSE[@]}" exec -T -e GATEWAY_CUBE_CONFIG_MODULE=/cube/conf/cube.js cube node --test tests/runtime/auth-wrapper.test.js
for service in cube cube-test; do
  "${COMPOSE[@]}" exec -T "$service" node - <<'JS'
const net=require('node:net');
Promise.all([["1.1.1.1",443],["8.8.8.8",53]].map(([host,port])=>new Promise((resolve,reject)=>{
 const socket=net.connect({host,port});
 socket.setTimeout(2000);
 socket.on('connect',()=>{socket.destroy();reject(new Error('Unexpected outbound connectivity'));});
 socket.on('error',()=>{socket.destroy();resolve();});
 socket.on('timeout',()=>{socket.destroy();resolve();});
}))).then(()=>console.log('Observed blocked Cube outbound connectivity')).catch(()=>process.exit(1));
JS
done
"${COMPOSE[@]}" run --rm --no-deps probe
uv run --locked --offline python - "$GATEWAY_RUNTIME_EVIDENCE_DIR/cube.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET
suites=ET.parse(sys.argv[1]).getroot().findall('testsuite')
assert suites and sum(int(s.get('tests','0')) for s in suites)>0
assert all(int(s.get(k,'0'))==0 for s in suites for k in ('errors','failures','skipped'))
print('Real pinned Cube gate: '+str(sum(int(s.get('tests','0')) for s in suites))+' tests, zero skips/errors/failures')
PY
# Restart the actual databases and both Cube endpoints, preserving named volumes.
"${COMPOSE[@]}" restart registry-db demo-db cube cube-test
"${COMPOSE[@]}" up -d --wait --wait-timeout 150 registry-db demo-db cube cube-test
"${COMPOSE[@]}" run --rm --no-deps -e GATEWAY_CUBE_RESTART_PHASE=after probe tests/integration/test_cube_compatibility.py -m cube -q -k 'known_answer_lossless_rows or runtime_watermark_is_load_time_persisted_across_restart'
# Existing registry verifier remains unchanged. Its final bare pytest now has
# both genuine backends. No Cube/source port is published: Linux runner reads
# fixed synthetic endpoint addresses derived from this exact Compose project.
export GATEWAY_CUBE_TEST_AUTHORIZATION=private-synthetic-cube-only
export GATEWAY_CUBE_TEST_HOSTS_FILE="$GATEWAY_RUNTIME_EVIDENCE_DIR/hosts.json"
export GATEWAY_CUBE_RESTART_PHASE=after
bash scripts/verify_registry_postgres.sh
# Selected, sanitized actual response shapes and source/network evidence only.
uv run --locked --offline python - "$GATEWAY_RUNTIME_EVIDENCE_DIR" <<'PY'
import json
from pathlib import Path
import sys
root=Path(sys.argv[1])
for name in ('meta.json','load-shape.json','pending.json','source-timeout.json','cube-source-stop.json','watermark-before-restart.json','probe-egress.json'):
    value=json.loads((root/name).read_text())
    print('Actual synthetic evidence '+name+': '+json.dumps(value,sort_keys=True,separators=(',',':')))
PY
