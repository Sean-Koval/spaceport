"""Separate pinned draft fixtures and append-only publication reasons.

Fixture runs admit directly to running under the shared capacity row. There is
no pending queue or reclaim: uncertain attempts retain capacity indefinitely.
"""
import os
import re
from alembic import op
from sqlalchemy import text

revision = '0004_publishing_fixtures'
down_revision = '0003_execution_authority'
branch_labels = None
depends_on = None


def roles():
    configured = {}
    for key in ('owner','app','audit_retention','source_reader'):
        value = op.get_context().config.attributes.get('registry_roles',{}).get(key) or os.environ.get(f'GATEWAY_{key.upper()}_ROLE',f'gateway_{key}')
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}',value):
            raise RuntimeError('Unsafe registry role')
        configured[key] = value
    if len(set(configured.values())) != 4:
        raise RuntimeError('Registry roles must be distinct')
    if not op.get_context().as_sql:
        connection = op.get_bind()
        if connection.execute(text('SELECT current_user')).scalar_one() != configured['owner']:
            raise RuntimeError('Run migrations as the separately provisioned registry owner')
        found = connection.execute(text('SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls,rolreplication FROM pg_roles WHERE rolname IN (:owner,:app,:audit_retention,:source_reader)'),configured).all()
        if len(found) != 4 or any(any(row[1:]) for row in found):
            raise RuntimeError('Registry roles require non-elevated privileges')
        if connection.execute(text('SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname IN (:owner,:app,:audit_retention,:source_reader)'),configured).scalar_one():
            raise RuntimeError('Registry roles must have no explicit role memberships')
    return configured


def upgrade():
    configured = roles()
    op.execute('''CREATE TABLE gateway.fixture_attempts (
        id VARCHAR(128) PRIMARY KEY, product_id VARCHAR(128) NOT NULL,
        draft_id VARCHAR(128) NOT NULL, draft_digest VARCHAR(64) NOT NULL,
        consumer_binding_id VARCHAR(128) NOT NULL, consumer_binding_digest VARCHAR(64) NOT NULL,
        fixture_binding_id VARCHAR(128) NOT NULL, fixture_binding_digest VARCHAR(64) NOT NULL,
        fixture_manifest JSONB NOT NULL, fixture_manifest_digest VARCHAR(64) NOT NULL,
        execution_contract_digest VARCHAR(64) NOT NULL, catalog_digest VARCHAR(64) NOT NULL,
        policy_id VARCHAR(128) NOT NULL, policy_revision INTEGER NOT NULL,
        fixture_id VARCHAR(128) NOT NULL, fixture_version VARCHAR(128) NOT NULL,
        actor_id VARCHAR(128) NOT NULL REFERENCES gateway.principals(id),
        fixed_principal_id VARCHAR(128) NOT NULL REFERENCES gateway.principals(id),
        principal_snapshot JSONB NOT NULL, decision JSONB NOT NULL, canonical_query JSONB NOT NULL,
        canonical_hash VARCHAR(64) NOT NULL, runner_token VARCHAR(128) NOT NULL UNIQUE,
        request_id VARCHAR(128) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        deadline_at TIMESTAMPTZ NOT NULL,
        UNIQUE(id,actor_id,fixed_principal_id,runner_token),
        FOREIGN KEY(draft_id,product_id,draft_digest) REFERENCES gateway.draft_revisions(id,product_id,content_digest),
        FOREIGN KEY(consumer_binding_id,consumer_binding_digest) REFERENCES gateway.binding_revisions(id,content_digest),
        FOREIGN KEY(fixture_binding_id,fixture_binding_digest) REFERENCES gateway.binding_revisions(id,content_digest),
        FOREIGN KEY(policy_id,policy_revision) REFERENCES gateway.policy_revisions(policy_id,revision),
        CHECK(deadline_at>created_at AND deadline_at<=created_at+interval '120 seconds'
            AND deadline_at=created_at+make_interval(secs=>(decision->'limits'->>'execution_timeout_seconds')::integer)),
        CHECK(draft_digest ~ '^[a-f0-9]{64}$' AND consumer_binding_digest ~ '^[a-f0-9]{64}$'
            AND fixture_binding_digest ~ '^[a-f0-9]{64}$' AND fixture_manifest_digest ~ '^[a-f0-9]{64}$'
            AND execution_contract_digest ~ '^[a-f0-9]{64}$' AND catalog_digest ~ '^[a-f0-9]{64}$'
            AND canonical_hash ~ '^[a-f0-9]{64}$'),
        CHECK(jsonb_typeof(fixture_manifest)='object' AND octet_length(fixture_manifest::text)<=1048576
            AND jsonb_typeof(principal_snapshot)='object' AND octet_length(principal_snapshot::text)<=8192
            AND jsonb_typeof(decision)='object' AND octet_length(decision::text)<=32768
            AND jsonb_typeof(canonical_query)='object' AND octet_length(canonical_query::text)<=65536))''')
    op.execute('''CREATE TABLE gateway.fixture_attempt_states (
        attempt_id VARCHAR(128) PRIMARY KEY REFERENCES gateway.fixture_attempts(id),
        state VARCHAR(16) NOT NULL CHECK(state IN ('running','succeeded','failed','timed_out')),
        source_completed BOOLEAN NOT NULL DEFAULT false, completed_at TIMESTAMPTZ,
        error_code VARCHAR(32), check_digest VARCHAR(64),
        CHECK((state='succeeded' AND check_digest ~ '^[a-f0-9]{64}$' AND check_digest IS NOT NULL)
            OR (state<>'succeeded' AND check_digest IS NULL)),
        CHECK((state='running' AND completed_at IS NULL AND error_code IS NULL AND NOT source_completed)
            OR (state='succeeded' AND completed_at IS NOT NULL AND error_code IS NULL AND source_completed)
            OR (state IN ('failed','timed_out') AND completed_at IS NOT NULL AND error_code IS NOT NULL)),
        CHECK(error_code IS NULL OR error_code IN ('UNAUTHENTICATED','NOT_FOUND_OR_FORBIDDEN','INVALID_INPUT',
            'BINDING_DRIFT','UPSTREAM_UNAVAILABLE','RESOURCE_LIMIT','EXECUTION_TIMEOUT','FRESHNESS_UNKNOWN','STALE_DATA','CONFLICT')))''')
    op.execute('''CREATE TABLE gateway.fixture_source_reservations (
        id VARCHAR(128) PRIMARY KEY, attempt_id VARCHAR(128) NOT NULL UNIQUE,
        actor_id VARCHAR(128) NOT NULL, fixed_principal_id VARCHAR(128) NOT NULL,
        runner_token VARCHAR(128) NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), released_at TIMESTAMPTZ, release_reason VARCHAR(32),
        FOREIGN KEY(attempt_id,actor_id,fixed_principal_id,runner_token)
            REFERENCES gateway.fixture_attempts(id,actor_id,fixed_principal_id,runner_token),
        CHECK((released_at IS NULL AND release_reason IS NULL)
            OR (released_at>=created_at AND release_reason='completed')))''')
    op.execute('CREATE INDEX ix_fixture_source_capacity ON gateway.fixture_source_reservations(actor_id,released_at)')
    op.execute('''CREATE TABLE gateway.publication_events (
        id VARCHAR(128) PRIMARY KEY, product_id VARCHAR(128) NOT NULL, version_id VARCHAR(128) NOT NULL,
        action VARCHAR(16) NOT NULL CHECK(action IN ('publish','deprecate','withdraw','rollback')),
        actor_id VARCHAR(128) NOT NULL REFERENCES gateway.principals(id), reason VARCHAR(1024) NOT NULL,
        request_id VARCHAR(128) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY(version_id,product_id) REFERENCES gateway.product_versions(id,product_id),
        CHECK(length(btrim(reason)) BETWEEN 1 AND 1024 AND reason !~ '[[:cntrl:]]'),
        CHECK(reason !~* '(bearer|password|api[_ -]?key|token)[ :=]+[^ ]+'
            AND reason !~ '(secret://|env:|postgres(ql)?://|dpg_(svc|demo)_[A-Za-z0-9_-]{43}|eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+)'))''')
    op.execute('''CREATE FUNCTION gateway.guard_fixture_attempt() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,gateway AS $$
        DECLARE draft gateway.draft_revisions%ROWTYPE;
                consumer gateway.binding_revisions%ROWTYPE; fixture gateway.binding_revisions%ROWTYPE;
                accepted TIMESTAMPTZ; timeout_seconds INTEGER; current_revision INTEGER;
                policy_rules JSONB; product_limits JSONB; policy_limits JSONB;
                product_limit JSONB; policy_limit JSONB; product_seconds INTEGER; policy_seconds INTEGER;
        BEGIN
            IF NOT (jsonb_typeof(NEW.decision->'limits'->'execution_timeout_seconds')='number'
                AND NEW.decision->'limits'->>'execution_timeout_seconds' ~ '^([1-9]|[1-9][0-9]|1[01][0-9]|120)$') IS TRUE THEN
                RAISE EXCEPTION 'Invalid fixture timeout authority' USING ERRCODE='23514'; END IF;
            timeout_seconds:=(NEW.decision->'limits'->>'execution_timeout_seconds')::integer;
            SELECT * INTO STRICT draft FROM gateway.draft_revisions WHERE id=NEW.draft_id;
            SELECT p.current_revision INTO STRICT current_revision FROM gateway.policies p
                WHERE p.id=NEW.policy_id FOR UPDATE;
            IF current_revision IS DISTINCT FROM NEW.policy_revision THEN
                RAISE EXCEPTION 'The fixture requires current policy authority' USING ERRCODE='23514'; END IF;
            SELECT p.content->'rules' INTO STRICT policy_rules FROM gateway.policy_revisions p
                WHERE p.policy_id=NEW.policy_id AND p.revision=NEW.policy_revision;
            product_limits:=draft.manifest->'limits'; policy_limits:=policy_rules->'limits';
            IF jsonb_typeof(policy_rules) IS DISTINCT FROM 'object'
                OR (product_limits IS NOT NULL AND jsonb_typeof(product_limits) IS DISTINCT FROM 'object')
                OR (policy_limits IS NOT NULL AND jsonb_typeof(policy_limits) IS DISTINCT FROM 'object') THEN
                RAISE EXCEPTION 'Malformed fixture limits' USING ERRCODE='23514'; END IF;
            product_limit:=coalesce(product_limits->'execution_timeout_seconds','120'::jsonb);
            policy_limit:=coalesce(policy_limits->'execution_timeout_seconds','120'::jsonb);
            IF NOT (jsonb_typeof(product_limit)='number' AND product_limit::text ~ '^([1-9]|[1-9][0-9]|1[01][0-9]|120)$'
                AND jsonb_typeof(policy_limit)='number' AND policy_limit::text ~ '^([1-9]|[1-9][0-9]|1[01][0-9]|120)$') IS TRUE THEN
                RAISE EXCEPTION 'Malformed fixture time limits' USING ERRCODE='23514'; END IF;
            product_seconds:=product_limit::text::integer; policy_seconds:=policy_limit::text::integer;
            IF timeout_seconds>least(product_seconds,policy_seconds,120) THEN
                RAISE EXCEPTION 'Fixture timeout exceeds current authority' USING ERRCODE='23514'; END IF;
            accepted:=clock_timestamp(); NEW.created_at:=accepted;
            NEW.deadline_at:=accepted+make_interval(secs=>timeout_seconds);
            SELECT * INTO STRICT consumer FROM gateway.binding_revisions WHERE id=NEW.consumer_binding_id;
            SELECT * INTO STRICT fixture FROM gateway.binding_revisions WHERE id=NEW.fixture_binding_id;
            IF NOT (draft.manifest->>'policy_id'=NEW.policy_id
                AND draft.manifest->>'binding_id'=consumer.binding_id
                AND draft.manifest->'binding_revision'=to_jsonb(consumer.revision)
                AND draft.manifest->>'model_bundle_digest'=consumer.model_bundle_digest
                AND consumer.content->>'context'='consumer' AND fixture.content->>'context'='fixture_test'
                AND consumer.model_bundle_digest<>fixture.model_bundle_digest
                AND NEW.fixture_manifest->>'content_digest'=NEW.fixture_manifest_digest
                AND NEW.fixture_manifest->>'product_id'=NEW.product_id
                AND NEW.fixture_manifest->>'policy_id'=NEW.policy_id
                AND NEW.fixture_manifest->>'binding_id'=fixture.binding_id
                AND NEW.fixture_manifest->'binding_revision'=to_jsonb(fixture.revision)
                AND NEW.fixture_manifest->>'model_bundle_digest'=fixture.model_bundle_digest
                AND NEW.principal_snapshot->>'id'=NEW.fixed_principal_id
                AND NEW.decision->>'principal_id'=NEW.fixed_principal_id
                AND NEW.decision->'policy_revision'=to_jsonb(NEW.policy_revision)
                AND NEW.canonical_query->>'product_id'=NEW.product_id
                AND NEW.canonical_query->>'version'=draft.manifest->>'version') IS TRUE THEN
                RAISE EXCEPTION 'Inconsistent fixture authority' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$''')
    op.execute('CREATE TRIGGER fixture_attempt_identity BEFORE INSERT ON gateway.fixture_attempts FOR EACH ROW EXECUTE FUNCTION gateway.guard_fixture_attempt()')
    op.execute('''CREATE FUNCTION gateway.guard_fixture_state() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,gateway AS $$
        BEGIN
            IF TG_OP='INSERT' THEN
                IF NEW.state<>'running' OR NEW.source_completed OR NEW.completed_at IS NOT NULL OR NEW.error_code IS NOT NULL OR NEW.check_digest IS NOT NULL THEN
                    RAISE EXCEPTION 'Fixture attempts start running' USING ERRCODE='23514'; END IF;
            ELSE
                IF OLD.state<>'running' OR NEW.state='running' OR NEW.attempt_id<>OLD.attempt_id THEN
                    RAISE EXCEPTION 'Fixture completion is immutable' USING ERRCODE='23514'; END IF;
                NEW.completed_at:=clock_timestamp();
                IF NEW.state='succeeded' AND NEW.completed_at >= (SELECT deadline_at FROM gateway.fixture_attempts WHERE id=OLD.attempt_id) THEN
                    RAISE EXCEPTION 'Fixture deadline exceeded' USING ERRCODE='23514'; END IF;
            END IF;
            RETURN NEW;
        END $$''')
    op.execute('CREATE TRIGGER fixture_state_transition BEFORE INSERT OR UPDATE ON gateway.fixture_attempt_states FOR EACH ROW EXECUTE FUNCTION gateway.guard_fixture_state()')
    op.execute('''CREATE FUNCTION gateway.guard_fixture_release() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,gateway AS $$
        BEGIN
            IF TG_OP='INSERT' THEN
                IF NEW.released_at IS NOT NULL OR NEW.release_reason IS NOT NULL THEN
                    RAISE EXCEPTION 'A fixture reservation starts held' USING ERRCODE='23514'; END IF;
                NEW.created_at:=clock_timestamp(); RETURN NEW;
            END IF;
            IF (to_jsonb(NEW)-ARRAY['released_at','release_reason']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['released_at','release_reason'])
                OR OLD.released_at IS NOT NULL OR NEW.release_reason IS DISTINCT FROM 'completed'
                OR NOT EXISTS(SELECT 1 FROM gateway.fixture_attempt_states WHERE attempt_id=OLD.attempt_id
                    AND state<>'running' AND source_completed) THEN
                RAISE EXCEPTION 'Fixture source completion is not established' USING ERRCODE='23514'; END IF;
            NEW.released_at:=clock_timestamp(); RETURN NEW;
        END $$''')
    op.execute('CREATE TRIGGER fixture_release BEFORE INSERT OR UPDATE ON gateway.fixture_source_reservations FOR EACH ROW EXECUTE FUNCTION gateway.guard_fixture_release()')
    for table in ('fixture_attempts','publication_events'):
        op.execute(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON gateway.{table} FOR EACH ROW EXECUTE FUNCTION gateway.deny_immutable_change()')
    for table in ('fixture_attempt_states','fixture_source_reservations'):
        op.execute(f'CREATE TRIGGER {table}_no_delete BEFORE DELETE ON gateway.{table} FOR EACH ROW EXECUTE FUNCTION gateway.deny_immutable_change()')
    op.execute('''CREATE OR REPLACE VIEW gateway.held_source_capacity WITH (security_invoker=true) AS
        SELECT id AS reservation_id, principal_id FROM gateway.source_reservations WHERE released_at IS NULL
        UNION ALL SELECT id AS reservation_id, actor_id AS principal_id FROM gateway.fixture_source_reservations WHERE released_at IS NULL''')
    # No accepted fixture jobs exist. The existing pending view needs no change.
    op.execute('''DO $$ DECLARE obj RECORD; recipient RECORD; who TEXT;
        BEGIN
        FOR obj IN SELECT c.oid,format('%I.%I',n.nspname,c.relname) name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='gateway' AND c.relname IN ('fixture_attempts','fixture_attempt_states','fixture_source_reservations','publication_events') LOOP
            FOR recipient IN SELECT DISTINCT a.grantee FROM pg_class c,LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a WHERE c.oid=obj.oid AND a.grantee<>c.relowner LOOP
                who:=CASE WHEN recipient.grantee=0 THEN 'PUBLIC' ELSE format('%I',pg_get_userbyid(recipient.grantee)) END;
                EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE %s FROM %s CASCADE',obj.name,who);
            END LOOP;
        END LOOP;
        FOR obj IN SELECT p.oid,format('%I.%I(%s)',n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)) name FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='gateway' AND p.proname IN ('guard_fixture_attempt','guard_fixture_state','guard_fixture_release') LOOP
            FOR recipient IN SELECT DISTINCT a.grantee FROM pg_proc p,LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a WHERE p.oid=obj.oid AND a.grantee<>p.proowner LOOP
                who:=CASE WHEN recipient.grantee=0 THEN 'PUBLIC' ELSE format('%I',pg_get_userbyid(recipient.grantee)) END;
                EXECUTE format('REVOKE ALL PRIVILEGES ON FUNCTION %s FROM %s CASCADE',obj.name,who);
            END LOOP;
        END LOOP;
        END $$''')
    app = '"'+configured['app']+'"'
    columns = {
        'fixture_attempts':'id,product_id,draft_id,draft_digest,consumer_binding_id,consumer_binding_digest,fixture_binding_id,fixture_binding_digest,fixture_manifest,fixture_manifest_digest,execution_contract_digest,policy_id,policy_revision,catalog_digest,fixture_id,fixture_version,actor_id,fixed_principal_id,principal_snapshot,decision,canonical_query,canonical_hash,runner_token,request_id,deadline_at',
        'fixture_attempt_states':'attempt_id,state,source_completed,error_code',
        'fixture_source_reservations':'id,attempt_id,actor_id,fixed_principal_id,runner_token',
        'publication_events':'id,product_id,version_id,action,actor_id,reason,request_id',
    }
    for table, names in columns.items():
        op.execute(f'GRANT SELECT, INSERT ({names}) ON gateway.{table} TO {app}')
    op.execute(f'GRANT UPDATE (state,source_completed,error_code,check_digest) ON gateway.fixture_attempt_states TO {app}')
    op.execute(f'GRANT UPDATE (release_reason) ON gateway.fixture_source_reservations TO {app}')


def downgrade():
    raise RuntimeError('Destructive fixture evidence downgrade is disabled')
