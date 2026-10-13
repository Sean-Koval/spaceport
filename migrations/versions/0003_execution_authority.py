"""Immutable execution authority and independently fenced source reservations.

Frozen predecessors remain unchanged. No source-duration expiry is inferred.
"""
import os
import re

from alembic import op
from sqlalchemy import text

revision='0003_execution_authority'
down_revision='0002_audit_policy_id'
branch_labels=None
depends_on=None


def roles():
    configured={}
    for key in ('owner','app','audit_retention','source_reader'):
        value=op.get_context().config.attributes.get('registry_roles',{}).get(key) or os.environ.get(f'GATEWAY_{key.upper()}_ROLE',f'gateway_{key}')
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}',value): raise RuntimeError('Unsafe registry role')
        configured[key]=value
    if len(set(configured.values()))!=4: raise RuntimeError('Registry roles must be distinct')
    if not op.get_context().as_sql:
        connection=op.get_bind()
        if connection.execute(text('SELECT current_user')).scalar_one()!=configured['owner']:
            raise RuntimeError('Run migrations as the separately provisioned registry owner')
        found=connection.execute(text('SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls,rolreplication FROM pg_roles WHERE rolname IN (:owner,:app,:audit_retention,:source_reader)'),configured).all()
        if len(found)!=4 or any(any(row[1:]) for row in found): raise RuntimeError('Registry roles require non-elevated privileges')
        if connection.execute(text('SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname IN (:owner,:app,:audit_retention,:source_reader)'),configured).scalar_one():
            raise RuntimeError('Registry roles must have no explicit role memberships')
    return configured


def upgrade():
    configured=roles()
    op.execute('ALTER TABLE gateway.executions ADD COLUMN execution_timeout_seconds INTEGER NOT NULL DEFAULT 120 CONSTRAINT ck_executions_execution_timeout_bounded CHECK(execution_timeout_seconds BETWEEN 1 AND 120)')
    op.execute('ALTER TABLE gateway.executions ADD CONSTRAINT uq_execution_principal UNIQUE (id,principal_id)')
    op.execute('''CREATE TABLE gateway.execution_authorizations (
        execution_id VARCHAR(128) PRIMARY KEY REFERENCES gateway.executions(id),
        principal_snapshot JSONB NOT NULL, decision JSONB NOT NULL,
        scope_digest VARCHAR(64) NOT NULL, full_decision_digest VARCHAR(64) NOT NULL,
        CONSTRAINT ck_execution_authorizations_digest_format CHECK (scope_digest ~ '^[0-9a-f]{64}$' AND full_decision_digest ~ '^[0-9a-f]{64}$'),
        CONSTRAINT ck_execution_authorizations_bounded_objects CHECK (
            jsonb_typeof(principal_snapshot)='object' AND jsonb_typeof(decision)='object'
            AND octet_length(principal_snapshot::text)<=8192 AND octet_length(decision::text)<=32768))''')
    op.execute('''CREATE TABLE gateway.execution_preparations (
        execution_id VARCHAR(128) PRIMARY KEY REFERENCES gateway.executions(id),
        prepared_query JSONB NOT NULL,
        CONSTRAINT ck_execution_preparations_bounded_query CHECK (
            jsonb_typeof(prepared_query)='object' AND octet_length(prepared_query::text)<=65536))''')
    op.execute('''CREATE TABLE gateway.source_reservations (
        id VARCHAR(128) PRIMARY KEY, execution_id VARCHAR(128) NOT NULL, principal_id VARCHAR(128) NOT NULL,
        lease_token VARCHAR(128) NOT NULL UNIQUE, generation INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), released_at TIMESTAMPTZ, release_reason VARCHAR(32),
        CONSTRAINT fk_source_reservations_execution_principal FOREIGN KEY(execution_id,principal_id) REFERENCES gateway.executions(id,principal_id),
        CONSTRAINT uq_source_reservations_execution_generation UNIQUE(execution_id,generation),
        CONSTRAINT ck_source_reservations_generation_positive CHECK(generation>0),
        CONSTRAINT ck_source_reservations_release_coherent CHECK ((released_at IS NULL AND release_reason IS NULL)
            OR (released_at>=created_at AND release_reason='completed')))''')
    op.execute('CREATE INDEX ix_source_reservations_capacity ON gateway.source_reservations(principal_id,released_at)')
    op.execute("CREATE VIEW gateway.held_source_capacity WITH (security_invoker=true) AS SELECT id AS reservation_id,principal_id FROM gateway.source_reservations WHERE released_at IS NULL")
    op.execute("CREATE VIEW gateway.pending_execution_capacity WITH (security_invoker=true) AS SELECT e.id AS execution_id,e.principal_id FROM gateway.executions e JOIN gateway.execution_states s ON s.execution_id=e.id WHERE s.state='accepted'")
    op.execute('''CREATE FUNCTION gateway.set_execution_acceptance() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,gateway AS $$
        DECLARE accepted TIMESTAMPTZ; product_limit JSONB; policy_limit JSONB;
            product_limits JSONB; policy_rules JSONB; policy_limits JSONB;
            product_seconds INTEGER; policy_seconds INTEGER; current_revision INTEGER; pinned_policy VARCHAR(128);
        BEGIN
            SELECT p.current_revision INTO STRICT current_revision FROM gateway.policies p
                WHERE p.id=NEW.policy_id FOR UPDATE;
            IF current_revision IS DISTINCT FROM NEW.policy_revision THEN
                RAISE EXCEPTION 'The execution requires current policy authority' USING ERRCODE='23514';
            END IF;
            SELECT v.manifest->'limits',v.policy_id INTO STRICT product_limits,pinned_policy
                FROM gateway.product_versions v WHERE v.id=NEW.version_id AND v.product_id=NEW.product_id;
            IF pinned_policy IS DISTINCT FROM NEW.policy_id THEN
                RAISE EXCEPTION 'The execution requires its pinned product policy' USING ERRCODE='23514';
            END IF;
            SELECT p.content->'rules' INTO STRICT policy_rules
                FROM gateway.policy_revisions p WHERE p.policy_id=NEW.policy_id AND p.revision=NEW.policy_revision;
            policy_limits:=policy_rules->'limits';
            IF jsonb_typeof(policy_rules) IS DISTINCT FROM 'object'
                OR (product_limits IS NOT NULL AND jsonb_typeof(product_limits) IS DISTINCT FROM 'object')
                OR (policy_limits IS NOT NULL AND jsonb_typeof(policy_limits) IS DISTINCT FROM 'object') THEN
                RAISE EXCEPTION 'Malformed execution limit representation' USING ERRCODE='23514';
            END IF;
            IF (policy_limits IS NOT NULL AND policy_limits-ARRAY['max_rows','max_bytes','page_size',
                    'per_principal_concurrency','deployment_concurrency','max_date_range_days',
                    'sync_wait_seconds','execution_timeout_seconds']<>'{}'::jsonb) THEN
                RAISE EXCEPTION 'Unrecognized execution limit representation' USING ERRCODE='23514';
            END IF;
            product_limit:=product_limits->'execution_timeout_seconds';
            policy_limit:=policy_limits->'execution_timeout_seconds';
            -- Missing values use the reviewed Manifest/PolicyRules Limits default.
            -- JSON null, booleans, strings, fractions and out-of-range caps fail.
            product_limit:=coalesce(product_limit,'120'::jsonb);
            policy_limit:=coalesce(policy_limit,'120'::jsonb);
            IF NOT (jsonb_typeof(product_limit)='number' AND product_limit::text ~ '^[0-9]{1,3}$'
                AND jsonb_typeof(policy_limit)='number' AND policy_limit::text ~ '^[0-9]{1,3}$') IS TRUE THEN
                RAISE EXCEPTION 'Malformed execution time limit' USING ERRCODE='23514';
            END IF;
            product_seconds:=product_limit::text::integer; policy_seconds:=policy_limit::text::integer;
            IF product_seconds NOT BETWEEN 1 AND 120 OR policy_seconds NOT BETWEEN 1 AND 120
                OR NEW.execution_timeout_seconds IS NULL OR NEW.execution_timeout_seconds NOT BETWEEN 1 AND 120 THEN
                RAISE EXCEPTION 'Execution time limit is outside bounds' USING ERRCODE='23514';
            END IF;
            NEW.execution_timeout_seconds:=least(NEW.execution_timeout_seconds,product_seconds,policy_seconds,120);
            accepted:=clock_timestamp(); NEW.created_at:=accepted;
            NEW.deadline_at:=accepted+make_interval(secs=>NEW.execution_timeout_seconds);
            RETURN NEW;
        END $$''')
    op.execute('CREATE TRIGGER execution_acceptance BEFORE INSERT ON gateway.executions FOR EACH ROW EXECUTE FUNCTION gateway.set_execution_acceptance()')
    op.execute('''CREATE FUNCTION gateway.guard_execution_authority() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE execution gateway.executions%ROWTYPE;
        BEGIN
            SELECT * INTO STRICT execution FROM gateway.executions WHERE id=NEW.execution_id;
            IF TG_TABLE_NAME='execution_authorizations' THEN
                IF NOT ((NEW.principal_snapshot - ARRAY['id','subject','service_client_id','kind'])='{}'::jsonb
                    AND NEW.principal_snapshot ?& ARRAY['id','subject','service_client_id','kind']
                    AND NEW.principal_snapshot->>'id'=execution.principal_id
                    AND (NEW.decision - ARRAY['principal_id','policy_revision','tenant_id','row_constraints','visible_fields','limits'])='{}'::jsonb
                    AND NEW.decision ?& ARRAY['principal_id','policy_revision','tenant_id','row_constraints','visible_fields','limits']
                    AND NEW.decision->>'principal_id'=execution.principal_id
                    AND NEW.decision->'policy_revision'=to_jsonb(execution.policy_revision)
                    AND jsonb_typeof(NEW.decision->'row_constraints')='array'
                    AND jsonb_typeof(NEW.decision->'visible_fields')='array'
                    AND jsonb_typeof(NEW.decision->'limits')='object'
                    AND jsonb_typeof(NEW.decision->'limits'->'execution_timeout_seconds')='number'
                    AND (NEW.decision->'limits'->'execution_timeout_seconds')::text ~ '^[0-9]{1,3}$'
                    AND NEW.decision->'limits'->'execution_timeout_seconds'=to_jsonb(execution.execution_timeout_seconds)) IS TRUE THEN
                    RAISE EXCEPTION 'Inconsistent immutable execution authority' USING ERRCODE='23514';
                END IF;
            ELSE
                IF NOT ((NEW.prepared_query - ARRAY['endpoint_id','model_bundle_digest','cube_query'])='{}'::jsonb
                    AND NEW.prepared_query ?& ARRAY['endpoint_id','model_bundle_digest','cube_query']
                    AND jsonb_typeof(NEW.prepared_query->'cube_query')='object'
                    AND NEW.prepared_query->>'model_bundle_digest'=(SELECT manifest->>'model_bundle_digest' FROM gateway.product_versions WHERE id=execution.version_id)) IS TRUE THEN
                    RAISE EXCEPTION 'Inconsistent immutable execution preparation' USING ERRCODE='23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$''')
    for table in ('execution_authorizations','execution_preparations'):
        op.execute(f'CREATE TRIGGER {table}_identity BEFORE INSERT ON gateway.{table} FOR EACH ROW EXECUTE FUNCTION gateway.guard_execution_authority()')
        op.execute(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON gateway.{table} FOR EACH ROW EXECUTE FUNCTION gateway.deny_immutable_change()')
    op.execute('''CREATE FUNCTION gateway.guard_source_release() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='INSERT' THEN
                IF NEW.released_at IS NOT NULL OR NEW.release_reason IS NOT NULL THEN
                    RAISE EXCEPTION 'A source reservation starts held' USING ERRCODE='23514';
                END IF;
                RETURN NEW;
            END IF;
            IF (to_jsonb(NEW)-ARRAY['released_at','release_reason']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['released_at','release_reason'])
                OR OLD.released_at IS NOT NULL OR NEW.release_reason IS DISTINCT FROM 'completed' THEN
                RAISE EXCEPTION 'Source completion is not fenced by its current lease' USING ERRCODE='23514';
            END IF;
            PERFORM 1 FROM gateway.execution_leases WHERE execution_id=OLD.execution_id
                AND lease_token=OLD.lease_token AND generation=OLD.generation FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Source completion lost its lease fence' USING ERRCODE='23514';
            END IF;
            NEW.released_at:=clock_timestamp(); RETURN NEW;
        END $$''')
    op.execute('CREATE TRIGGER source_release BEFORE INSERT OR UPDATE ON gateway.source_reservations FOR EACH ROW EXECUTE FUNCTION gateway.guard_source_release()')
    op.execute('CREATE TRIGGER source_reservations_no_delete BEFORE DELETE ON gateway.source_reservations FOR EACH ROW EXECUTE FUNCTION gateway.deny_immutable_change()')
    # Reset any hostile owner default ACLs for these new objects, including PUBLIC
    # and unknown non-owner grantees. Never broaden existing table privileges.
    op.execute('''DO $$ DECLARE obj RECORD; recipient RECORD; who TEXT;
        BEGIN
        FOR obj IN SELECT c.oid, format('%I.%I',n.nspname,c.relname) name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='gateway' AND c.relname IN ('execution_authorizations','execution_preparations','source_reservations','held_source_capacity','pending_execution_capacity') LOOP
            FOR recipient IN SELECT DISTINCT a.grantee FROM pg_class c,LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a WHERE c.oid=obj.oid AND a.grantee<>c.relowner LOOP
                who:=CASE WHEN recipient.grantee=0 THEN 'PUBLIC' ELSE format('%I',pg_get_userbyid(recipient.grantee)) END;
                EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE %s FROM %s CASCADE',obj.name,who);
            END LOOP;
        END LOOP;
        FOR obj IN SELECT p.oid, format('%I.%I(%s)',n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)) name FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='gateway' AND p.proname IN ('set_execution_acceptance','guard_execution_authority','guard_source_release') LOOP
            FOR recipient IN SELECT DISTINCT a.grantee FROM pg_proc p,LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a WHERE p.oid=obj.oid AND a.grantee<>p.proowner LOOP
                who:=CASE WHEN recipient.grantee=0 THEN 'PUBLIC' ELSE format('%I',pg_get_userbyid(recipient.grantee)) END;
                EXECUTE format('REVOKE ALL PRIVILEGES ON FUNCTION %s FROM %s CASCADE',obj.name,who);
            END LOOP;
        END LOOP;
        END $$''')
    app='"'+configured['app']+'"'
    op.execute(f'GRANT INSERT (execution_timeout_seconds) ON gateway.executions TO {app}')
    for table,columns in {
        'execution_authorizations':'execution_id,principal_snapshot,decision,scope_digest,full_decision_digest',
        'execution_preparations':'execution_id,prepared_query',
        'source_reservations':'id,execution_id,principal_id,lease_token,generation',
    }.items():
        op.execute(f'GRANT SELECT, INSERT ({columns}) ON gateway.{table} TO {app}')
    op.execute(f'GRANT UPDATE (release_reason) ON gateway.source_reservations TO {app}')
    op.execute(f'GRANT SELECT ON gateway.held_source_capacity,gateway.pending_execution_capacity TO {app}')


def downgrade():
    raise RuntimeError('Destructive execution authority downgrade is disabled')
