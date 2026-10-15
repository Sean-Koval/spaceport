"""Actual PostgreSQL typed preparation boundaries, with disposable synthetic roles."""
import pytest
pytestmark=pytest.mark.postgres


def test_typed_preparations_have_narrow_immutable_grants(registry_database):
    db=registry_database
    assert db.scalar('SELECT count(*) FROM gateway.source_preparations')==0
    assert db.denied_as_app('UPDATE gateway.source_preparations SET prepared_digest=prepared_digest')=='permission_denied'
    assert db.denied_as_app('DELETE FROM gateway.source_preparations')=='permission_denied'
    assert db.denied_as_source('SELECT * FROM gateway.source_preparations')=='permission_denied'
    assert db.denied_as_retention('SELECT * FROM gateway.source_preparations')=='permission_denied'
    assert db.denied_as_app('SELECT gateway.guard_source_preparation()')=='permission_denied'
