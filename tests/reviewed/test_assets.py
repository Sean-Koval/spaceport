"""Offline assets/composition checks. Actual source cases live in backend suite."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

from unittest.mock import Mock, AsyncMock

import pytest

from gateway.reviewed_runtime import build_runtime,load_assets
from gateway.adapters.reviewed import ReviewedCubeBinding
from gateway.registry.repository import Registry

pytestmark=[pytest.mark.unit,pytest.mark.contract]
ROOT=Path(__file__).resolve().parents[2]


def test_retained_assets_compile_isolated_v2_fixture_catalog():
    assets=load_assets()
    assert assets.manifest.product_id=='bookings'
    assert all(isinstance(b,ReviewedCubeBinding) for b in assets.bindings)
    assert assets.bindings[0].model_bundle_digest!=assets.bindings[1].model_bundle_digest
    assert assets.fixture_catalog.execution_manifest(assets.manifest).binding_id==assets.bindings[1].binding_id
    result=json.loads(subprocess.check_output([sys.executable,'scripts/reviewed_cube_bundle_digest.py'],cwd=ROOT))
    assert result=={b.context:b.model_bundle_digest for b in assets.bindings}


def test_security_contract_node_suite_and_legacy_bundle_digests():
    result=subprocess.run(['node','--test','tests/runtime/reviewed-context.test.js'],cwd=ROOT,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    old=json.loads(subprocess.check_output([sys.executable,'scripts/cube_bundle_digest.py'],cwd=ROOT))
    assert old=={'consumer':'228090dcbeaf8416a1e8a14d4e17c2873f9963ec3346d728650308ee5684d8ba','fixture_test':'8c3e9a8b030fb495bbdbbec19061df43c17d508ffadaf390a025d19120e43f17'}


def test_composition_uses_existing_services_and_v2_adapter_without_network(clock):
    registry=Registry('postgresql+psycopg://gwt_0123456789abcdef_app:synthetic@127.0.0.1:1/gateway_test_0123456789abcdef')
    authenticator=Mock(close=AsyncMock())
    runtime=build_runtime(registry=registry,authenticator=authenticator,secret_resolver=lambda _: 'a'*43,
        audit_key=b'a'*32,cursor_key=b'c'*32,clock=clock)
    assert runtime.services.execution.bindings is runtime.services.bindings
    assert runtime.services.results.bindings is runtime.services.bindings
    assert runtime.services.publishing.fixture_runner.catalog_provider().consumer_binding.adapter_revision=='cube-rest-v2'
    import asyncio
    asyncio.run(runtime.close())


def test_loaded_model_drift_fails_closed(tmp_path):
    for name in ['cube','fixtures']:
        shutil.copytree(ROOT/name,tmp_path/name)
    path=tmp_path/'cube/reviewed-postgres/model/bookings.yml'
    path.write_text(path.read_text()+'\n# changed\n')
    with pytest.raises(ValueError):load_assets(tmp_path)
