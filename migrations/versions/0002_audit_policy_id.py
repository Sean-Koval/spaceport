"""Frozen additive policy identity for append-only audit records.

No policy foreign key: a denied request can name a nonexistent policy, and a
separate denial audit transaction must never wait for the policy decision lock.
Legacy events keep NULL identity; historical audit content is never rewritten.
"""
import os
import re

from alembic import op
from sqlalchemy import text

revision = "0002_audit_policy_id"
down_revision = "0001_registry"
branch_labels = None
depends_on = None


def roles():
    configured = {}
    for key in ("owner", "app", "audit_retention", "source_reader"):
        value = op.get_context().config.attributes.get("registry_roles", {}).get(key) or os.environ.get(f"GATEWAY_{key.upper()}_ROLE", f"gateway_{key}")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
            raise RuntimeError("Registry role names must be safe PostgreSQL identifiers")
        configured[key] = value
    if len(set(configured.values())) != 4:
        raise RuntimeError("Registry roles must be distinct")
    return configured


def upgrade():
    configured = roles()
    if not op.get_context().as_sql:
        connection = op.get_bind()
        if connection.execute(text("SELECT current_user")).scalar_one() != configured["owner"]:
            raise RuntimeError("Run migrations as the separately provisioned registry owner")
        found = connection.execute(text("SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolreplication FROM pg_roles WHERE rolname IN (:owner, :app, :audit_retention, :source_reader)"), configured).all()
        if len(found) != 4 or any(any(row[1:]) for row in found):
            raise RuntimeError("All four registry roles must exist without elevated privileges")
        if connection.execute(text("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname IN (:owner, :app, :audit_retention, :source_reader)"), configured).scalar_one():
            raise RuntimeError("Registry roles must have no explicit role memberships")
    op.execute("ALTER TABLE gateway.audit_events ADD COLUMN policy_id VARCHAR(128)")
    op.execute("ALTER TABLE gateway.audit_events ADD CONSTRAINT ck_audit_events_policy_id_format CHECK (policy_id IS NULL OR policy_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')")
    # Frozen 0001 grants INSERT column-by-column, deliberately excluding clocks.
    # Existing SELECT/retention rights are table-level and need no expansion.
    # This adds exactly one INSERT column; never UPDATE/DELETE or created_at.
    op.execute(f'GRANT INSERT (policy_id) ON gateway.audit_events TO "{configured["app"]}"')


def downgrade():
    raise RuntimeError("Destructive audit identity downgrade is disabled")
