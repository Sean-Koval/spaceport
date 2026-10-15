"""Stable source ownership and narrow serving functions; legacy roles still suffice.

Source login roles are separately provisioned and granted only by the explicit
source-pilot provisioning command. No provider grant is implicit in migration.
"""
import importlib

from alembic import op

revision = "0005_source_lifecycle"
down_revision = "0004_publishing_fixtures"
branch_labels = None
depends_on = None

PROVIDER_FUNCTIONS = (
    "source_check_epoch", "source_check_attempt", "source_bq_lookup",
    "source_bq_admit", "source_bq_check_data", "source_bq_observe",
    "source_bq_reconcile_claim", "source_bq_check_observation",
    "source_domo_arm_send", "source_domo_observe", "source_domo_not_sent",
    "source_seal_attempt", "source_status", "source_settle_group",
    "source_expire_groups",
)
FUNCTION_NAMES = PROVIDER_FUNCTIONS + (
    "source_seal_session",
    "source_current_authority", "source_lock_session", "source_role_allowed",
    "source_job_projection", "source_check_context", "source_check_owner",
    "source_guard_group", "source_guard_session", "source_close_group",
    "source_reclaim_session", "source_guard_owner_completion", "source_global_limit", "source_require_durability",
)
TABLES = ("source_runtime_settings", "source_pools", "source_profiles",
    "source_role_mappings", "source_operation_groups", "source_sessions",
    "bigquery_source_jobs", "domo_source_sends")


def function(name, arguments, result, body, *, stable=False):
    if name in {"source_guard_group","source_guard_session",
            "source_lock_session","source_settle_group","source_close_group","source_expire_groups",
            "source_bq_reconcile_claim"}:
        body=body.replace("BEGIN", "BEGIN\n      PERFORM gateway.source_require_durability();",1)
    op.execute(f"CREATE FUNCTION gateway.{name}({arguments}) RETURNS {result} "
        f"LANGUAGE plpgsql {'STABLE ' if stable else ''}SECURITY DEFINER "
        f"SET search_path=pg_catalog,gateway AS $source${body}$source$")


def upgrade():
    configured = importlib.import_module("migrations.versions.0004_publishing_fixtures").roles()
    app = '"' + configured["app"] + '"'
    op.execute("""CREATE TABLE gateway.source_runtime_settings (
        singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK(singleton),
        deployment_epoch VARCHAR(128) NOT NULL,
        app_role NAME NOT NULL,
        legacy_roles NAME[] NOT NULL CHECK(cardinality(legacy_roles)=4),
        max_active_operations INTEGER NOT NULL CHECK(max_active_operations BETWEEN 1 AND 100),
        max_groups INTEGER NOT NULL DEFAULT 1000 CHECK(max_groups BETWEEN 1 AND 1000),
        max_intents INTEGER NOT NULL DEFAULT 8000 CHECK(max_intents BETWEEN 1 AND 8000))""")
    legacy_roles = ",".join("'" + configured[key] + "'" for key in ("owner", "app", "audit_retention", "source_reader"))
    op.execute("INSERT INTO gateway.source_runtime_settings(singleton,deployment_epoch,app_role,legacy_roles,max_active_operations) "
        f"VALUES(true,'unconfigured','{configured['app']}',ARRAY[{legacy_roles}]::name[],1)")
    op.execute("""CREATE TABLE gateway.source_pools (
        capacity_key VARCHAR(128) PRIMARY KEY, provider VARCHAR(16) NOT NULL CHECK(provider IN ('bigquery','domo')),
        physical_identity JSONB NOT NULL CHECK(jsonb_typeof(physical_identity)='object' AND octet_length(physical_identity::text)<=2048),
        max_active INTEGER NOT NULL CHECK(max_active BETWEEN 1 AND 100),
        max_dispatches BIGINT NOT NULL CHECK(max_dispatches BETWEEN 1 AND 1000000),
        dispatches BIGINT NOT NULL DEFAULT 0 CHECK(dispatches BETWEEN 0 AND max_dispatches))""")
    op.execute("""CREATE TABLE gateway.source_profiles (
        source_id VARCHAR(128) NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        context VARCHAR(16) NOT NULL CHECK(context IN ('consumer','fixture_test')),
        provider VARCHAR(16) NOT NULL CHECK(provider IN ('bigquery','domo')),
        capacity_key VARCHAR(128) NOT NULL REFERENCES gateway.source_pools(capacity_key),
        descriptor_digest VARCHAR(64) NOT NULL CHECK(descriptor_digest ~ '^[a-f0-9]{64}$'),
        descriptor JSONB NOT NULL CHECK(jsonb_typeof(descriptor)='object' AND octet_length(descriptor::text)<=32768),
        PRIMARY KEY(source_id,revision,context))""")
    op.execute("""CREATE TABLE gateway.source_role_mappings (
        login_name NAME NOT NULL, source_id VARCHAR(128) NOT NULL, revision INTEGER NOT NULL,
        context VARCHAR(16) NOT NULL, role_kind VARCHAR(16) NOT NULL CHECK(role_kind IN ('runtime','observer')),
        PRIMARY KEY(login_name,source_id,revision,context),
        FOREIGN KEY(source_id,revision,context) REFERENCES gateway.source_profiles(source_id,revision,context))""")
    op.execute("""CREATE TABLE gateway.source_operation_groups (
        id UUID PRIMARY KEY, owner_kind VARCHAR(16) NOT NULL CHECK(owner_kind IN ('consumer','fixture_test')),
        owner_id VARCHAR(128) NOT NULL, actor_id VARCHAR(128) NOT NULL REFERENCES gateway.principals(id),
        source_id VARCHAR(128) NOT NULL, source_revision INTEGER NOT NULL, context VARCHAR(16) NOT NULL,
        capacity_key VARCHAR(128) NOT NULL REFERENCES gateway.source_pools(capacity_key),
        deployment_epoch VARCHAR(128) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        deadline_at TIMESTAMPTZ NOT NULL, roster JSONB NOT NULL,
        authority_spec JSONB NOT NULL, authority_snapshot JSONB NOT NULL, allow_audit_id VARCHAR(128) NOT NULL REFERENCES gateway.audit_events(id),
        max_submissions INTEGER NOT NULL CHECK(max_submissions BETWEEN 1 AND 8),
        max_total_bytes BIGINT CHECK(max_total_bytes BETWEEN 1 AND 9007199254740991),
        admitted_count INTEGER NOT NULL DEFAULT 0 CHECK(admitted_count BETWEEN 0 AND max_submissions),
        reserved_bytes BIGINT NOT NULL DEFAULT 0 CHECK(reserved_bytes>=0 AND (max_total_bytes IS NULL OR reserved_bytes<=max_total_bytes)),
        closed BOOLEAN NOT NULL DEFAULT false, close_reason VARCHAR(32), settled_at TIMESTAMPTZ,
        UNIQUE(owner_kind,owner_id),
        FOREIGN KEY(source_id,source_revision,context) REFERENCES gateway.source_profiles(source_id,revision,context),
        CHECK(owner_kind=context AND deadline_at>created_at AND deadline_at<=created_at+interval '120 seconds'),
        CHECK(jsonb_typeof(roster)='array' AND jsonb_array_length(roster) BETWEEN 1 AND 8 AND octet_length(roster::text)<=2048),
        CHECK(jsonb_typeof(authority_spec)='object' AND octet_length(authority_spec::text)<=2048
            AND jsonb_typeof(authority_snapshot)='object' AND octet_length(authority_snapshot::text)<=65536),
        CHECK((NOT closed AND close_reason IS NULL AND settled_at IS NULL) OR (closed AND close_reason IS NOT NULL)))""")
    op.execute("""CREATE TABLE gateway.source_sessions (
        id UUID PRIMARY KEY, group_id UUID NOT NULL REFERENCES gateway.source_operation_groups(id),
        case_id VARCHAR(128) NOT NULL, owner_id VARCHAR(128) NOT NULL,
        source_id VARCHAR(128) NOT NULL, source_revision INTEGER NOT NULL, context VARCHAR(16) NOT NULL,
        capacity_key VARCHAR(128) NOT NULL REFERENCES gateway.source_pools(capacity_key),
        plan JSONB NOT NULL, provider_nonce VARCHAR(128) NOT NULL,
        authority_spec JSONB NOT NULL, authority_snapshot JSONB NOT NULL,
        authority_stamp VARCHAR(64) NOT NULL CHECK(authority_stamp ~ '^[a-f0-9]{64}$'),
        context_digest VARCHAR(64) NOT NULL CHECK(context_digest ~ '^[a-f0-9]{64}$'),
        allow_audit_id VARCHAR(128) NOT NULL REFERENCES gateway.audit_events(id),
        generation INTEGER NOT NULL CHECK(generation>0), lease_token VARCHAR(128) NOT NULL,
        deadline_at TIMESTAMPTZ NOT NULL, sealed BOOLEAN NOT NULL DEFAULT false,
        seal_reason VARCHAR(32), created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(group_id,case_id), UNIQUE(context,owner_id),
        FOREIGN KEY(source_id,source_revision,context) REFERENCES gateway.source_profiles(source_id,revision,context),
        CHECK(jsonb_typeof(plan)='object' AND octet_length(plan::text)<=8192
            AND jsonb_typeof(authority_spec)='object' AND octet_length(authority_spec::text)<=2048
            AND jsonb_typeof(authority_snapshot)='object' AND octet_length(authority_snapshot::text)<=65536),
        CHECK((NOT sealed AND seal_reason IS NULL) OR (sealed AND seal_reason IS NOT NULL)))""")
    op.execute("""CREATE TABLE gateway.bigquery_source_jobs (
        job_id VARCHAR(128) PRIMARY KEY, session_id UUID NOT NULL REFERENCES gateway.source_sessions(id),
        semantic_key VARCHAR(64) NOT NULL CHECK(semantic_key ~ '^[a-f0-9]{64}$'),
        project_id VARCHAR(128) NOT NULL, location VARCHAR(64) NOT NULL, principal VARCHAR(254) NOT NULL,
        intent JSONB NOT NULL CHECK(jsonb_typeof(intent)='object' AND octet_length(intent::text)<=4096),
        intent_digest VARCHAR(64) NOT NULL CHECK(intent_digest ~ '^[a-f0-9]{64}$'),
        state VARCHAR(16) NOT NULL DEFAULT 'intent' CHECK(state IN ('intent','pending','running','done')),
        terminal_error BOOLEAN, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), terminal_at TIMESTAMPTZ,
        next_observation_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), observation_attempts INTEGER NOT NULL DEFAULT 0,
        observation_login NAME, observation_token_hash VARCHAR(64), observation_until TIMESTAMPTZ,
        UNIQUE(session_id,semantic_key),
        CHECK((state='done' AND terminal_error IS NOT NULL AND terminal_at IS NOT NULL)
            OR (state<>'done' AND terminal_error IS NULL AND terminal_at IS NULL)))""")
    op.execute("""CREATE TABLE gateway.domo_source_sends (
        session_id UUID PRIMARY KEY REFERENCES gateway.source_sessions(id),
        plan_fingerprint VARCHAR(64) NOT NULL CHECK(plan_fingerprint ~ '^[a-f0-9]{64}$'),
        nonce_hash VARCHAR(64) NOT NULL CHECK(nonce_hash ~ '^[a-f0-9]{64}$'),
        intent JSONB NOT NULL CHECK(jsonb_typeof(intent)='object' AND octet_length(intent::text)<=8192),
        state VARCHAR(24) NOT NULL DEFAULT 'intent' CHECK(state IN ('intent','terminal_success','proven_not_sent')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), terminal_at TIMESTAMPTZ, terminal_evidence JSONB,
        CHECK(terminal_evidence IS NULL OR (jsonb_typeof(terminal_evidence)='object' AND octet_length(terminal_evidence::text)<=2048)),
        CHECK((state='intent' AND terminal_at IS NULL AND terminal_evidence IS NULL)
          OR (state='terminal_success' AND terminal_at IS NOT NULL AND terminal_evidence IS NOT NULL)
          OR (state='proven_not_sent' AND terminal_at IS NOT NULL AND terminal_evidence IS NULL)))""")
    op.execute("CREATE INDEX source_groups_expiry ON gateway.source_operation_groups(deadline_at,id) WHERE settled_at IS NULL")
    op.execute("CREATE INDEX source_bq_reconciliation ON gateway.bigquery_source_jobs(next_observation_at,job_id) WHERE state<>'done'")
    _functions(configured)
    for table in TABLES:
        op.execute(f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON gateway.{table} FOR EACH ROW EXECUTE FUNCTION gateway.deny_immutable_change()")
    for table in ("source_profiles", "source_role_mappings"):
        op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE ON gateway.{table} FOR EACH ROW EXECUTE FUNCTION gateway.deny_immutable_change()")
    # Revoke every inherited default ACL, not just PUBLIC. Source grants are an
    # explicit later operation and the four-role legacy migration remains valid.
    tables = ",".join("'" + value + "'" for value in TABLES)
    functions = ",".join("'" + value + "'" for value in FUNCTION_NAMES)
    op.execute(f"""DO $$ DECLARE obj RECORD; recipient RECORD; who TEXT;
    BEGIN
      FOR obj IN SELECT c.oid,format('%I.%I',n.nspname,c.relname) name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='gateway' AND c.relname IN ({tables}) LOOP
        FOR recipient IN SELECT DISTINCT a.grantee FROM pg_class c,LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a WHERE c.oid=obj.oid AND a.grantee<>c.relowner LOOP
          who:=CASE WHEN recipient.grantee=0 THEN 'PUBLIC' ELSE format('%I',pg_get_userbyid(recipient.grantee)) END;
          EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE %s FROM %s CASCADE',obj.name,who);
        END LOOP;
      END LOOP;
      FOR obj IN SELECT p.oid,format('%I.%I(%s)',n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)) name FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='gateway' AND p.proname IN ({functions}) LOOP
        FOR recipient IN SELECT DISTINCT a.grantee FROM pg_proc p,LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a WHERE p.oid=obj.oid AND a.grantee<>p.proowner LOOP
          who:=CASE WHEN recipient.grantee=0 THEN 'PUBLIC' ELSE format('%I',pg_get_userbyid(recipient.grantee)) END;
          EXECUTE format('REVOKE ALL PRIVILEGES ON FUNCTION %s FROM %s CASCADE',obj.name,who);
        END LOOP;
      END LOOP;
    END $$""")
    op.execute(f"GRANT SELECT ON gateway.source_profiles,gateway.source_operation_groups,gateway.source_sessions TO {app}")
    op.execute(f"GRANT INSERT ON gateway.source_operation_groups,gateway.source_sessions TO {app}")
    for signature in ("source_current_authority(jsonb)", "source_check_epoch(text)",
        "source_seal_session(uuid,text)", "source_seal_attempt(jsonb,text)", "source_close_group(uuid,text)", "source_status(uuid)",
        "source_settle_group(uuid)", "source_reclaim_session(uuid,integer,text,timestamptz)",
        "source_expire_groups(integer)"):
        op.execute(f"GRANT EXECUTE ON FUNCTION gateway.{signature} TO {app}")
    op.execute("""CREATE OR REPLACE VIEW gateway.held_source_capacity WITH (security_invoker=true) AS
        SELECT id AS reservation_id,principal_id FROM gateway.source_reservations WHERE released_at IS NULL
        UNION ALL SELECT id,actor_id FROM gateway.fixture_source_reservations WHERE released_at IS NULL
        UNION ALL SELECT id::varchar(128),actor_id FROM gateway.source_operation_groups WHERE settled_at IS NULL""")


def _functions(configured):
    # Added below in dependency order. No source role can invoke private helpers.
    _authority_functions()
    _lifecycle_functions(configured)
    _provider_functions()


def _authority_functions():
    function("source_require_durability", "", "void", """
    BEGIN
      IF current_setting('fsync') IS DISTINCT FROM 'on' OR current_setting('full_page_writes') IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'Durable source storage settings are required' USING ERRCODE='42501'; END IF;
      PERFORM set_config('synchronous_commit','on',true);
    END""")
    function("source_current_authority", "spec jsonb", "jsonb", """
    DECLARE result jsonb;
    BEGIN
      IF jsonb_typeof(spec) IS DISTINCT FROM 'object' OR octet_length(spec::text)>2048
        OR NOT (spec ?& ARRAY['principal_id','actor_id','policy_id','version_id','draft_id','catalog_digest'])
        OR spec-ARRAY['principal_id','actor_id','policy_id','version_id','draft_id','catalog_digest']<>'{}'::jsonb
        OR length(spec->>'principal_id') NOT BETWEEN 1 AND 128 OR length(spec->>'policy_id') NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'Invalid source authority' USING ERRCODE='23514'; END IF;
      SELECT jsonb_build_object(
        'principals',(SELECT coalesce(jsonb_agg(to_jsonb(p) ORDER BY p.id),'[]'::jsonb) FROM
          (SELECT id,subject,service_client_id,kind,active FROM gateway.principals
           WHERE id IN (spec->>'principal_id',spec->>'actor_id') ORDER BY id LIMIT 3) p),
        'grants',(SELECT coalesce(jsonb_agg(to_jsonb(g) ORDER BY g.principal_id,g.policy_id,g.tenant_id),'[]'::jsonb) FROM
          (SELECT principal_id,policy_id,tenant_id,roles,entitlements,active FROM gateway.principal_grants
           WHERE principal_id IN (spec->>'principal_id',spec->>'actor_id') AND policy_id=spec->>'policy_id'
           ORDER BY principal_id,policy_id,tenant_id LIMIT 2001) g),
        'policy',(SELECT jsonb_build_object('id',p.id,'revision',p.current_revision,'digest',r.content_digest,'content',r.content)
          FROM gateway.policies p JOIN gateway.policy_revisions r ON r.policy_id=p.id AND r.revision=p.current_revision
          WHERE p.id=spec->>'policy_id'),
        'version',(SELECT jsonb_build_object('id',v.id,'product_id',v.product_id,'digest',v.content_digest,
          'status',s.status,'updated_at',s.updated_at,'manifest',v.manifest,'binding_id',v.binding_id)
          FROM gateway.product_versions v JOIN gateway.version_states s ON s.version_id=v.id WHERE v.id=spec->>'version_id'),
        'draft',(SELECT jsonb_build_object('id',d.id,'product_id',d.product_id,'revision',d.revision,
          'digest',d.content_digest,'manifest',d.manifest,'head_revision',p.draft_revision,'head_id',p.latest_draft_id)
          FROM gateway.draft_revisions d JOIN gateway.products p ON p.id=d.product_id WHERE d.id=spec->>'draft_id'),
        'catalog_digest',spec->'catalog_digest') INTO result;
      IF octet_length(result::text)>65536 OR jsonb_array_length(result->'grants')>2000 THEN
        RAISE EXCEPTION 'Source authority exceeds bound' USING ERRCODE='23514'; END IF;
      RETURN result;
    END""", stable=True)
    function("source_global_limit", "", "integer", """
    BEGIN
      RETURN (SELECT least(s.max_active_operations,coalesce((SELECT min((descriptor->>'max_global_active_jobs')::integer)
        FROM gateway.source_profiles WHERE provider='bigquery'),s.max_active_operations))
        FROM gateway.source_runtime_settings s WHERE singleton);
    END""", stable=True)
    function("source_check_epoch", "expected_epoch text", "boolean", """
    BEGIN
      PERFORM gateway.source_require_durability();
      IF length(expected_epoch) NOT BETWEEN 1 AND 128 OR NOT EXISTS(SELECT 1 FROM gateway.source_runtime_settings
        WHERE singleton AND deployment_epoch=expected_epoch) THEN
        RAISE EXCEPTION 'Source deployment epoch mismatch' USING ERRCODE='42501'; END IF;
      RETURN true;
    END""")
    function("source_role_allowed", "sid uuid, required_kind text DEFAULT NULL", "boolean", """
    BEGIN
      RETURN EXISTS(SELECT 1 FROM gateway.source_sessions s JOIN gateway.source_role_mappings m
        ON (m.source_id,m.revision,m.context)=(s.source_id,s.source_revision,s.context)
        WHERE s.id=sid AND m.login_name=session_user AND (required_kind IS NULL OR m.role_kind=required_kind))
        OR (required_kind IS NULL AND EXISTS(SELECT 1 FROM gateway.source_runtime_settings WHERE app_role=session_user));
    END""", stable=True)
    function("source_lock_session", "sid uuid", "gateway.source_sessions", """
    DECLARE value gateway.source_sessions%ROWTYPE;
    BEGIN
      PERFORM 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Source capacity unavailable' USING ERRCODE='23514'; END IF;
      SELECT * INTO STRICT value FROM gateway.source_sessions WHERE id=sid;
      PERFORM 1 FROM gateway.source_pools WHERE capacity_key=value.capacity_key FOR UPDATE;
      PERFORM 1 FROM gateway.source_operation_groups WHERE id=value.group_id FOR UPDATE;
      SELECT * INTO STRICT value FROM gateway.source_sessions WHERE id=sid FOR UPDATE;
      RETURN value;
    END""")
    function("source_check_owner", "value gateway.source_sessions", "boolean", """
    BEGIN
      IF value.context='consumer' THEN
        RETURN EXISTS(SELECT 1 FROM gateway.execution_states st JOIN gateway.execution_leases l ON l.execution_id=st.execution_id
          JOIN gateway.executions e ON e.id=st.execution_id WHERE st.execution_id=value.owner_id AND st.state='running'
          AND l.generation=value.generation AND l.lease_token=value.lease_token AND l.expires_at>clock_timestamp()
          AND e.context='consumer' AND e.deadline_at>clock_timestamp());
      END IF;
      RETURN value.generation=1 AND EXISTS(SELECT 1 FROM gateway.fixture_attempts a JOIN gateway.fixture_attempt_states st
        ON st.attempt_id=a.id WHERE a.id=value.owner_id AND a.runner_token=value.lease_token AND st.state='running'
        AND a.deadline_at>clock_timestamp());
    END""", stable=True)
    function("source_check_context", "context jsonb, require_open boolean DEFAULT false", "gateway.source_sessions", """
    DECLARE value gateway.source_sessions%ROWTYPE; grp gateway.source_operation_groups%ROWTYPE;
    BEGIN
      IF jsonb_typeof(context) IS DISTINCT FROM 'object' OR octet_length(context::text)>8192
        OR jsonb_typeof(context->'generation') IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION 'Source context unavailable' USING ERRCODE='42501'; END IF;
      value:=gateway.source_lock_session((context->>'session_id')::uuid);
      IF NOT gateway.source_role_allowed(value.id,'runtime') THEN
        RAISE EXCEPTION 'Source context unavailable' USING ERRCODE='42501'; END IF;
      SELECT * INTO STRICT grp FROM gateway.source_operation_groups WHERE id=value.group_id;
      IF (context-ARRAY['group_id','session_id','generation','authority_stamp','context_digest','deadline_at']) IS DISTINCT FROM value.plan
        OR context->>'group_id' IS DISTINCT FROM value.group_id::text
        OR (context->>'generation')::integer IS DISTINCT FROM value.generation
        OR context->>'authority_stamp' IS DISTINCT FROM value.authority_stamp
        OR context->>'context_digest' IS DISTINCT FROM value.context_digest
        OR (context->>'deadline_at')::timestamptz IS DISTINCT FROM value.deadline_at
        OR value.deadline_at<=clock_timestamp() OR grp.deadline_at<=clock_timestamp()
        OR NOT gateway.source_check_owner(value) OR grp.settled_at IS NOT NULL
        OR (require_open AND (value.sealed OR grp.closed))
        OR gateway.source_current_authority(value.authority_spec) IS DISTINCT FROM value.authority_snapshot
        OR gateway.source_current_authority(grp.authority_spec) IS DISTINCT FROM grp.authority_snapshot
        OR NOT EXISTS(SELECT 1 FROM gateway.source_runtime_settings WHERE deployment_epoch=grp.deployment_epoch) THEN
        RAISE EXCEPTION 'Source authority expired or changed' USING ERRCODE='42501'; END IF;
      RETURN value;
    END""")
    function("source_check_attempt", "context jsonb", "jsonb", """
    DECLARE value gateway.source_sessions%ROWTYPE;
    BEGIN value:=gateway.source_check_context(context,false);
      RETURN jsonb_build_object('allowed',true,'deadline_at',value.deadline_at,'context_digest',value.context_digest);
    END""")


def _lifecycle_functions(configured):
    function("source_guard_group", "", "trigger", """
    DECLARE profile gateway.source_profiles%ROWTYPE; settings gateway.source_runtime_settings%ROWTYPE;
      snapshot jsonb; manifest jsonb; own_count bigint; total_count bigint;
      own_limit integer; total_limit integer; seconds integer; expected_request text;
    BEGIN
      PERFORM 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE;
      SELECT * INTO STRICT settings FROM gateway.source_runtime_settings WHERE singleton;
      SELECT * INTO STRICT profile FROM gateway.source_profiles WHERE
        (source_id,revision,context)=(NEW.source_id,NEW.source_revision,NEW.context);
      PERFORM 1 FROM gateway.source_pools WHERE capacity_key=profile.capacity_key FOR UPDATE;
      snapshot:=gateway.source_current_authority(NEW.authority_spec);
      manifest:=CASE WHEN NEW.context='consumer' THEN snapshot->'version'->'manifest' ELSE snapshot->'draft'->'manifest' END;
      expected_request:=CASE WHEN NEW.context='consumer' THEN (SELECT request_id FROM gateway.executions WHERE id=NEW.owner_id) ELSE NEW.owner_id END;
      IF NEW.authority_snapshot IS DISTINCT FROM snapshot OR NEW.capacity_key<>profile.capacity_key
        OR NEW.deployment_epoch<>settings.deployment_epoch OR NEW.owner_kind<>NEW.context
        OR NEW.closed OR NEW.settled_at IS NOT NULL OR NEW.admitted_count<>0 OR NEW.reserved_bytes<>0
        OR NEW.close_reason IS NOT NULL OR manifest IS NULL
        OR NEW.authority_spec->>'policy_id' IS DISTINCT FROM manifest->>'policy_id'
        OR NEW.authority_spec->>'principal_id' IS DISTINCT FROM NEW.actor_id
        OR NEW.authority_spec->>'actor_id' IS DISTINCT FROM NEW.actor_id
        OR (NEW.context='consumer' AND (NEW.authority_spec->'draft_id'<>'null'::jsonb OR NEW.authority_spec->'catalog_digest'<>'null'::jsonb))
        OR (NEW.context='fixture_test' AND (NEW.authority_spec->'version_id'<>'null'::jsonb
          OR NOT (NEW.authority_spec->>'catalog_digest' ~ '^[a-f0-9]{64}$') IS TRUE))
        OR NOT EXISTS(SELECT 1 FROM gateway.principals WHERE id=NEW.actor_id AND active)
        OR NOT EXISTS(SELECT 1 FROM gateway.audit_events WHERE id=NEW.allow_audit_id AND actor_id=NEW.actor_id
          AND decision='allow' AND action=CASE WHEN NEW.context='consumer' THEN 'execute' ELSE 'author' END
          AND policy_id=manifest->>'policy_id' AND product_id=manifest->>'product_id'
          AND version_id=coalesce(NEW.authority_spec->>'version_id',NEW.authority_spec->>'draft_id')
          AND correlation_id=expected_request AND policy_revision=(snapshot->'policy'->>'revision')::integer)
        OR (NEW.context='consumer' AND NOT EXISTS(SELECT 1 FROM gateway.executions e WHERE e.id=NEW.owner_id
          AND e.context='consumer' AND e.principal_id=NEW.actor_id AND e.version_id=NEW.authority_spec->>'version_id'
          AND e.policy_id=NEW.authority_spec->>'policy_id'
          AND NEW.deadline_at<=e.deadline_at))
        OR (NEW.context='fixture_test' AND (snapshot->'draft'->>'id' IS DISTINCT FROM snapshot->'draft'->>'head_id'
          OR snapshot->'draft'->>'revision' IS DISTINCT FROM snapshot->'draft'->>'head_revision')) THEN
        RAISE EXCEPTION 'Invalid source group authority' USING ERRCODE='23514'; END IF;
      IF (SELECT count(DISTINCT v) FROM jsonb_array_elements_text(NEW.roster) v)<>jsonb_array_length(NEW.roster)
        OR EXISTS(SELECT 1 FROM jsonb_array_elements_text(NEW.roster) v WHERE v !~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
        OR (NEW.context='consumer' AND NEW.roster<>jsonb_build_array(NEW.owner_id)) THEN
        RAISE EXCEPTION 'Invalid fixed source roster' USING ERRCODE='23514'; END IF;
      IF (profile.provider='domo' AND (NEW.max_total_bytes IS NOT NULL OR NEW.max_submissions<>jsonb_array_length(NEW.roster)))
        OR (profile.provider='bigquery' AND (NEW.max_total_bytes IS NULL
          OR NEW.max_total_bytes>(profile.descriptor->>'total_execution_bytes')::bigint*jsonb_array_length(NEW.roster)
          OR NEW.max_submissions>(profile.descriptor->>'max_jobs_per_execution')::integer*jsonb_array_length(NEW.roster))) THEN
        RAISE EXCEPTION 'Invalid source group budget' USING ERRCODE='23514'; END IF;
      own_limit:=least(coalesce((manifest->'limits'->>'per_principal_concurrency')::integer,2),
        coalesce((snapshot->'policy'->'content'->'rules'->'limits'->>'per_principal_concurrency')::integer,2));
      total_limit:=least(coalesce((manifest->'limits'->>'deployment_concurrency')::integer,10),
        coalesce((snapshot->'policy'->'content'->'rules'->'limits'->>'deployment_concurrency')::integer,10));
      SELECT count(*) FILTER(WHERE principal_id=NEW.actor_id),count(*) INTO own_count,total_count FROM gateway.held_source_capacity;
      IF own_count>=own_limit OR total_count>=total_limit OR (SELECT count(*) FROM gateway.source_operation_groups)>=settings.max_groups THEN
        RAISE EXCEPTION 'Source capacity exhausted' USING ERRCODE='54000'; END IF;
      seconds:=least(coalesce((manifest->'limits'->>'execution_timeout_seconds')::integer,120),
        coalesce((snapshot->'policy'->'content'->'rules'->'limits'->>'execution_timeout_seconds')::integer,120),120);
      NEW.created_at:=clock_timestamp();
      IF NEW.deadline_at<=NEW.created_at OR NEW.deadline_at>NEW.created_at+make_interval(secs=>seconds) THEN
        RAISE EXCEPTION 'Invalid source deadline' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END""")
    op.execute("CREATE TRIGGER source_group_insert BEFORE INSERT ON gateway.source_operation_groups FOR EACH ROW EXECUTE FUNCTION gateway.source_guard_group()")
    function("source_guard_session", "", "trigger", """
    DECLARE grp gateway.source_operation_groups%ROWTYPE; profile gateway.source_profiles%ROWTYPE;
      binding jsonb; actual_token text; actual_generation integer; actual_deadline timestamptz; expected_principal text; owner_pins jsonb;
    BEGIN
      PERFORM 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE;
      SELECT * INTO STRICT grp FROM gateway.source_operation_groups WHERE id=NEW.group_id;
      PERFORM 1 FROM gateway.source_pools WHERE capacity_key=grp.capacity_key FOR UPDATE;
      SELECT * INTO STRICT grp FROM gateway.source_operation_groups WHERE id=NEW.group_id FOR UPDATE;
      SELECT * INTO STRICT profile FROM gateway.source_profiles WHERE
        (source_id,revision,context)=(grp.source_id,grp.source_revision,grp.context);
      IF NOT (NEW.plan ?& ARRAY['provider','source_id','source_revision','context','capacity_key','definition_digest',
        'request_digest','prepared_digest','scope_digest','decision_digest','binding_digest','model_bundle_digest',
        'catalog_digest','max_submissions','max_bytes_per_submission','max_total_bytes'])
        OR NEW.plan-ARRAY['provider','source_id','source_revision','context','capacity_key','definition_digest',
        'request_digest','prepared_digest','scope_digest','decision_digest','binding_digest','model_bundle_digest',
        'catalog_digest','max_submissions','max_bytes_per_submission','max_total_bytes']<>'{}'::jsonb
        OR EXISTS(SELECT 1 FROM jsonb_each_text(NEW.plan) e WHERE e.key IN ('definition_digest','request_digest',
          'prepared_digest','scope_digest','decision_digest','binding_digest','model_bundle_digest') AND (e.value IS NULL OR e.value !~ '^[a-f0-9]{64}$'))
        OR NOT (jsonb_typeof(NEW.plan->'max_submissions')='number' AND NEW.plan->>'max_submissions' ~ '^[1-8]$') IS TRUE
        OR (profile.provider='domo' AND (NEW.plan->'max_submissions'<>'1'::jsonb
          OR NEW.plan->'max_bytes_per_submission'<>'null'::jsonb OR NEW.plan->'max_total_bytes'<>'null'::jsonb))
        OR (profile.provider='bigquery' AND NOT (
          jsonb_typeof(NEW.plan->'max_bytes_per_submission')='number' AND NEW.plan->>'max_bytes_per_submission' ~ '^[1-9][0-9]{0,15}$'
          AND jsonb_typeof(NEW.plan->'max_total_bytes')='number' AND NEW.plan->>'max_total_bytes' ~ '^[1-9][0-9]{0,15}$'
          AND (NEW.plan->>'max_submissions')::integer<=(profile.descriptor->>'max_jobs_per_execution')::integer
          AND (NEW.plan->>'max_bytes_per_submission')::bigint<=(profile.descriptor->>'per_job_maximum_bytes_billed')::bigint
          AND (NEW.plan->>'max_total_bytes')::bigint<=(profile.descriptor->>'total_execution_bytes')::bigint
          AND (NEW.plan->>'max_total_bytes')::bigint>=(NEW.plan->>'max_bytes_per_submission')::bigint) IS TRUE) THEN
        RAISE EXCEPTION 'Invalid bounded source plan' USING ERRCODE='23514'; END IF;
      IF grp.closed OR grp.settled_at IS NOT NULL OR grp.deadline_at<=clock_timestamp()
        OR NOT (grp.roster ? NEW.case_id) OR NEW.deadline_at>grp.deadline_at OR NEW.deadline_at<=clock_timestamp()
        OR (NEW.source_id,NEW.source_revision,NEW.context,NEW.capacity_key) IS DISTINCT FROM
          (grp.source_id,grp.source_revision,grp.context,grp.capacity_key)
        OR NEW.sealed OR NEW.seal_reason IS NOT NULL
        OR NEW.plan->>'source_id' IS DISTINCT FROM NEW.source_id
        OR (NEW.plan->>'source_revision')::integer IS DISTINCT FROM NEW.source_revision
        OR NEW.plan->>'context' IS DISTINCT FROM NEW.context OR NEW.plan->>'provider' IS DISTINCT FROM profile.provider
        OR NEW.plan->>'definition_digest' IS DISTINCT FROM profile.descriptor_digest
        OR NEW.plan->>'capacity_key' IS DISTINCT FROM profile.capacity_key
        OR NEW.authority_snapshot IS DISTINCT FROM gateway.source_current_authority(NEW.authority_spec)
        OR NEW.authority_stamp IS DISTINCT FROM encode(sha256(convert_to(NEW.authority_snapshot::text,'UTF8')),'hex')
        OR NEW.provider_nonce !~ '^[A-Za-z0-9_-]{43}$'
        OR (SELECT count(*) FROM gateway.source_sessions WHERE group_id=grp.id)>=8
        OR (SELECT count(*) FROM gateway.source_sessions)>=8000 THEN
        RAISE EXCEPTION 'Invalid source session authority' USING ERRCODE='23514'; END IF;
      IF NEW.context='consumer' THEN
        SELECT b.content,l.lease_token,l.generation,e.deadline_at,e.principal_id
          INTO STRICT binding,actual_token,actual_generation,actual_deadline,expected_principal
          FROM gateway.executions e JOIN gateway.execution_leases l ON l.execution_id=e.id
          JOIN gateway.execution_states st ON st.execution_id=e.id JOIN gateway.product_versions v ON v.id=e.version_id
          JOIN gateway.binding_revisions b ON b.id=v.binding_id
          WHERE e.id=NEW.owner_id AND e.id=grp.owner_id AND e.context='consumer' AND st.state='running' AND l.expires_at>clock_timestamp();
      ELSE
        SELECT b.content,a.runner_token,1,a.deadline_at,a.fixed_principal_id
          INTO STRICT binding,actual_token,actual_generation,actual_deadline,expected_principal
          FROM gateway.fixture_attempts a JOIN gateway.fixture_attempt_states st ON st.attempt_id=a.id
          JOIN gateway.binding_revisions b ON b.id=a.fixture_binding_id
          WHERE a.id=NEW.owner_id AND a.actor_id=grp.actor_id AND a.fixture_id=NEW.case_id AND st.state='running'
            AND a.draft_id=grp.authority_spec->>'draft_id' AND a.catalog_digest=grp.authority_spec->>'catalog_digest';
      END IF;
      IF NEW.context='consumer' THEN
        SELECT jsonb_build_object('authority_spec',jsonb_build_object('principal_id',e.principal_id,'actor_id',e.principal_id,
          'policy_id',e.policy_id,'version_id',e.version_id,'draft_id',NULL,'catalog_digest',NULL),
          'product_id',e.product_id,'version_id',e.version_id,'request_id',e.request_id,'request_digest',e.canonical_hash,
          'scope_digest',a.scope_digest,'decision_digest',a.full_decision_digest,'binding_digest',b.content_digest,'model_bundle_digest',b.model_bundle_digest)
          INTO STRICT owner_pins FROM gateway.executions e JOIN gateway.execution_authorizations a ON a.execution_id=e.id
          JOIN gateway.product_versions v ON v.id=e.version_id JOIN gateway.binding_revisions b ON b.id=v.binding_id WHERE e.id=NEW.owner_id;
      ELSE
        SELECT jsonb_build_object('authority_spec',jsonb_build_object('principal_id',a.fixed_principal_id,'actor_id',a.actor_id,
          'policy_id',a.policy_id,'version_id',NULL,'draft_id',a.draft_id,'catalog_digest',a.catalog_digest),
          'product_id',a.product_id,'version_id',a.draft_id,'request_id',a.request_id,'request_digest',a.canonical_hash,
          'binding_digest',a.fixture_binding_digest,'model_bundle_digest',b.model_bundle_digest)
          INTO STRICT owner_pins FROM gateway.fixture_attempts a JOIN gateway.binding_revisions b ON b.id=a.fixture_binding_id WHERE a.id=NEW.owner_id;
      END IF;
      IF NEW.lease_token<>actual_token OR NEW.generation<>actual_generation OR NEW.deadline_at>actual_deadline
        OR NOT EXISTS(SELECT 1 FROM gateway.principals WHERE id=expected_principal AND active)
        OR NEW.authority_spec IS DISTINCT FROM owner_pins->'authority_spec'
        OR NEW.plan->>'request_digest' IS DISTINCT FROM owner_pins->>'request_digest'
        OR NEW.plan->>'binding_digest' IS DISTINCT FROM owner_pins->>'binding_digest'
        OR NEW.plan->>'model_bundle_digest' IS DISTINCT FROM owner_pins->>'model_bundle_digest'
        OR NEW.plan->'catalog_digest' IS DISTINCT FROM owner_pins->'authority_spec'->'catalog_digest'
        OR (NEW.context='consumer' AND (NEW.plan->>'scope_digest' IS DISTINCT FROM owner_pins->>'scope_digest'
          OR NEW.plan->>'decision_digest' IS DISTINCT FROM owner_pins->>'decision_digest'))
        OR NEW.authority_spec->>'principal_id' IS DISTINCT FROM expected_principal
        OR binding->>'source_id' IS DISTINCT FROM NEW.source_id
        OR (binding->>'source_revision')::integer IS DISTINCT FROM NEW.source_revision
        OR binding->>'source_digest' IS DISTINCT FROM profile.descriptor_digest
        OR binding->>'context' IS DISTINCT FROM NEW.context
        OR NOT EXISTS(SELECT 1 FROM gateway.audit_events WHERE id=NEW.allow_audit_id AND actor_id=expected_principal
          AND decision='allow' AND action='execute' AND policy_id=NEW.authority_spec->>'policy_id'
          AND product_id=owner_pins->>'product_id' AND version_id=owner_pins->>'version_id'
          AND correlation_id=owner_pins->>'request_id' AND policy_revision=(NEW.authority_snapshot->'policy'->>'revision')::integer) THEN
        RAISE EXCEPTION 'Source session owner mismatch' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END""")

    op.execute("CREATE TRIGGER source_session_insert BEFORE INSERT ON gateway.source_sessions FOR EACH ROW EXECUTE FUNCTION gateway.source_guard_session()")
    function("source_guard_owner_completion", "", "trigger", """
    DECLARE sid uuid; profile_revision text; status jsonb; deadline timestamptz;
    BEGIN
      IF TG_TABLE_NAME='fixture_attempt_states' THEN
        IF NOT NEW.source_completed THEN RETURN NEW; END IF;
        SELECT b.adapter_revision INTO profile_revision FROM gateway.fixture_attempts a
          JOIN gateway.binding_revisions b ON b.id=a.fixture_binding_id WHERE a.id=NEW.attempt_id;
        SELECT id,deadline_at INTO sid,deadline FROM gateway.source_sessions WHERE context='fixture_test' AND owner_id=NEW.attempt_id;
      ELSE
        IF NEW.state<>'succeeded' THEN RETURN NEW; END IF;
        SELECT b.adapter_revision INTO profile_revision FROM gateway.executions e JOIN gateway.product_versions v ON v.id=e.version_id
          JOIN gateway.binding_revisions b ON b.id=v.binding_id WHERE e.id=NEW.execution_id;
        SELECT id,deadline_at INTO sid,deadline FROM gateway.source_sessions WHERE context='consumer' AND owner_id=NEW.execution_id;
      END IF;
      IF profile_revision NOT IN ('cube-bigquery-v1','domo-named-v2') THEN RETURN NEW; END IF;
      PERFORM gateway.source_require_durability();
      IF sid IS NULL THEN RAISE EXCEPTION 'Source completion requires a retained session' USING ERRCODE='23514'; END IF;
      status:=gateway.source_status(sid);
      IF NOT (status->>'sealed')::boolean OR NOT (status->>'terminal')::boolean
        OR (NEW.state='succeeded' AND deadline<=clock_timestamp())
        OR (TG_TABLE_NAME='execution_states' AND NOT (status->>'group_closed')::boolean) THEN
        RAISE EXCEPTION 'Source completion is not durably proven' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END""")
    op.execute("CREATE TRIGGER source_fixture_completion BEFORE INSERT OR UPDATE ON gateway.fixture_attempt_states FOR EACH ROW EXECUTE FUNCTION gateway.source_guard_owner_completion()")
    op.execute("CREATE TRIGGER source_execution_completion BEFORE INSERT OR UPDATE ON gateway.execution_states FOR EACH ROW EXECUTE FUNCTION gateway.source_guard_owner_completion()")
    function("source_status", "sid uuid", "jsonb", """
    DECLARE value gateway.source_sessions%ROWTYPE; grp gateway.source_operation_groups%ROWTYPE; admitted integer; unresolved integer;
    BEGIN
      SELECT * INTO STRICT value FROM gateway.source_sessions WHERE id=sid;
      IF NOT gateway.source_role_allowed(sid) THEN RAISE EXCEPTION 'Source status unavailable' USING ERRCODE='42501'; END IF;
      SELECT * INTO STRICT grp FROM gateway.source_operation_groups WHERE id=value.group_id;
      SELECT count(*),count(*) FILTER(WHERE NOT terminal) INTO admitted,unresolved FROM (
        SELECT state='done' terminal FROM gateway.bigquery_source_jobs WHERE session_id=sid
        UNION ALL SELECT state<>'intent' FROM gateway.domo_source_sends WHERE session_id=sid) operations;
      RETURN jsonb_build_object('session_id',sid,'sealed',value.sealed,'terminal',unresolved=0,
        'group_closed',grp.closed,'group_settled',grp.settled_at IS NOT NULL,'admitted_count',admitted,'unresolved_count',unresolved);
    END""")
    function("source_settle_group", "gid uuid", "boolean", """
    DECLARE grp gateway.source_operation_groups%ROWTYPE;
    BEGIN
      PERFORM 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE;
      SELECT * INTO STRICT grp FROM gateway.source_operation_groups WHERE id=gid;
      IF NOT EXISTS(SELECT 1 FROM gateway.source_runtime_settings WHERE app_role=session_user)
        AND NOT EXISTS(SELECT 1 FROM gateway.source_role_mappings WHERE login_name=session_user
          AND (source_id,revision,context)=(grp.source_id,grp.source_revision,grp.context)) THEN
        RAISE EXCEPTION 'Source group unavailable' USING ERRCODE='42501'; END IF;
      PERFORM 1 FROM gateway.source_pools WHERE capacity_key=grp.capacity_key FOR UPDATE;
      SELECT * INTO STRICT grp FROM gateway.source_operation_groups WHERE id=gid FOR UPDATE;
      PERFORM 1 FROM gateway.source_sessions WHERE group_id=gid ORDER BY id FOR UPDATE;
      IF grp.settled_at IS NOT NULL THEN RETURN true; END IF;
      IF NOT grp.closed OR EXISTS(SELECT 1 FROM gateway.source_sessions WHERE group_id=gid AND NOT sealed)
        OR EXISTS(SELECT 1 FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.group_id=gid AND j.state<>'done')
        OR EXISTS(SELECT 1 FROM gateway.domo_source_sends d JOIN gateway.source_sessions s ON s.id=d.session_id WHERE s.group_id=gid AND d.state='intent') THEN RETURN false; END IF;
      UPDATE gateway.source_operation_groups SET settled_at=clock_timestamp() WHERE id=gid;
      RETURN true;
    END""")
    function("source_seal_session", "sid uuid, reason text", "jsonb", """
    DECLARE value gateway.source_sessions%ROWTYPE;
    BEGIN
      value:=gateway.source_lock_session(sid);
      IF NOT EXISTS(SELECT 1 FROM gateway.source_runtime_settings WHERE app_role=session_user)
        OR reason IS NULL OR reason NOT IN ('completed','cancelled','deadline','authorization','runtime_error','failure','aborted') THEN
        RAISE EXCEPTION 'Source seal unavailable' USING ERRCODE='42501'; END IF;
      UPDATE gateway.source_sessions SET sealed=true,seal_reason=coalesce(seal_reason,reason) WHERE id=sid;
      RETURN gateway.source_status(sid);
    END""")
    function("source_seal_attempt", "context jsonb, reason text", "jsonb", """
    DECLARE value gateway.source_sessions%ROWTYPE; same_owner boolean;
    BEGIN
      IF jsonb_typeof(context) IS DISTINCT FROM 'object' OR octet_length(context::text)>8192
        OR jsonb_typeof(context->'generation') IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION 'Source seal context unavailable' USING ERRCODE='42501'; END IF;
      value:=gateway.source_lock_session((context->>'session_id')::uuid);
      IF value.context='consumer' THEN
        SELECT EXISTS(SELECT 1 FROM gateway.execution_leases l JOIN gateway.executions e ON e.id=l.execution_id
          WHERE l.execution_id=value.owner_id AND e.context='consumer'
          AND l.generation=value.generation AND l.lease_token=value.lease_token) INTO same_owner;
      ELSE
        SELECT value.generation=1 AND EXISTS(SELECT 1 FROM gateway.fixture_attempts a
          WHERE a.id=value.owner_id AND a.runner_token=value.lease_token) INTO same_owner;
      END IF;
      IF NOT gateway.source_role_allowed(value.id) OR NOT same_owner
        OR (context-ARRAY['group_id','session_id','generation','authority_stamp','context_digest','deadline_at']) IS DISTINCT FROM value.plan
        OR context->>'group_id' IS DISTINCT FROM value.group_id::text
        OR (context->>'generation')::integer IS DISTINCT FROM value.generation
        OR context->>'authority_stamp' IS DISTINCT FROM value.authority_stamp
        OR context->>'context_digest' IS DISTINCT FROM value.context_digest
        OR (context->>'deadline_at')::timestamptz IS DISTINCT FROM value.deadline_at
        OR reason IS NULL OR reason NOT IN ('completed','cancelled','deadline','authorization','runtime_error','failure','aborted') THEN
        RAISE EXCEPTION 'Source seal fence unavailable' USING ERRCODE='42501'; END IF;
      UPDATE gateway.source_sessions SET sealed=true,seal_reason=coalesce(seal_reason,reason) WHERE id=value.id;
      RETURN gateway.source_status(value.id);
    END""")
    function("source_close_group", "gid uuid, reason text", "jsonb", """
    DECLARE grp gateway.source_operation_groups%ROWTYPE;
    BEGIN
      PERFORM 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE;
      SELECT * INTO STRICT grp FROM gateway.source_operation_groups WHERE id=gid;
      IF NOT EXISTS(SELECT 1 FROM gateway.source_runtime_settings WHERE app_role=session_user)
        AND NOT (reason='deadline' AND grp.deadline_at<=clock_timestamp() AND EXISTS(
          SELECT 1 FROM gateway.source_role_mappings WHERE login_name=session_user AND role_kind='observer'
          AND (source_id,revision,context)=(grp.source_id,grp.source_revision,grp.context))) THEN
        RAISE EXCEPTION 'Source group closure unavailable' USING ERRCODE='42501'; END IF;
      IF reason IS NULL OR reason NOT IN ('completed','cancelled','deadline','authorization','runtime_error','failure','aborted') THEN
        RAISE EXCEPTION 'Invalid source closure' USING ERRCODE='23514'; END IF;
      PERFORM 1 FROM gateway.source_pools WHERE capacity_key=grp.capacity_key FOR UPDATE;
      PERFORM 1 FROM gateway.source_operation_groups WHERE id=gid FOR UPDATE;
      PERFORM 1 FROM gateway.source_sessions WHERE group_id=gid ORDER BY id FOR UPDATE;
      UPDATE gateway.source_operation_groups SET closed=true,close_reason=coalesce(close_reason,reason) WHERE id=gid;
      UPDATE gateway.source_sessions SET sealed=true,seal_reason=coalesce(seal_reason,reason) WHERE group_id=gid;
      RETURN jsonb_build_object('closed',true,'settled',gateway.source_settle_group(gid));
    END""")
    function("source_reclaim_session", "sid uuid, new_generation integer, new_token text, new_deadline timestamptz", "jsonb", """
    DECLARE value gateway.source_sessions%ROWTYPE;
    BEGIN
      IF NOT EXISTS(SELECT 1 FROM gateway.source_runtime_settings WHERE app_role=session_user) THEN
        RAISE EXCEPTION 'Source reclaim unavailable' USING ERRCODE='42501'; END IF;
      value:=gateway.source_lock_session(sid);
      IF value.sealed OR new_generation<value.generation OR new_deadline>value.deadline_at
        OR new_deadline<=clock_timestamp() OR gateway.source_current_authority(value.authority_spec) IS DISTINCT FROM value.authority_snapshot THEN
        RAISE EXCEPTION 'Source reclaim unavailable' USING ERRCODE='42501'; END IF;
      value.generation:=new_generation; value.lease_token:=new_token; value.deadline_at:=new_deadline;
      IF NOT gateway.source_check_owner(value) THEN RAISE EXCEPTION 'Source reclaim fence mismatch' USING ERRCODE='42501'; END IF;
      UPDATE gateway.source_sessions SET generation=new_generation,lease_token=new_token,deadline_at=new_deadline WHERE id=sid;
      RETURN gateway.source_status(sid);
    END""")
    function("source_expire_groups", "max_groups integer", "integer", """
    DECLARE grp record; count integer:=0;
    BEGIN
      IF max_groups IS NULL OR max_groups NOT BETWEEN 1 AND 32 THEN RAISE EXCEPTION 'Invalid source maintenance bound' USING ERRCODE='23514'; END IF;
      FOR grp IN SELECT g.id FROM gateway.source_operation_groups g WHERE g.settled_at IS NULL AND NOT g.closed AND g.deadline_at<=clock_timestamp()
        AND (EXISTS(SELECT 1 FROM gateway.source_runtime_settings WHERE app_role=session_user)
          OR EXISTS(SELECT 1 FROM gateway.source_role_mappings m WHERE m.login_name=session_user AND m.role_kind='observer'
            AND (m.source_id,m.revision,m.context)=(g.source_id,g.source_revision,g.context))) ORDER BY g.deadline_at,g.id LIMIT max_groups LOOP
        PERFORM gateway.source_close_group(grp.id,'deadline'); count:=count+1;
      END LOOP;
      RETURN count;
    END""")


def _provider_functions():
    function("source_job_projection", "jid text", "jsonb", """
    DECLARE result jsonb;
    BEGIN
      SELECT jsonb_build_object('job_id',j.job_id,'session_id',j.session_id,'group_id',s.group_id,
        'semantic_key',j.semantic_key,'project_id',j.project_id,'location',j.location,'principal',j.principal,
        'intent',j.intent,'intent_digest',j.intent_digest,'state',j.state,'terminal_error',j.terminal_error)
        INTO result FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE j.job_id=jid;
      RETURN result;
    END""", stable=True)
    function("source_bq_lookup", "context jsonb, semantic_key text", "jsonb", """
    DECLARE value gateway.source_sessions%ROWTYPE; jid text;
    BEGIN
      value:=gateway.source_check_context(context,false);
      IF semantic_key !~ '^[a-f0-9]{64}$' OR value.plan->>'provider'<>'bigquery' THEN
        RAISE EXCEPTION 'Invalid source job identity' USING ERRCODE='23514'; END IF;
      SELECT j.job_id INTO jid FROM gateway.bigquery_source_jobs j WHERE j.session_id=value.id AND j.semantic_key=source_bq_lookup.semantic_key;
      RETURN gateway.source_job_projection(jid);
    END""")
    function("source_bq_admit", "context jsonb, semantic_key text, intent jsonb", "jsonb", """
    DECLARE value gateway.source_sessions%ROWTYPE; grp gateway.source_operation_groups%ROWTYPE;
      profile gateway.source_profiles%ROWTYPE; pool gateway.source_pools%ROWTYPE; settings gateway.source_runtime_settings%ROWTYPE;
      previous gateway.bigquery_source_jobs%ROWTYPE; jid text; bytes bigint; active_total bigint; active_pool bigint;
      used_count bigint; used_bytes bigint; safe_identity jsonb;
    BEGIN
      value:=gateway.source_check_context(context,true);
      SELECT * INTO STRICT grp FROM gateway.source_operation_groups WHERE id=value.group_id;
      SELECT * INTO STRICT profile FROM gateway.source_profiles WHERE (source_id,revision,source_profiles.context)=(value.source_id,value.source_revision,value.context);
      SELECT * INTO STRICT pool FROM gateway.source_pools WHERE capacity_key=value.capacity_key;
      SELECT * INTO STRICT settings FROM gateway.source_runtime_settings WHERE singleton;
      IF profile.provider<>'bigquery' OR semantic_key !~ '^[a-f0-9]{64}$'
        OR jsonb_typeof(intent) IS DISTINCT FROM 'object' OR octet_length(intent::text)>4096
        OR NOT (intent ?& ARRAY['query_digest','parameters_digest','parameter_types_digest','configuration_digest','maximum_bytes_billed','job_timeout_ms'])
        OR intent-ARRAY['query_digest','parameters_digest','parameter_types_digest','configuration_digest','maximum_bytes_billed','job_timeout_ms']<>'{}'::jsonb
        OR EXISTS(SELECT 1 FROM jsonb_each_text(intent) e WHERE e.key LIKE '%digest' AND (e.value IS NULL OR e.value !~ '^[a-f0-9]{64}$'))
        OR NOT (jsonb_typeof(intent->'maximum_bytes_billed')='string' AND intent->>'maximum_bytes_billed' ~ '^[1-9][0-9]{0,15}$'
          AND jsonb_typeof(intent->'job_timeout_ms')='number' AND intent->>'job_timeout_ms' ~ '^[1-9][0-9]{0,5}$') IS TRUE THEN
        RAISE EXCEPTION 'Invalid source job intent' USING ERRCODE='23514'; END IF;
      bytes:=(intent->>'maximum_bytes_billed')::bigint;
      IF bytes<>(value.plan->>'max_bytes_per_submission')::bigint THEN
        RAISE EXCEPTION 'Source job byte limit mismatch' USING ERRCODE='23514'; END IF;
      SELECT * INTO previous FROM gateway.bigquery_source_jobs j WHERE j.session_id=value.id AND j.semantic_key=source_bq_admit.semantic_key FOR UPDATE;
      IF FOUND THEN
        IF previous.intent-ARRAY['job_timeout_ms','configuration_digest'] IS DISTINCT FROM intent-ARRAY['job_timeout_ms','configuration_digest'] THEN
          RAISE EXCEPTION 'Source job intent changed' USING ERRCODE='23514'; END IF;
        RETURN jsonb_build_object('submit_once',false,'job',gateway.source_job_projection(previous.job_id));
      END IF;
      IF (intent->>'job_timeout_ms')::integer>least(120000,(profile.descriptor->>'job_timeout_ms')::integer,
        floor(extract(epoch FROM least(value.deadline_at,grp.deadline_at)-clock_timestamp())*1000)::integer) THEN
        RAISE EXCEPTION 'Source job timeout exceeds authority' USING ERRCODE='23514'; END IF;
      SELECT count(*),coalesce(sum((j.intent->>'maximum_bytes_billed')::bigint),0) INTO used_count,used_bytes FROM gateway.bigquery_source_jobs j WHERE j.session_id=value.id;
      SELECT count(*),count(*) FILTER(WHERE ops.capacity_key=value.capacity_key) INTO active_total,active_pool FROM (
        SELECT s.capacity_key FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE j.state<>'done'
        UNION ALL SELECT s.capacity_key FROM gateway.domo_source_sends d JOIN gateway.source_sessions s ON s.id=d.session_id WHERE d.state='intent') ops;
      IF used_count>=(value.plan->>'max_submissions')::integer OR used_bytes+bytes>(value.plan->>'max_total_bytes')::bigint
        OR grp.admitted_count>=grp.max_submissions OR grp.reserved_bytes+bytes>grp.max_total_bytes
        OR active_total>=gateway.source_global_limit()
        OR active_pool>=pool.max_active OR pool.dispatches>=pool.max_dispatches
        OR (SELECT count(*) FROM gateway.bigquery_source_jobs)+(SELECT count(*) FROM gateway.domo_source_sends)>=settings.max_intents THEN
        RAISE EXCEPTION 'Source operation budget exhausted' USING ERRCODE='54000'; END IF;
      jid:='sp_'||encode(sha256(convert_to(grp.deployment_epoch||':'||value.provider_nonce||':'||semantic_key,'UTF8')),'hex');
      safe_identity:=jsonb_build_object('job_id',jid,'session_id',value.id,'semantic_key',semantic_key,
        'project_id',profile.descriptor->>'billing_project','location',profile.descriptor->>'location',
        'principal',profile.descriptor->'auth'->>'expected_principal','intent',intent);
      INSERT INTO gateway.bigquery_source_jobs(job_id,session_id,semantic_key,project_id,location,principal,intent,intent_digest)
        VALUES(jid,value.id,semantic_key,profile.descriptor->>'billing_project',profile.descriptor->>'location',
          profile.descriptor->'auth'->>'expected_principal',intent,encode(sha256(convert_to(safe_identity::text,'UTF8')),'hex'));
      UPDATE gateway.source_operation_groups SET admitted_count=admitted_count+1,reserved_bytes=reserved_bytes+bytes WHERE id=grp.id;
      UPDATE gateway.source_pools SET dispatches=dispatches+1 WHERE capacity_key=value.capacity_key;
      RETURN jsonb_build_object('submit_once',true,'job',gateway.source_job_projection(jid));
    END""")
    function("source_bq_check_data", "context jsonb, job_id text", "jsonb", """
    DECLARE value gateway.source_sessions%ROWTYPE;
    BEGIN
      value:=gateway.source_check_context(context,false);
      IF NOT EXISTS(SELECT 1 FROM gateway.bigquery_source_jobs j WHERE j.job_id=source_bq_check_data.job_id AND j.session_id=value.id) THEN
        RAISE EXCEPTION 'Source job unavailable' USING ERRCODE='42501'; END IF;
      RETURN gateway.source_job_projection(job_id);
    END""")
    function("source_bq_observe", "job_id text, evidence jsonb", "jsonb", """
    DECLARE job gateway.bigquery_source_jobs%ROWTYPE; value gateway.source_sessions%ROWTYPE;
    BEGIN
      SELECT * INTO STRICT job FROM gateway.bigquery_source_jobs j WHERE j.job_id=source_bq_observe.job_id;
      value:=gateway.source_lock_session(job.session_id);
      IF NOT gateway.source_role_allowed(value.id) OR value.plan->>'provider'<>'bigquery' THEN
        RAISE EXCEPTION 'Source observation unavailable' USING ERRCODE='42501'; END IF;
      SELECT * INTO STRICT job FROM gateway.bigquery_source_jobs j WHERE j.job_id=source_bq_observe.job_id FOR UPDATE;
      IF jsonb_typeof(evidence) IS DISTINCT FROM 'object' OR octet_length(evidence::text)>1024
        OR NOT (evidence ?& ARRAY['intent_digest','state','terminal_error'])
        OR evidence-ARRAY['intent_digest','state','terminal_error']<>'{}'::jsonb
        OR evidence->>'intent_digest' IS DISTINCT FROM job.intent_digest
        OR NOT (jsonb_typeof(evidence->'state')='string' AND evidence->>'state' IN ('pending','running','done')) IS TRUE
        OR (evidence->>'state'='done' AND jsonb_typeof(evidence->'terminal_error') IS DISTINCT FROM 'boolean')
        OR (evidence->>'state'<>'done' AND evidence->'terminal_error' IS DISTINCT FROM 'null'::jsonb) THEN
        RAISE EXCEPTION 'Invalid exact source observation' USING ERRCODE='23514'; END IF;
      IF job.state='done' THEN
        IF evidence->>'state'<>'done' OR (evidence->>'terminal_error')::boolean IS DISTINCT FROM job.terminal_error THEN
          RAISE EXCEPTION 'Source terminal evidence is immutable' USING ERRCODE='23514'; END IF;
      ELSE
        UPDATE gateway.bigquery_source_jobs j SET
          state=CASE WHEN j.state='running' AND evidence->>'state'='pending' THEN 'running' ELSE evidence->>'state' END,
          terminal_error=CASE WHEN evidence->>'state'='done' THEN (evidence->>'terminal_error')::boolean ELSE NULL END,
          terminal_at=CASE WHEN evidence->>'state'='done' THEN clock_timestamp() ELSE NULL END,
          observation_login=NULL,observation_token_hash=NULL,observation_until=NULL
          WHERE j.job_id=source_bq_observe.job_id;
      END IF;
      PERFORM gateway.source_settle_group(value.group_id);
      RETURN gateway.source_status(value.id);
    END""")
    _observer_functions()
    _domo_functions()


def _observer_functions():
    function("source_bq_reconcile_claim", "max_jobs integer", "jsonb", """
    DECLARE candidate record; value gateway.source_sessions%ROWTYPE; job gateway.bigquery_source_jobs%ROWTYPE;
      token text; output jsonb:='[]'::jsonb; descriptor jsonb;
    BEGIN
      IF max_jobs IS NULL OR max_jobs NOT BETWEEN 1 AND 8 THEN RAISE EXCEPTION 'Invalid source observation bound' USING ERRCODE='23514'; END IF;
      PERFORM 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE;
      FOR candidate IN SELECT j.job_id FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id
        JOIN gateway.source_role_mappings m ON (m.source_id,m.revision,m.context)=(s.source_id,s.source_revision,s.context)
        WHERE m.login_name=session_user AND m.role_kind='observer' AND j.state<>'done'
          AND j.next_observation_at<=clock_timestamp() AND (j.observation_until IS NULL OR j.observation_until<=clock_timestamp())
        ORDER BY j.next_observation_at,j.job_id LIMIT max_jobs LOOP
        SELECT * INTO STRICT job FROM gateway.bigquery_source_jobs WHERE job_id=candidate.job_id;
        value:=gateway.source_lock_session(job.session_id);
        SELECT * INTO STRICT job FROM gateway.bigquery_source_jobs WHERE job_id=candidate.job_id FOR UPDATE;
        token:=gen_random_uuid()::text||gen_random_uuid()::text;
        UPDATE gateway.bigquery_source_jobs SET observation_login=session_user,
          observation_token_hash=encode(sha256(convert_to(token,'UTF8')),'hex'),observation_until=clock_timestamp()+interval '10 seconds',
          observation_attempts=least(observation_attempts+1,1000000),
          next_observation_at=clock_timestamp()+make_interval(secs=>least(60,power(2,least(observation_attempts,6))::integer))
          WHERE job_id=candidate.job_id;
        SELECT p.descriptor INTO STRICT descriptor FROM gateway.source_profiles p WHERE
          (p.source_id,p.revision,p.context)=(value.source_id,value.source_revision,value.context);
        output:=output||jsonb_build_array(jsonb_build_object('job',gateway.source_job_projection(job.job_id),'observation_token',token,'source',descriptor));
      END LOOP;
      RETURN output;
    END""")
    function("source_bq_check_observation", "job_id text, observation_token text", "jsonb", """
    DECLARE job gateway.bigquery_source_jobs%ROWTYPE;
    BEGIN
      SELECT * INTO STRICT job FROM gateway.bigquery_source_jobs j WHERE j.job_id=source_bq_check_observation.job_id;
      IF length(observation_token)>128 OR NOT gateway.source_role_allowed(job.session_id,'observer')
        OR job.observation_login IS DISTINCT FROM session_user OR job.observation_until<=clock_timestamp()
        OR job.observation_token_hash IS DISTINCT FROM encode(sha256(convert_to(observation_token,'UTF8')),'hex') THEN
        RAISE EXCEPTION 'Source observation lease unavailable' USING ERRCODE='42501'; END IF;
      RETURN gateway.source_job_projection(job_id);
    END""")


def _domo_functions():
    function("source_domo_arm_send", "context jsonb, plan_fingerprint text, nonce_hash text, intent jsonb", "jsonb", """
    DECLARE value gateway.source_sessions%ROWTYPE; grp gateway.source_operation_groups%ROWTYPE;
      pool gateway.source_pools%ROWTYPE; settings gateway.source_runtime_settings%ROWTYPE;
      profile gateway.source_profiles%ROWTYPE; active_total bigint; active_pool bigint;
    BEGIN
      value:=gateway.source_check_context(context,true);
      SELECT * INTO STRICT grp FROM gateway.source_operation_groups WHERE id=value.group_id;
      SELECT * INTO STRICT pool FROM gateway.source_pools WHERE capacity_key=value.capacity_key;
      SELECT * INTO STRICT settings FROM gateway.source_runtime_settings WHERE singleton;
      SELECT * INTO STRICT profile FROM gateway.source_profiles WHERE (source_id,revision,source_profiles.context)=(value.source_id,value.source_revision,value.context);
      IF value.plan->>'provider'<>'domo' OR plan_fingerprint IS DISTINCT FROM value.plan->>'prepared_digest'
        OR nonce_hash !~ '^[a-f0-9]{64}$' OR jsonb_typeof(intent) IS DISTINCT FROM 'object' OR octet_length(intent::text)>8192
        OR NOT (intent ?& ARRAY['statement_id','statement_digest','dataset_id','api_origin_digest','response_profile_digest'])
        OR intent-ARRAY['statement_id','statement_digest','dataset_id','api_origin_digest','response_profile_digest']<>'{}'::jsonb
        OR NOT (intent->>'statement_id' ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'
          AND intent->>'statement_digest' ~ '^[a-f0-9]{64}$'
          AND profile.descriptor->'dataset_ids' ? (intent->>'dataset_id')
          AND intent->>'api_origin_digest'=encode(sha256(convert_to('"https://api.domo.com"','UTF8')),'hex')
          AND intent->>'response_profile_digest'=encode(sha256(convert_to('"domo-query-positional-v1"','UTF8')),'hex')) IS TRUE
        OR EXISTS(SELECT 1 FROM gateway.domo_source_sends WHERE session_id=value.id) THEN
        RAISE EXCEPTION 'Source send unavailable' USING ERRCODE='23514'; END IF;
      SELECT count(*),count(*) FILTER(WHERE ops.capacity_key=value.capacity_key) INTO active_total,active_pool FROM (
        SELECT s.capacity_key FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE j.state<>'done'
        UNION ALL SELECT s.capacity_key FROM gateway.domo_source_sends d JOIN gateway.source_sessions s ON s.id=d.session_id WHERE d.state='intent') ops;
      IF grp.admitted_count>=grp.max_submissions OR active_total>=gateway.source_global_limit() OR active_pool>=pool.max_active
        OR pool.dispatches>=pool.max_dispatches
        OR (SELECT count(*) FROM gateway.bigquery_source_jobs)+(SELECT count(*) FROM gateway.domo_source_sends)>=settings.max_intents THEN
        RAISE EXCEPTION 'Source operation budget exhausted' USING ERRCODE='54000'; END IF;
      INSERT INTO gateway.domo_source_sends(session_id,plan_fingerprint,nonce_hash,intent) VALUES(value.id,plan_fingerprint,nonce_hash,intent);
      UPDATE gateway.source_operation_groups SET admitted_count=admitted_count+1 WHERE id=grp.id;
      UPDATE gateway.source_pools SET dispatches=dispatches+1 WHERE capacity_key=value.capacity_key;
      RETURN jsonb_build_object('armed',true,'session_id',value.id,'plan_fingerprint',plan_fingerprint);
    END""")
    function("source_domo_observe", "sid uuid, nonce text, evidence jsonb", "jsonb", """
    DECLARE value gateway.source_sessions%ROWTYPE; sent gateway.domo_source_sends%ROWTYPE;
    BEGIN
      value:=gateway.source_lock_session(sid);
      IF value.plan->>'provider'<>'domo' OR NOT gateway.source_role_allowed(sid,'runtime') THEN
        RAISE EXCEPTION 'Source observation unavailable' USING ERRCODE='42501'; END IF;
      SELECT * INTO STRICT sent FROM gateway.domo_source_sends WHERE session_id=sid FOR UPDATE;
      IF length(nonce)>128 OR sent.nonce_hash IS DISTINCT FROM encode(sha256(convert_to(nonce,'UTF8')),'hex')
        OR jsonb_typeof(evidence) IS DISTINCT FROM 'object' OR octet_length(evidence::text)>2048
        OR NOT (evidence ?& ARRAY['plan_fingerprint','response_profile_digest','response_sha256','status_code','media_type','completed_at','row_count','response_bytes'])
        OR evidence-ARRAY['plan_fingerprint','response_profile_digest','response_sha256','status_code','media_type','completed_at','row_count','response_bytes']<>'{}'::jsonb
        OR evidence->>'plan_fingerprint' IS DISTINCT FROM sent.plan_fingerprint
        OR evidence->>'response_profile_digest' IS DISTINCT FROM sent.intent->>'response_profile_digest'
        OR NOT (evidence->>'response_sha256' ~ '^[a-f0-9]{64}$' AND evidence->'status_code'='200'::jsonb
          AND evidence->>'media_type'='application/json' AND jsonb_typeof(evidence->'completed_at')='string'
          AND evidence->>'completed_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$'
          AND (evidence->>'completed_at')::timestamptz>=sent.created_at
          AND (evidence->>'completed_at')::timestamptz<=clock_timestamp()+interval '5 seconds') IS TRUE
        OR NOT (jsonb_typeof(evidence->'row_count')='number' AND (evidence->>'row_count') ~ '^[0-9]{1,4}$'
          AND (evidence->>'row_count')::integer<=1001 AND jsonb_typeof(evidence->'response_bytes')='number'
          AND evidence->>'response_bytes' ~ '^[1-9][0-9]{0,6}$' AND (evidence->>'response_bytes')::integer<=1048576) IS TRUE
        OR sent.state='proven_not_sent' OR (sent.terminal_evidence IS NOT NULL AND sent.terminal_evidence IS DISTINCT FROM evidence)
        THEN RAISE EXCEPTION 'Invalid source terminal evidence' USING ERRCODE='23514'; END IF;
      UPDATE gateway.domo_source_sends SET state='terminal_success',terminal_at=coalesce(terminal_at,clock_timestamp()),terminal_evidence=evidence WHERE session_id=sid;
      PERFORM gateway.source_settle_group(value.group_id);
      RETURN gateway.source_status(sid);
    END""")
    function("source_domo_not_sent", "context jsonb, nonce text", "jsonb", """
    DECLARE value gateway.source_sessions%ROWTYPE; sent gateway.domo_source_sends%ROWTYPE;
    BEGIN
      PERFORM gateway.source_seal_attempt(context,'failure');
      value:=gateway.source_lock_session((context->>'session_id')::uuid);
      SELECT * INTO STRICT sent FROM gateway.domo_source_sends WHERE session_id=value.id FOR UPDATE;
      IF value.plan->>'provider'<>'domo' OR sent.state<>'intent' OR length(nonce)>128
        OR sent.nonce_hash IS DISTINCT FROM encode(sha256(convert_to(nonce,'UTF8')),'hex') THEN
        RAISE EXCEPTION 'Definite source no-I/O proof unavailable' USING ERRCODE='42501'; END IF;
      UPDATE gateway.domo_source_sends SET state='proven_not_sent',terminal_at=clock_timestamp() WHERE session_id=value.id;
      PERFORM gateway.source_settle_group(value.group_id);
      RETURN gateway.source_status(value.id);
    END""")


def downgrade():
    raise RuntimeError("Destructive source evidence downgrade is disabled")
