"""Immutable typed Domo preparation retained with its one logical source session."""
import importlib
from alembic import op

revision='0006_source_preparations'
down_revision='0005_source_lifecycle'
branch_labels=None
depends_on=None


def upgrade():
    configured=importlib.import_module('migrations.versions.0004_publishing_fixtures').roles()
    op.execute('''CREATE TABLE gateway.source_preparations (
        owner_kind VARCHAR(16) NOT NULL CHECK(owner_kind IN ('consumer','fixture_test')),
        owner_id VARCHAR(128) NOT NULL CHECK(owner_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'),
        session_id UUID NOT NULL UNIQUE REFERENCES gateway.source_sessions(id),
        prepared_query JSONB NOT NULL, prepared_canonical TEXT NOT NULL,
        prepared_digest VARCHAR(64) NOT NULL CHECK(prepared_digest ~ '^[a-f0-9]{64}$'),
        PRIMARY KEY(owner_kind,owner_id),
        CHECK(jsonb_typeof(prepared_query)='object' AND octet_length(prepared_canonical) BETWEEN 1 AND 8192
            AND octet_length(prepared_query::text)<=8192
            AND prepared_query=prepared_canonical::jsonb
            AND prepared_digest=encode(sha256(convert_to(prepared_canonical,'UTF8')),'hex')))''')
    op.execute('''CREATE FUNCTION gateway.guard_source_preparation() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,gateway AS $guard$
        DECLARE source gateway.source_sessions%ROWTYPE; binding jsonb; manifest jsonb; decision jsonb;
          p jsonb:=NEW.prepared_query; field text; expected_keys text[]:=ARRAY[
          'kind','context','accepted_spec_digest','binding_id','binding_revision','binding_digest','model_bundle_digest',
          'source_id','source_revision','source_digest','dataset_id','definition_digest','query_digest','scope_digest',
          'decision_digest','statement_id','statement_revision','statement_digest','response_profile','max_rows','max_bytes'];
        BEGIN
          SELECT * INTO STRICT source FROM gateway.source_sessions WHERE id=NEW.session_id FOR UPDATE;
          IF (source.context,source.owner_id) IS DISTINCT FROM (NEW.owner_kind,NEW.owner_id)
            OR source.plan->>'provider'<>'domo' OR source.sealed
            OR source.deadline_at<=clock_timestamp() OR NOT gateway.source_check_owner(source)
            OR NOT (p ?& expected_keys) OR p-expected_keys<>'{}'::jsonb
            OR p->>'kind' IS DISTINCT FROM 'domo-named-v2'
            OR p->>'response_profile' IS DISTINCT FROM 'domo-query-positional-v1'
            OR p->>'context' IS DISTINCT FROM source.context
            OR NEW.prepared_digest IS DISTINCT FROM source.plan->>'prepared_digest' THEN
            RAISE EXCEPTION 'Invalid typed source preparation owner' USING ERRCODE='23514'; END IF;
          IF source.context='consumer' THEN
            SELECT b.content,v.manifest,a.decision INTO STRICT binding,manifest,decision
              FROM gateway.executions e JOIN gateway.product_versions v ON v.id=e.version_id
              JOIN gateway.binding_revisions b ON b.id=v.binding_id
              JOIN gateway.execution_authorizations a ON a.execution_id=e.id WHERE e.id=source.owner_id;
          ELSE
            SELECT b.content,a.fixture_manifest,a.decision INTO STRICT binding,manifest,decision
              FROM gateway.fixture_attempts a JOIN gateway.binding_revisions b ON b.id=a.fixture_binding_id
              WHERE a.id=source.owner_id;
          END IF;
          FOREACH field IN ARRAY ARRAY['accepted_spec_digest','binding_digest','model_bundle_digest','source_digest',
            'definition_digest','query_digest','scope_digest','decision_digest','statement_digest'] LOOP
            IF jsonb_typeof(p->field) IS DISTINCT FROM 'string' OR p->>field !~ '^[a-f0-9]{64}$' THEN
              RAISE EXCEPTION 'Invalid typed preparation digest' USING ERRCODE='23514'; END IF;
          END LOOP;
          FOREACH field IN ARRAY ARRAY['binding_digest','model_bundle_digest','scope_digest','decision_digest'] LOOP
            IF p->field IS DISTINCT FROM source.plan->field THEN
              RAISE EXCEPTION 'Typed preparation differs from admitted source plan' USING ERRCODE='23514'; END IF;
          END LOOP;
          FOREACH field IN ARRAY ARRAY['binding_id','model_bundle_digest','source_id','source_revision','source_digest','dataset_id'] LOOP
            IF p->field IS DISTINCT FROM binding->field THEN
              RAISE EXCEPTION 'Typed preparation differs from exact binding' USING ERRCODE='23514'; END IF;
          END LOOP;
          IF binding->>'adapter_revision' IS DISTINCT FROM 'domo-named-v2'
            OR p->'binding_revision' IS DISTINCT FROM binding->'revision'
            OR p->'definition_digest' IS DISTINCT FROM binding->'authoritative_definition_digest'
            OR p->'source_digest' IS DISTINCT FROM source.plan->'definition_digest'
            OR NOT (jsonb_typeof(p->'max_rows')='number' AND p->>'max_rows' ~ '^[1-9][0-9]{0,3}$'
                AND (p->>'max_rows')::integer=least((manifest->'limits'->>'max_rows')::integer,(decision->'limits'->>'max_rows')::integer)) IS TRUE
            OR NOT (jsonb_typeof(p->'max_bytes')='number' AND p->>'max_bytes' ~ '^[1-9][0-9]{0,7}$'
                AND (p->>'max_bytes')::integer=least((manifest->'limits'->>'max_bytes')::integer,(decision->'limits'->>'max_bytes')::integer)) IS TRUE
            OR NOT EXISTS (SELECT 1 FROM jsonb_array_elements(binding->'variants') v WHERE
                v->'query_digest'=p->'query_digest' AND v->'scope_digest'=p->'scope_digest'
                AND v->'decision_digest'=p->'decision_digest' AND v->'statement_id'=p->'statement_id'
                AND v->'statement_revision'=p->'statement_revision' AND v->'statement_digest'=p->'statement_digest') THEN
            RAISE EXCEPTION 'Unreviewed typed preparation' USING ERRCODE='23514'; END IF;
          RETURN NEW;
        END $guard$''')
    op.execute('CREATE TRIGGER source_preparations_insert BEFORE INSERT ON gateway.source_preparations FOR EACH ROW EXECUTE FUNCTION gateway.guard_source_preparation()')
    op.execute('CREATE TRIGGER source_preparations_immutable BEFORE UPDATE OR DELETE ON gateway.source_preparations FOR EACH ROW EXECUTE FUNCTION gateway.deny_immutable_change()')
    op.execute('''DO $$ DECLARE recipient RECORD; who TEXT;
      BEGIN
        FOR recipient IN SELECT DISTINCT a.grantee FROM pg_class c,LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
          WHERE c.oid='gateway.source_preparations'::regclass AND a.grantee<>c.relowner LOOP
          who:=CASE WHEN recipient.grantee=0 THEN 'PUBLIC' ELSE format('%I',pg_get_userbyid(recipient.grantee)) END;
          EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE gateway.source_preparations FROM %s CASCADE',who);
        END LOOP;
        FOR recipient IN SELECT DISTINCT a.grantee FROM pg_proc p,LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
          WHERE p.oid='gateway.guard_source_preparation()'::regprocedure AND a.grantee<>p.proowner LOOP
          who:=CASE WHEN recipient.grantee=0 THEN 'PUBLIC' ELSE format('%I',pg_get_userbyid(recipient.grantee)) END;
          EXECUTE format('REVOKE ALL PRIVILEGES ON FUNCTION gateway.guard_source_preparation() FROM %s CASCADE',who);
        END LOOP;
      END $$''')
    app='"'+configured['app']+'"'
    op.execute(f'GRANT SELECT, INSERT (owner_kind,owner_id,session_id,prepared_query,prepared_canonical,prepared_digest) ON gateway.source_preparations TO {app}')


def downgrade():
    raise RuntimeError('Destructive typed preparation downgrade is disabled')
