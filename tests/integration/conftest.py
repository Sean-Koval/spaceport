"""Explicitly authorized, isolated PostgreSQL integration fixtures.

This only accepts the synthetic loopback admin database created by the CI script.
Each test gets a fresh random database and four non-elevated random roles. It
never drops the admin database or an object not created by this fixture run.
"""
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import re
import subprocess
import sys

import psycopg
from psycopg import sql
import pytest
from sqlalchemy.engine import URL, make_url

AUTHORIZATION = "disposable-local-postgres-only"
ROOT = Path(__file__).resolve().parents[2]
MIGRATION_EVIDENCE = []


def validate_test_admin_url(raw: str | None, authorization: str | None):
    if authorization != AUTHORIZATION or not raw:
        raise ValueError("Real PostgreSQL tests require explicit disposable-test authorization and a synthetic admin URL")
    try:
        url = make_url(raw)
        if (url.drivername != "postgresql+psycopg" or url.host not in {"127.0.0.1", "localhost"}
            or not url.port or url.database != "gateway_test_admin" or url.username != "gateway_test_admin"
            or not url.password or url.query):
            raise ValueError
    except Exception:
        raise ValueError("The test admin URL must name only the synthetic loopback gateway_test_admin database") from None
    return url


def connection_kwargs(url):
    return {"host": url.host, "port": url.port, "dbname": url.database,
        "user": url.username, "password": url.password, "connect_timeout": 5}


@pytest.fixture
def postgres_admin_guard():
    return validate_test_admin_url


@dataclass
class RegistryDatabase:
    registry: object
    roles: dict
    urls: dict
    migration_head: str
    first_migration_snapshot: list
    second_migration_snapshot: list
    migration_error: str | None = None
    admin_url: object | None = None

    def _execute(self, role, statement, params=(), *, scalar=False):
        with psycopg.connect(**connection_kwargs(self.urls[role])) as connection:
            cursor = connection.execute(statement, params)
            return cursor.fetchone()[0] if scalar else None

    def scalar(self, statement, params=()):
        return self._execute("owner", statement, params, scalar=True)

    def execute_owner(self, statement, params=()):
        return self._execute("owner", statement, params)

    def execute_app(self, statement, params=()):
        return self._execute("app", statement, params)

    def execute_retention(self, statement, params=()):
        return self._execute("audit_retention", statement, params)

    def execute_source(self, statement, params=(), *, scalar=False):
        return self._execute("source_reader", statement, params, scalar=scalar)

    def _denied(self, role, statement, params):
        try:
            self._execute(role, statement, params)
        except psycopg.Error as error:
            return "permission_denied" if error.sqlstate == "42501" else error.sqlstate
        return "unexpectedly_allowed"

    def denied_as_app(self, statement, params=()):
        return self._denied("app", statement, params)

    def denied_as_owner(self, statement, params=()):
        return self._denied("owner", statement, params)

    def denied_as_retention(self, statement, params=()):
        return self._denied("audit_retention", statement, params)

    def denied_as_source(self, statement, params=()):
        return self._denied("source_reader", statement, params)

    def constraint_failure(self, statement, params=()):
        try:
            self.execute_app(statement, params)
        except psycopg.Error as error:
            return error.sqlstate
        return "unexpectedly_allowed"

    def insert_audit(self, audit_id, at):
        self.execute_owner("INSERT INTO gateway.audit_events (id, actor_id, action, decision, correlation_id, created_at) VALUES (%s,'operator','synthetic_test','deny','test-correlation',%s)", (audit_id, at))


def migration_snapshot(url):
    with psycopg.connect(**connection_kwargs(url)) as connection:
        return connection.execute("SELECT n.nspname, c.relname, c.oid, c.relfilenode FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='gateway' OR (n.nspname='public' AND c.relname='alembic_version') ORDER BY n.nspname,c.relname").fetchall()


def run_migration(url, roles, target="head"):
    env = {**os.environ, "GATEWAY_MIGRATION_DATABASE_URL": url.render_as_string(hide_password=False),
        **{f"GATEWAY_{key.upper()}_ROLE": value for key, value in roles.items()}}
    return subprocess.run([sys.executable, "-m", "alembic", "upgrade", target], cwd=ROOT,
        env=env, capture_output=True, text=True, timeout=60)


def safe_migration_error(completed, url):
    error_lines = [line for line in completed.stderr.splitlines()
        if re.match(r"^(RuntimeError:|sqlalchemy\.exc\.[A-Za-z]+:|psycopg\.[A-Za-z.]+:)", line)]
    detail = error_lines[-1] if error_lines else "No safe exception summary available"
    detail = detail.replace(url.render_as_string(hide_password=False), "[redacted database URL]")
    detail = detail.replace(url.password, "[redacted synthetic password]")
    return detail.removeprefix("RuntimeError: ")[:1024]


def migrate(url, roles, target="head"):
    completed = run_migration(url, roles, target)
    if completed.returncode:
        pytest.fail("Disposable owner migration failed: " + safe_migration_error(completed, url), pytrace=False)


@pytest.fixture
def registry_database(request):
    options = getattr(request, "param", {})
    try:
        admin_url = validate_test_admin_url(os.environ.get("GATEWAY_TEST_POSTGRES_ADMIN_URL"),
            os.environ.get("GATEWAY_TEST_POSTGRES_AUTHORIZATION"))
    except ValueError as error:
        pytest.fail(str(error), pytrace=False)
    token = secrets.token_hex(8)
    database = f"gateway_test_{token}"
    roles = {key: f"gwt_{token}_{key}" for key in ("owner", "app", "audit_retention", "source_reader")}
    passwords = {key: secrets.token_urlsafe(32) for key in roles}
    urls = {key: URL.create("postgresql+psycopg", username=role, password=passwords[key],
        host=admin_url.host, port=admin_url.port, database=database) for key, role in roles.items()}
    created_roles = []
    created_database = False
    registry = None
    admin = None
    try:
        try:
            admin = psycopg.connect(**connection_kwargs(admin_url), autocommit=True)
        except psycopg.Error:
            pytest.fail("The explicitly configured disposable PostgreSQL server is unavailable", pytrace=False)
        for key, role in roles.items():
            admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}").format(sql.Identifier(role), sql.Literal(passwords[key])))
            created_roles.append(role)
        admin.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(sql.Identifier(database), sql.Identifier(roles["owner"])))
        created_database = True
        admin.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
        for role in roles.values():
            admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(role)))
        if options.get("unsafe_membership"):
            key, predefined, transitive = options["unsafe_membership"]
            recipient = roles[key]
            if transitive:
                bridge = f"gwt_{token}_bridge"
                admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS").format(sql.Identifier(bridge)))
                created_roles.append(bridge)
                admin.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(bridge), sql.Identifier(recipient)))
                recipient = bridge
            admin.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(predefined), sql.Identifier(recipient)))
        if options.get("unsafe_role_flag"):
            key = options["unsafe_role_flag"]
            admin.execute(sql.SQL("ALTER ROLE {} REPLICATION").format(sql.Identifier(roles[key])))
        if options.get("expect_rejection"):
            completed = run_migration(urls["owner"], roles)
            if completed.returncode == 0:
                pytest.fail("Migration unexpectedly admitted unsafe fixture-only role authority", pytrace=False)
            yield RegistryDatabase(None, roles, urls, "not-migrated", [], [],
                migration_error=safe_migration_error(completed, urls["owner"]), admin_url=admin_url)
            return
        if options.get("unsafe_default_grants"):
            with psycopg.connect(**connection_kwargs(urls["owner"])) as owner:
                recipients = sql.SQL(", ").join(sql.Identifier(roles[key]) for key in ("app", "source_reader", "audit_retention"))
                for object_kind in ("TABLES", "SCHEMAS", "FUNCTIONS"):
                    owner.execute(sql.SQL("ALTER DEFAULT PRIVILEGES GRANT ALL PRIVILEGES ON {} TO {}").format(sql.SQL(object_kind), recipients))
        initial_revision = options.get("initial_revision", "head")
        if initial_revision not in {"head", "0001_registry"}:
            pytest.fail("The disposable fixture accepts only the reviewed migration targets", pytrace=False)
        migrate(urls["owner"], roles, initial_revision)
        first = migration_snapshot(urls["owner"])
        migrate(urls["owner"], roles, initial_revision)
        second = migration_snapshot(urls["owner"])
        with psycopg.connect(**connection_kwargs(urls["owner"])) as owner:
            head = owner.execute("SELECT version_num FROM public.alembic_version").fetchone()[0]
            owner.execute("CREATE SCHEMA fixture_source; CREATE TABLE fixture_source.orders (id text PRIMARY KEY, amount numeric(38,2)); INSERT INTO fixture_source.orders VALUES ('synthetic-order',10.10)")
            # The adversarial default-ACL case deliberately leaves the owner's
            # global default policy unchanged. Isolate the separately created
            # source fixture itself before giving its reader SELECT only.
            recipients = sql.SQL(", ").join([sql.SQL("PUBLIC"), *(sql.Identifier(roles[key]) for key in ("app", "source_reader", "audit_retention"))])
            owner.execute(sql.SQL("REVOKE ALL ON SCHEMA fixture_source FROM {}; REVOKE ALL ON ALL TABLES IN SCHEMA fixture_source FROM {}").format(recipients, recipients))
            owner.execute(sql.SQL("GRANT USAGE ON SCHEMA fixture_source TO {}; GRANT SELECT ON ALL TABLES IN SCHEMA fixture_source TO {}").format(sql.Identifier(roles["source_reader"]), sql.Identifier(roles["source_reader"])))
            permissions = owner.execute("SELECT has_schema_privilege(%s,'gateway','CREATE'), has_table_privilege(%s,'gateway.audit_events','UPDATE'), has_schema_privilege(%s,'gateway','USAGE'), has_table_privilege(%s,'gateway.products','SELECT')", (roles["app"],roles["app"],roles["source_reader"],roles["audit_retention"])).fetchone()
            MIGRATION_EVIDENCE.append((head, first == second, permissions))
        from gateway.registry.repository import Registry
        registry = Registry(urls["app"].render_as_string(hide_password=False))
        yield RegistryDatabase(registry, roles, urls, head, first, second, admin_url=admin_url)
    finally:
        if registry is not None:
            asyncio.run(registry.close())
        if admin is not None:
            try:
                # Identifiers are generated here, never from a URL or user input.
                if created_database:
                    admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
                for role in reversed(created_roles):
                    admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
            finally:
                admin.close()


def pytest_terminal_summary(terminalreporter):
    if MIGRATION_EVIDENCE:
        terminalreporter.write_line("Real PostgreSQL migration heads: " + ", ".join(sorted({item[0] for item in MIGRATION_EVIDENCE})))
        terminalreporter.write_line(f"Second owner upgrade preserved table/index identities: {sum(item[1] for item in MIGRATION_EVIDENCE)}/{len(MIGRATION_EVIDENCE)} disposable databases")
        terminalreporter.write_line("Observed (app schema CREATE, app audit UPDATE, source registry USAGE, retention product SELECT): " + str(sorted({item[2] for item in MIGRATION_EVIDENCE})))
