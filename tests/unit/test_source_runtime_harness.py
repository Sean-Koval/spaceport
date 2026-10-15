"""Synthetic source gate preparation; actual database/image evidence is hosted."""
import os
from pathlib import Path
import re
import runpy
import stat

import pytest
from tests.unit.ci_shell_probes import expanded_legacy_source

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]
NAMES = {'source_bq_consumer','source_bq_fixture','source_bq_observer',
    'source_domo_consumer','source_domo_fixture'}


def prepare(path):
    return runpy.run_path(str(ROOT / 'scripts/source_runtime_secrets.py'))['prepare'](path)


def test_source_synthetic_secrets_are_separate_fixed_and_container_readable(tmp_path):
    directory = tmp_path / 'source-secrets'
    directory.mkdir(mode=0o700)
    assert prepare(directory) == 5
    assert {item.name for item in directory.iterdir()} == NAMES
    values = []
    for item in directory.iterdir():
        info = item.stat()
        assert info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o444
        value = item.read_text()
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}\n',value):
            pytest.fail('Invalid synthetic secret shape',pytrace=False)
        values.append(value)
    assert len(set(values)) == 5


@pytest.mark.parametrize('kind',['nonempty','symlink','public_directory','public_parent'])
def test_source_secrets_reject_unsafe_or_existing_destination(tmp_path,kind):
    directory = tmp_path / 'source-secrets'
    directory.mkdir(mode=0o700)
    target = directory
    if kind == 'nonempty':
        (directory/'existing').write_text('untouched')
    elif kind == 'symlink':
        target = tmp_path/'alias'
        target.symlink_to(directory,target_is_directory=True)
    elif kind == 'public_directory':
        directory.chmod(0o755)
    else:
        tmp_path.chmod(0o755)
    try:
        with pytest.raises(ValueError):
            prepare(target)
        assert {item.name for item in directory.iterdir()} == ({'existing'} if kind=='nonempty' else set())
    finally:
        tmp_path.chmod(0o700)
        directory.chmod(0o700)


def test_source_overlay_separates_bootstrap_and_runtime_privilege():
    import json
    overlay = json.loads((ROOT/'infra/compose.source-test.yaml').read_text())
    assert set(overlay['services']) == {'cube-reviewed','source-bootstrap'}
    bootstrap = overlay['services']['source-bootstrap']
    runtime = overlay['services']['cube-reviewed']
    assert set(bootstrap['secrets']) == NAMES|{'registry_admin','registry_owner','registry_app'}
    assert set(runtime['secrets']) == {'source_bq_consumer','source_bq_fixture','source_bq_observer','cube_test_signing_key'}
    assert set(overlay['secrets']) == NAMES
    assert bootstrap['networks'] == ['private'] and 'ports' not in bootstrap
    assert bootstrap['environment']['PYTHONPATH'] == '/opt/packages:/app'
    assert all('GATEWAY_RUNTIME_EVIDENCE_DIR' not in volume for volume in runtime['volumes'])
    assert 'GATEWAY_SOURCE_FIXTURE_DIR' in runtime['volumes'][1]
    assert bootstrap['environment']['GATEWAY_SOURCE_TEST_AUTHORIZATION'] == 'private-synthetic-source-only'
    assert bootstrap['entrypoint'] == ['/opt/python/bin/python3.12','-m','tests.integration.source_runtime_fixture']
    assert bootstrap['command'] == ['bootstrap','--secret-dir','/run/secrets','--output','/evidence/source-runtime-fixture.json']
    assert bootstrap['read_only'] and bootstrap['cap_drop']==['ALL']
    assert all(volume.endswith(':ro') for volume in runtime['volumes'])
    assert bootstrap['pull_policy']=='never' and 'GATEWAY_PROBE_IMAGE' in bootstrap['image']


def test_source_gate_is_required_bounded_and_runs_bootstrap_before_full_cube_http():
    source = expanded_legacy_source((ROOT/'scripts/verify_cube_runtime.sh').read_text())
    assert '-f infra/compose.source-test.yaml)' in source
    assert 'export GATEWAY_SOURCE_SECRET_DIR="$TEMP_DIR/source-secrets"' in source
    prepare = 'python scripts/source_runtime_secrets.py --secret-dir "$GATEWAY_SOURCE_SECRET_DIR"'
    assert prepare in source
    bootstrap = 'bounded 60 "${COMPOSE[@]}" run --rm --no-deps --user "$(id -u):$(id -g)" source-bootstrap'
    serving = 'bounded 120 uv run --locked --offline python scripts/source_runtime_gate.py --project "$PROJECT" --run-label "$GATEWAY_CUBE_RUN_LABEL"'
    assert source.splitlines().count(bootstrap) == source.splitlines().count(serving) == 1
    assert source.index(prepare)<source.index('CHECKPOINT=cube-stack-readiness')<source.index(bootstrap)<source.index(serving)<source.index('CHECKPOINT=cube-egress')
    assert 'mkdir -m 0755 "$GATEWAY_SOURCE_FIXTURE_DIR"' in source
    assert 'set -euo pipefail' in source
    from scripts.pytest_runtime_diagnostics import CHECKPOINTS
    assert {'source-create-secrets','source-fixture-bootstrap','cube-bigquery-serving'} <= CHECKPOINTS
