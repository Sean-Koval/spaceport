"""Real PostgreSQL invariants; availability failures are never skips/passes."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
from types import SimpleNamespace

import pytest

from gateway.contracts.errors import GatewayError
from gateway.contracts.manifest import validate_manifest


def api():
    try:
        return SimpleNamespace(
            models=importlib.import_module("gateway.registry.models"),
            repository=importlib.import_module("gateway.registry.repository"),
        )
    except ModuleNotFoundError as error:
        pytest.fail(f"Registry implementation is missing: {error.name}")


@pytest.mark.unit
def test_registry_rejects_non_postgres_before_any_connection():
    with pytest.raises(ValueError, match="PostgreSQL"):
        api().repository.Registry("sqlite+aiosqlite:///:memory:")


@pytest.mark.unit
def test_draft_product_mismatch_is_rejected_before_database_access(valid_manifest):
    registry = api().repository.Registry("postgresql+psycopg://test:test@127.0.0.1:1/test")
    with pytest.raises(GatewayError) as caught:
        asyncio.run(registry.save_draft("another-product", validate_manifest(valid_manifest), 0, "editor"))
    assert caught.value.code == "INVALID_INPUT"
    asyncio.run(registry.close())


@pytest.mark.unit
@pytest.mark.parametrize("revision", [-1, True, "0"])
def test_invalid_expected_revision_is_rejected_without_database_access(valid_manifest, revision):
    registry = api().repository.Registry("postgresql+psycopg://test:test@127.0.0.1:1/test")
    with pytest.raises(GatewayError) as caught:
        asyncio.run(registry.save_draft("revenue", validate_manifest(valid_manifest), revision, "editor"))
    assert caught.value.code == "INVALID_INPUT"
    asyncio.run(registry.close())


async def seed(database, valid_manifest):
    m = api().models
    manifest = validate_manifest(valid_manifest)
    async with database.registry.transaction() as uow:
        uow.session.add(m.BindingRevision(id="binding-1", binding_id=manifest.binding_id, revision=1,
            content={"measures": ["Orders.revenue"]}, content_digest="b" * 64,
            model_bundle_digest=manifest.model_bundle_digest, adapter_revision=manifest.adapter_revision,
            actor_id="operator"))
        uow.session.add(m.PolicyRow(id=manifest.policy_id, current_revision=1))
        # The revision's immediate FK needs its head row. No ORM relationship is
        # declared here; explicit flush keeps insertion order unambiguous.
        await uow.session.flush()
        uow.session.add(m.PolicyRevision(id="policy-1", policy_id=manifest.policy_id, revision=1,
            content={"tenant_ids": ["north", "south"]}, content_digest="c" * 64, actor_id="operator"))
    draft = await database.registry.save_draft("revenue", manifest, 0, "editor")
    async with database.registry.transaction() as uow:
        uow.session.add(m.TestRun(id="test-1", product_id="revenue", draft_id=draft.id,
            draft_digest=manifest.content_digest, binding_id="binding-1", binding_digest="b" * 64,
            policy_id=manifest.policy_id, policy_revision=1, fixture_version="1",
            evidence={"known_answer": "pass"}, status="passed", actor_id="tester"))
    return manifest, draft


async def publish(registry, draft, *, version_id="version-1", test_run_id="test-1"):
    async with registry.transaction() as uow:
        return await uow.insert_version(draft_id=draft.id, expected_revision=draft.revision,
            test_run_id=test_run_id, actor_id="publisher", version_id=version_id)


@pytest.mark.postgres
def test_publish_race_commits_exactly_one_version(registry_database, valid_manifest):
    async def scenario():
        _, draft = await seed(registry_database, valid_manifest)
        ready = asyncio.Event()
        async def racer(version_id):
            await ready.wait()
            try:
                await publish(registry_database.registry, draft, version_id=version_id)
                return "committed"
            except GatewayError as error:
                return error.code.value
        tasks = [asyncio.create_task(racer("version-a")), asyncio.create_task(racer("version-b"))]
        ready.set()
        assert sorted(await asyncio.gather(*tasks)) == ["CONFLICT", "committed"]
        assert registry_database.scalar("SELECT count(*) FROM gateway.product_versions") == 1
    asyncio.run(scenario())


@pytest.mark.postgres
def test_stored_version_is_exact_tested_digest(registry_database, valid_manifest):
    async def scenario():
        manifest, draft = await seed(registry_database, valid_manifest)
        await publish(registry_database.registry, draft)
        pinned = await registry_database.registry.resolve("revenue", "1.0.0")
        assert pinned.manifest.content_digest == manifest.content_digest
        assert pinned.binding_digest == "b" * 64
        assert pinned.version_id == "version-1"
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize("table,column,id_value", [
    ("product_versions", "manifest", "version-1"),
    ("draft_revisions", "manifest", "draft"),
    ("binding_revisions", "content", "binding-1"),
    ("policy_revisions", "content", "policy-1"),
    ("test_runs", "evidence", "test-1"),
])
def test_app_cannot_mutate_immutable_content(registry_database, valid_manifest, table, column, id_value):
    async def scenario():
        _, draft = await seed(registry_database, valid_manifest)
        await publish(registry_database.registry, draft)
        return draft.id
    draft_id = asyncio.run(scenario())
    target = draft_id if id_value == "draft" else id_value
    assert registry_database.denied_as_app(f"UPDATE gateway.{table} SET {column} = '{{}}'::jsonb WHERE id = %s", (target,)) == "permission_denied"
    assert registry_database.denied_as_app(f"DELETE FROM gateway.{table} WHERE id = %s", (target,)) == "permission_denied"


@pytest.mark.postgres
def test_owner_trigger_also_rejects_in_place_content_edits(registry_database, valid_manifest):
    async def scenario():
        _, draft = await seed(registry_database, valid_manifest)
        await publish(registry_database.registry, draft)
    asyncio.run(scenario())
    assert registry_database.denied_as_owner("UPDATE gateway.product_versions SET manifest = '{}'::jsonb WHERE id = 'version-1'") == "permission_denied"


@pytest.mark.postgres
def test_pointer_rollback_and_default_resolution_are_pinned(registry_database, valid_manifest):
    async def scenario():
        _, first_draft = await seed(registry_database, valid_manifest)
        await publish(registry_database.registry, first_draft)
        first = await registry_database.registry.resolve("revenue", None)
        changed = deepcopy(valid_manifest)
        changed["version"] = "1.1.0"
        changed["description"] = "New release"
        second = validate_manifest(changed)
        draft = await registry_database.registry.save_draft("revenue", second, 1, "editor")
        async with registry_database.registry.transaction() as uow:
            uow.session.add(api().models.TestRun(id="test-2", product_id="revenue", draft_id=draft.id,
                draft_digest=second.content_digest, binding_id="binding-1", binding_digest="b" * 64,
                policy_id=second.policy_id, policy_revision=1, fixture_version="1",
                evidence={"known_answer": "pass"}, status="passed", actor_id="tester"))
        with pytest.raises(RuntimeError):
            async with registry_database.registry.transaction() as uow:
                await uow.insert_version(draft_id=draft.id, expected_revision=2,
                    test_run_id="test-2", actor_id="publisher", version_id="version-2")
                raise RuntimeError("abort synthetic transaction")
        assert (await registry_database.registry.resolve("revenue", None)).version_id == "version-1"
        await publish(registry_database.registry, draft, version_id="version-2", test_run_id="test-2")
        assert (await registry_database.registry.resolve("revenue", None)).version_id == "version-2"
        assert first.version_id == "version-1"
        assert first.manifest.version == "1.0.0"
        assert (await registry_database.registry.resolve("revenue", "1.0.0")).version_id == "version-1"
    asyncio.run(scenario())


@pytest.mark.postgres
def test_optimistic_draft_writes_conflict_without_losing_history(registry_database, valid_manifest):
    async def scenario():
        first = await registry_database.registry.save_draft("revenue", validate_manifest(valid_manifest), 0, "editor")
        with pytest.raises(GatewayError) as caught:
            await registry_database.registry.save_draft("revenue", validate_manifest(valid_manifest), 0, "editor")
        assert caught.value.code == "CONFLICT"
        assert registry_database.scalar("SELECT count(*) FROM gateway.draft_revisions") == 1
        assert first.revision == 1
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize("change", ["draft", "policy", "failed_test", "binding_digest"])
def test_publication_rejects_stale_or_inconsistent_evidence(registry_database, valid_manifest, change):
    async def scenario():
        _, draft = await seed(registry_database, valid_manifest)
        if change == "draft":
            updated = deepcopy(valid_manifest)
            updated["description"] = "Edited after testing"
            await registry_database.registry.save_draft("revenue", validate_manifest(updated), 1, "editor")
        elif change == "policy":
            async with registry_database.registry.transaction() as uow:
                uow.session.add(api().models.PolicyRevision(id="policy-2", policy_id="revenue-access", revision=2,
                    content={"tenant_ids": ["north"]}, content_digest="d" * 64, actor_id="operator"))
                (await uow.lock_policy_head("revenue-access")).current_revision = 2
        else:
            # Owner inserts deliberately inconsistent evidence; publication must reject it.
            registry_database.execute_owner("INSERT INTO gateway.test_runs (id,product_id,draft_id,draft_digest,binding_id,binding_digest,policy_id,policy_revision,fixture_version,evidence,status,actor_id,created_at) SELECT 'test-bad', product_id, draft_id, draft_digest, binding_id, %s, policy_id, policy_revision, fixture_version, evidence, %s, actor_id, created_at FROM gateway.test_runs WHERE id = 'test-1'", ("e" * 64 if change == "binding_digest" else "b" * 64, "failed" if change == "failed_test" else "passed"))
        with pytest.raises(GatewayError) as caught:
            await publish(registry_database.registry, draft, test_run_id="test-bad" if change in ("failed_test", "binding_digest") else "test-1")
        assert caught.value.code == "CONFLICT"
        assert registry_database.scalar("SELECT count(*) FROM gateway.product_versions") == 0
        assert registry_database.scalar("SELECT current_version_id FROM gateway.products WHERE id='revenue'") is None
    asyncio.run(scenario())


@pytest.mark.postgres
def test_missing_and_withdrawn_versions_are_indistinguishable(registry_database, valid_manifest):
    async def scenario():
        _, draft = await seed(registry_database, valid_manifest)
        await publish(registry_database.registry, draft)
        registry_database.execute_app("UPDATE gateway.version_states SET status='withdrawn' WHERE version_id='version-1'")
        for product_id, version in [("missing", None), ("revenue", None), ("revenue", "1.0.0"), ("revenue", "9.9.9")]:
            with pytest.raises(GatewayError) as caught:
                await registry_database.registry.resolve(product_id, version)
            assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"
    asyncio.run(scenario())


@pytest.mark.postgres
def test_roles_are_separate_and_source_reader_has_no_registry_access(registry_database):
    roles = registry_database.roles
    assert len(set(roles.values())) == 4
    assert registry_database.scalar("SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s) AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls)", (list(roles.values()),)) == 0
    assert registry_database.scalar("SELECT has_schema_privilege(%s, 'gateway', 'USAGE')", (roles["source_reader"],)) is False
    assert registry_database.scalar("SELECT has_schema_privilege(%s, 'gateway', 'CREATE')", (roles["app"],)) is False
    assert registry_database.scalar("SELECT has_table_privilege(%s, 'gateway.audit_events', 'UPDATE')", (roles["app"],)) is False
    assert registry_database.scalar("SELECT has_table_privilege(%s, 'gateway.products', 'SELECT')", (roles["audit_retention"],)) is False


@pytest.mark.postgres
def test_audit_is_append_only_and_retention_cannot_delete_fresh_events(registry_database):
    now = datetime.now(timezone.utc)
    registry_database.insert_audit("audit-fresh", now)
    registry_database.insert_audit("audit-old", now - timedelta(days=31))
    assert registry_database.denied_as_app("UPDATE gateway.audit_events SET decision='allow' WHERE id='audit-fresh'") == "permission_denied"
    assert registry_database.denied_as_app("DELETE FROM gateway.audit_events WHERE id='audit-old'") == "permission_denied"
    assert registry_database.denied_as_retention("DELETE FROM gateway.audit_events WHERE id='audit-fresh'") == "permission_denied"
    registry_database.execute_retention("DELETE FROM gateway.audit_events WHERE id='audit-old'")
    assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events") == 1


@pytest.mark.postgres
def test_second_migration_is_unchanged_and_head_is_recorded(registry_database):
    assert registry_database.migration_head == "0001_registry"
    assert registry_database.first_migration_snapshot == registry_database.second_migration_snapshot


@pytest.mark.postgres
def test_database_itself_rejects_duplicate_publication_and_cross_product_pointer(registry_database, valid_manifest):
    async def scenario():
        _, draft = await seed(registry_database, valid_manifest)
        await publish(registry_database.registry, draft)
        await registry_database.registry.save_draft("other", validate_manifest({**valid_manifest, "product_id": "other"}), 0, "editor")
    asyncio.run(scenario())
    assert registry_database.constraint_failure("UPDATE gateway.products SET current_version_id='version-1' WHERE id='other'") == "23503"
    assert registry_database.constraint_failure("INSERT INTO gateway.product_versions (id,product_id,version,manifest,content_digest,binding_id,binding_digest,policy_id,policy_revision,test_run_id,draft_id,actor_id,name,domain,classification) SELECT 'duplicate-version', product_id, version, manifest, content_digest, binding_id, binding_digest, policy_id, policy_revision, test_run_id, draft_id, actor_id, name, domain, classification FROM gateway.product_versions WHERE id='version-1'") == "23505"


@pytest.mark.unit
@pytest.mark.parametrize("raw,authorization", [
    (None, None),
    ("postgresql+psycopg://gateway_test_admin:synthetic@127.0.0.1:5432/gateway_test_admin", None),
    ("postgresql+psycopg://gateway_test_admin:synthetic@remote.example:5432/gateway_test_admin", "disposable-local-postgres-only"),
    ("postgresql+psycopg://gateway_test_admin:synthetic@127.0.0.1:5432/user_data", "disposable-local-postgres-only"),
    ("postgresql+psycopg://postgres:synthetic@127.0.0.1:5432/gateway_test_admin", "disposable-local-postgres-only"),
    ("postgresql+psycopg://gateway_test_admin:synthetic@127.0.0.1:5432/gateway_test_admin?options=unsafe", "disposable-local-postgres-only"),
    ("sqlite:///gateway_test_admin", "disposable-local-postgres-only"),
])
def test_disposable_fixture_refuses_missing_authority_or_other_databases(postgres_admin_guard, raw, authorization):
    with pytest.raises(ValueError):
        postgres_admin_guard(raw, authorization)


@pytest.mark.unit
def test_disposable_fixture_accepts_only_explicit_synthetic_loopback_database(postgres_admin_guard):
    url = postgres_admin_guard("postgresql+psycopg://gateway_test_admin:synthetic@127.0.0.1:5432/gateway_test_admin", "disposable-local-postgres-only")
    assert url.database == "gateway_test_admin"


@pytest.mark.postgres
def test_source_reader_can_only_read_synthetic_source(registry_database):
    assert str(registry_database.execute_source("SELECT amount FROM fixture_source.orders", scalar=True)) == "10.10"
    assert registry_database.denied_as_source("UPDATE fixture_source.orders SET amount=0") == "permission_denied"
    assert registry_database.denied_as_source("CREATE TABLE fixture_source.unapproved (id int)") == "permission_denied"
    assert registry_database.denied_as_source("SELECT * FROM gateway.products") == "permission_denied"
    assert registry_database.denied_as_app("SELECT * FROM fixture_source.orders") == "permission_denied"


@pytest.mark.postgres
def test_app_cannot_backdate_audit_or_modify_principal_grants(registry_database):
    assert registry_database.denied_as_app("INSERT INTO gateway.audit_events (id,actor_id,action,decision,correlation_id,created_at) VALUES ('forged','editor','query','allow','test',now()-interval '31 days')") == "permission_denied"
    assert registry_database.denied_as_app("INSERT INTO gateway.principals (id,subject,kind) VALUES ('forged','forged','demo')") == "permission_denied"
    assert registry_database.denied_as_app("UPDATE gateway.principal_grants SET active=false") == "permission_denied"


async def seed_execution(database, valid_manifest):
    _, draft = await seed(database, valid_manifest)
    await publish(database.registry, draft)
    database.execute_owner("INSERT INTO gateway.principals (id,subject,kind) VALUES ('north-user','synthetic-north','demo'),('south-user','synthetic-south','demo')")
    async with database.registry.transaction() as uow:
        execution = api().models.Execution(id="execution-1", principal_id="north-user", product_id="revenue",
            version_id="version-1", policy_id="revenue-access", policy_revision=1,
            canonical_query={"product_id": "revenue", "version": "1.0.0"}, canonical_hash="e"*64,
            context="consumer", request_id="request-1", deadline_at=datetime.now(timezone.utc)+timedelta(seconds=119))
        uow.session.add(execution)
        await uow.session.flush()
        uow.session.add(api().models.ExecutionState(execution_id=execution.id))


@pytest.mark.postgres
def test_idempotency_uniqueness_and_execution_binding_are_database_enforced(registry_database, valid_manifest):
    asyncio.run(seed_execution(registry_database, valid_manifest))
    statement = "INSERT INTO gateway.idempotency_records (principal_id,product_id,version_id,key_digest,canonical_hash,execution_id) VALUES (%s,'revenue','version-1',%s,%s,'execution-1')"
    registry_database.execute_app(statement, ("north-user", "f"*64, "e"*64))
    assert registry_database.constraint_failure(statement, ("north-user", "f"*64, "e"*64)) == "23505"
    assert registry_database.constraint_failure(statement, ("south-user", "f"*64, "e"*64)) == "23503"
    assert registry_database.constraint_failure(statement, ("north-user", "a"*64, "b"*64)) == "23503"


@pytest.mark.postgres
def test_results_and_cursors_cannot_change_principal_or_policy_scope(registry_database, valid_manifest):
    asyncio.run(seed_execution(registry_database, valid_manifest))
    statement = "INSERT INTO gateway.results (id,execution_id,principal_id,version_id,policy_revision,canonical_hash,envelope,expires_at) VALUES ('result-1','execution-1',%s,'version-1',%s,%s,'{}',now()+interval '23 hours')"
    assert registry_database.constraint_failure(statement, ("south-user", 1, "e"*64)) == "23503"
    assert registry_database.constraint_failure(statement, ("north-user", 2, "e"*64)) == "23503"
    registry_database.execute_app(statement, ("north-user", 1, "e"*64))
    cursor = "INSERT INTO gateway.result_cursors (id,token_digest,result_id,principal_id,version_id,policy_revision,position,expires_at) VALUES ('cursor-1',%s,'result-1',%s,'version-1',%s,200,now()+interval '1 hour')"
    assert registry_database.constraint_failure(cursor, ("a"*64,"south-user",1)) == "23503"
    assert registry_database.constraint_failure(cursor, ("a"*64,"north-user",2)) == "23503"
    registry_database.execute_app(cursor, ("a"*64,"north-user",1))
    assert registry_database.denied_as_app("UPDATE gateway.results SET envelope='{}' WHERE id='result-1'") == "permission_denied"
    assert registry_database.denied_as_app("UPDATE gateway.result_cursors SET position=0 WHERE id='cursor-1'") == "permission_denied"
    assert registry_database.denied_as_app("DELETE FROM gateway.results WHERE id='result-1'") == "permission_denied"


@pytest.mark.postgres
def test_cursor_lifetime_cannot_exceed_its_result(registry_database, valid_manifest):
    asyncio.run(seed_execution(registry_database, valid_manifest))
    registry_database.execute_app("INSERT INTO gateway.results (id,execution_id,principal_id,version_id,policy_revision,canonical_hash,envelope,expires_at) VALUES ('result-1','execution-1','north-user','version-1',1,%s,'{}',now()+interval '1 hour')", ("e"*64,))
    assert registry_database.constraint_failure("INSERT INTO gateway.result_cursors (id,token_digest,result_id,principal_id,version_id,policy_revision,position,expires_at) VALUES ('cursor-too-long',%s,'result-1','north-user','version-1',1,0,now()+interval '2 hours')", ("a"*64,)) == "23514"


@pytest.mark.unit
def test_registry_index_backed_constraint_names_do_not_collide():
    # PostgreSQL UNIQUE/PK constraints create schema-global index names.
    from sqlalchemy import PrimaryKeyConstraint, UniqueConstraint
    names = [constraint.name for table in api().models.Base.metadata.tables.values()
        for constraint in table.constraints if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint))]
    names += [index.name for table in api().models.Base.metadata.tables.values() for index in table.indexes]
    assert len(names) == len(set(names)), "Index-backed registry constraints must have distinct physical names"


@pytest.mark.postgres
def test_required_audit_failure_rolls_back_publication_and_pointer(registry_database, valid_manifest):
    async def scenario():
        _, draft = await seed(registry_database, valid_manifest)
        from psycopg import sql
        registry_database.execute_owner(sql.SQL("REVOKE INSERT (id,actor_id,service_client_id,action,product_id,version_id,policy_revision,decision,parameter_names,parameter_digest,duration_ms,row_count,adapter,correlation_id) ON gateway.audit_events FROM {}").format(sql.Identifier(registry_database.roles["app"])))
        with pytest.raises(GatewayError) as caught:
            await publish(registry_database.registry, draft)
        assert caught.value.code == "UPSTREAM_UNAVAILABLE"
        assert registry_database.scalar("SELECT count(*) FROM gateway.product_versions") == 0
        assert registry_database.scalar("SELECT current_version_id FROM gateway.products WHERE id='revenue'") is None
    asyncio.run(scenario())


@pytest.mark.postgres
def test_app_can_lock_capacity_row_but_cannot_rewrite_scope(registry_database):
    async def scenario():
        from sqlalchemy import select
        async with registry_database.registry.transaction() as uow:
            row = await uow.session.scalar(select(api().models.CapacityLock).where(api().models.CapacityLock.scope_id == "deployment").with_for_update())
            assert row.scope_id == "deployment"
    asyncio.run(scenario())
    assert registry_database.denied_as_app("UPDATE gateway.capacity_locks SET scope_id='other' WHERE scope_id='deployment'") == "permission_denied"


@pytest.mark.postgres
def test_policy_lock_refreshes_identity_map_after_second_connection_commits(registry_database, valid_manifest):
    async def scenario():
        await seed(registry_database, valid_manifest)
        async with registry_database.registry.transaction() as first:
            head = await first.session.get(api().models.PolicyRow, "revenue-access")
            assert head.current_revision == 1
            async with registry_database.registry.transaction() as second:
                second.session.add(api().models.PolicyRevision(id="policy-2", policy_id="revenue-access", revision=2,
                    content={"tenant_ids": ["south"]}, content_digest="d"*64, actor_id="operator"))
                (await second.lock_policy_head("revenue-access")).current_revision = 2
            locked = await first.lock_policy("revenue-access")
            assert head.current_revision == 2
            assert locked.revision == 2
            assert locked.content == {"tenant_ids": ["south"]}
    asyncio.run(scenario())


@pytest.mark.postgres
def test_product_lock_refreshes_draft_and_pointer_after_second_connection_commits(registry_database, valid_manifest):
    async def scenario():
        _, draft = await seed(registry_database, valid_manifest)
        await publish(registry_database.registry, draft)
        async with registry_database.registry.transaction() as first:
            head = await first.session.get(api().models.ProductRow, "revenue")
            assert (head.draft_revision, head.current_version_id) == (1, "version-1")
            changed = validate_manifest({**valid_manifest, "version": "1.1.0"})
            newer = await registry_database.registry.save_draft("revenue", changed, 1, "editor")
            async with registry_database.registry.transaction() as second:
                second.session.add(api().models.TestRun(id="test-2", product_id="revenue", draft_id=newer.id,
                    draft_digest=changed.content_digest, binding_id="binding-1", binding_digest="b"*64,
                    policy_id="revenue-access", policy_revision=1, fixture_version="1", evidence={"pass": True}, status="passed", actor_id="tester"))
                await second.session.flush()
                await second.insert_version(draft_id=newer.id, expected_revision=2, test_run_id="test-2", actor_id="publisher", version_id="version-2")
            locked = await first.lock_product("revenue")
            assert locked is head
            assert (head.draft_revision, head.latest_draft_id, head.current_version_id) == (2, newer.id, "version-2")
    asyncio.run(scenario())


@pytest.mark.postgres
def test_refreshing_locks_preserve_autoflushed_pending_head_writes(registry_database, valid_manifest):
    async def scenario():
        _, draft = await seed(registry_database, valid_manifest)
        await publish(registry_database.registry, draft)
        async with registry_database.registry.transaction() as uow:
            policy_head = await uow.session.get(api().models.PolicyRow, "revenue-access")
            product_head = await uow.session.get(api().models.ProductRow, "revenue")
            uow.session.add(api().models.PolicyRevision(id="policy-2", policy_id="revenue-access", revision=2,
                content={"tenant_ids": ["north"]}, content_digest="d"*64, actor_id="operator"))
            policy_head.current_revision = 2
            product_head.current_version_id = None
            assert (await uow.lock_policy("revenue-access")).revision == 2
            assert (await uow.lock_product("revenue")).current_version_id is None
        assert registry_database.scalar("SELECT current_revision FROM gateway.policies WHERE id='revenue-access'") == 2
        assert registry_database.scalar("SELECT current_version_id FROM gateway.products WHERE id='revenue'") is None
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize("registry_database", [
    {"unsafe_membership": ("app", "pg_write_all_data", False), "expect_rejection": True},
    {"unsafe_membership": ("source_reader", "pg_read_all_data", False), "expect_rejection": True},
    {"unsafe_membership": ("audit_retention", "pg_write_all_data", False), "expect_rejection": True},
    {"unsafe_membership": ("app", "pg_write_all_data", True), "expect_rejection": True},
    {"unsafe_membership": ("owner", "pg_read_all_data", False), "expect_rejection": True},
], indirect=True)
def test_migration_rejects_external_direct_or_transitive_role_authority(registry_database):
    assert registry_database.migration_error == "Registry roles must have no explicit role memberships"
    assert registry_database.scalar("SELECT count(*) FROM pg_namespace WHERE nspname='gateway'") == 0


@pytest.mark.postgres
@pytest.mark.parametrize("registry_database", [{"unsafe_default_grants": True}], indirect=True)
def test_owner_default_privileges_do_not_expand_configured_role_access(registry_database):
    assert registry_database.scalar("SELECT pg_has_role(%s,'pg_database_owner','MEMBER')", (registry_database.roles["owner"],)) is True
    assert registry_database.denied_as_app("UPDATE gateway.principal_grants SET active=false") == "permission_denied"
    assert registry_database.denied_as_app("INSERT INTO gateway.audit_events (id,actor_id,action,decision,correlation_id,created_at) VALUES ('backdated','operator','test','allow','request',now()-interval '31 days')") == "permission_denied"
    assert registry_database.denied_as_source("SELECT * FROM gateway.products") == "permission_denied"
    assert registry_database.denied_as_source("UPDATE gateway.principal_grants SET active=false") == "permission_denied"
    assert registry_database.denied_as_source("UPDATE fixture_source.orders SET amount=0") == "permission_denied"
    assert registry_database.denied_as_app("SELECT * FROM fixture_source.orders") == "permission_denied"
    assert registry_database.denied_as_retention("SELECT * FROM gateway.products") == "permission_denied"
    assert registry_database.denied_as_retention("UPDATE gateway.audit_events SET decision='allow'") == "permission_denied"
    assert registry_database.scalar("SELECT has_table_privilege(%s,'public.alembic_version','UPDATE')", (registry_database.roles["app"],)) is False
    assert registry_database.denied_as_app("TRUNCATE gateway.audit_events") == "permission_denied"
    assert registry_database.denied_as_retention("TRUNCATE gateway.audit_events") == "permission_denied"
    for role in ("app", "source_reader", "audit_retention"):
        assert registry_database.scalar("SELECT has_function_privilege(%s,'gateway.deny_immutable_change()','EXECUTE')", (registry_database.roles[role],)) is False


@pytest.mark.postgres
def test_acl_baseline_removes_existing_column_grants(registry_database):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine
    from psycopg import sql
    migration = importlib.import_module("migrations.versions.0001_registry")
    for role in ("app", "source_reader", "audit_retention"):
        registry_database.execute_owner(sql.SQL("GRANT UPDATE (active), SELECT (principal_id) ON gateway.principal_grants TO {}").format(sql.Identifier(registry_database.roles[role])))
    engine = create_engine(registry_database.urls["owner"], hide_parameters=True)
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.reset_acl_baseline(registry_database.roles)
            migration.grant_runtime_privileges(registry_database.roles)
    engine.dispose()
    for role in ("app", "source_reader", "audit_retention"):
        assert registry_database.scalar("SELECT has_column_privilege(%s,'gateway.principal_grants','active','UPDATE')", (registry_database.roles[role],)) is False
    assert registry_database.denied_as_source("SELECT principal_id FROM gateway.principal_grants") == "permission_denied"
    assert registry_database.denied_as_retention("SELECT principal_id FROM gateway.principal_grants") == "permission_denied"


@pytest.mark.postgres
@pytest.mark.parametrize("field", ["product_id", "content_digest"])
@pytest.mark.parametrize("kind", ["missing", "json_null"])
def test_draft_identity_requires_present_nonnull_json_keys(registry_database, valid_manifest, field, kind):
    from psycopg.types.json import Jsonb
    manifest, _ = asyncio.run(seed(registry_database, valid_manifest))
    malformed = manifest.model_dump(mode="json")
    if kind == "missing": malformed.pop(field)
    else: malformed[field] = None
    assert registry_database.constraint_failure("INSERT INTO gateway.draft_revisions (id,product_id,revision,manifest,content_digest,actor_id) VALUES ('bad-draft','revenue',2,%s,%s,'editor')", (Jsonb(malformed),manifest.content_digest)) == "23514"


@pytest.mark.postgres
@pytest.mark.parametrize("field", ["product_id", "version", "content_digest"])
@pytest.mark.parametrize("kind", ["missing", "json_null"])
def test_published_identity_check_independently_requires_nonnull_json_keys(registry_database, valid_manifest, field, kind):
    from psycopg.types.json import Jsonb
    manifest, draft = asyncio.run(seed(registry_database, valid_manifest))
    malformed = manifest.model_dump(mode="json")
    if kind == "missing": malformed.pop(field)
    else: malformed[field] = None
    # Isolate this table CHECK from the stronger publication trigger, only in the
    # fixture-created disposable database under its owner role.
    registry_database.execute_owner("ALTER TABLE gateway.product_versions DISABLE TRIGGER publication_integrity")
    assert registry_database.constraint_failure("INSERT INTO gateway.product_versions (id,product_id,version,manifest,content_digest,binding_id,binding_digest,policy_id,policy_revision,test_run_id,draft_id,actor_id,name,domain,classification) VALUES ('bad-version','revenue','1.0.0',%s,%s,'binding-1',%s,'revenue-access',1,'test-1',%s,'publisher','Daily revenue','finance','internal')", (Jsonb(malformed),manifest.content_digest,"b"*64,draft.id)) == "23514"


@pytest.mark.unit
def test_authenticated_tcp_readiness_rejects_a_closed_published_port(tmp_path):
    import socket
    import subprocess
    import sys
    from pathlib import Path
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    password = tmp_path / "synthetic-password"
    password.write_text("synthetic-not-a-real-account")
    completed = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] / "scripts/wait_registry_postgres.py"), str(password), str(port), "--timeout", "0.1"], capture_output=True, text=True, timeout=5)
    assert completed.returncode == 1
    assert "authenticated TCP" in completed.stderr
    assert "synthetic-not-a-real-account" not in completed.stdout + completed.stderr


@pytest.mark.postgres
@pytest.mark.parametrize("registry_database", [{"unsafe_role_flag": "app", "expect_rejection": True}], indirect=True)
def test_migration_rejects_replication_role_capability(registry_database):
    assert registry_database.migration_error == "All four registry roles must exist without elevated privileges"
    assert registry_database.scalar("SELECT count(*) FROM pg_namespace WHERE nspname='gateway'") == 0


@pytest.mark.postgres
def test_authenticated_tcp_readiness_reaches_real_test_server(registry_database, tmp_path):
    from pathlib import Path
    import subprocess
    import sys
    password = tmp_path / "synthetic-admin-password"
    password.write_text(registry_database.admin_url.password)
    completed = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] / "scripts/wait_registry_postgres.py"), str(password), str(registry_database.admin_url.port), "--timeout", "5"], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0
    assert "authenticated TCP readiness confirmed" in completed.stdout
    assert registry_database.admin_url.password not in completed.stdout + completed.stderr
