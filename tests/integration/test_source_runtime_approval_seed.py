"""Standalone probe bootstrap registry path; no Node or provider HTTP evidence."""
import asyncio
import pytest
from sqlalchemy import create_engine

pytestmark = pytest.mark.postgres


def test_standalone_probe_preserves_five_authenticated_source_owners(registry_database):
    from tests.integration.source_approval_seed import fixture_provider
    from tests.integration.source_runtime_fixture import EPOCH, CASES, seed_sessions, source_profile
    from tests.integration.source_runtime_approval_seed import CLIENT, fixture_source_profile
    from gateway.source_lifecycle import provision_sources
    db = registry_database
    source = source_profile()
    fixture_source = fixture_source_profile(source)
    with fixture_provider(db, fixture_source, deployment_epoch=EPOCH):
        engine = create_engine(db.urls['owner'])
        try:
            with engine.begin() as connection:
                provision_sources(connection, [source, fixture_source], [dict(login_name=db.roles['fixture_observer'],
                    source_id=fixture_source.source_id, revision=fixture_source.revision, context='fixture_test', role_kind='observer')],
                    deployment_epoch=EPOCH, max_active_operations=10)
        finally:
            engine.dispose()
        cases = asyncio.run(seed_sessions(db.urls['owner'], db.urls['app'], source,
            db.urls['fixture_runtime'], db.urls['fixture_observer']))
        assert tuple(case['name'] for case in cases) == CASES
        assert len({case['source_attempt']['session_id'] for case in cases}) == 5
        assert db.scalar("SELECT count(*) FROM gateway.executions e JOIN gateway.principals p ON p.id=e.principal_id WHERE p.kind='service' AND p.service_client_id=%s", (CLIENT,)) == 5
        assert db.scalar("SELECT count(*) FROM gateway.source_operation_groups WHERE context='consumer' AND NOT closed") == 5
        assert db.scalar("SELECT count(*) FROM gateway.product_versions WHERE source_operation_id IS NOT NULL") == 1
        assert db.scalar("SELECT count(*) FROM gateway.source_operation_groups WHERE context='fixture_test' AND (NOT closed OR settled_at IS NULL)") == 0
        assert db.scalar("SELECT count(*) FROM gateway.source_sessions WHERE context='fixture_test' AND NOT sealed") == 0
        assert db.scalar("SELECT count(*) FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context='fixture_test' AND j.state<>'done'") == 0
