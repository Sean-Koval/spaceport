"""Explicit local exact-package + disposable-PG developer acceptance.

Not a substitute for the required pinned-image source gate. Invoke this path
explicitly with authorized disposable PostgreSQL and actual Cube1.8.5 packages.
"""
import asyncio
import os
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[2]

@pytest.mark.postgres
@pytest.mark.cube
def test_actual_bigquery_serving_with_real_postgresql(tmp_path, monkeypatch):
    from tests.integration.conftest import validate_test_admin_url
    from tests.integration.source_runtime_fixture import bootstrap_local, control_local
    admin = validate_test_admin_url(os.environ.get('GATEWAY_TEST_POSTGRES_ADMIN_URL'),
        os.environ.get('GATEWAY_TEST_POSTGRES_AUTHORIZATION'))
    fixture = asyncio.run(bootstrap_local(admin, tmp_path))
    monkeypatch.setenv('GATEWAY_SOURCE_TEST_AUTHORIZATION', 'private-synthetic-source-only')
    monkeypatch.setenv('GATEWAY_SOURCE_FIXTURE_FILE', str(fixture))
    monkeypatch.setenv('GATEWAY_BIGQUERY_RUNTIME_DIR', str(ROOT/'cube/bigquery-runtime'))
    # The reviewed host protocol is the same one used by the exact-image gate.
    from scripts.source_runtime_gate import execute
    async def run():
        async def control(operation):
            await control_local(fixture, operation)
        return await execute(['node', '--test', '--test-reporter=spec',
            str(ROOT/'tests/runtime/bigquery-serving.test.cjs')], control)
    result = asyncio.run(run())
    assert result['status'] == 'passed'

