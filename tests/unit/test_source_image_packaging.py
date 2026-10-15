"""The supported module CLI must be present in the runnable image."""
from pathlib import Path
import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]


def test_gateway_runtime_image_includes_supported_operator_cli():
    dockerfile = (ROOT/'gateway/Dockerfile').read_text()
    assert dockerfile.splitlines().count('COPY scripts ./scripts') == 1
    assert dockerfile.index('COPY scripts ./scripts') < dockerfile.index('USER 65532:65532')
    for name in ('spaceport.py','spaceport_client.py','spaceport_files.py'):
        assert (ROOT/'scripts'/name).is_file()
    assert 'ENTRYPOINT ["python"]' in dockerfile
    assert 'PYTHONPATH=/app' in dockerfile
    assert 'COPY . .' not in dockerfile


def test_actual_gateway_image_build_and_no_network_smoke_are_required_and_bounded():
    source=(ROOT/'scripts/verify_cube_runtime.sh').read_text()
    build='bounded 120 docker build --label "gateway.cube.run=$GATEWAY_CUBE_RUN_LABEL" --tag "$GATEWAY_APP_PROBE_IMAGE" -f gateway/Dockerfile .'
    cli='bounded 15 docker run --rm --label "gateway.cube.run=$GATEWAY_CUBE_RUN_LABEL" --network none --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --memory 256m --workdir /tmp "$GATEWAY_APP_PROBE_IMAGE" -m scripts.spaceport --help'
    imports='bounded 15 docker run --rm --label "gateway.cube.run=$GATEWAY_CUBE_RUN_LABEL" --network none --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --memory 256m "$GATEWAY_APP_PROBE_IMAGE" -c \'import gateway.source_pilot_runtime, gateway.source_pilot_config, gateway.product_packages; print("Verified gateway source runtime imports without network or credentials")\''
    assert source.splitlines().count(build)==source.splitlines().count(cli)==source.splitlines().count(imports)==1
    assert source.index(build)<source.index(cli)<source.index(imports)<source.index('CHECKPOINT=cube-stack-readiness')
    assert 'export GATEWAY_APP_PROBE_IMAGE="$PROJECT-app-probe:local"' in source
    assert 'docker image rm "$GATEWAY_APP_PROBE_IMAGE"' in source
    from scripts.pytest_runtime_diagnostics import CHECKPOINTS
    assert {'source-gateway-image','source-gateway-cli','source-gateway-import'}<=CHECKPOINTS


def test_actual_bigquery_runtime_image_is_built_and_qualified_before_sources():
    source=(ROOT/'scripts/verify_cube_runtime.sh').read_text()
    build='bounded 120 docker build --network none --platform linux/amd64 --label "gateway.cube.run=$GATEWAY_CUBE_RUN_LABEL" --tag "$GATEWAY_BIGQUERY_PROBE_IMAGE" -f cube/bigquery-runtime/Dockerfile .'
    smoke='bounded 30 uv run --locked --offline python -m tests.integration.source_runtime_image_gate --image "$GATEWAY_BIGQUERY_PROBE_IMAGE" --run-label "$GATEWAY_CUBE_RUN_LABEL"'
    assert source.splitlines().count(build)==source.splitlines().count(smoke)==1
    assert source.index('CHECKPOINT=source-gateway-import')<source.index(build)<source.index(smoke)<source.index('CHECKPOINT=cube-stack-readiness')
    assert 'export GATEWAY_BIGQUERY_PROBE_IMAGE="$PROJECT-bigquery:local"' in source
    assert 'bounded 15 docker image rm "$GATEWAY_BIGQUERY_PROBE_IMAGE"' in source
    from scripts.pytest_runtime_diagnostics import CHECKPOINTS
    assert {'source-image-build','source-image-smoke'}<=CHECKPOINTS


def test_combined_journey_uses_locked_inputs_and_keeps_backend_selection():
    source=(ROOT/'scripts/verify_cube_runtime.sh').read_text()
    registry=(ROOT/'scripts/verify_registry_postgres.sh').read_text()
    build='bounded 120 docker build --network none --platform linux/amd64 --label "gateway.cube.run=$GATEWAY_CUBE_RUN_LABEL" --build-arg "RUNTIME_IMAGE_ID=$GATEWAY_SOURCE_JOURNEY_RUNTIME_IMAGE_ID" --build-arg "PROBE_IMAGE_ID=$GATEWAY_SOURCE_JOURNEY_PROBE_IMAGE_ID" --tag "$GATEWAY_SOURCE_JOURNEY_IMAGE_TAG" -f "$TEMP_DIR/build/SourceJourney.Dockerfile" "$TEMP_DIR/build"'
    assert source.splitlines().count(build)==1
    assert 'cp -a cube/bigquery-runtime "$TEMP_DIR/build/bigquery-runtime"' in source
    assert 'cp infra/SourceJourney.Dockerfile "$TEMP_DIR/build/SourceJourney.Dockerfile"' in source
    assert '--build-arg "RUNTIME_IMAGE=' not in source
    assert '--build-arg "PROBE_IMAGE=' not in source
    assert source.index('CHECKPOINT=source-image-smoke')<source.index(build)<source.index('CHECKPOINT=cube-stack-readiness')
    for name in ('GATEWAY_SOURCE_JOURNEY_RUNTIME_IMAGE_ID','GATEWAY_SOURCE_JOURNEY_PROBE_IMAGE_ID','GATEWAY_SOURCE_JOURNEY_IMAGE_ID'):
        assert name in source and 'docker image inspect' in source
    assert source.index('docker image rm "$GATEWAY_SOURCE_JOURNEY_IMAGE_TAG"')<source.index('docker image rm "$GATEWAY_BIGQUERY_PROBE_IMAGE"')
    assert 'export GATEWAY_SOURCE_JOURNEY_REGISTRY_CONTAINER="$CONTAINER_ID"' in registry
    assert 'export GATEWAY_SOURCE_JOURNEY_REGISTRY_RUN_LABEL="$RUN_LABEL"' in registry
    from scripts.gateway_integration import BACKEND_SELECTION
    assert BACKEND_SELECTION==['-m','postgres or cube']
    from scripts.pytest_runtime_diagnostics import CHECKPOINTS
    assert 'source-journey-image' in CHECKPOINTS


@pytest.mark.parametrize(('original','cleanup_status','expected','retained'),[(0,0,0,False),(0,1,1,True),(7,0,7,False),(7,1,7,True)])
def test_every_mode_cleanup_preserves_failure_and_recovery_record(tmp_path,original,cleanup_status,expected,retained):
    import os
    import re
    import subprocess
    source=(ROOT/'scripts/verify_cube_runtime.sh').read_text()
    function=re.search(r'^cleanup\(\) \{\n.*?^\}',source,re.M|re.S).group(0)
    root=tmp_path/'private';root.mkdir(mode=0o700)
    (root/'cube-cleanup.json').write_text('{}')
    script='''
set -uo pipefail
COMPOSE=(docker compose)
GATEWAY_PROBE_IMAGE=synthetic-probe
failure_checkpoint() { :; }
bounded() {
  if [[ "$1" == 45 && "$2" == uv ]]; then
    [[ "$*" == *"python - $TEMP_DIR" ]] || exit 99
    cat >/dev/null
    return "$CLEANUP_STATUS"
  fi
  return 0
}
'''+function+'\n(exit "$ORIGINAL_STATUS"); cleanup\n'
    result=subprocess.run(['bash','-c',script],env={**os.environ,'TEMP_DIR':str(root),'CLEANUP_STATUS':str(cleanup_status),'ORIGINAL_STATUS':str(original)},capture_output=True,text=True,timeout=5)
    assert result.returncode==expected
    assert root.exists()==retained
    assert ('private recovery identity retained' in result.stderr)==retained
    assert source.index('PYREGISTERLOCAL\ntrap cleanup EXIT')<source.index('CHECKPOINT=cube-create-secrets')
    assert 'export GATEWAY_CUBE_RUN_LABEL="$PROJECT-local"' in source
    assert 'RUN_LABEL="$RUN_ID-local"' in (ROOT/'scripts/verify_registry_postgres.sh').read_text()
