"""Frozen initial registry schema and least-privilege application roles.

Revision ID: 0001_registry
This migration deliberately does not import live ORM metadata.
"""
import os
import re

from alembic import op

revision = "0001_registry"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA_STATEMENTS = (
    """CREATE SCHEMA gateway""",
    """REVOKE ALL ON SCHEMA gateway FROM PUBLIC""",
    """CREATE TABLE gateway.audit_events (
    actor_id VARCHAR(128) NOT NULL,
    service_client_id VARCHAR(128),
    action VARCHAR(64) NOT NULL,
    product_id VARCHAR(128),
    version_id VARCHAR(128),
    policy_revision INTEGER,
    decision VARCHAR(32) NOT NULL,
    parameter_names JSONB DEFAULT '[]'::jsonb NOT NULL,
    parameter_digest VARCHAR(64),
    duration_ms INTEGER,
    row_count INTEGER,
    adapter VARCHAR(128),
    correlation_id VARCHAR(128) NOT NULL,
    id VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT pk_audit_events PRIMARY KEY (id),
    CONSTRAINT ck_audit_events_parameter_names_array CHECK (jsonb_typeof(parameter_names) = 'array'),
    CONSTRAINT ck_audit_events_digest_format CHECK (parameter_digest IS NULL OR parameter_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_audit_events_duration_nonnegative CHECK (duration_ms IS NULL OR duration_ms >= 0),
    CONSTRAINT ck_audit_events_row_count_nonnegative CHECK (row_count IS NULL OR row_count >= 0)
)""",
    """CREATE TABLE gateway.binding_revisions (
    binding_id VARCHAR(128) NOT NULL,
    revision INTEGER NOT NULL,
    content JSONB NOT NULL,
    content_digest VARCHAR(64) NOT NULL,
    model_bundle_digest VARCHAR(64) NOT NULL,
    adapter_revision VARCHAR(128) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    id VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT pk_binding_revisions PRIMARY KEY (id),
    CONSTRAINT uq_binding_revisions_binding_id UNIQUE (binding_id, revision),
    CONSTRAINT uq_binding_revisions_id UNIQUE (id, content_digest),
    CONSTRAINT ck_binding_revisions_revision_positive CHECK (revision > 0),
    CONSTRAINT ck_binding_revisions_digest_format CHECK (content_digest ~ '^[0-9a-f]{64}$' AND model_bundle_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_binding_revisions_content_object CHECK (jsonb_typeof(content) = 'object')
)""",
    """CREATE TABLE gateway.capacity_locks (
    scope_id VARCHAR(128) NOT NULL,
    CONSTRAINT pk_capacity_locks PRIMARY KEY (scope_id)
)""",
    """CREATE TABLE gateway.policies (
    id VARCHAR(128) NOT NULL,
    current_revision INTEGER NOT NULL,
    CONSTRAINT pk_policies PRIMARY KEY (id),
    CONSTRAINT ck_policies_revision_positive CHECK (current_revision > 0)
)""",
    """CREATE TABLE gateway.principals (
    id VARCHAR(128) NOT NULL,
    subject VARCHAR(512) NOT NULL,
    service_client_id VARCHAR(128),
    kind VARCHAR(16) NOT NULL,
    active BOOLEAN DEFAULT true NOT NULL,
    CONSTRAINT pk_principals PRIMARY KEY (id),
    CONSTRAINT ck_principals_kind_valid CHECK (kind IN ('user', 'service', 'demo')),
    CONSTRAINT uq_principals_subject UNIQUE (subject)
)""",
    """CREATE TABLE gateway.products (
    id VARCHAR(128) NOT NULL,
    draft_revision INTEGER DEFAULT '0' NOT NULL,
    latest_draft_id VARCHAR(128),
    current_version_id VARCHAR(128),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT pk_products PRIMARY KEY (id),
    CONSTRAINT ck_products_draft_revision_nonnegative CHECK (draft_revision >= 0),
    CONSTRAINT ck_products_draft_head_coherent CHECK ((draft_revision = 0 AND latest_draft_id IS NULL) OR (draft_revision > 0 AND latest_draft_id IS NOT NULL))
)""",
    """CREATE TABLE gateway.draft_revisions (
    product_id VARCHAR(128) NOT NULL,
    revision INTEGER NOT NULL,
    manifest JSONB NOT NULL,
    content_digest VARCHAR(64) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    id VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT pk_draft_revisions PRIMARY KEY (id),
    CONSTRAINT uq_draft_revisions_product_id UNIQUE (product_id, revision),
    CONSTRAINT uq_draft_id_product_revision UNIQUE (id, product_id, revision),
    CONSTRAINT uq_draft_id_product_digest UNIQUE (id, product_id, content_digest),
    CONSTRAINT ck_draft_revisions_revision_positive CHECK (revision > 0),
    CONSTRAINT ck_draft_revisions_digest_format CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_draft_revisions_manifest_identity CHECK ((jsonb_typeof(manifest) = 'object' AND manifest->>'product_id' = product_id AND manifest->>'content_digest' = content_digest) IS TRUE),
    CONSTRAINT fk_draft_revisions_product_id_products FOREIGN KEY(product_id) REFERENCES gateway.products (id)
)""",
    """CREATE TABLE gateway.policy_revisions (
    policy_id VARCHAR(128) NOT NULL,
    revision INTEGER NOT NULL,
    content JSONB NOT NULL,
    content_digest VARCHAR(64) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    id VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT pk_policy_revisions PRIMARY KEY (id),
    CONSTRAINT uq_policy_revisions_policy_id UNIQUE (policy_id, revision),
    CONSTRAINT ck_policy_revisions_revision_positive CHECK (revision > 0),
    CONSTRAINT ck_policy_revisions_digest_format CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_policy_revisions_content_object CHECK (jsonb_typeof(content) = 'object'),
    CONSTRAINT fk_policy_revisions_policy_id_policies FOREIGN KEY(policy_id) REFERENCES gateway.policies (id)
)""",
    """CREATE TABLE gateway.principal_grants (
    principal_id VARCHAR(128) NOT NULL,
    policy_id VARCHAR(128) NOT NULL,
    tenant_id VARCHAR(128) NOT NULL,
    roles JSONB NOT NULL,
    entitlements JSONB NOT NULL,
    active BOOLEAN DEFAULT true NOT NULL,
    CONSTRAINT pk_principal_grants PRIMARY KEY (principal_id, policy_id, tenant_id),
    CONSTRAINT ck_principal_grants_grants_shape CHECK (jsonb_typeof(roles) = 'array' AND jsonb_typeof(entitlements) = 'object'),
    CONSTRAINT fk_principal_grants_principal_id_principals FOREIGN KEY(principal_id) REFERENCES gateway.principals (id),
    CONSTRAINT fk_principal_grants_policy_id_policies FOREIGN KEY(policy_id) REFERENCES gateway.policies (id)
)""",
    """CREATE TABLE gateway.test_runs (
    product_id VARCHAR(128) NOT NULL,
    draft_id VARCHAR(128) NOT NULL,
    draft_digest VARCHAR(64) NOT NULL,
    binding_id VARCHAR(128) NOT NULL,
    binding_digest VARCHAR(64) NOT NULL,
    policy_id VARCHAR(128) NOT NULL,
    policy_revision INTEGER NOT NULL,
    fixture_version VARCHAR(128) NOT NULL,
    evidence JSONB NOT NULL,
    status VARCHAR(16) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    id VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT pk_test_runs PRIMARY KEY (id),
    CONSTRAINT fk_test_runs_policy_id_policy_revisions FOREIGN KEY(policy_id, policy_revision) REFERENCES gateway.policy_revisions (policy_id, revision),
    CONSTRAINT ck_test_runs_status_valid CHECK (status IN ('passed', 'failed')),
    CONSTRAINT ck_test_runs_digest_format CHECK (draft_digest ~ '^[0-9a-f]{64}$' AND binding_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_test_runs_evidence_object CHECK (jsonb_typeof(evidence) = 'object'),
    CONSTRAINT fk_test_runs_product_id_products FOREIGN KEY(product_id) REFERENCES gateway.products (id),
    CONSTRAINT fk_test_runs_draft_id_draft_revisions FOREIGN KEY(draft_id) REFERENCES gateway.draft_revisions (id),
    CONSTRAINT fk_test_runs_binding_id_binding_revisions FOREIGN KEY(binding_id) REFERENCES gateway.binding_revisions (id)
)""",
    """CREATE TABLE gateway.product_versions (
    product_id VARCHAR(128) NOT NULL,
    version VARCHAR(128) NOT NULL,
    manifest JSONB NOT NULL,
    content_digest VARCHAR(64) NOT NULL,
    binding_id VARCHAR(128) NOT NULL,
    binding_digest VARCHAR(64) NOT NULL,
    policy_id VARCHAR(128) NOT NULL,
    policy_revision INTEGER NOT NULL,
    test_run_id VARCHAR(128) NOT NULL,
    draft_id VARCHAR(128) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    name VARCHAR(256) NOT NULL,
    domain VARCHAR(128) NOT NULL,
    classification VARCHAR(16) NOT NULL,
    id VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT pk_product_versions PRIMARY KEY (id),
    CONSTRAINT uq_product_versions_product_id UNIQUE (product_id, version),
    CONSTRAINT uq_product_versions_id UNIQUE (id, product_id),
    CONSTRAINT fk_product_versions_draft_id_draft_revisions FOREIGN KEY(draft_id, product_id, content_digest) REFERENCES gateway.draft_revisions (id, product_id, content_digest),
    CONSTRAINT fk_product_versions_binding_id_binding_revisions FOREIGN KEY(binding_id, binding_digest) REFERENCES gateway.binding_revisions (id, content_digest),
    CONSTRAINT fk_product_versions_policy_id_policy_revisions FOREIGN KEY(policy_id, policy_revision) REFERENCES gateway.policy_revisions (policy_id, revision),
    CONSTRAINT ck_product_versions_digest_format CHECK (content_digest ~ '^[0-9a-f]{64}$' AND binding_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_product_versions_manifest_identity CHECK ((jsonb_typeof(manifest) = 'object' AND manifest->>'product_id' = product_id AND manifest->>'version' = version AND manifest->>'content_digest' = content_digest) IS TRUE),
    CONSTRAINT fk_product_versions_product_id_products FOREIGN KEY(product_id) REFERENCES gateway.products (id),
    CONSTRAINT uq_product_versions_test_run_id UNIQUE (test_run_id),
    CONSTRAINT fk_product_versions_test_run_id_test_runs FOREIGN KEY(test_run_id) REFERENCES gateway.test_runs (id),
    CONSTRAINT uq_product_versions_draft_id UNIQUE (draft_id)
)""",
    """CREATE TABLE gateway.executions (
    principal_id VARCHAR(128) NOT NULL,
    product_id VARCHAR(128) NOT NULL,
    version_id VARCHAR(128) NOT NULL,
    policy_id VARCHAR(128) NOT NULL,
    policy_revision INTEGER NOT NULL,
    canonical_query JSONB NOT NULL,
    canonical_hash VARCHAR(64) NOT NULL,
    context VARCHAR(16) NOT NULL,
    request_id VARCHAR(128) NOT NULL,
    deadline_at TIMESTAMP WITH TIME ZONE NOT NULL,
    id VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT pk_executions PRIMARY KEY (id),
    CONSTRAINT fk_executions_version_id_product_versions FOREIGN KEY(version_id, product_id) REFERENCES gateway.product_versions (id, product_id),
    CONSTRAINT fk_executions_policy_id_policy_revisions FOREIGN KEY(policy_id, policy_revision) REFERENCES gateway.policy_revisions (policy_id, revision),
    CONSTRAINT uq_execution_result_scope UNIQUE (id, principal_id, version_id, policy_revision, canonical_hash),
    CONSTRAINT uq_execution_idempotency_scope UNIQUE (id, principal_id, version_id, canonical_hash),
    CONSTRAINT ck_executions_context_valid CHECK (context IN ('consumer', 'fixture_test')),
    CONSTRAINT ck_executions_digest_format CHECK (canonical_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_executions_query_object CHECK (jsonb_typeof(canonical_query) = 'object'),
    CONSTRAINT ck_executions_bounded_deadline CHECK (deadline_at > created_at AND deadline_at <= created_at + interval '120 seconds'),
    CONSTRAINT fk_executions_principal_id_principals FOREIGN KEY(principal_id) REFERENCES gateway.principals (id)
)""",
    """CREATE TABLE gateway.version_states (
    version_id VARCHAR(128) NOT NULL,
    status VARCHAR(16) DEFAULT 'published' NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    reason VARCHAR(1024) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT pk_version_states PRIMARY KEY (version_id),
    CONSTRAINT ck_version_states_status_valid CHECK (status IN ('published', 'deprecated', 'withdrawn')),
    CONSTRAINT fk_version_states_version_id_product_versions FOREIGN KEY(version_id) REFERENCES gateway.product_versions (id)
)""",
    """CREATE TABLE gateway.execution_leases (
    execution_id VARCHAR(128) NOT NULL,
    worker_id VARCHAR(128) NOT NULL,
    lease_token VARCHAR(128) NOT NULL,
    generation INTEGER DEFAULT '1' NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    source_reserved_until TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_execution_leases PRIMARY KEY (execution_id),
    CONSTRAINT ck_execution_leases_generation_positive CHECK (generation > 0),
    CONSTRAINT fk_execution_leases_execution_id_executions FOREIGN KEY(execution_id) REFERENCES gateway.executions (id),
    CONSTRAINT uq_execution_leases_lease_token UNIQUE (lease_token)
)""",
    """CREATE TABLE gateway.execution_states (
    execution_id VARCHAR(128) NOT NULL,
    state VARCHAR(16) DEFAULT 'accepted' NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    error_code VARCHAR(32),
    CONSTRAINT pk_execution_states PRIMARY KEY (execution_id),
    CONSTRAINT ck_execution_states_state_valid CHECK (state IN ('accepted', 'running', 'succeeded', 'failed', 'timed_out', 'expired')),
    CONSTRAINT fk_execution_states_execution_id_executions FOREIGN KEY(execution_id) REFERENCES gateway.executions (id)
)""",
    """CREATE TABLE gateway.idempotency_records (
    principal_id VARCHAR(128) NOT NULL,
    product_id VARCHAR(128) NOT NULL,
    version_id VARCHAR(128) NOT NULL,
    key_digest VARCHAR(64) NOT NULL,
    canonical_hash VARCHAR(64) NOT NULL,
    execution_id VARCHAR(128) NOT NULL,
    CONSTRAINT pk_idempotency_records PRIMARY KEY (principal_id, product_id, version_id, key_digest),
    CONSTRAINT fk_idempotency_records_execution_id_executions FOREIGN KEY(execution_id, principal_id, version_id, canonical_hash) REFERENCES gateway.executions (id, principal_id, version_id, canonical_hash),
    CONSTRAINT fk_idempotency_records_version_id_product_versions FOREIGN KEY(version_id, product_id) REFERENCES gateway.product_versions (id, product_id),
    CONSTRAINT ck_idempotency_records_digest_format CHECK (key_digest ~ '^[0-9a-f]{64}$' AND canonical_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fk_idempotency_records_principal_id_principals FOREIGN KEY(principal_id) REFERENCES gateway.principals (id)
)""",
    """CREATE TABLE gateway.results (
    execution_id VARCHAR(128) NOT NULL,
    principal_id VARCHAR(128) NOT NULL,
    version_id VARCHAR(128) NOT NULL,
    policy_revision INTEGER NOT NULL,
    canonical_hash VARCHAR(64) NOT NULL,
    envelope JSONB NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    id VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT pk_results PRIMARY KEY (id),
    CONSTRAINT fk_results_execution_id_executions FOREIGN KEY(execution_id, principal_id, version_id, policy_revision, canonical_hash) REFERENCES gateway.executions (id, principal_id, version_id, policy_revision, canonical_hash),
    CONSTRAINT ck_results_envelope_object CHECK (jsonb_typeof(envelope) = 'object'),
    CONSTRAINT ck_results_bounded_retention CHECK (expires_at > created_at AND expires_at <= created_at + interval '24 hours'),
    CONSTRAINT uq_results_id UNIQUE (id, principal_id, version_id, policy_revision),
    CONSTRAINT uq_results_execution_id UNIQUE (execution_id)
)""",
    """CREATE TABLE gateway.result_cursors (
    token_digest VARCHAR(64) NOT NULL,
    result_id VARCHAR(128) NOT NULL,
    principal_id VARCHAR(128) NOT NULL,
    version_id VARCHAR(128) NOT NULL,
    policy_revision INTEGER NOT NULL,
    position INTEGER NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    id VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT pk_result_cursors PRIMARY KEY (id),
    CONSTRAINT fk_result_cursors_result_id_results FOREIGN KEY(result_id, principal_id, version_id, policy_revision) REFERENCES gateway.results (id, principal_id, version_id, policy_revision) ON DELETE CASCADE,
    CONSTRAINT ck_result_cursors_position_nonnegative CHECK (position >= 0),
    CONSTRAINT ck_result_cursors_digest_format CHECK (token_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT uq_result_cursors_token_digest UNIQUE (token_digest)
)""",
    """ALTER TABLE gateway.policies ADD CONSTRAINT fk_policy_current_revision FOREIGN KEY(id, current_revision) REFERENCES gateway.policy_revisions (policy_id, revision) DEFERRABLE INITIALLY DEFERRED""",
    """ALTER TABLE gateway.products ADD CONSTRAINT fk_product_latest_draft FOREIGN KEY(latest_draft_id, id, draft_revision) REFERENCES gateway.draft_revisions (id, product_id, revision) DEFERRABLE INITIALLY DEFERRED""",
    """ALTER TABLE gateway.products ADD CONSTRAINT fk_product_current_version FOREIGN KEY(current_version_id, id) REFERENCES gateway.product_versions (id, product_id)""",
    """CREATE INDEX ix_audit_events_retention ON gateway.audit_events (created_at)""",
    """CREATE INDEX ix_product_versions_catalog ON gateway.product_versions (domain, classification, name)""",
    """CREATE INDEX ix_execution_states_queue ON gateway.execution_states (state)""",
    """CREATE INDEX ix_results_retention ON gateway.results (expires_at)""",
)

IMMUTABLE_TABLES = ("draft_revisions", "binding_revisions", "policy_revisions", "test_runs", "product_versions", "executions", "idempotency_records", "capacity_locks")


def roles():
    values = {}
    for key in ("owner", "app", "audit_retention", "source_reader"):
        value = op.get_context().config.attributes.get("registry_roles", {}).get(key) or os.environ.get(f"GATEWAY_{key.upper()}_ROLE", f"gateway_{key}")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
            raise RuntimeError("Registry role names must be safe PostgreSQL identifiers")
        values[key] = value
    if len(set(values.values())) != 4:
        raise RuntimeError("Registry roles must be distinct")
    return values


def upgrade():
    configured = roles()
    if not op.get_context().as_sql:
        from sqlalchemy import text
        connection = op.get_bind()
        actual = connection.execute(text("SELECT current_user")).scalar_one()
        if actual != configured["owner"]:
            raise RuntimeError("Run migrations as the separately provisioned registry owner")
        found = connection.execute(text("SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolreplication FROM pg_roles WHERE rolname IN (:owner, :app, :audit_retention, :source_reader)"), configured).all()
        if len(found) != 4 or any(any(row[1:]) for row in found):
            raise RuntimeError("All four registry roles must exist without elevated privileges")
        # Any explicit membership can carry inherited or SET ROLE authority,
        # including transitive predefined-role privileges. Fresh service roles
        # need none. pg_database_owner membership is implicit and has no catalog
        # edge, so the legitimate owner's database ownership remains accepted.
        memberships = connection.execute(text("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname IN (:owner, :app, :audit_retention, :source_reader)"), configured).scalar_one()
        if memberships:
            raise RuntimeError("Registry roles must have no explicit role memberships")
    for statement in SCHEMA_STATEMENTS:
        op.execute(statement)
    op.execute("""
        CREATE FUNCTION gateway.deny_immutable_change() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, gateway AS $$
        BEGIN
            RAISE EXCEPTION 'Immutable registry content cannot be changed' USING ERRCODE = '42501';
        END $$
    """)
    for table in IMMUTABLE_TABLES:
        op.execute(f"CREATE TRIGGER immutable_content BEFORE UPDATE OR DELETE ON gateway.{table} FOR EACH ROW EXECUTE FUNCTION gateway.deny_immutable_change()")
    op.execute("""
        CREATE FUNCTION gateway.guard_audit_retention() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, gateway AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND OLD.created_at <= statement_timestamp() - interval '30 days' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'Audit events are append-only until retention expiry' USING ERRCODE = '42501';
        END $$
    """)
    op.execute("CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON gateway.audit_events FOR EACH ROW EXECUTE FUNCTION gateway.guard_audit_retention()")
    op.execute("""
        CREATE FUNCTION gateway.guard_result_retention() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, gateway AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND OLD.expires_at <= statement_timestamp() THEN RETURN OLD; END IF;
            RAISE EXCEPTION 'Result content is immutable until expiry' USING ERRCODE = '42501';
        END $$
    """)
    op.execute("CREATE TRIGGER result_immutable BEFORE UPDATE OR DELETE ON gateway.results FOR EACH ROW EXECUTE FUNCTION gateway.guard_result_retention()")
    op.execute("""
        CREATE FUNCTION gateway.guard_cursor() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, gateway AS $$
        DECLARE expiry timestamptz;
        BEGIN
            SELECT expires_at INTO expiry FROM gateway.results WHERE id = NEW.result_id;
            IF expiry IS NULL OR NEW.expires_at > expiry OR NEW.expires_at <= statement_timestamp() THEN
                RAISE EXCEPTION 'Cursor lifetime must fit a live result' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER cursor_expiry BEFORE INSERT ON gateway.result_cursors FOR EACH ROW EXECUTE FUNCTION gateway.guard_cursor()")
    # Cursor DELETE is safe invalidation; payload edits are forbidden.
    op.execute("CREATE TRIGGER cursor_immutable BEFORE UPDATE ON gateway.result_cursors FOR EACH ROW EXECUTE FUNCTION gateway.deny_immutable_change()")
    op.execute("""
        CREATE FUNCTION gateway.guard_head_revisions() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, gateway AS $$
        BEGIN
            IF TG_TABLE_NAME = 'products' THEN
                IF NEW.draft_revision != OLD.draft_revision AND NEW.draft_revision != OLD.draft_revision + 1 THEN
                    RAISE EXCEPTION 'Draft revision must advance once' USING ERRCODE = 'P0001';
                END IF;
                IF NEW.draft_revision = OLD.draft_revision AND NEW.latest_draft_id IS DISTINCT FROM OLD.latest_draft_id THEN
                    RAISE EXCEPTION 'Draft head requires a new revision' USING ERRCODE = 'P0001';
                END IF;
            ELSE
                IF NEW.current_revision != OLD.current_revision AND NEW.current_revision != OLD.current_revision + 1 THEN
                    RAISE EXCEPTION 'Policy revision must advance once' USING ERRCODE = 'P0001';
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER product_revision BEFORE UPDATE ON gateway.products FOR EACH ROW EXECUTE FUNCTION gateway.guard_head_revisions()")
    op.execute("CREATE TRIGGER policy_revision BEFORE UPDATE ON gateway.policies FOR EACH ROW EXECUTE FUNCTION gateway.guard_head_revisions()")
    op.execute("""
        CREATE FUNCTION gateway.guard_publication() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, gateway AS $$
        DECLARE p gateway.products; d gateway.draft_revisions; b gateway.binding_revisions;
                t gateway.test_runs; policy_head integer;
        BEGIN
            -- The same lock order as the repository makes direct SQL safe too.
            SELECT * INTO p FROM gateway.products WHERE id = NEW.product_id FOR UPDATE;
            SELECT * INTO d FROM gateway.draft_revisions WHERE id = NEW.draft_id;
            SELECT current_revision INTO policy_head FROM gateway.policies WHERE id = NEW.policy_id FOR UPDATE;
            SELECT * INTO b FROM gateway.binding_revisions WHERE id = NEW.binding_id;
            SELECT * INTO t FROM gateway.test_runs WHERE id = NEW.test_run_id;
            IF p.id IS NULL OR d.id IS NULL OR b.id IS NULL OR t.id IS NULL OR policy_head IS NULL
                OR p.latest_draft_id IS DISTINCT FROM d.id OR p.draft_revision IS DISTINCT FROM d.revision
                OR d.product_id IS DISTINCT FROM NEW.product_id OR d.manifest IS DISTINCT FROM NEW.manifest
                OR d.content_digest IS DISTINCT FROM NEW.content_digest
                OR b.content_digest IS DISTINCT FROM NEW.binding_digest
                OR b.binding_id IS DISTINCT FROM NEW.manifest->>'binding_id'
                OR b.revision::text IS DISTINCT FROM NEW.manifest->>'binding_revision'
                OR b.model_bundle_digest IS DISTINCT FROM NEW.manifest->>'model_bundle_digest'
                OR b.adapter_revision IS DISTINCT FROM NEW.manifest->>'adapter_revision'
                OR NEW.policy_id IS DISTINCT FROM NEW.manifest->>'policy_id'
                OR NEW.policy_revision IS DISTINCT FROM policy_head
                OR t.status IS DISTINCT FROM 'passed' OR t.product_id IS DISTINCT FROM NEW.product_id
                OR t.draft_id IS DISTINCT FROM NEW.draft_id OR t.draft_digest IS DISTINCT FROM NEW.content_digest
                OR t.binding_id IS DISTINCT FROM NEW.binding_id OR t.binding_digest IS DISTINCT FROM NEW.binding_digest
                OR t.policy_id IS DISTINCT FROM NEW.policy_id OR t.policy_revision IS DISTINCT FROM NEW.policy_revision
                OR NEW.name IS DISTINCT FROM NEW.manifest->>'name'
                OR NEW.domain IS DISTINCT FROM NEW.manifest->>'domain'
                OR NEW.classification IS DISTINCT FROM NEW.manifest->>'classification' THEN
                RAISE EXCEPTION 'Publication requires exact current tested content' USING ERRCODE = 'P0001';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER publication_integrity BEFORE INSERT ON gateway.product_versions FOR EACH ROW EXECUTE FUNCTION gateway.guard_publication()")
    reset_acl_baseline(configured)
    grant_runtime_privileges(configured)
    op.execute("INSERT INTO gateway.capacity_locks (scope_id) VALUES ('deployment')")


def reset_acl_baseline(configured):
    """Remove inherited owner-default ACLs, including column grants.

    Only this migration's gateway objects and Alembic version table are touched.
    Every non-owner grantee is cleared, so unknown default-grant recipients do
    not survive. Role membership admission is handled separately above.
    """
    op.execute("""
        DO $$
        DECLARE obj record; recipient record; recipient_sql text; columns_sql text;
        BEGIN
            FOR obj IN
                SELECT c.oid, c.relowner, format('%I.%I',n.nspname,c.relname) AS name
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE c.relkind IN ('r','p') AND (n.nspname='gateway'
                    OR (n.nspname='public' AND c.relname='alembic_version'))
            LOOP
                SELECT string_agg(format('%I',a.attname),', ' ORDER BY a.attnum)
                INTO columns_sql FROM pg_attribute a
                WHERE a.attrelid=obj.oid AND a.attnum>0 AND NOT a.attisdropped;
                FOR recipient IN
                    SELECT DISTINCT grants.grantee FROM (
                        SELECT acl.grantee FROM pg_class c,
                            LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) acl
                            WHERE c.oid=obj.oid
                        UNION ALL
                        SELECT acl.grantee FROM pg_attribute a,
                            LATERAL aclexplode(a.attacl) acl WHERE a.attrelid=obj.oid
                    ) grants WHERE grants.grantee<>obj.relowner
                LOOP
                    recipient_sql := CASE WHEN recipient.grantee=0 THEN 'PUBLIC'
                        ELSE format('%I',pg_get_userbyid(recipient.grantee)) END;
                    EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE %s FROM %s CASCADE',obj.name,recipient_sql);
                    EXECUTE format('REVOKE ALL PRIVILEGES (%s) ON TABLE %s FROM %s CASCADE',columns_sql,obj.name,recipient_sql);
                END LOOP;
            END LOOP;
            FOR obj IN SELECT n.oid,n.nspowner FROM pg_namespace n WHERE n.nspname='gateway'
            LOOP
                FOR recipient IN SELECT DISTINCT acl.grantee FROM pg_namespace n,
                    LATERAL aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) acl
                    WHERE n.oid=obj.oid AND acl.grantee<>obj.nspowner
                LOOP
                    recipient_sql := CASE WHEN recipient.grantee=0 THEN 'PUBLIC'
                        ELSE format('%I',pg_get_userbyid(recipient.grantee)) END;
                    EXECUTE format('REVOKE ALL PRIVILEGES ON SCHEMA gateway FROM %s CASCADE',recipient_sql);
                END LOOP;
            END LOOP;
            FOR obj IN SELECT p.oid,p.proowner,
                format('%I.%I(%s)',n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)) AS name
                FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='gateway'
            LOOP
                FOR recipient IN SELECT DISTINCT acl.grantee FROM pg_proc p,
                    LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) acl
                    WHERE p.oid=obj.oid AND acl.grantee<>obj.proowner
                LOOP
                    recipient_sql := CASE WHEN recipient.grantee=0 THEN 'PUBLIC'
                        ELSE format('%I',pg_get_userbyid(recipient.grantee)) END;
                    EXECUTE format('REVOKE ALL PRIVILEGES ON FUNCTION %s FROM %s CASCADE',obj.name,recipient_sql);
                END LOOP;
            END LOOP;
        END $$
    """)


def grant_runtime_privileges(configured):
    app = f'"{configured["app"]}"'
    retention = f'"{configured["audit_retention"]}"'
    op.execute(f"GRANT USAGE ON SCHEMA gateway TO {app}, {retention}")
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA gateway TO {app}")
    op.execute(f"GRANT SELECT ON public.alembic_version TO {app}")
    # Exclude created_at on inserts: app cannot backdate audit/retention clocks.
    # This list is frozen with the schema, avoiding grants on future columns.
    for table, columns in APP_INSERT_COLUMNS.items():
        op.execute(f"GRANT INSERT ({', '.join(columns)}) ON gateway.{table} TO {app}")
    for table, columns in APP_UPDATE_COLUMNS.items():
        op.execute(f"GRANT UPDATE ({', '.join(columns)}) ON gateway.{table} TO {app}")
    op.execute(f"GRANT DELETE ON gateway.results, gateway.result_cursors TO {app}")
    op.execute(f"GRANT SELECT, DELETE ON gateway.audit_events TO {retention}")


def downgrade():
    # This would destroy immutable evidence. Operators use reviewed restore/forward
    # migrations; there is no casual destructive alembic downgrade.
    raise RuntimeError("Destructive registry downgrade is disabled")

APP_INSERT_COLUMNS = {'audit_events': ['actor_id',
                  'service_client_id',
                  'action',
                  'product_id',
                  'version_id',
                  'policy_revision',
                  'decision',
                  'parameter_names',
                  'parameter_digest',
                  'duration_ms',
                  'row_count',
                  'adapter',
                  'correlation_id',
                  'id'],
 'binding_revisions': ['binding_id',
                       'revision',
                       'content',
                       'content_digest',
                       'model_bundle_digest',
                       'adapter_revision',
                       'actor_id',
                       'id'],
 'capacity_locks': ['scope_id'],
 'policies': ['id', 'current_revision'],
 'products': ['id', 'draft_revision', 'latest_draft_id', 'current_version_id'],
 'draft_revisions': ['product_id', 'revision', 'manifest', 'content_digest', 'actor_id', 'id'],
 'policy_revisions': ['policy_id', 'revision', 'content', 'content_digest', 'actor_id', 'id'],
 'test_runs': ['product_id',
               'draft_id',
               'draft_digest',
               'binding_id',
               'binding_digest',
               'policy_id',
               'policy_revision',
               'fixture_version',
               'evidence',
               'status',
               'actor_id',
               'id'],
 'product_versions': ['product_id',
                      'version',
                      'manifest',
                      'content_digest',
                      'binding_id',
                      'binding_digest',
                      'policy_id',
                      'policy_revision',
                      'test_run_id',
                      'draft_id',
                      'actor_id',
                      'name',
                      'domain',
                      'classification',
                      'id'],
 'executions': ['principal_id',
                'product_id',
                'version_id',
                'policy_id',
                'policy_revision',
                'canonical_query',
                'canonical_hash',
                'context',
                'request_id',
                'deadline_at',
                'id'],
 'version_states': ['version_id', 'status', 'actor_id', 'reason', 'updated_at'],
 'execution_leases': ['execution_id',
                      'worker_id',
                      'lease_token',
                      'generation',
                      'expires_at',
                      'source_reserved_until'],
 'execution_states': ['execution_id', 'state', 'started_at', 'completed_at', 'error_code'],
 'idempotency_records': ['principal_id',
                         'product_id',
                         'version_id',
                         'key_digest',
                         'canonical_hash',
                         'execution_id'],
 'results': ['execution_id',
             'principal_id',
             'version_id',
             'policy_revision',
             'canonical_hash',
             'envelope',
             'expires_at',
             'id'],
 'result_cursors': ['token_digest',
                    'result_id',
                    'principal_id',
                    'version_id',
                    'policy_revision',
                    'position',
                    'expires_at',
                    'id']}
APP_UPDATE_COLUMNS = {'capacity_locks': ['scope_id'],
 'products': ['draft_revision', 'latest_draft_id', 'current_version_id'],
 'policies': ['current_revision'],
 'draft_revisions': ['id'],
 'policy_revisions': ['id'],
 'version_states': ['status', 'actor_id', 'reason', 'updated_at'],
 'execution_states': ['state', 'started_at', 'completed_at', 'error_code'],
 'execution_leases': ['worker_id', 'lease_token', 'generation', 'expires_at', 'source_reserved_until']}
