"""Real PostgreSQL audit durability and atomic policy changes; owner can alter storage."""
import asyncio
import importlib
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb
from sqlalchemy import select

from gateway.auth.principals import PrincipalStore
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.identity import Principal
from gateway.registry.models import AuditEvent, ProductRow
from tests.unit.test_policy import rules

pytestmark = pytest.mark.postgres


def api():
    try:
        return SimpleNamespace(service=importlib.import_module("gateway.policy.service"),
            audit=importlib.import_module("gateway.policy.audit"))
    except ModuleNotFoundError as error:
        pytest.fail(f"Policy/audit implementation is missing: {error.name}")


def seed(database, *, roles=("policy_admin",), active=True):
    content = {"rules": rules(), "reason": "Reviewed synthetic policy"}
    # Head+revision circular foreign key is deferred only within this transaction.
    import psycopg
    from tests.integration.conftest import connection_kwargs
    with psycopg.connect(**connection_kwargs(database.urls["owner"])) as connection:
        connection.execute("INSERT INTO gateway.policies (id,current_revision) VALUES ('revenue-access',1)")
        connection.execute("INSERT INTO gateway.policy_revisions (id,policy_id,revision,content,content_digest,actor_id) "
            "VALUES ('policy-1','revenue-access',1,%s,%s,'operator')", (Jsonb(content), canonical_digest(content)))
    database.execute_owner("INSERT INTO gateway.principals (id,subject,kind,active) VALUES ('north-user','synthetic-north','demo',%s)", (active,))
    database.execute_owner("INSERT INTO gateway.principal_grants (principal_id,policy_id,tenant_id,roles,entitlements) "
        "VALUES ('north-user','revenue-access','north',%s,'{\"groups\":[\"north\"]}')", (Jsonb(list(roles)),))
    return content


def service(database):
    registry = database.registry
    return api().service.PolicyService(registry, PrincipalStore(registry.sessions),
        api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!"))


def principal():
    return Principal(id="north-user", subject="synthetic-north", service_client_id=None, kind="demo")


def test_append_audit_is_redacted_in_callers_transaction(registry_database):
    async def scenario():
        writer = api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!")
        event = writer.event(principal(), "result", decision="allow", parameters={"reason": "private-value"}, correlation_id="request-1")
        async with registry_database.registry.transaction() as uow:
            await writer.append(uow, event)
            row = await uow.session.scalar(select(AuditEvent))
            assert row.parameter_names == ["reason"] and len(row.parameter_digest) == 64
            assert "private-value" not in str(row.__dict__)
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events") == 1
    asyncio.run(scenario())


def test_denied_policy_change_survives_normal_exception_and_rolls_back_caller(registry_database):
    seed(registry_database, roles=("author",))
    async def scenario():
        with pytest.raises(GatewayError) as caught:
            await service(registry_database).change_policy(principal(), "revenue-access", rules(tenant_ids=["south"]), "Denied broadening")
        assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"
        assert registry_database.scalar("SELECT count(*) FROM gateway.policy_revisions") == 1
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='change_policy' AND decision='deny' AND correlation_id=%s", (caught.value.request_id,)) == 1
    asyncio.run(scenario())


def test_policy_admin_mutation_appends_revision_reason_actor_and_audit_atomically(registry_database):
    initial = seed(registry_database)
    async def scenario():
        new_rules = rules(tenant_ids=["south"])
        assert await service(registry_database).change_policy(principal(), "revenue-access", new_rules, "Revoke synthetic north") == 2
        assert registry_database.scalar("SELECT current_revision FROM gateway.policies") == 2
        assert registry_database.scalar("SELECT content FROM gateway.policy_revisions WHERE revision=1") == initial
        updated = registry_database.scalar("SELECT content FROM gateway.policy_revisions WHERE revision=2")
        assert updated["reason"] == "Revoke synthetic north" and updated["rules"]["tenant_ids"] == ["south"]
        assert registry_database.scalar("SELECT actor_id FROM gateway.policy_revisions WHERE revision=2") == "north-user"
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='change_policy' AND decision='allow' AND policy_id='revenue-access' AND policy_revision=2") == 1
        serialized = registry_database.scalar("SELECT row_to_json(a)::text FROM gateway.audit_events a WHERE decision='allow'")
        assert "Revoke synthetic north" not in serialized
    asyncio.run(scenario())


def test_audit_insert_failure_rolls_back_policy_revision_and_head(registry_database):
    seed(registry_database)
    registry_database.execute_owner('REVOKE INSERT ON gateway.audit_events FROM "' + registry_database.roles["app"] + '"')
    # INSERT is column-level, so revoke those actual ACLs as well.
    registry_database.execute_owner('REVOKE INSERT (id,actor_id,service_client_id,action,product_id,version_id,policy_revision,decision,parameter_names,parameter_digest,duration_ms,row_count,adapter,correlation_id) ON gateway.audit_events FROM "' + registry_database.roles["app"] + '"')
    async def scenario():
        with pytest.raises(GatewayError) as caught:
            await service(registry_database).change_policy(principal(), "revenue-access", rules(tenant_ids=["south"]), "Synthetic change")
        assert caught.value.code == "UPSTREAM_UNAVAILABLE"
        assert "INSERT" not in str(caught.value) and registry_database.roles["app"] not in str(caught.value)
        assert registry_database.scalar("SELECT current_revision FROM gateway.policies") == 1
        assert registry_database.scalar("SELECT count(*) FROM gateway.policy_revisions") == 1
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events") == 0
    asyncio.run(scenario())


def test_app_cannot_update_or_delete_appended_audit_or_policy_revisions(registry_database):
    seed(registry_database)
    async def scenario():
        writer = api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!")
        async with registry_database.registry.transaction() as uow:
            await writer.append(uow, writer.event(principal(), "audit_view", decision="allow", correlation_id="request-1"))
    asyncio.run(scenario())
    assert registry_database.denied_as_app("UPDATE gateway.audit_events SET decision='allow'") == "permission_denied"
    assert registry_database.denied_as_app("DELETE FROM gateway.audit_events") == "permission_denied"
    assert registry_database.denied_as_app("UPDATE gateway.policy_revisions SET content='{}'") == "permission_denied"


def test_audit_failure_cannot_be_suppressed_to_commit_pending_mutation(registry_database):
    async def scenario():
        writer = api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!")
        async with registry_database.registry.transaction() as uow:
            uow.session.add(ProductRow(id="uncommitted-product"))
            event = api().audit.AuditEventInput.model_construct(actor_id="invalid actor", action="result", decision="allow", correlation_id="request-1")
            with pytest.raises(GatewayError):
                await writer.append(uow, event)
        assert registry_database.scalar("SELECT count(*) FROM gateway.products") == 0
    asyncio.run(scenario())


def test_database_owner_can_alter_storage_no_tamperproof_claim(registry_database):
    registry_database.insert_audit("audit-1", "2024-03-12T00:00:00Z")
    registry_database.execute_owner("ALTER TABLE gateway.audit_events DISABLE TRIGGER audit_append_only; UPDATE gateway.audit_events SET decision='allow' WHERE id='audit-1'; ALTER TABLE gateway.audit_events ENABLE TRIGGER audit_append_only")
    assert registry_database.scalar("SELECT decision FROM gateway.audit_events WHERE id='audit-1'") == "allow"


@pytest.mark.parametrize("registry_database", [{"initial_revision": "0001_registry"}], indirect=True)
def test_existing_0001_audit_upgrades_without_rewriting_history(registry_database):
    from tests.integration.conftest import run_migration
    assert registry_database.migration_head == "0001_registry"
    registry_database.insert_audit("legacy-audit", "2024-03-12T00:00:00Z")
    completed = run_migration(registry_database.urls["owner"], registry_database.roles, target="0002_audit_policy_id")
    assert completed.returncode == 0, "Additive migration failed on disposable PostgreSQL"
    assert registry_database.scalar("SELECT version_num FROM public.alembic_version") == "0002_audit_policy_id"
    assert registry_database.scalar("SELECT policy_id FROM gateway.audit_events WHERE id='legacy-audit'") is None
    assert registry_database.scalar("SELECT decision FROM gateway.audit_events WHERE id='legacy-audit'") == "deny"
    registry_database.execute_app("INSERT INTO gateway.audit_events (id,actor_id,action,policy_id,decision,correlation_id) VALUES ('new-audit','operator','change_policy','nonexistent-policy','deny','request-1')")
    assert registry_database.scalar("SELECT policy_id FROM gateway.audit_events WHERE id='new-audit'") == "nonexistent-policy"
    assert registry_database.denied_as_app("UPDATE gateway.audit_events SET policy_id='broader-policy'") == "permission_denied"
    assert registry_database.denied_as_app("INSERT INTO gateway.audit_events (id,actor_id,action,decision,correlation_id,created_at) VALUES ('backdated','operator','result','deny','request-2','2024-03-12T00:00:00Z')") == "permission_denied"


@pytest.mark.parametrize("registry_database", [{"initial_revision": "0001_registry"}], indirect=True)
def test_destructive_audit_identity_downgrade_is_rejected_without_data_loss(registry_database):
    import os
    import subprocess
    import sys
    from tests.integration.conftest import ROOT, run_migration
    upgraded = run_migration(registry_database.urls["owner"], registry_database.roles, target="0002_audit_policy_id")
    assert upgraded.returncode == 0, "Exact audit migration failed on disposable PostgreSQL"
    registry_database.execute_app("INSERT INTO gateway.audit_events (id,actor_id,action,policy_id,decision,correlation_id) VALUES ('preserved-audit','operator','change_policy','revenue-access','deny','request-1')")
    env = {**os.environ, "GATEWAY_MIGRATION_DATABASE_URL": registry_database.urls["owner"].render_as_string(hide_password=False),
        **{f"GATEWAY_{key.upper()}_ROLE": value for key, value in registry_database.roles.items()}}
    completed = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0001_registry"], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=60)
    assert completed.returncode != 0 and "downgrade is disabled" in completed.stderr
    assert registry_database.scalar("SELECT version_num FROM public.alembic_version") == "0002_audit_policy_id"
    assert registry_database.scalar("SELECT policy_id FROM gateway.audit_events WHERE id='preserved-audit'") == "revenue-access"


@pytest.mark.parametrize("table", ["principals", "principal_grants", "policies", "policy_revisions"])
def test_policy_change_unreadable_current_state_has_surviving_denial(registry_database, table):
    seed(registry_database)
    from psycopg import sql
    registry_database.execute_owner(sql.SQL("REVOKE SELECT ON gateway.{} FROM {}").format(
        sql.Identifier(table), sql.Identifier(registry_database.roles["app"])))
    async def scenario():
        with pytest.raises(GatewayError) as caught:
            await service(registry_database).change_policy(principal(), "revenue-access", rules(tenant_ids=["south"]), "Private submitted reason")
        assert caught.value.code == "UPSTREAM_UNAVAILABLE"
        assert registry_database.scalar("SELECT current_revision FROM gateway.policies") == 1
        assert registry_database.scalar("SELECT count(*) FROM gateway.policy_revisions") == 1
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='change_policy' AND decision='deny' AND policy_id='revenue-access' AND correlation_id=%s", (caught.value.request_id,)) == 1
        serialized = registry_database.scalar("SELECT row_to_json(a)::text FROM gateway.audit_events a")
        assert "Private submitted reason" not in serialized and "tenant_ids" not in serialized
        assert "SELECT" not in str(caught.value.as_dict()) and registry_database.roles["app"] not in str(caught.value.as_dict())
        if table == "principal_grants":
            assert caught.value.safe_message == "Current access permissions are unavailable."
    asyncio.run(scenario())


@pytest.mark.parametrize("corruption", ["malformed", "digest_mismatch"])
def test_policy_change_invalid_current_policy_has_surviving_denial(registry_database, corruption):
    content = seed(registry_database)
    if corruption == "malformed":
        content = {"rules": {"tenant_ids": ["north"]}, "reason": "Synthetic invalid rules"}
    digest = canonical_digest(content) if corruption == "malformed" else "e" * 64
    import psycopg
    from tests.integration.conftest import connection_kwargs
    with psycopg.connect(**connection_kwargs(registry_database.urls["owner"])) as connection:
        connection.execute("INSERT INTO gateway.policy_revisions (id,policy_id,revision,content,content_digest,actor_id) VALUES ('invalid-policy-2','revenue-access',2,%s,%s,'operator')", (Jsonb(content), digest))
        connection.execute("UPDATE gateway.policies SET current_revision=2")
    async def scenario():
        with pytest.raises(GatewayError) as caught:
            await service(registry_database).change_policy(principal(), "revenue-access", rules(), "Private submitted reason")
        assert caught.value.code == "UPSTREAM_UNAVAILABLE"
        assert registry_database.scalar("SELECT current_revision FROM gateway.policies") == 2
        assert registry_database.scalar("SELECT count(*) FROM gateway.policy_revisions") == 2
        assert registry_database.scalar("SELECT content FROM gateway.policy_revisions WHERE revision=2") == content
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='change_policy' AND decision='deny' AND policy_id='revenue-access' AND correlation_id=%s", (caught.value.request_id,)) == 1
        assert "Private submitted reason" not in registry_database.scalar("SELECT row_to_json(a)::text FROM gateway.audit_events a")
    asyncio.run(scenario())


def test_policy_change_state_and_audit_failure_remain_safe_and_unmutated(registry_database):
    seed(registry_database)
    from psycopg import sql
    app = sql.Identifier(registry_database.roles["app"])
    registry_database.execute_owner(sql.SQL("REVOKE SELECT ON gateway.policy_revisions FROM {}").format(app))
    registry_database.execute_owner(sql.SQL("REVOKE INSERT (id,actor_id,service_client_id,action,product_id,version_id,policy_id,policy_revision,decision,parameter_names,parameter_digest,duration_ms,row_count,adapter,correlation_id) ON gateway.audit_events FROM {}").format(app))
    async def scenario():
        with pytest.raises(GatewayError) as caught:
            await service(registry_database).change_policy(principal(), "revenue-access", rules(), "Private submitted reason")
        assert caught.value.code == "UPSTREAM_UNAVAILABLE"
        assert caught.value.safe_message == "Required audit recording is unavailable."
        assert registry_database.scalar("SELECT current_revision FROM gateway.policies") == 1
        assert registry_database.scalar("SELECT count(*) FROM gateway.policy_revisions") == 1
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events") == 0
        assert "SELECT" not in str(caught.value.as_dict()) and "INSERT" not in str(caught.value.as_dict())
    asyncio.run(scenario())
