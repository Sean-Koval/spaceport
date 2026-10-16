"""Static closed workflow authority checks, not hosted execution evidence."""
import hashlib
from pathlib import Path
import re

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / '.github/workflows/verify-gateway-partitions.yml'
LANES = ('sql_owners','sql_execution','sql_publishing','sql_registry_results','runtime_process_socket')


def source():
    return PATH.read_text()


def jobs():
    text=source(); matches=list(re.finditer(r'^  ([a-z_]+):\n',text.split('jobs:\n',1)[1],re.M))
    body=text.split('jobs:\n',1)[1]
    return {m[1]:body[m.start():matches[i+1].start() if i+1<len(matches) else len(body)] for i,m in enumerate(matches)}


def test_fixed_manual_serial_graph_and_unchanged_permissions():
    text=source(); sections=jobs()
    assert tuple(sections)==('source',*LANES,'aggregate')
    assert 'workflow_dispatch:' in text and 'push:' not in text and 'pull_request:' not in text
    assert 'permissions:\n  contents: read' in text
    assert 'group: data-product-gateway-manual-verification\n  cancel-in-progress: false' in text
    assert 'strategy:' not in text and 'matrix:' not in text
    for i,lane in enumerate(LANES):
        needs='[source]' if not i else '[source, '+LANES[i-1]+']'
        assert 'needs: '+needs in sections[lane]
        assert "if: ${{ always() && !cancelled() && needs.source.result == 'success' }}" in sections[lane]
        assert 'timeout-minutes: 20' in sections[lane]
    assert 'timeout-minutes: 20' in sections['source']
    assert 'needs: [source, '+', '.join(LANES)+']' in sections['aggregate']
    assert 'if: always()' in sections['aggregate'] and 'timeout-minutes: 5' in sections['aggregate']


def test_no_artifacts_cache_writes_arbitrary_selector_or_credentials():
    text=source()
    for forbidden in ('upload-artifact','download-artifact','actions/cache','secrets:', 'secrets.',
            'id-token:', 'contents: write','continue-on-error','workflow_call:', 'source_url:',
            'test_selection:', 'lane:', 'eval ', 'sudo ', 'apt-get ', 'docker login'):
        assert forbidden not in text
    assert set(re.findall(r'uses: (\S+)',text))=={
        'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
        'astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884',
        'actions/setup-node@820762786026740c76f36085b0efc47a31fe5020'}
    assert text.count('persist-credentials: false')==13


def test_each_lane_uses_exact_same_source_and_trusted_controller_paths():
    sections=jobs()
    for job in ('source',*LANES):
        text=sections[job]
        assert 'REVIEWED_SOURCE_SHA: ${{ inputs.source_sha }}' in text
        assert 'REVIEWED_MANIFEST_SHA256: ${{ inputs.manifest_sha256 }}' in text
        assert 'ref: ${{ github.workflow_sha }}\n          path: trusted' in text
        assert 'ref: ${{ inputs.source_sha }}\n          path: candidate' in text
        assert '"$GITHUB_WORKSPACE/trusted/.venv/bin/python" -I -S' in text
        assert '--job '+job+' > "$GATEWAY_CI_PARENT/console.log" 2>&1' in text
        assert 'uv sync --locked --dev --project trusted' in text
        assert 'uv sync --locked --dev --project candidate' in text
        assert 'if: always()\n        shell: bash' in text
        assert 'cleanup --candidate "$GITHUB_WORKSPACE/candidate"' in text
        assert 'finalize --candidate "$GITHUB_WORKSPACE/candidate"' in text
    assert 'path: candidate' not in sections['aggregate']
    assert 'uv sync' not in sections['aggregate']


def test_fixed_chunks_never_interpolate_data_into_shell():
    text=source()
    assert 'toJSON(needs)' not in text
    for job,body in jobs().items():
        lines=body.splitlines()
        in_run=False
        for line in lines:
            if line.strip()=='run: |':in_run=True
            elif line.startswith('      - '):in_run=False
            if in_run:assert '${{ needs.' not in line
    for i in range(8):assert f'SOURCE_MANIFEST_CHUNK_{i}:' in text
    assert 'SOURCE_MANIFEST_CHUNK_8:' not in text
    for i in range(3):assert f'SOURCE_RECEIPT_CHUNK_{i}:' in text
    assert 'SOURCE_RECEIPT_CHUNK_3:' not in text


def test_legacy_manual_workflow_and_entrypoint_bytes_are_unchanged():
    # Exact files from reviewed remote main tree953f742293590e00a3f12545f87a62a6a72baa45.
    # gateway_integration.py has one reviewed inert-observer revision; its original
    # bytes are also pinned by the prephase reconstruction/parity regression.
    # Hashes avoid requiring unavailable local-only commits in shallow CI checkouts.
    expected={
        '.github/workflows/verify-gateway.yml': '823ea58ae0aad3330bf88ddcde8293ae76c37bb4380bf922a257c8abb6649b92',
        'scripts/verify_gateway_integration.sh': 'b342989e5fce7686534d36e276c6be8a26ab1f893e0e7e73a355286a9b081c97',
        'scripts/gateway_integration.py': '32b68b6315ad2f2c255ec3f8af684113151e83ed56452155065c84b82f1f43cb',
        'scripts/runtime_process.py': 'c59b0e492db066ed4fe047b2f64d877f4640adb752122e077f51ad9835ca32d5',
    }
    for name,sha256 in expected.items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==sha256


def test_partition_shell_helper_and_registry_child_stay_in_trusted_root():
    for name in ('verify_cube_runtime.sh','verify_registry_postgres.sh'):
        text=(ROOT/'scripts'/name).read_text()
        assert '--partition-acceptance)' in text
        assert 'GATE_PYTHON=("$TRUSTED_SCRIPT_ROOT/.venv/bin/python" -I -S' in text
        assert 'helper --candidate "$PARTITION_CANDIDATE" --root "$PARTITION_ROOT" --)' in text
        assert 'GATE_PYTHON=(uv run --locked --offline python)' in text
        assert 'if [[ "$PARTITION_MODE" == true ]]; then' in text
    text=(ROOT/'scripts/verify_cube_runtime.sh').read_text()
    assert 'bounded 600 bash "$TRUSTED_SCRIPT_ROOT/scripts/verify_registry_postgres.sh"' in text
    assert 'bounded 600 bash scripts/verify_registry_postgres.sh --backend-acceptance' in text


@pytest.mark.parametrize('status',[0,37])
def test_trusted_partition_registry_child_executes_exact_fixed_argv(tmp_path,status):
    import json,os,subprocess,sys
    text=(ROOT/'scripts/verify_cube_runtime.sh').read_text()
    function='run_registry_verification() {'+text.split('run_registry_verification() {',1)[1].split('\n}',1)[0]+'\n}'
    recorder=tmp_path/'record.py';output=tmp_path/'argv.json'
    recorder.write_text('import json,os,pathlib,sys\n'
        'pathlib.Path(os.environ["CAPTURE"]).write_text(json.dumps(sys.argv[1:]))\n'
        'raise SystemExit(int(os.environ["STATUS"]))\n')
    program='set -euo pipefail\n'+function+'\n'+'''bounded() { "$PYTHON" "$RECORDER" "$@"; }
VERIFY_MODE=backend-acceptance
PARTITION_MODE=true
TRUSTED_SCRIPT_ROOT=/exact/trusted
PARTITION_CANDIDATE=/exact/candidate
PARTITION_ROOT=/exact/private/run
INTEGRATION_RECEIPTS=/exact/private/run/backends
run_registry_verification
'''
    result=subprocess.run(['bash','-c',program],capture_output=True,text=True,timeout=5,
        env={**os.environ,'CAPTURE':str(output),'RECORDER':str(recorder),'PYTHON':sys.executable,'STATUS':str(status)})
    assert result.returncode==status
    assert json.loads(output.read_text())==['600','bash','/exact/trusted/scripts/verify_registry_postgres.sh',
        '--partition-acceptance','/exact/candidate','/exact/private/run']
