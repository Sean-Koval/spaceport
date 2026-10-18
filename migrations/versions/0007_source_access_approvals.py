"""Operator-owned, immutable shared-non-PII source approval registry.

Historical owners are never backfilled and outstanding source holds are retained.
All time-sensitive decisions use the database clock after the approval lock.
"""
import importlib
from alembic import op

revision = '0007_source_access_approvals'
down_revision = '0006_source_preparations'
branch_labels = None
depends_on = None

TABLES = ('source_access_approvals', 'source_access_approval_states', 'source_access_admin_events', 'execution_source_authorities')
FUNCTIONS = []


def function(name, arguments, result, body, *, invoker=False, immutable=False):
    FUNCTIONS.append(name)
    op.execute(f'''CREATE OR REPLACE FUNCTION gateway.{name}({arguments}) RETURNS {result}
        LANGUAGE plpgsql {'IMMUTABLE' if immutable else 'VOLATILE'} SECURITY {'INVOKER' if invoker else 'DEFINER'}
        SET search_path=pg_catalog,gateway AS $approval${body}$approval$''')


def _scrub(tables, functions):
    tables = ','.join("'"+name+"'" for name in tables)
    functions = ','.join("'"+name+"'" for name in functions)
    op.execute(f'''DO $$ DECLARE obj RECORD; recipient RECORD; who TEXT;
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
    END $$''')


def upgrade():
    configured = importlib.import_module('migrations.versions.0004_publishing_fixtures').roles()
    op.execute('''CREATE TABLE gateway.source_access_approvals (
        id UUID PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0),
        supersedes_id UUID REFERENCES gateway.source_access_approvals(id),
        content JSONB NOT NULL, canonical_content TEXT NOT NULL,
        content_digest CHAR(64) NOT NULL UNIQUE CHECK(content_digest ~ '^[a-f0-9]{64}$'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), expires_at TIMESTAMPTZ NOT NULL,
        product_id VARCHAR(128) NOT NULL, product_version VARCHAR(64) NOT NULL,
        manifest_digest CHAR(64) NOT NULL, policy_id VARCHAR(128) NOT NULL REFERENCES gateway.policies(id),
        consumer_binding_id VARCHAR(128) NOT NULL, consumer_binding_digest CHAR(64) NOT NULL,
        fixture_binding_id VARCHAR(128) NOT NULL, fixture_binding_digest CHAR(64) NOT NULL,
        fixture_catalog_digest CHAR(64) NOT NULL, package_pins_digest CHAR(64) NOT NULL,
        UNIQUE(id,revision,content_digest),
        FOREIGN KEY(consumer_binding_id,consumer_binding_digest) REFERENCES gateway.binding_revisions(id,content_digest),
        FOREIGN KEY(fixture_binding_id,fixture_binding_digest) REFERENCES gateway.binding_revisions(id,content_digest),
        CHECK((jsonb_typeof(content)='object' AND content->>'mode'='shared_non_pii'
          AND content=canonical_content::jsonb AND octet_length(canonical_content) BETWEEN 1 AND 65536
          AND expires_at>created_at) IS TRUE))''')
    op.execute('CREATE INDEX ix_source_approval_package ON gateway.source_access_approvals(product_id,product_version,manifest_digest)')
    op.execute('''CREATE TABLE gateway.source_access_approval_states (
        approval_id UUID PRIMARY KEY REFERENCES gateway.source_access_approvals(id),
        state TEXT NOT NULL CHECK(state IN ('active','revoked')),
        state_revision INTEGER NOT NULL, changed_at TIMESTAMPTZ NOT NULL,
        revocation_reason_code VARCHAR(128),
        CHECK((state='active' AND state_revision=1 AND revocation_reason_code IS NULL)
          OR (state='revoked' AND state_revision=2 AND revocation_reason_code ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')))''')
    op.execute('''CREATE TABLE gateway.source_access_admin_events (
        id UUID PRIMARY KEY, approval_id UUID NOT NULL, approval_revision INTEGER NOT NULL,
        approval_digest CHAR(64) NOT NULL, action TEXT NOT NULL CHECK(action IN ('install','revoke')),
        operator_role NAME NOT NULL, review_id VARCHAR(128) NOT NULL, evidence_ref VARCHAR(128) NOT NULL,
        evidence_digest CHAR(64) NOT NULL, request_id UUID NOT NULL UNIQUE,
        reason_code VARCHAR(128) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY(approval_id,approval_revision,approval_digest) REFERENCES gateway.source_access_approvals(id,revision,content_digest),
        UNIQUE(approval_id,action))''')
    op.execute('''ALTER TABLE gateway.audit_events
        ADD COLUMN source_approval_id UUID,
        ADD COLUMN source_approval_revision INTEGER,
        ADD COLUMN source_approval_digest VARCHAR(64),
        ADD COLUMN source_approval_state_revision INTEGER,
        ADD COLUMN source_access_reason_code VARCHAR(32),
        ADD COLUMN created_transaction_id xid8,
        ADD CONSTRAINT source_approval_audit_shape CHECK (
          (source_access_reason_code IS NULL OR source_access_reason_code IN ('APPROVED','MISSING','REVOKED','EXPIRED','PACKAGE_MISMATCH','AUDIENCE_DENIED','AMBIGUOUS','UNSUPPORTED_SOURCE','INVALID_INPUT','UNAVAILABLE'))
          AND ((source_approval_id IS NULL AND source_approval_revision IS NULL AND source_approval_digest IS NULL AND source_approval_state_revision IS NULL)
            OR (source_approval_id IS NOT NULL AND source_approval_revision IS NOT NULL AND source_approval_revision>0
              AND source_approval_digest IS NOT NULL AND source_approval_digest ~ '^[a-f0-9]{64}$'
              AND source_approval_state_revision IS NOT NULL AND source_approval_state_revision IN (1,2) AND source_access_reason_code IS NOT NULL))
          AND (source_access_reason_code IS DISTINCT FROM 'APPROVED' OR (decision='allow' AND source_approval_id IS NOT NULL AND source_approval_state_revision=1)))''')
    function('source_audit_transaction', '', 'trigger', '''
    BEGIN NEW.created_transaction_id:=pg_current_xact_id(); RETURN NEW; END''')
    op.execute('CREATE TRIGGER source_audit_transaction BEFORE INSERT ON gateway.audit_events FOR EACH ROW EXECUTE FUNCTION gateway.source_audit_transaction()')

    _validation_functions()
    _registry_functions(configured)
    _consumer_functions(configured)
    _final_access_functions()
    _lifecycle_approval_functions(configured)
    _retention_functions(configured)
    fixture=importlib.import_module('migrations.source_fixture_access')
    fixture_tables=fixture.install(op,function,configured)
    for table in ('source_access_approvals', 'source_access_admin_events'):
        op.execute(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON gateway.{table} FOR EACH ROW EXECUTE FUNCTION gateway.deny_immutable_change()')
    op.execute('CREATE TRIGGER source_approval_state_guard BEFORE INSERT OR UPDATE OR DELETE ON gateway.source_access_approval_states FOR EACH ROW EXECUTE FUNCTION gateway.source_approval_guard_state()')
    op.execute('CREATE TRIGGER source_approval_content_guard BEFORE INSERT ON gateway.source_access_approvals FOR EACH ROW EXECUTE FUNCTION gateway.source_approval_guard_content()')
    op.execute('CREATE TRIGGER source_approval_event_guard BEFORE INSERT ON gateway.source_access_admin_events FOR EACH ROW EXECUTE FUNCTION gateway.source_approval_guard_event()')
    op.execute('CREATE CONSTRAINT TRIGGER source_approval_install_complete AFTER INSERT ON gateway.source_access_approvals DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.source_approval_complete()')
    op.execute('CREATE CONSTRAINT TRIGGER source_approval_revoke_complete AFTER UPDATE ON gateway.source_access_approval_states DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.source_approval_complete()')
    _scrub(TABLES+fixture_tables, FUNCTIONS)
    app='"'+configured['app']+'"'
    op.execute(f'GRANT EXECUTE ON FUNCTION gateway.source_approval_candidates(text,text,text), gateway.source_approval_read(uuid), gateway.source_approval_check_packages(jsonb), gateway.source_approval_scope_fingerprint() TO {app}')
    op.execute(f'GRANT EXECUTE ON FUNCTION gateway.source_owner_snapshot(text,text,jsonb) TO {app}')
    op.execute(f'GRANT SELECT ON gateway.execution_source_authorities TO {app}')
    op.execute(f'GRANT INSERT(query_canonical) ON gateway.executions TO {app}')
    op.execute(f'GRANT INSERT(manifest_canonical) ON gateway.product_versions TO {app}')
    op.execute(f'GRANT EXECUTE ON FUNCTION gateway.source_assert_owner_access(text,text,text), gateway.source_read_execution_access(text,text), gateway.source_finalize_access(jsonb) TO {app}')
    op.execute(f'GRANT INSERT(source_approval_id,source_approval_revision,source_approval_digest,source_approval_state_revision,source_access_reason_code) ON gateway.audit_events TO {app}')
    fixture.grant(op,configured)
    retention='"'+configured['audit_retention']+'"'
    op.execute(f'GRANT EXECUTE ON FUNCTION gateway.prune_unreferenced_audits(timestamptz,integer) TO {retention}')


def _validation_functions():
    function('source_approval_canonical', 'value json, depth integer DEFAULT 0', 'text', '''
    DECLARE result text; kind text; item record; pieces text[]:=ARRAY[]::text[];
    BEGIN
      IF value IS NULL OR depth IS NULL OR depth<0 OR depth>14 THEN
        RAISE EXCEPTION 'Invalid source approval document' USING ERRCODE='23514'; END IF;
      kind:=json_typeof(value);
      IF kind='object' THEN
        IF (SELECT count(*)<>count(DISTINCT key) FROM json_each(value)) THEN
          RAISE EXCEPTION 'Invalid source approval document' USING ERRCODE='23514'; END IF;
        FOR item IN SELECT key,val FROM json_each(value) AS e(key,val) ORDER BY key COLLATE "C" LOOP
          IF item.key !~ '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$' THEN
            RAISE EXCEPTION 'Invalid source approval document' USING ERRCODE='23514'; END IF;
          pieces:=array_append(pieces,to_json(item.key)::text||':'||gateway.source_approval_canonical(item.val,depth+1));
        END LOOP;
        result:='{'||array_to_string(pieces,',')||'}';
      ELSIF kind='array' THEN
        IF json_array_length(value)>200 THEN RAISE EXCEPTION 'Invalid source approval document' USING ERRCODE='23514'; END IF;
        FOR item IN SELECT v FROM json_array_elements(value) AS e(v) LOOP
          pieces:=array_append(pieces,gateway.source_approval_canonical(item.v,depth+1));
        END LOOP;
        result:='['||array_to_string(pieces,',')||']';
      ELSIF kind='number' THEN
        IF value::text !~ '^-?(0|[1-9][0-9]{0,15})$' OR abs((value::text)::numeric)>9007199254740991 THEN
          RAISE EXCEPTION 'Invalid source approval document' USING ERRCODE='23514'; END IF;
        result:=value::text;
      ELSIF kind='string' THEN
        IF value#>>'{}' ~ '-----BEGIN .*PRIVATE KEY-----'
          OR value#>>'{}' ~ '^(sk-|ghp_|github_pat_|AKIA|secret://|env:|postgres://|postgresql://)'
          OR value#>>'{}' ~ '^eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+$' THEN
          RAISE EXCEPTION 'Invalid source approval document' USING ERRCODE='23514'; END IF;
        result:=to_json(value#>>'{}')::text;
      ELSIF kind IN ('null','boolean') THEN result:=value::text;
      ELSE RAISE EXCEPTION 'Invalid source approval document' USING ERRCODE='23514'; END IF;
      IF octet_length(result)>65536 THEN RAISE EXCEPTION 'Invalid source approval document' USING ERRCODE='23514'; END IF;
      RETURN result;
    END''', immutable=True)
    function('source_approval_keys', 'value jsonb, keys text[]', 'boolean', '''
    BEGIN RETURN (jsonb_typeof(value)='object' AND value ?& keys AND value-keys='{}'::jsonb) IS TRUE; END''', immutable=True)
    function('source_approval_schema_valid', 'value jsonb, schema jsonb, root jsonb, depth integer DEFAULT 0', 'boolean', '''
    DECLARE item record; child jsonb; kind text; valid boolean:=false; key text;
    BEGIN
      IF value IS NULL OR schema IS NULL OR root IS NULL OR depth IS NULL OR depth>20 THEN RETURN false; END IF;
      IF schema ? '$ref' THEN
        RETURN gateway.source_approval_schema_valid(value,root->'$defs'->split_part(schema->>'$ref','/',3),root,depth+1); END IF;
      IF schema ? 'anyOf' THEN
        FOR child IN SELECT v FROM jsonb_array_elements(schema->'anyOf') AS a(v) LOOP
          IF gateway.source_approval_schema_valid(value,child,root,depth+1) THEN valid:=true; END IF;
        END LOOP;
        IF NOT valid THEN RETURN false; END IF;
      END IF;
      IF schema ? 'const' AND value IS DISTINCT FROM schema->'const' THEN RETURN false; END IF;
      IF schema ? 'enum' AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(schema->'enum') AS a(candidate) WHERE candidate=source_approval_schema_valid.value) THEN RETURN false; END IF;
      kind:=schema->>'type';
      IF kind='object' THEN
        IF jsonb_typeof(value) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
        IF (schema ? 'minProperties' AND (SELECT count(*) FROM jsonb_object_keys(value))<(schema->>'minProperties')::integer)
          OR (schema ? 'maxProperties' AND (SELECT count(*) FROM jsonb_object_keys(value))>(schema->>'maxProperties')::integer) THEN RETURN false; END IF;
        FOR key IN SELECT jsonb_array_elements_text(coalesce(schema->'required','[]')) LOOP
          IF NOT value ? key THEN RETURN false; END IF;
        END LOOP;
        FOR item IN SELECT * FROM jsonb_each(value) LOOP
          IF (schema->'properties') ? item.key THEN child:=schema->'properties'->item.key;
          ELSIF schema->'additionalProperties'='false'::jsonb THEN RETURN false;
          ELSIF jsonb_typeof(schema->'additionalProperties')='object' THEN child:=schema->'additionalProperties';
          ELSE child:='{}'; END IF;
          IF NOT gateway.source_approval_schema_valid(item.value,child,root,depth+1) THEN RETURN false; END IF;
        END LOOP;
      ELSIF kind='array' THEN
        IF jsonb_typeof(value) IS DISTINCT FROM 'array' THEN RETURN false; END IF;
        IF (schema ? 'minItems' AND jsonb_array_length(value)<(schema->>'minItems')::integer)
          OR (schema ? 'maxItems' AND jsonb_array_length(value)>(schema->>'maxItems')::integer) THEN RETURN false; END IF;
        FOR child IN SELECT v FROM jsonb_array_elements(value) AS a(v) LOOP
          IF NOT gateway.source_approval_schema_valid(child,schema->'items',root,depth+1) THEN RETURN false; END IF;
        END LOOP;
      ELSIF kind='string' THEN
        IF jsonb_typeof(value) IS DISTINCT FROM 'string' THEN RETURN false; END IF;
        IF (schema ? 'minLength' AND length(value#>>'{}')<(schema->>'minLength')::integer)
          OR (schema ? 'maxLength' AND length(value#>>'{}')>(schema->>'maxLength')::integer)
          OR (schema ? 'pattern' AND (value#>>'{}') !~ (schema->>'pattern')) THEN RETURN false; END IF;
      ELSIF kind IN ('integer','number') THEN
        IF jsonb_typeof(value) IS DISTINCT FROM 'number' OR value::text !~ '^-?(0|[1-9][0-9]*)$' THEN RETURN false; END IF;
        IF (schema ? 'minimum' AND value::text::numeric<(schema->>'minimum')::numeric)
          OR (schema ? 'maximum' AND value::text::numeric>(schema->>'maximum')::numeric) THEN RETURN false; END IF;
      ELSIF kind IN ('boolean','null') THEN
        IF jsonb_typeof(value) IS DISTINCT FROM kind THEN RETURN false; END IF;
      ELSIF kind IS NOT NULL THEN RETURN false; END IF;
      RETURN true;
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END''', immutable=True)
    function('source_approval_utc', 'value text', 'boolean', '''
    DECLARE instant timestamp; expected text;
    BEGIN
      IF value IS NULL OR value !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{6})?Z$' THEN RETURN false; END IF;
      instant:=value::timestamptz AT TIME ZONE 'UTC';
      expected:=to_char(instant,'YYYY-MM-DD"T"HH24:MI:SS');
      IF extract(microseconds FROM instant)::integer%1000000<>0 THEN expected:=expected||'.'||to_char(instant,'US'); END IF;
      RETURN value=expected||'Z';
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END''', immutable=True)
    function('source_approval_scalar', 'value jsonb', 'boolean', '''
    DECLARE s text;
    BEGIN
      IF value IS NULL THEN RETURN false; END IF;
      IF jsonb_typeof(value)='boolean' THEN RETURN true; END IF;
      IF jsonb_typeof(value)='number' THEN RETURN value::text ~ '^-?(0|[1-9][0-9]*)$' AND abs(value::text::numeric)<=9007199254740991; END IF;
      IF jsonb_typeof(value)<>'string' THEN RETURN false; END IF;
      s:=value#>>'{}';
      RETURN length(s)<=128 AND s ~ '^[A-Za-z0-9_-][A-Za-z0-9_.:/+-]*$'
        AND s !~ '^(sk-|ghp_|github_pat_|AKIA|secret://|env:|postgres://|postgresql://|eyJ[^.]+\\.[^.]+\\.[^.]+$)';
    END''', immutable=True)
    function('source_approval_unique_scalars', 'value jsonb', 'boolean', '''
    DECLARE item jsonb; seen jsonb:='[]'; normalized jsonb;
    BEGIN
      IF jsonb_typeof(value) IS DISTINCT FROM 'array' THEN RETURN false; END IF;
      FOR item IN SELECT v FROM jsonb_array_elements(value) a(v) LOOP
        IF NOT gateway.source_approval_scalar(item) THEN RETURN false; END IF;
        normalized:=CASE WHEN item='true'::jsonb THEN '1'::jsonb WHEN item='false'::jsonb THEN '0'::jsonb ELSE item END;
        IF seen @> jsonb_build_array(normalized) THEN RETURN false; END IF;
        seen:=seen||jsonb_build_array(normalized);
      END LOOP;
      RETURN true;
    END''', immutable=True)
    function('source_approval_domain_type', 'kind text, value jsonb, bound boolean', 'boolean', '''
    DECLARE s text;
    BEGIN
      IF kind IS NULL OR value IS NULL OR bound IS NULL OR NOT gateway.source_approval_scalar(value) THEN RETURN false; END IF;
      s:=value#>>'{}';
      IF kind='string' THEN RETURN jsonb_typeof(value)='string';
      ELSIF kind='integer' THEN RETURN jsonb_typeof(value)='number';
      ELSIF kind='boolean' THEN RETURN jsonb_typeof(value)='boolean';
      ELSIF kind='number' AND NOT bound THEN RETURN jsonb_typeof(value)='number';
      ELSIF kind IN ('decimal','number') THEN
        RETURN jsonb_typeof(value)='string' AND s ~ '^-?(0|[1-9][0-9]*)(\\.[0-9]*[1-9])?$' AND (s::numeric<>0 OR s='0');
      ELSIF kind='date' THEN RETURN jsonb_typeof(value)='string' AND s ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' AND to_char(s::date,'YYYY-MM-DD')=s;
      ELSIF kind='timestamp' THEN RETURN jsonb_typeof(value)='string' AND gateway.source_approval_utc(s);
      END IF;
      RETURN false;
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END''', immutable=True)
    function('source_approval_physical', 'descriptor jsonb', 'jsonb', '''
    BEGIN
      IF descriptor->>'provider'='bigquery' THEN
        RETURN jsonb_build_object('provider','bigquery','billing_project',descriptor->'billing_project',
          'data_project',descriptor->'data_project','dataset',descriptor->'dataset',
          'tables',(SELECT jsonb_agg(v ORDER BY v) FROM jsonb_array_elements(descriptor->'tables') a(v)),
          'location',descriptor->'location','dataset_location',descriptor->'dataset_location',
          'cube_endpoint',descriptor->'cube_endpoint','auth',descriptor->'auth');
      ELSIF descriptor->>'provider'='domo' THEN
        RETURN jsonb_build_object('provider','domo','api_origin',descriptor->'api_origin',
          'dataset_ids',(SELECT jsonb_agg(v ORDER BY v) FROM jsonb_array_elements(descriptor->'dataset_ids') a(v)),
          'auth',jsonb_build_object('mode',descriptor->'auth'->'mode',
            'client_id_sha256',descriptor->'auth'->'client_id_sha256','expected_customer',descriptor->'auth'->'expected_customer',
            'expected_user_id',descriptor->'auth'->'expected_user_id','expected_role',descriptor->'auth'->'expected_role'));
      END IF;
      RAISE EXCEPTION 'Invalid source identity' USING ERRCODE='23514';
    END''', immutable=True)

    function('source_approval_credential_digest', 'descriptor jsonb', 'text', '''
    DECLARE auth jsonb:=descriptor->'auth'; value jsonb; name text; version_ref text;
    BEGIN
      IF descriptor IS NULL OR jsonb_typeof(descriptor) IS DISTINCT FROM 'object'
        OR jsonb_typeof(auth) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'Invalid source credential binding' USING ERRCODE='23514'; END IF;
      IF descriptor->>'provider'='bigquery' THEN
        IF (auth->>'mode'='gke-workload-identity'
          AND auth->>'expected_principal' ~ '^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\\.iam\\.gserviceaccount\\.com$') IS NOT TRUE THEN
          RAISE EXCEPTION 'Invalid source credential binding' USING ERRCODE='23514'; END IF;
        value:=jsonb_build_object('provider','bigquery','mode',auth->'mode','expected_principal',auth->'expected_principal');
      ELSIF descriptor->>'provider'='domo' THEN
        IF (auth->>'mode'='oauth-client-credentials' AND auth->>'client_id_sha256' ~ '^[a-f0-9]{64}$'
          AND auth->>'expected_customer' ~ '^[a-z0-9][a-z0-9-]{0,127}$'
          AND jsonb_typeof(auth->'expected_user_id')='number' AND auth->>'expected_user_id' ~ '^[1-9][0-9]*$'
          AND (auth->>'expected_user_id')::numeric BETWEEN 1 AND 9007199254740991
          AND (auth->'expected_role'='null'::jsonb OR (jsonb_typeof(auth->'expected_role')='string'
            AND length(auth->>'expected_role') BETWEEN 1 AND 256))) IS NOT TRUE THEN
          RAISE EXCEPTION 'Invalid source credential binding' USING ERRCODE='23514'; END IF;
        FOREACH name IN ARRAY ARRAY['client_id_version','client_secret_version'] LOOP
          version_ref:=auth->>name;
          IF (jsonb_typeof(auth->name)='string' AND length(version_ref)<=512
            AND version_ref ~ '^projects/([a-z][a-z0-9-]{4,28}[a-z0-9]|[1-9][0-9]{0,19})/secrets/[A-Za-z0-9_-]{1,255}/versions/[1-9][0-9]{0,18}$') IS NOT TRUE THEN
            RAISE EXCEPTION 'Invalid source credential binding' USING ERRCODE='23514'; END IF;
        END LOOP;
        value:=jsonb_build_object('provider','domo','mode',auth->'mode','client_id_sha256',auth->'client_id_sha256',
          'expected_customer',auth->'expected_customer','expected_user_id',auth->'expected_user_id','expected_role',auth->'expected_role',
          'client_id_version',auth->'client_id_version','client_secret_version',auth->'client_secret_version');
      ELSE RAISE EXCEPTION 'Invalid source credential binding' USING ERRCODE='23514'; END IF;
      RETURN encode(sha256(convert_to('spaceport-source-credential-binding-v1','UTF8')||decode('00','hex')
        ||convert_to(gateway.source_approval_canonical(value::json),'UTF8')),'hex');
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
      RAISE EXCEPTION 'Invalid source credential binding' USING ERRCODE='23514';
    END''', immutable=True)

    # Frozen JSON schema is embedded below; no imports of mutable app contracts.
    function('source_approval_document_valid', 'value jsonb', 'boolean', r'''
    DECLARE schema jsonb:=$schema${
  "$defs": {
    "ApprovalArtifacts": {
      "additionalProperties": false,
      "properties": {
        "fixture_catalog_digest": {
          "pattern": "^[a-f0-9]{64}$",
          "title": "Fixture Catalog Digest",
          "type": "string"
        },
        "fixture_model_bundle_digest": {
          "pattern": "^[a-f0-9]{64}$",
          "title": "Fixture Model Bundle Digest",
          "type": "string"
        },
        "fixture_statement_catalog_digest": {
          "anyOf": [
            {
              "pattern": "^[a-f0-9]{64}$",
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "title": "Fixture Statement Catalog Digest"
        },
        "model_bundle_digest": {
          "pattern": "^[a-f0-9]{64}$",
          "title": "Model Bundle Digest",
          "type": "string"
        },
        "normalization_profile": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Normalization Profile",
          "type": "string"
        },
        "schema_profile": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Schema Profile",
          "type": "string"
        },
        "statement_catalog_digest": {
          "anyOf": [
            {
              "pattern": "^[a-f0-9]{64}$",
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "title": "Statement Catalog Digest"
        }
      },
      "required": [
        "model_bundle_digest",
        "fixture_model_bundle_digest",
        "statement_catalog_digest",
        "fixture_statement_catalog_digest",
        "fixture_catalog_digest",
        "schema_profile",
        "normalization_profile"
      ],
      "title": "ApprovalArtifacts",
      "type": "object"
    },
    "ApprovalBindings": {
      "additionalProperties": false,
      "properties": {
        "consumer": {
          "$ref": "#/$defs/BindingApprovalPin"
        },
        "fixture": {
          "$ref": "#/$defs/BindingApprovalPin"
        }
      },
      "required": [
        "consumer",
        "fixture"
      ],
      "title": "ApprovalBindings",
      "type": "object"
    },
    "ApprovalSources": {
      "additionalProperties": false,
      "properties": {
        "consumer": {
          "$ref": "#/$defs/SourceApprovalPin"
        },
        "fixture": {
          "$ref": "#/$defs/SourceApprovalPin"
        }
      },
      "required": [
        "consumer",
        "fixture"
      ],
      "title": "ApprovalSources",
      "type": "object"
    },
    "BindingApprovalPin": {
      "additionalProperties": false,
      "properties": {
        "binding_id": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Binding Id",
          "type": "string"
        },
        "content_digest": {
          "pattern": "^[a-f0-9]{64}$",
          "title": "Content Digest",
          "type": "string"
        },
        "revision": {
          "maximum": 2147483647,
          "minimum": 1,
          "title": "Revision",
          "type": "integer"
        }
      },
      "required": [
        "binding_id",
        "revision",
        "content_digest"
      ],
      "title": "BindingApprovalPin",
      "type": "object"
    },
    "ProductApprovalPin": {
      "additionalProperties": false,
      "properties": {
        "manifest_digest": {
          "pattern": "^[a-f0-9]{64}$",
          "title": "Manifest Digest",
          "type": "string"
        },
        "policy_id": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Policy Id",
          "type": "string"
        },
        "product_id": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Product Id",
          "type": "string"
        },
        "version": {
          "maxLength": 64,
          "minLength": 5,
          "pattern": "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(?:-[A-Za-z0-9.-]+)?$",
          "title": "Version",
          "type": "string"
        }
      },
      "required": [
        "product_id",
        "version",
        "manifest_digest",
        "policy_id"
      ],
      "title": "ProductApprovalPin",
      "type": "object"
    },
    "ReviewedValueDomain": {
      "additionalProperties": false,
      "properties": {
        "classification": {
          "const": "non-personal",
          "title": "Classification",
          "type": "string"
        },
        "kind": {
          "enum": [
            "enum",
            "range"
          ],
          "title": "Kind",
          "type": "string"
        },
        "maximum": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "title": "Maximum"
        },
        "minimum": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "title": "Minimum"
        },
        "name": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Name",
          "type": "string"
        },
        "nullable": {
          "title": "Nullable",
          "type": "boolean"
        },
        "type": {
          "enum": [
            "string",
            "integer",
            "number",
            "boolean",
            "date",
            "timestamp",
            "decimal"
          ],
          "title": "Type",
          "type": "string"
        },
        "values": {
          "items": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "integer"
              },
              {
                "type": "number"
              },
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ]
          },
          "maxItems": 100,
          "title": "Values",
          "type": "array"
        }
      },
      "required": [
        "name",
        "kind",
        "type",
        "values",
        "minimum",
        "maximum",
        "nullable",
        "classification"
      ],
      "title": "ReviewedValueDomain",
      "type": "object"
    },
    "RowConstraint": {
      "additionalProperties": false,
      "properties": {
        "member": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Member",
          "type": "string"
        },
        "operator": {
          "enum": [
            "eq",
            "in"
          ],
          "title": "Operator",
          "type": "string"
        },
        "values": {
          "items": {
            "anyOf": [
              {
                "type": "string"
              },
              {
                "type": "integer"
              },
              {
                "type": "number"
              },
              {
                "type": "boolean"
              },
              {
                "type": "null"
              }
            ]
          },
          "maxItems": 100,
          "minItems": 1,
          "title": "Values",
          "type": "array"
        }
      },
      "required": [
        "member",
        "operator",
        "values"
      ],
      "title": "RowConstraint",
      "type": "object"
    },
    "SourceApprovalPin": {
      "additionalProperties": false,
      "properties": {
        "context": {
          "enum": [
            "consumer",
            "fixture_test"
          ],
          "title": "Context",
          "type": "string"
        },
        "credential_binding_digest": {
          "pattern": "^[a-f0-9]{64}$",
          "title": "Credential Binding Digest",
          "type": "string"
        },
        "descriptor_digest": {
          "pattern": "^[a-f0-9]{64}$",
          "title": "Descriptor Digest",
          "type": "string"
        },
        "physical_identity_digest": {
          "pattern": "^[a-f0-9]{64}$",
          "title": "Physical Identity Digest",
          "type": "string"
        },
        "provider": {
          "enum": [
            "bigquery",
            "domo"
          ],
          "title": "Provider",
          "type": "string"
        },
        "revision": {
          "maximum": 2147483647,
          "minimum": 1,
          "title": "Revision",
          "type": "integer"
        },
        "source_id": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Source Id",
          "type": "string"
        }
      },
      "required": [
        "source_id",
        "revision",
        "provider",
        "context",
        "descriptor_digest",
        "physical_identity_digest",
        "credential_binding_digest"
      ],
      "title": "SourceApprovalPin",
      "type": "object"
    },
    "SourceAudienceEntry": {
      "additionalProperties": false,
      "properties": {
        "actions": {
          "items": {
            "enum": [
              "discover",
              "describe",
              "execute",
              "result",
              "author",
              "publish",
              "policy_admin",
              "operate",
              "audit_view"
            ],
            "type": "string"
          },
          "maxItems": 9,
          "minItems": 1,
          "title": "Actions",
          "type": "array"
        },
        "client_ids": {
          "items": {
            "anyOf": [
              {
                "maxLength": 128,
                "minLength": 1,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
                "type": "string"
              },
              {
                "type": "null"
              }
            ]
          },
          "maxItems": 100,
          "minItems": 1,
          "title": "Client Ids",
          "type": "array"
        },
        "context": {
          "enum": [
            "consumer",
            "fixture_test"
          ],
          "title": "Context",
          "type": "string"
        },
        "fixture_case_ids": {
          "items": {
            "maxLength": 128,
            "minLength": 1,
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
            "type": "string"
          },
          "maxItems": 8,
          "title": "Fixture Case Ids",
          "type": "array"
        },
        "id": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Id",
          "type": "string"
        },
        "principal_id": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Principal Id",
          "type": "string"
        },
        "principal_kind": {
          "enum": [
            "user",
            "service",
            "demo"
          ],
          "title": "Principal Kind",
          "type": "string"
        },
        "row_constraints": {
          "items": {
            "$ref": "#/$defs/RowConstraint"
          },
          "maxItems": 20,
          "minItems": 1,
          "title": "Row Constraints",
          "type": "array"
        },
        "tenant_id": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Tenant Id",
          "type": "string"
        },
        "visible_fields": {
          "items": {
            "maxLength": 128,
            "minLength": 1,
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
            "type": "string"
          },
          "maxItems": 50,
          "minItems": 1,
          "title": "Visible Fields",
          "type": "array"
        }
      },
      "required": [
        "id",
        "context",
        "principal_id",
        "principal_kind",
        "client_ids",
        "actions",
        "tenant_id",
        "row_constraints",
        "visible_fields",
        "fixture_case_ids"
      ],
      "title": "SourceAudienceEntry",
      "type": "object"
    },
    "SourceLineageEntry": {
      "additionalProperties": false,
      "properties": {
        "classification": {
          "const": "non-personal",
          "title": "Classification",
          "type": "string"
        },
        "id": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Id",
          "type": "string"
        },
        "purpose": {
          "enum": [
            "exposed",
            "hidden_scope",
            "checkpoint",
            "grouping",
            "source_input"
          ],
          "title": "Purpose",
          "type": "string"
        },
        "source_context": {
          "enum": [
            "consumer",
            "fixture_test"
          ],
          "title": "Source Context",
          "type": "string"
        },
        "source_member": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Source Member",
          "type": "string"
        }
      },
      "required": [
        "id",
        "source_context",
        "source_member",
        "purpose",
        "classification"
      ],
      "title": "SourceLineageEntry",
      "type": "object"
    },
    "SourceReviewAttestation": {
      "additionalProperties": false,
      "properties": {
        "evidence_digest": {
          "pattern": "^[a-f0-9]{64}$",
          "title": "Evidence Digest",
          "type": "string"
        },
        "evidence_ref": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Evidence Ref",
          "type": "string"
        },
        "inference_risk_reviewed": {
          "title": "Inference Risk Reviewed",
          "type": "boolean"
        },
        "lineage_reviewed": {
          "title": "Lineage Reviewed",
          "type": "boolean"
        },
        "operator_id": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Operator Id",
          "type": "string"
        },
        "purpose": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Purpose",
          "type": "string"
        },
        "review_id": {
          "maxLength": 128,
          "minLength": 1,
          "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
          "title": "Review Id",
          "type": "string"
        },
        "shared_identity_approved": {
          "title": "Shared Identity Approved",
          "type": "boolean"
        },
        "source_publication_controlled": {
          "title": "Source Publication Controlled",
          "type": "boolean"
        }
      },
      "required": [
        "review_id",
        "operator_id",
        "evidence_ref",
        "evidence_digest",
        "purpose",
        "lineage_reviewed",
        "source_publication_controlled",
        "shared_identity_approved",
        "inference_risk_reviewed"
      ],
      "title": "SourceReviewAttestation",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "properties": {
    "artifacts": {
      "$ref": "#/$defs/ApprovalArtifacts"
    },
    "audiences": {
      "items": {
        "$ref": "#/$defs/SourceAudienceEntry"
      },
      "maxItems": 100,
      "minItems": 1,
      "title": "Audiences",
      "type": "array"
    },
    "bindings": {
      "$ref": "#/$defs/ApprovalBindings"
    },
    "expires_at": {
      "title": "Expires At",
      "type": "string"
    },
    "filter_domains": {
      "items": {
        "$ref": "#/$defs/ReviewedValueDomain"
      },
      "maxItems": 50,
      "title": "Filter Domains",
      "type": "array"
    },
    "input_domains": {
      "items": {
        "$ref": "#/$defs/ReviewedValueDomain"
      },
      "maxItems": 50,
      "title": "Input Domains",
      "type": "array"
    },
    "lineage": {
      "items": {
        "$ref": "#/$defs/SourceLineageEntry"
      },
      "maxItems": 200,
      "minItems": 1,
      "title": "Lineage",
      "type": "array"
    },
    "mode": {
      "const": "shared_non_pii",
      "title": "Mode",
      "type": "string"
    },
    "product": {
      "$ref": "#/$defs/ProductApprovalPin"
    },
    "review": {
      "$ref": "#/$defs/SourceReviewAttestation"
    },
    "revision": {
      "maximum": 2147483647,
      "minimum": 1,
      "title": "Revision",
      "type": "integer"
    },
    "schema_version": {
      "maximum": 1,
      "minimum": 1,
      "title": "Schema Version",
      "type": "integer"
    },
    "sources": {
      "$ref": "#/$defs/ApprovalSources"
    },
    "supersedes_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Supersedes Id"
    }
  },
  "required": [
    "product",
    "bindings",
    "sources",
    "artifacts",
    "lineage",
    "input_domains",
    "filter_domains",
    "schema_version",
    "mode",
    "revision",
    "supersedes_id",
    "expires_at",
    "audiences",
    "review"
  ],
  "title": "SourceApprovalDocument",
  "type": "object"
}$schema$::jsonb;
      item jsonb; row_value jsonb; domain jsonb; key text; observed integer; distinct_count integer;
      numeric_min numeric; numeric_max numeric;
    BEGIN
      PERFORM gateway.source_approval_canonical(value::json);
      IF NOT gateway.source_approval_schema_valid(value,schema,schema) THEN RETURN false; END IF;
      IF NOT gateway.source_approval_utc(value->>'expires_at')
        OR ((value->>'supersedes_id') IS NULL) IS DISTINCT FROM ((value->>'revision')::integer=1)
        OR (value->>'supersedes_id' IS NOT NULL AND value->>'supersedes_id' !~ '^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
        OR value->'sources'->'consumer'->>'context' IS DISTINCT FROM 'consumer'
        OR value->'sources'->'fixture'->>'context' IS DISTINCT FROM 'fixture_test'
        OR value->'sources'->'consumer'->>'provider' IS DISTINCT FROM value->'sources'->'fixture'->>'provider'
        OR value->'sources'->'consumer'->>'source_id'=value->'sources'->'fixture'->>'source_id'
        OR value->'bindings'->'consumer'->>'binding_id'=value->'bindings'->'fixture'->>'binding_id' THEN RETURN false; END IF;
      FOREACH key IN ARRAY ARRAY['lineage_reviewed','source_publication_controlled','shared_identity_approved','inference_risk_reviewed'] LOOP
        IF value->'review'->key IS DISTINCT FROM 'true'::jsonb THEN RETURN false; END IF;
      END LOOP;
      IF value->'sources'->'consumer'->>'provider'='bigquery' THEN
        IF value->'artifacts'->'statement_catalog_digest' IS DISTINCT FROM 'null'::jsonb
          OR value->'artifacts'->'fixture_statement_catalog_digest' IS DISTINCT FROM 'null'::jsonb THEN RETURN false; END IF;
      ELSE
        IF value->'artifacts'->'statement_catalog_digest'='null'::jsonb
          OR value->'artifacts'->'fixture_statement_catalog_digest'='null'::jsonb THEN RETURN false; END IF;
      END IF;
      SELECT count(*),count(DISTINCT jsonb_build_array(e->'source_context',e->'purpose',e->'id')) INTO observed,distinct_count FROM jsonb_array_elements(value->'lineage') e;
      IF observed<>distinct_count THEN RETURN false; END IF;
      SELECT count(*),count(DISTINCT e->>'id') INTO observed,distinct_count FROM jsonb_array_elements(value->'audiences') e;
      IF observed<>distinct_count THEN RETURN false; END IF;
      FOR item IN SELECT e FROM jsonb_array_elements(value->'audiences') e LOOP
        FOREACH key IN ARRAY ARRAY['client_ids','actions','visible_fields','fixture_case_ids'] LOOP
          IF (SELECT count(*)<>count(DISTINCT e) FROM jsonb_array_elements(item->key) e) THEN RETURN false; END IF;
        END LOOP;
        IF (item->>'context'='consumer' AND jsonb_array_length(item->'fixture_case_ids')<>0)
          OR (item->>'principal_kind'='demo' AND (item->>'context'<>'fixture_test'
            OR item->'client_ids'<>'[null]'::jsonb OR jsonb_array_length(item->'fixture_case_ids')=0
            OR item->'actions' ?| ARRAY['author','publish']))
          OR (item->>'principal_kind'<>'demo' AND item->'client_ids' @> '[null]'::jsonb)
          OR (jsonb_array_length(item->'fixture_case_ids')>0 AND item->'actions' ?| ARRAY['author','publish']) THEN RETURN false; END IF;
        IF (SELECT count(*)<>count(DISTINCT e->>'member') FROM jsonb_array_elements(item->'row_constraints') e) THEN RETURN false; END IF;
        FOR row_value IN SELECT e FROM jsonb_array_elements(item->'row_constraints') e LOOP
          IF (row_value->>'operator'='eq' AND jsonb_array_length(row_value->'values')<>1)
            OR NOT gateway.source_approval_unique_scalars(row_value->'values') THEN RETURN false; END IF;
        END LOOP;
      END LOOP;
      FOREACH key IN ARRAY ARRAY['input_domains','filter_domains'] LOOP
        IF (SELECT count(*)<>count(DISTINCT e->>'name') FROM jsonb_array_elements(value->key) e) THEN RETURN false; END IF;
        FOR domain IN SELECT e FROM jsonb_array_elements(value->key) e LOOP
          IF domain->>'kind'='enum' THEN
            IF jsonb_array_length(domain->'values')=0 OR domain->'minimum'<>'null'::jsonb OR domain->'maximum'<>'null'::jsonb
              OR NOT gateway.source_approval_unique_scalars(domain->'values') THEN RETURN false; END IF;
            FOR item IN SELECT e FROM jsonb_array_elements(domain->'values') e LOOP
              IF NOT gateway.source_approval_domain_type(domain->>'type',item,false) THEN RETURN false; END IF;
            END LOOP;
          ELSE
            IF jsonb_array_length(domain->'values')<>0 OR domain->>'type' IN ('string','boolean')
              OR NOT gateway.source_approval_domain_type(domain->>'type',domain->'minimum',true)
              OR NOT gateway.source_approval_domain_type(domain->>'type',domain->'maximum',true) THEN RETURN false; END IF;
            IF domain->>'type' IN ('number','decimal','integer') THEN
              IF (domain->>'minimum')::numeric>(domain->>'maximum')::numeric THEN RETURN false; END IF;
            ELSIF domain->>'type'='date' THEN
              IF (domain->>'minimum')::date>(domain->>'maximum')::date THEN RETURN false; END IF;
            ELSIF (domain->>'minimum')::timestamptz>(domain->>'maximum')::timestamptz THEN RETURN false; END IF;
          END IF;
        END LOOP;
      END LOOP;
      RETURN true;
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END
''', immutable=True)


    function('source_approval_binding_inventory', 'binding jsonb, input_domains jsonb', 'jsonb', '''
    DECLARE expected jsonb:='[]'; row_value record; name text; member text; context_name text:=binding->>'context';
      time_member text; column_name text; members text[]; item jsonb; purpose text; extra_inputs integer:=0;
    BEGIN
      IF binding->>'adapter_revision'='cube-bigquery-v1' THEN
        FOR row_value IN SELECT * FROM jsonb_each(binding->'fields') LOOP
          expected:=expected||jsonb_build_array(jsonb_build_object('id',row_value.key,'source_context',context_name,
            'source_member',row_value.value->>'member','purpose','exposed','classification','non-personal'));
          IF row_value.value->>'kind'<>'measure' THEN
            expected:=expected||jsonb_build_array(jsonb_build_object('id',row_value.key,'source_context',context_name,
              'source_member',row_value.value->>'member','purpose','grouping','classification','non-personal'));
          END IF;
          IF row_value.value->>'kind'='time' THEN time_member:=row_value.value->>'member'; END IF;
        END LOOP;
        members:=ARRAY[binding->'scope'->>'tenant_member',binding->'scope'->>'access_group_member'];
        member:=binding->>'checkpoint_member';
      ELSIF binding->>'adapter_revision'='domo-named-v2' THEN
        FOR item IN SELECT v FROM jsonb_array_elements(binding->'columns') a(v) LOOP
          expected:=expected||jsonb_build_array(jsonb_build_object('id',item->>'field','source_context',context_name,
            'source_member',item->>'source_name','purpose','exposed','classification','non-personal'));
          IF binding->'grain_keys' ? (item->>'field') THEN
            expected:=expected||jsonb_build_array(jsonb_build_object('id',item->>'field','source_context',context_name,
              'source_member',item->>'source_name','purpose','grouping','classification','non-personal'));
          END IF;
          IF item->>'field'=binding->>'time_field' THEN time_member:=item->>'source_name'; END IF;
        END LOOP;
        members:=ARRAY[binding->>'tenant_member',binding->>'entitlement_member'];
        member:=binding->'checkpoint'->>'source_name';
      ELSE RAISE EXCEPTION 'Unsupported source binding' USING ERRCODE='23514'; END IF;
      IF member IS NOT NULL THEN
        expected:=expected||jsonb_build_array(jsonb_build_object('id',member,'source_context',context_name,
          'source_member',member,'purpose','checkpoint','classification','non-personal'));
      END IF;
      FOREACH member IN ARRAY members LOOP
        expected:=expected||jsonb_build_array(jsonb_build_object('id',member,'source_context',context_name,
          'source_member',member,'purpose','hidden_scope','classification','non-personal'));
      END LOOP;
      FOR item IN SELECT v FROM jsonb_array_elements(input_domains) a(v) LOOP
        name:=item->>'name';column_name:=binding->'input_fields'->>name;
        IF column_name IS NULL THEN
          extra_inputs:=extra_inputs+1;
          IF item->>'type'<>'date' THEN RAISE EXCEPTION 'Invalid source input inventory' USING ERRCODE='23514'; END IF;
          member:=time_member;
        ELSIF binding->>'adapter_revision'='cube-bigquery-v1' THEN member:=binding->'fields'->column_name->>'member';
        ELSE SELECT v->>'source_name' INTO STRICT member FROM jsonb_array_elements(binding->'columns') a(v) WHERE v->>'field'=column_name;
        END IF;
        IF member IS NULL THEN RAISE EXCEPTION 'Invalid source input inventory' USING ERRCODE='23514'; END IF;
        expected:=expected||jsonb_build_array(jsonb_build_object('id',name,'source_context',context_name,
          'source_member',member,'purpose','source_input','classification','non-personal'));
      END LOOP;
      IF extra_inputs<>2 OR (SELECT count(*) FROM jsonb_each(binding->'input_fields'))+2<>jsonb_array_length(input_domains) THEN
        RAISE EXCEPTION 'Invalid source input inventory' USING ERRCODE='23514'; END IF;
      RETURN expected;
    END''', immutable=True)
    function('source_approval_package_valid', 'value jsonb', 'boolean', '''
    DECLARE consumer gateway.binding_revisions%ROWTYPE; fixture gateway.binding_revisions%ROWTYPE;
      inventory jsonb; pin jsonb; source gateway.source_profiles%ROWTYPE; name text; binding jsonb;
    BEGIN
      SELECT * INTO STRICT consumer FROM gateway.binding_revisions b WHERE b.binding_id=value->'bindings'->'consumer'->>'binding_id'
        AND b.revision=(value->'bindings'->'consumer'->>'revision')::integer AND b.content_digest=value->'bindings'->'consumer'->>'content_digest';
      SELECT * INTO STRICT fixture FROM gateway.binding_revisions b WHERE b.binding_id=value->'bindings'->'fixture'->>'binding_id'
        AND b.revision=(value->'bindings'->'fixture'->>'revision')::integer AND b.content_digest=value->'bindings'->'fixture'->>'content_digest';
      IF consumer.adapter_revision NOT IN ('cube-bigquery-v1','domo-named-v2') OR fixture.adapter_revision IS DISTINCT FROM consumer.adapter_revision
        OR consumer.model_bundle_digest IS DISTINCT FROM value->'artifacts'->>'model_bundle_digest'
        OR fixture.model_bundle_digest IS DISTINCT FROM value->'artifacts'->>'fixture_model_bundle_digest' THEN RETURN false; END IF;
      IF consumer.adapter_revision='domo-named-v2' AND (
        value->'artifacts'->>'statement_catalog_digest' IS DISTINCT FROM consumer.model_bundle_digest
        OR value->'artifacts'->>'fixture_statement_catalog_digest' IS DISTINCT FROM fixture.model_bundle_digest) THEN RETURN false; END IF;
      inventory:=gateway.source_approval_binding_inventory(consumer.content,value->'input_domains')
        ||gateway.source_approval_binding_inventory(fixture.content,value->'input_domains');
      IF jsonb_array_length(inventory)<>jsonb_array_length(value->'lineage') OR NOT inventory @> (value->'lineage') OR NOT (value->'lineage') @> inventory THEN RETURN false; END IF;
      FOREACH name IN ARRAY ARRAY['consumer','fixture'] LOOP
        pin:=value->'sources'->name;
        SELECT * INTO STRICT source FROM gateway.source_profiles p WHERE p.source_id=pin->>'source_id'
          AND p.revision=(pin->>'revision')::integer AND p.context=pin->>'context';
        IF source.descriptor_digest IS DISTINCT FROM pin->>'descriptor_digest' OR source.provider IS DISTINCT FROM pin->>'provider'
          OR encode(sha256(convert_to(gateway.source_approval_canonical(gateway.source_approval_physical(source.descriptor)::json),'UTF8')),'hex') IS DISTINCT FROM pin->>'physical_identity_digest'
          OR gateway.source_approval_credential_digest(source.descriptor) IS DISTINCT FROM pin->>'credential_binding_digest' THEN RETURN false; END IF;
        binding:=CASE WHEN name='consumer' THEN consumer.content ELSE fixture.content END;
        IF binding->>'source_id' IS DISTINCT FROM source.source_id OR binding->'source_revision' IS DISTINCT FROM to_jsonb(source.revision)
          OR binding->>'source_digest' IS DISTINCT FROM source.descriptor_digest OR binding->>'context' IS DISTINCT FROM source.context THEN RETURN false; END IF;
      END LOOP;
      RETURN true;
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END''')


def _registry_functions(configured):
    function('source_approval_owner', '', 'boolean', f'''
    BEGIN RETURN session_user='{configured['owner']}' AND current_user='{configured['owner']}'; END''')
    function('source_approval_assert', 'approval_id uuid, expected_revision integer, expected_digest text', 'jsonb', '''
    DECLARE approval gateway.source_access_approvals%ROWTYPE; state gateway.source_access_approval_states%ROWTYPE;
    BEGIN
      IF approval_id IS NULL OR expected_revision IS NULL OR expected_digest IS NULL THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      SELECT * INTO STRICT approval FROM gateway.source_access_approvals a WHERE a.id=approval_id;
      SELECT * INTO STRICT state FROM gateway.source_access_approval_states s WHERE s.approval_id=approval.id FOR UPDATE;
      IF (approval.revision=expected_revision AND approval.content_digest=expected_digest
          AND state.state='active' AND state.state_revision=1 AND approval.expires_at>clock_timestamp()) IS NOT TRUE THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      RETURN jsonb_build_object('approval_id',approval.id,'revision',approval.revision,'digest',approval.content_digest,
        'state_revision',state.state_revision,'expires_at',approval.expires_at);
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501';
    END''')
    function('source_approval_guard_state', '', 'trigger', '''
    BEGIN
      IF NOT gateway.source_approval_owner() OR TG_OP='DELETE' THEN
        RAISE EXCEPTION 'Immutable source approval state' USING ERRCODE='23514'; END IF;
      IF TG_OP='INSERT' THEN
        IF (NEW.state='active' AND NEW.state_revision=1 AND NEW.revocation_reason_code IS NULL) IS NOT TRUE THEN
          RAISE EXCEPTION 'Invalid source approval state' USING ERRCODE='23514'; END IF;
      ELSIF OLD.state IS DISTINCT FROM 'active' OR OLD.state_revision IS DISTINCT FROM 1
        OR NEW.state IS DISTINCT FROM 'revoked' OR NEW.state_revision IS DISTINCT FROM 2
        OR NEW.approval_id IS DISTINCT FROM OLD.approval_id
        OR NEW.revocation_reason_code IS NULL OR NEW.revocation_reason_code !~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$' THEN
        RAISE EXCEPTION 'Terminal source approval state' USING ERRCODE='23514'; END IF;
      NEW.changed_at:=clock_timestamp(); RETURN NEW;
    END''')
    function('source_approval_guard_content', '', 'trigger', '''
    BEGIN
      IF NOT gateway.source_approval_owner() OR NOT gateway.source_approval_document_valid(NEW.content)
        OR NEW.canonical_content IS DISTINCT FROM gateway.source_approval_canonical(NEW.canonical_content::json)
        OR NEW.content IS DISTINCT FROM NEW.canonical_content::jsonb
        OR NEW.content_digest IS DISTINCT FROM encode(sha256(convert_to('spaceport-source-approval-v1','UTF8')||decode('00','hex')||convert_to(NEW.canonical_content,'UTF8')),'hex')
        OR NEW.revision IS DISTINCT FROM (NEW.content->>'revision')::integer
        OR NEW.supersedes_id IS DISTINCT FROM (NEW.content->>'supersedes_id')::uuid
        OR NEW.expires_at IS DISTINCT FROM (NEW.content->>'expires_at')::timestamptz
        OR NEW.product_id IS DISTINCT FROM NEW.content->'product'->>'product_id'
        OR NEW.product_version IS DISTINCT FROM NEW.content->'product'->>'version'
        OR NEW.manifest_digest IS DISTINCT FROM NEW.content->'product'->>'manifest_digest'
        OR NEW.policy_id IS DISTINCT FROM NEW.content->'product'->>'policy_id'
        OR NOT gateway.source_approval_package_valid(NEW.content)
        OR NEW.consumer_binding_id IS DISTINCT FROM (SELECT id FROM gateway.binding_revisions WHERE binding_id=NEW.content->'bindings'->'consumer'->>'binding_id' AND revision=(NEW.content->'bindings'->'consumer'->>'revision')::integer AND content_digest=NEW.content->'bindings'->'consumer'->>'content_digest')
        OR NEW.fixture_binding_id IS DISTINCT FROM (SELECT id FROM gateway.binding_revisions WHERE binding_id=NEW.content->'bindings'->'fixture'->>'binding_id' AND revision=(NEW.content->'bindings'->'fixture'->>'revision')::integer AND content_digest=NEW.content->'bindings'->'fixture'->>'content_digest')
        OR NEW.consumer_binding_digest IS DISTINCT FROM NEW.content->'bindings'->'consumer'->>'content_digest'
        OR NEW.fixture_binding_digest IS DISTINCT FROM NEW.content->'bindings'->'fixture'->>'content_digest'
        OR NEW.fixture_catalog_digest IS DISTINCT FROM NEW.content->'artifacts'->>'fixture_catalog_digest'
        OR NEW.package_pins_digest IS DISTINCT FROM encode(sha256(convert_to('spaceport-reviewed-package-v1','UTF8')||decode('00','hex')||convert_to(gateway.source_approval_canonical((NEW.content-ARRAY['schema_version','mode','revision','supersedes_id','expires_at','audiences','review'])::json),'UTF8')),'hex') THEN
        RAISE EXCEPTION 'Invalid source approval document' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END''')
    function('source_approval_guard_event', '', 'trigger', '''
    DECLARE approval gateway.source_access_approvals%ROWTYPE;
    BEGIN
      IF NOT gateway.source_approval_owner() THEN RAISE EXCEPTION 'Invalid approval receipt' USING ERRCODE='23514'; END IF;
      SELECT * INTO STRICT approval FROM gateway.source_access_approvals WHERE id=NEW.approval_id;
      NEW.operator_role:=session_user; NEW.created_at:=clock_timestamp();
      IF NEW.review_id IS DISTINCT FROM approval.content->'review'->>'review_id'
        OR NEW.evidence_ref IS DISTINCT FROM approval.content->'review'->>'evidence_ref'
        OR NEW.evidence_digest IS DISTINCT FROM approval.content->'review'->>'evidence_digest'
        OR NEW.reason_code IS NULL OR NEW.reason_code !~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$' THEN
        RAISE EXCEPTION 'Invalid approval receipt' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END''')
    function('source_approval_complete', '', 'trigger', '''
    DECLARE aid uuid; expected_action text;
    BEGIN
      IF TG_TABLE_NAME='source_access_approvals' THEN aid:=NEW.id; expected_action:='install';
      ELSE aid:=NEW.approval_id; expected_action:='revoke'; END IF;
      IF NOT EXISTS(SELECT 1 FROM gateway.source_access_approval_states WHERE approval_id=aid)
        OR NOT EXISTS(SELECT 1 FROM gateway.source_access_admin_events WHERE approval_id=aid AND action=expected_action) THEN
        RAISE EXCEPTION 'Source approval requires a durable receipt' USING ERRCODE='23514'; END IF;
      RETURN NULL;
    END''')
    function('source_approval_install', 'canonical_content text', 'jsonb', '''
    DECLARE value jsonb; canonical text; digest text; approval gateway.source_access_approvals%ROWTYPE;
      previous gateway.source_access_approvals%ROWTYPE; consumer gateway.binding_revisions%ROWTYPE;
      fixture gateway.binding_revisions%ROWTYPE; source_pin jsonb; profile gateway.source_profiles%ROWTYPE;
      context_name text; bid text; package jsonb; installed_at timestamptz;
    BEGIN
      PERFORM gateway.source_require_durability();
      IF NOT gateway.source_approval_owner() OR canonical_content IS NULL OR octet_length(canonical_content) NOT BETWEEN 1 AND 65536 THEN
        RAISE EXCEPTION 'Invalid source approval document' USING ERRCODE='23514'; END IF;
      canonical:=gateway.source_approval_canonical(canonical_content::json);
      IF canonical IS DISTINCT FROM canonical_content THEN RAISE EXCEPTION 'Invalid source approval document' USING ERRCODE='23514'; END IF;
      value:=canonical::jsonb;
      IF NOT gateway.source_approval_document_valid(value) THEN RAISE EXCEPTION 'Invalid source approval document' USING ERRCODE='23514'; END IF;
      digest:=encode(sha256(convert_to('spaceport-source-approval-v1','UTF8')||decode('00','hex')||convert_to(canonical,'UTF8')),'hex');
      PERFORM 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Source registry unavailable' USING ERRCODE='23514'; END IF;
      -- Replay lookup precedes time validation. Expired/revoked exact bytes retain identity.
      SELECT * INTO approval FROM gateway.source_access_approvals a WHERE a.content_digest=digest;
      IF FOUND THEN
        IF approval.canonical_content IS DISTINCT FROM canonical THEN RAISE EXCEPTION 'Invalid source approval document' USING ERRCODE='23514'; END IF;
        RETURN jsonb_build_object('id',approval.id,'revision',approval.revision,'content_digest',approval.content_digest,
          'created_at',approval.created_at,'expires_at',approval.expires_at,'state',(SELECT state FROM gateway.source_access_approval_states WHERE approval_id=approval.id),'replayed',true);
      END IF;
      PERFORM 1 FROM gateway.products WHERE id=value->'product'->>'product_id' FOR UPDATE;
      PERFORM 1 FROM gateway.policies WHERE id=value->'product'->>'policy_id' FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Source package unavailable' USING ERRCODE='23514'; END IF;
      PERFORM 1 FROM gateway.policy_revisions r JOIN gateway.policies p ON (r.policy_id,r.revision)=(p.id,p.current_revision)
        WHERE p.id=value->'product'->>'policy_id' FOR UPDATE OF r;
      SELECT * INTO STRICT consumer FROM gateway.binding_revisions WHERE binding_id=value->'bindings'->'consumer'->>'binding_id'
        AND revision=(value->'bindings'->'consumer'->>'revision')::integer AND content_digest=value->'bindings'->'consumer'->>'content_digest';
      SELECT * INTO STRICT fixture FROM gateway.binding_revisions WHERE binding_id=value->'bindings'->'fixture'->>'binding_id'
        AND revision=(value->'bindings'->'fixture'->>'revision')::integer AND content_digest=value->'bindings'->'fixture'->>'content_digest';
      IF consumer.adapter_revision NOT IN ('cube-bigquery-v1','domo-named-v2') OR fixture.adapter_revision IS DISTINCT FROM consumer.adapter_revision
        OR consumer.id=fixture.id OR consumer.model_bundle_digest IS DISTINCT FROM value->'artifacts'->>'model_bundle_digest'
        OR fixture.model_bundle_digest IS DISTINCT FROM value->'artifacts'->>'fixture_model_bundle_digest' THEN
        RAISE EXCEPTION 'Source package unavailable' USING ERRCODE='23514'; END IF;
      FOREACH context_name IN ARRAY ARRAY['consumer','fixture'] LOOP
        source_pin:=value->'sources'->context_name;
        SELECT * INTO STRICT profile FROM gateway.source_profiles WHERE source_id=source_pin->>'source_id'
          AND revision=(source_pin->>'revision')::integer AND context=source_pin->>'context';
        IF profile.descriptor_digest IS DISTINCT FROM source_pin->>'descriptor_digest'
          OR profile.provider IS DISTINCT FROM source_pin->>'provider'
          OR encode(sha256(convert_to(gateway.source_approval_canonical(gateway.source_approval_physical(profile.descriptor)::json),'UTF8')),'hex')
            IS DISTINCT FROM source_pin->>'physical_identity_digest'
          OR gateway.source_approval_credential_digest(profile.descriptor) IS DISTINCT FROM source_pin->>'credential_binding_digest' THEN
          RAISE EXCEPTION 'Source package unavailable' USING ERRCODE='23514'; END IF;
        IF context_name='consumer' THEN package:=consumer.content; ELSE package:=fixture.content; END IF;
        IF package->>'source_id' IS DISTINCT FROM profile.source_id OR package->'source_revision' IS DISTINCT FROM to_jsonb(profile.revision)
          OR package->>'source_digest' IS DISTINCT FROM profile.descriptor_digest OR package->>'context' IS DISTINCT FROM profile.context THEN
          RAISE EXCEPTION 'Source package unavailable' USING ERRCODE='23514'; END IF;
      END LOOP;
      IF value->>'supersedes_id' IS NOT NULL THEN
        SELECT * INTO STRICT previous FROM gateway.source_access_approvals WHERE id=(value->>'supersedes_id')::uuid;
        IF (value->>'revision')::integer<>previous.revision+1 OR value->'product'->>'product_id' IS DISTINCT FROM previous.product_id
          OR value->'product'->>'version' IS DISTINCT FROM previous.product_version THEN
          RAISE EXCEPTION 'Invalid approval successor' USING ERRCODE='23514'; END IF;
      ELSIF (value->>'revision')::integer<>1 THEN RAISE EXCEPTION 'Invalid approval predecessor' USING ERRCODE='23514'; END IF;
      installed_at:=clock_timestamp();
      IF (value->>'expires_at')::timestamptz<=installed_at THEN RAISE EXCEPTION 'Invalid approval expiry' USING ERRCODE='23514'; END IF;
      package:=value-ARRAY['schema_version','mode','revision','supersedes_id','expires_at','audiences','review'];
      INSERT INTO gateway.source_access_approvals(id,revision,supersedes_id,content,canonical_content,content_digest,created_at,expires_at,
        product_id,product_version,manifest_digest,policy_id,consumer_binding_id,consumer_binding_digest,fixture_binding_id,fixture_binding_digest,
        fixture_catalog_digest,package_pins_digest)
      VALUES(gen_random_uuid(),(value->>'revision')::integer,(value->>'supersedes_id')::uuid,value,canonical,digest,installed_at,(value->>'expires_at')::timestamptz,
        value->'product'->>'product_id',value->'product'->>'version',value->'product'->>'manifest_digest',value->'product'->>'policy_id',
        consumer.id,consumer.content_digest,fixture.id,fixture.content_digest,value->'artifacts'->>'fixture_catalog_digest',
        encode(sha256(convert_to('spaceport-reviewed-package-v1','UTF8')||decode('00','hex')||convert_to(gateway.source_approval_canonical(package::json),'UTF8')),'hex')) RETURNING * INTO approval;
      INSERT INTO gateway.source_access_approval_states VALUES(approval.id,'active',1,installed_at,NULL);
      INSERT INTO gateway.source_access_admin_events VALUES(gen_random_uuid(),approval.id,approval.revision,approval.content_digest,'install',session_user,
        value->'review'->>'review_id',value->'review'->>'evidence_ref',value->'review'->>'evidence_digest',gen_random_uuid(),'installed',clock_timestamp());
      RETURN jsonb_build_object('id',approval.id,'revision',approval.revision,'content_digest',approval.content_digest,
        'created_at',approval.created_at,'expires_at',approval.expires_at,'state','active','replayed',false);
    EXCEPTION WHEN invalid_text_representation OR datetime_field_overflow OR NO_DATA_FOUND THEN
      RAISE EXCEPTION 'Invalid source approval document' USING ERRCODE='23514';
    END''', invoker=True)
    function('source_approval_revoke', 'approval_id uuid, expected_digest text, reason_code text', 'jsonb', '''
    DECLARE approval gateway.source_access_approvals%ROWTYPE; state gateway.source_access_approval_states%ROWTYPE;
    BEGIN
      PERFORM gateway.source_require_durability();
      IF NOT gateway.source_approval_owner() OR approval_id IS NULL OR expected_digest IS NULL OR reason_code IS NULL
        OR expected_digest !~ '^[a-f0-9]{64}$' OR reason_code !~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$' THEN
        RAISE EXCEPTION 'Invalid source approval revocation' USING ERRCODE='23514'; END IF;
      PERFORM 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE;
      SELECT * INTO STRICT approval FROM gateway.source_access_approvals WHERE id=approval_id;
      IF approval.content_digest IS DISTINCT FROM expected_digest THEN RAISE EXCEPTION 'Invalid source approval revocation' USING ERRCODE='23514'; END IF;
      PERFORM 1 FROM gateway.products WHERE id=approval.product_id FOR UPDATE;
      PERFORM 1 FROM gateway.policies WHERE id=approval.policy_id FOR UPDATE;
      PERFORM 1 FROM gateway.policy_revisions r JOIN gateway.policies p ON (r.policy_id,r.revision)=(p.id,p.current_revision)
        WHERE p.id=approval.policy_id FOR UPDATE OF r;
      SELECT * INTO STRICT state FROM gateway.source_access_approval_states s WHERE s.approval_id=approval.id FOR UPDATE;
      IF state.state='revoked' THEN
        IF state.revocation_reason_code IS DISTINCT FROM reason_code THEN RAISE EXCEPTION 'Revocation receipt differs' USING ERRCODE='23514'; END IF;
        RETURN jsonb_build_object('id',approval.id,'state','revoked','state_revision',2,'replayed',true);
      END IF;
      UPDATE gateway.source_access_approval_states SET state='revoked',state_revision=2,revocation_reason_code=reason_code WHERE source_access_approval_states.approval_id=approval.id;
      INSERT INTO gateway.source_access_admin_events VALUES(gen_random_uuid(),approval.id,approval.revision,approval.content_digest,'revoke',session_user,
        approval.content->'review'->>'review_id',approval.content->'review'->>'evidence_ref',approval.content->'review'->>'evidence_digest',gen_random_uuid(),reason_code,clock_timestamp());
      RETURN jsonb_build_object('id',approval.id,'state','revoked','state_revision',2,'replayed',false);
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Invalid source approval revocation' USING ERRCODE='23514';
    END''', invoker=True)


    function('source_approval_projection', 'approval_id uuid', 'jsonb', '''
    DECLARE approval gateway.source_access_approvals%ROWTYPE; projection jsonb;
    BEGIN
      IF approval_id IS NULL THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      SELECT * INTO STRICT approval FROM gateway.source_access_approvals WHERE id=approval_id;
      projection:=approval.content-ARRAY['schema_version','mode','revision','supersedes_id','review']
        ||jsonb_build_object('approval_id',approval.id,'approval_revision',approval.revision,'approval_digest',approval.content_digest);
      RETURN projection||jsonb_build_object('projection_digest',encode(sha256(convert_to('spaceport-source-projection-v1','UTF8')
        ||decode('00','hex')||convert_to(gateway.source_approval_canonical(projection::json),'UTF8')),'hex'));
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501';
    END''')
    function('source_approval_read', 'approval_id uuid', 'jsonb', '''
    BEGIN RETURN gateway.source_approval_projection(approval_id); END''')
    function('source_approval_candidates', 'product_id text, product_version text, manifest_digest text', 'SETOF jsonb', '''
    DECLARE approval gateway.source_access_approvals%ROWTYPE; count integer:=0;
    BEGIN
      IF product_id IS NULL OR product_version IS NULL OR manifest_digest IS NULL
        OR product_id !~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$' OR length(product_version) NOT BETWEEN 5 AND 64
        OR manifest_digest !~ '^[a-f0-9]{64}$' THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      FOR approval IN SELECT a.* FROM gateway.source_access_approvals a JOIN gateway.source_access_approval_states s ON s.approval_id=a.id
        WHERE a.product_id=source_approval_candidates.product_id AND a.product_version=source_approval_candidates.product_version
          AND a.manifest_digest=source_approval_candidates.manifest_digest AND s.state='active' AND s.state_revision=1
          AND a.expires_at>clock_timestamp() ORDER BY a.id LIMIT 101 LOOP
        count:=count+1;
        IF count>100 THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
        RETURN NEXT gateway.source_approval_projection(approval.id);
      END LOOP;
      RETURN;
    END''')

    function('source_approval_check_packages', 'expected jsonb', 'boolean', '''
    DECLARE item jsonb; approval gateway.source_access_approvals%ROWTYPE; ids uuid[]:=ARRAY[]::uuid[];
      aid uuid; checked_at timestamptz;
    BEGIN
      IF (jsonb_typeof(expected)='array' AND jsonb_array_length(expected) BETWEEN 1 AND 100
        AND octet_length(expected::text)<=65536) IS NOT TRUE THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      FOR item IN SELECT v FROM jsonb_array_elements(expected) a(v) LOOP
        IF NOT gateway.source_approval_keys(item,ARRAY['approval_digest','package_pins_digest','product_id','version','manifest_digest'])
          OR (jsonb_typeof(item->'approval_digest')='string' AND item->>'approval_digest' ~ '^[a-f0-9]{64}$'
            AND jsonb_typeof(item->'package_pins_digest')='string' AND item->>'package_pins_digest' ~ '^[a-f0-9]{64}$'
            AND jsonb_typeof(item->'product_id')='string' AND item->>'product_id' ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'
            AND jsonb_typeof(item->'version')='string' AND length(item->>'version') BETWEEN 5 AND 64
            AND jsonb_typeof(item->'manifest_digest')='string' AND item->>'manifest_digest' ~ '^[a-f0-9]{64}$') IS NOT TRUE THEN
          RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      END LOOP;
      -- Readiness has no product/data mutation: its complete lock prefix is C then sorted A.
      PERFORM 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      FOR item IN SELECT v FROM jsonb_array_elements(expected) a(v) LOOP
        SELECT * INTO STRICT approval FROM gateway.source_access_approvals a WHERE a.content_digest=item->>'approval_digest'
          AND a.package_pins_digest=item->>'package_pins_digest' AND a.product_id=item->>'product_id'
          AND a.product_version=item->>'version' AND a.manifest_digest=item->>'manifest_digest';
        ids:=array_append(ids,approval.id);
      END LOOP;
      FOR aid IN SELECT DISTINCT v FROM unnest(ids) a(v) ORDER BY v LOOP
        SELECT * INTO STRICT approval FROM gateway.source_access_approvals WHERE id=aid;
        PERFORM gateway.source_approval_assert(approval.id,approval.revision,approval.content_digest);
      END LOOP;
      -- Locks block revocation, not time. One final instant covers every configured package.
      checked_at:=clock_timestamp();
      IF EXISTS(SELECT 1 FROM gateway.source_access_approvals a JOIN gateway.source_access_approval_states s ON s.approval_id=a.id
        WHERE a.id=ANY(ids) AND (a.expires_at>checked_at AND s.state='active' AND s.state_revision=1) IS NOT TRUE) THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      RETURN true;
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501';
    END''')
    function('source_approval_scope_fingerprint', '', 'text', '''
    DECLARE value text; checked_at timestamptz:=clock_timestamp();
    BEGIN
      SELECT coalesce(string_agg(a.id::text||':'||a.revision::text||':'||a.content_digest||':'||s.state_revision::text,',' ORDER BY a.id),'')
        INTO value FROM gateway.source_access_approvals a JOIN gateway.source_access_approval_states s ON s.approval_id=a.id
        WHERE s.state='active' AND s.state_revision=1 AND a.expires_at>checked_at;
      RETURN encode(sha256(convert_to('spaceport-source-scope-v1','UTF8')||decode('00','hex')||convert_to(value,'UTF8')),'hex');
    END''')


def _consumer_functions(configured):
    # Canonical text is a bounded byte witness, not a PostgreSQL ECMAScript
    # serializer. A distinct protected digest and JSON equality are mandatory.
    op.execute('ALTER TABLE gateway.product_versions ADD COLUMN manifest_canonical TEXT')
    op.execute('ALTER TABLE gateway.executions ADD COLUMN query_canonical TEXT, ADD COLUMN source_access_generation SMALLINT NOT NULL DEFAULT 0 CHECK(source_access_generation IN (0,1))')
    op.execute('''CREATE TABLE gateway.execution_source_authorities (
      execution_id VARCHAR(128) PRIMARY KEY REFERENCES gateway.execution_authorizations(execution_id),
      approval_id UUID NOT NULL, approval_revision INTEGER NOT NULL, approval_digest CHAR(64) NOT NULL,
      audience_entry_id VARCHAR(128) NOT NULL, principal_snapshot JSONB NOT NULL,
      principal_digest CHAR(64) NOT NULL, package_pins_digest CHAR(64) NOT NULL,
      accepted_approval_expires_at TIMESTAMPTZ NOT NULL, effective_deadline_at TIMESTAMPTZ NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY(approval_id,approval_revision,approval_digest) REFERENCES gateway.source_access_approvals(id,revision,content_digest),
      CHECK(principal_digest ~ '^[a-f0-9]{64}$' AND package_pins_digest ~ '^[a-f0-9]{64}$'),
      CHECK(jsonb_typeof(principal_snapshot)='object' AND octet_length(principal_snapshot::text)<=8192),
      CHECK(effective_deadline_at<=accepted_approval_expires_at))''')
    function('source_json_unique', 'value json, depth integer DEFAULT 0', 'boolean', '''
    DECLARE item record;
    BEGIN
      IF value IS NULL OR depth IS NULL OR depth<0 OR depth>16 THEN RETURN false; END IF;
      IF json_typeof(value)='object' THEN
        IF (SELECT count(*)>4096 OR count(*)<>count(DISTINCT key) FROM json_each(value)) THEN RETURN false; END IF;
        FOR item IN SELECT key,v FROM json_each(value) e(key,v) LOOP
          IF octet_length(item.key)>512 OR NOT gateway.source_json_unique(item.v,depth+1) THEN RETURN false; END IF;
        END LOOP;
      ELSIF json_typeof(value)='array' THEN
        IF json_array_length(value)>4096 THEN RETURN false; END IF;
        FOR item IN SELECT v FROM json_array_elements(value) e(v) LOOP
          IF NOT gateway.source_json_unique(item.v,depth+1) THEN RETURN false; END IF;
        END LOOP;
      END IF;
      RETURN true;
    END''', immutable=True)
    function('source_json_witness', 'canonical text, value jsonb, expected_digest text', 'boolean', '''
    BEGIN
      IF canonical IS NULL OR value IS NULL OR expected_digest IS NULL
        OR octet_length(canonical) NOT BETWEEN 1 AND 65536 OR expected_digest !~ '^[a-f0-9]{64}$' THEN RETURN false; END IF;
      RETURN (gateway.source_json_unique(canonical::json) AND canonical::jsonb=value
        AND encode(sha256(convert_to(canonical,'UTF8')),'hex')=expected_digest) IS TRUE;
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END''', immutable=True)
    function('source_guard_manifest_witness', '', 'trigger', '''
    BEGIN
      IF NEW.manifest_canonical IS NOT NULL AND NOT gateway.source_json_witness(NEW.manifest_canonical,
          NEW.manifest-'content_digest',NEW.content_digest) THEN
        RAISE EXCEPTION 'Invalid protected manifest witness' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END''')
    op.execute('CREATE TRIGGER source_manifest_witness BEFORE INSERT ON gateway.product_versions FOR EACH ROW EXECUTE FUNCTION gateway.source_guard_manifest_witness()')
    # Replace the historical acceptance function without changing old migrations.
    function('set_execution_acceptance', '', 'trigger', '''
    DECLARE accepted timestamptz; version gateway.product_versions%ROWTYPE; policy gateway.policy_revisions%ROWTYPE;
      current_revision integer; product_seconds integer; policy_seconds integer; adapter text;
    BEGIN
      SELECT v.* INTO STRICT version FROM gateway.product_versions v WHERE v.id=NEW.version_id AND v.product_id=NEW.product_id;
      SELECT adapter_revision INTO STRICT adapter FROM gateway.binding_revisions WHERE id=version.binding_id;
      NEW.source_access_generation:=CASE WHEN adapter IN ('cube-bigquery-v1','domo-named-v2') THEN 1 ELSE 0 END;
      IF NEW.source_access_generation=1 THEN
        PERFORM 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
        PERFORM 1 FROM gateway.products WHERE id=NEW.product_id FOR UPDATE;
        IF NEW.context IS DISTINCT FROM 'consumer' OR NOT gateway.source_json_witness(NEW.query_canonical,NEW.canonical_query,NEW.canonical_hash)
          OR NOT gateway.source_json_witness(version.manifest_canonical,version.manifest-'content_digest',version.content_digest) THEN
          RAISE EXCEPTION 'Invalid protected source witness' USING ERRCODE='23514'; END IF;
      END IF;
      SELECT p.current_revision INTO STRICT current_revision FROM gateway.policies p WHERE p.id=NEW.policy_id FOR UPDATE;
      IF current_revision IS DISTINCT FROM NEW.policy_revision OR version.policy_id IS DISTINCT FROM NEW.policy_id THEN
        RAISE EXCEPTION 'The execution requires current policy authority' USING ERRCODE='23514'; END IF;
      SELECT * INTO STRICT policy FROM gateway.policy_revisions WHERE policy_id=NEW.policy_id AND revision=current_revision;
      IF (jsonb_typeof(policy.content->'rules')='object'
        AND (version.manifest->'limits' IS NULL OR jsonb_typeof(version.manifest->'limits')='object')
        AND (policy.content->'rules'->'limits' IS NULL OR jsonb_typeof(policy.content->'rules'->'limits')='object')
        AND coalesce(policy.content->'rules'->'limits','{}'::jsonb)-ARRAY['max_rows','max_bytes','page_size',
          'per_principal_concurrency','deployment_concurrency','max_date_range_days','sync_wait_seconds','execution_timeout_seconds']='{}'::jsonb
        AND coalesce(version.manifest->'limits'->'execution_timeout_seconds','120'::jsonb)::text ~ '^[0-9]{1,3}$'
        AND coalesce(policy.content->'rules'->'limits'->'execution_timeout_seconds','120'::jsonb)::text ~ '^[0-9]{1,3}$') IS NOT TRUE THEN
        RAISE EXCEPTION 'Malformed execution limit representation' USING ERRCODE='23514'; END IF;
      product_seconds:=coalesce((version.manifest->'limits'->>'execution_timeout_seconds')::integer,120);
      policy_seconds:=coalesce((policy.content->'rules'->'limits'->>'execution_timeout_seconds')::integer,120);
      IF product_seconds NOT BETWEEN 1 AND 120 OR policy_seconds NOT BETWEEN 1 AND 120 OR NEW.execution_timeout_seconds IS NULL
        OR NEW.execution_timeout_seconds NOT BETWEEN 1 AND 120 THEN
        RAISE EXCEPTION 'Execution time limit is outside bounds' USING ERRCODE='23514'; END IF;
      NEW.execution_timeout_seconds:=least(NEW.execution_timeout_seconds,product_seconds,policy_seconds,120);
      accepted:=clock_timestamp(); NEW.created_at:=accepted; NEW.deadline_at:=accepted+make_interval(secs=>NEW.execution_timeout_seconds);
      RETURN NEW;
    END''')
    function('source_current_grants_valid', 'principal_id text', 'boolean', '''
    DECLARE grant_row gateway.principal_grants%ROWTYPE; item record; scalar jsonb; nodes integer;
    BEGIN
      IF (SELECT count(*) FROM gateway.principal_grants g WHERE g.principal_id=source_current_grants_valid.principal_id AND active)>1000 THEN RETURN false; END IF;
      FOR grant_row IN SELECT g.* FROM gateway.principal_grants g WHERE g.principal_id=source_current_grants_valid.principal_id AND active LOOP
        IF grant_row.policy_id !~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$' OR grant_row.tenant_id !~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'
          OR jsonb_typeof(grant_row.roles) IS DISTINCT FROM 'array' OR jsonb_array_length(grant_row.roles)>6
          OR (SELECT count(*)<>count(DISTINCT v) FROM jsonb_array_elements(grant_row.roles) a(v))
          OR EXISTS(SELECT 1 FROM jsonb_array_elements(grant_row.roles) a(v) WHERE jsonb_typeof(v)<>'string' OR v#>>'{}' NOT IN ('consumer','author','publisher','operator','policy_admin','audit_view'))
          OR jsonb_typeof(grant_row.entitlements) IS DISTINCT FROM 'object'
          OR (SELECT count(*) FROM jsonb_each(grant_row.entitlements))>50 THEN RETURN false; END IF;
        nodes:=1;
        FOR item IN SELECT key,value FROM jsonb_each(grant_row.entitlements) LOOP
          IF item.key !~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$' OR jsonb_typeof(item.value) IS DISTINCT FROM 'array'
            OR jsonb_array_length(item.value) NOT BETWEEN 1 AND 100
            OR (SELECT count(*)<>count(DISTINCT CASE WHEN v='true'::jsonb THEN '1'::jsonb WHEN v='false'::jsonb THEN '0'::jsonb ELSE v END) FROM jsonb_array_elements(item.value) a(v)) THEN RETURN false; END IF;
          nodes:=nodes+1+jsonb_array_length(item.value);
          IF nodes>4096 THEN RETURN false; END IF;
          FOR scalar IN SELECT v FROM jsonb_array_elements(item.value) a(v) LOOP
            IF jsonb_typeof(scalar) NOT IN ('string','number','boolean')
              OR (jsonb_typeof(scalar)='string' AND length(scalar#>>'{}')>4096)
              OR (jsonb_typeof(scalar)='number' AND abs((scalar#>>'{}')::numeric)>9007199254740991) THEN RETURN false; END IF;
          END LOOP;
        END LOOP;
      END LOOP;
      RETURN true;
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END''')
    function('source_current_decision_or_denial', 'principal jsonb, manifest jsonb, action text', 'jsonb', '''
    DECLARE identity gateway.principals%ROWTYPE; policy gateway.policy_revisions%ROWTYPE; rules jsonb;
      grant_row gateway.principal_grants%ROWTYPE; grants integer; needed_role text; constraints jsonb; values jsonb;
      limits jsonb:='{}'::jsonb; defaults jsonb:='{"max_rows": 1000,"max_bytes": 1048576,"page_size": 200,"per_principal_concurrency": 2,"deployment_concurrency": 10,"max_date_range_days": 366,"sync_wait_seconds": 5,"execution_timeout_seconds": 120}';
      item record; p integer; m integer; source_limits jsonb;
    BEGIN
      IF NOT gateway.source_approval_keys(principal,ARRAY['id','subject','kind','service_client_id'])
        OR (jsonb_typeof(principal->'id')='string' AND principal->>'id' ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'
          AND jsonb_typeof(principal->'subject')='string' AND length(principal->>'subject') BETWEEN 1 AND 512
          AND principal->>'kind' IN ('user','service','demo')
          AND (principal->'service_client_id'='null'::jsonb OR (jsonb_typeof(principal->'service_client_id')='string'
            AND principal->>'service_client_id' ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'))) IS NOT TRUE THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      SELECT * INTO STRICT identity FROM gateway.principals WHERE id=principal->>'id' AND active;
      IF identity.kind IS DISTINCT FROM principal->>'kind' OR (identity.kind='service' AND to_jsonb(identity.service_client_id) IS DISTINCT FROM principal->'service_client_id') THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      IF NOT gateway.source_current_grants_valid(identity.id) THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      needed_role:=CASE WHEN action IN ('discover','describe','execute','result') THEN 'consumer' WHEN action='author' THEN 'author' WHEN action='publish' THEN 'publisher' END;
      IF needed_role IS NULL THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      SELECT r.* INTO STRICT policy FROM gateway.policies p JOIN gateway.policy_revisions r ON (r.policy_id,r.revision)=(p.id,p.current_revision) WHERE p.id=manifest->>'policy_id';
      rules:=jsonb_build_object('limits','{}'::jsonb)||(policy.content->'rules');
      IF encode(sha256(convert_to(gateway.source_approval_canonical(policy.content::json),'UTF8')),'hex') IS DISTINCT FROM policy.content_digest
        OR NOT gateway.source_approval_keys(policy.content,ARRAY['rules','reason'])
        OR (jsonb_typeof(policy.content->'reason')='string' AND length(policy.content->>'reason') BETWEEN 1 AND 1024
          AND btrim(policy.content->>'reason')<>'' AND policy.content->>'reason' !~ '[[:cntrl:]]') IS NOT TRUE
        OR NOT gateway.source_approval_keys(rules,ARRAY['tenant_ids','tenant_constraint','entitlement_constraint','visible_fields','limits'])
        OR NOT gateway.source_approval_keys(rules->'tenant_constraint',ARRAY['member','operator','source'])
        OR NOT gateway.source_approval_keys(rules->'entitlement_constraint',ARRAY['member','operator','source'])
        OR (jsonb_typeof(rules->'tenant_constraint'->'member')='string' AND rules->'tenant_constraint'->>'member' ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'
          AND jsonb_typeof(rules->'entitlement_constraint'->'member')='string' AND rules->'entitlement_constraint'->>'member' ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$') IS NOT TRUE
        OR jsonb_typeof(rules->'tenant_ids') IS DISTINCT FROM 'array' OR jsonb_array_length(rules->'tenant_ids')>100
        OR (SELECT count(*)<>count(DISTINCT v) FROM jsonb_array_elements(rules->'tenant_ids') a(v))
        OR EXISTS(SELECT 1 FROM jsonb_array_elements(rules->'tenant_ids') a(v) WHERE jsonb_typeof(v)<>'string' OR v#>>'{}' !~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
        OR jsonb_typeof(rules->'limits') IS DISTINCT FROM 'object'
        OR (rules->'limits')-ARRAY['max_rows','max_bytes','page_size','per_principal_concurrency','deployment_concurrency','max_date_range_days','sync_wait_seconds','execution_timeout_seconds']<>'{}'::jsonb
        OR rules->'tenant_constraint'->>'operator' IS DISTINCT FROM 'eq' OR rules->'tenant_constraint'->>'source' IS DISTINCT FROM 'grant.tenant_id'
        OR rules->'entitlement_constraint'->>'operator' IS DISTINCT FROM 'in'
        OR rules->'entitlement_constraint'->>'source' !~ '^grant\\.entitlements\\.[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'
        OR rules->'tenant_constraint'->>'member'=rules->'entitlement_constraint'->>'member' THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      IF jsonb_typeof(manifest->'outputs') IS DISTINCT FROM 'object'
        OR NOT gateway.source_approval_keys(manifest->'limits',ARRAY['max_rows','max_bytes','page_size','per_principal_concurrency','deployment_concurrency','max_date_range_days','sync_wait_seconds','execution_timeout_seconds']) THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      IF jsonb_typeof(rules->'visible_fields') IS DISTINCT FROM 'array' OR jsonb_array_length(rules->'visible_fields') NOT BETWEEN 1 AND 50
        OR (SELECT count(*)<>count(DISTINCT v) FROM jsonb_array_elements(rules->'visible_fields') a(v))
        OR EXISTS(SELECT 1 FROM jsonb_array_elements(rules->'visible_fields') f(v) WHERE jsonb_typeof(v)<>'string' OR NOT (manifest->'outputs' ? (v#>>'{}'))) THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      FOR item IN SELECT key,value FROM jsonb_each(defaults) LOOP
        IF (coalesce(rules->'limits'->item.key,item.value)::text ~ '^[0-9]{1,8}$'
          AND coalesce(manifest->'limits'->item.key,item.value)::text ~ '^[0-9]{1,8}$') IS NOT TRUE THEN
          RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
        p:=coalesce((rules->'limits'->>item.key)::integer,item.value::text::integer);
        m:=coalesce((manifest->'limits'->>item.key)::integer,item.value::text::integer);
        IF p<(CASE WHEN item.key='sync_wait_seconds' THEN 0 ELSE 1 END) OR m<(CASE WHEN item.key='sync_wait_seconds' THEN 0 ELSE 1 END)
          OR p>item.value::text::integer OR m>item.value::text::integer THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
        limits:=limits||jsonb_build_object(item.key,least(p,m));
      END LOOP;
      FOR source_limits IN SELECT v FROM jsonb_array_elements(jsonb_build_array(rules->'limits',manifest->'limits')) a(v) LOOP
        IF coalesce((source_limits->>'page_size')::integer,200)>coalesce((source_limits->>'max_rows')::integer,1000)
          OR coalesce((source_limits->>'per_principal_concurrency')::integer,2)>coalesce((source_limits->>'deployment_concurrency')::integer,10)
          OR coalesce((source_limits->>'sync_wait_seconds')::integer,5)>coalesce((source_limits->>'execution_timeout_seconds')::integer,120) THEN
          RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      END LOOP;
      SELECT count(*) INTO grants FROM gateway.principal_grants g WHERE g.principal_id=identity.id AND g.policy_id=policy.policy_id
        AND g.active AND rules->'tenant_ids' ? g.tenant_id;
      IF grants<>1 THEN RETURN NULL; END IF;
      SELECT * INTO STRICT grant_row FROM gateway.principal_grants g WHERE g.principal_id=identity.id AND g.policy_id=policy.policy_id
        AND g.active AND rules->'tenant_ids' ? g.tenant_id;
      IF NOT (grant_row.roles ? needed_role) THEN RETURN NULL; END IF;
      values:=grant_row.entitlements->substring(rules->'entitlement_constraint'->>'source' FROM 20);
      constraints:=jsonb_build_array(jsonb_build_object('member',rules->'tenant_constraint'->>'member','operator','eq','values',jsonb_build_array(grant_row.tenant_id)));
      IF values IS NOT NULL THEN
        IF jsonb_typeof(values) IS DISTINCT FROM 'array' OR jsonb_array_length(values) NOT BETWEEN 1 AND 100
          OR NOT gateway.source_approval_unique_scalars(values) THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
        constraints:=constraints||jsonb_build_array(jsonb_build_object('member',rules->'entitlement_constraint'->>'member','operator','in','values',values));
      ELSIF action IN ('discover','describe','execute','result') THEN RETURN NULL; END IF;
      RETURN jsonb_build_object('principal_id',identity.id,'policy_revision',policy.revision,'tenant_id',grant_row.tenant_id,
        'row_constraints',constraints,'visible_fields',rules->'visible_fields','limits',limits);
    EXCEPTION WHEN NO_DATA_FOUND OR invalid_text_representation THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501';
    END''')
    function('source_current_decision', 'principal jsonb, manifest jsonb, action text', 'jsonb', '''
    DECLARE decision jsonb;
    BEGIN
      decision:=gateway.source_current_decision_or_denial(principal,manifest,action);
      IF decision IS NULL THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      RETURN decision;
    END''')
    function('source_complete_audience', 'document jsonb, principal jsonb, decision jsonb, action text', 'text', '''
    DECLARE entry jsonb; matches integer:=0; matched text; row_value jsonb; approved jsonb;
    BEGIN
      FOR entry IN SELECT v FROM jsonb_array_elements(document->'audiences') a(v) LOOP
        IF (entry->>'context'='consumer' AND entry->>'principal_id'=principal->>'id' AND entry->>'principal_kind'=principal->>'kind'
          AND principal->'service_client_id'<>'null'::jsonb AND entry->'client_ids' @> jsonb_build_array(principal->'service_client_id')
          AND entry->'actions' ? action AND entry->>'tenant_id'=decision->>'tenant_id' AND decision->>'principal_id'=principal->>'id'
          AND (entry->'visible_fields') @> (decision->'visible_fields') AND entry->'fixture_case_ids'='[]'::jsonb
          AND jsonb_array_length(entry->'row_constraints')=jsonb_array_length(decision->'row_constraints')) IS NOT TRUE THEN CONTINUE; END IF;
        FOR row_value IN SELECT v FROM jsonb_array_elements(decision->'row_constraints') a(v) LOOP
          SELECT v INTO approved FROM jsonb_array_elements(entry->'row_constraints') a(v)
            WHERE v->'member'=row_value->'member' AND v->'operator'=row_value->'operator';
          IF approved IS NULL OR NOT (approved->'values') @> (row_value->'values') THEN EXIT; END IF;
        END LOOP;
        IF approved IS NULL OR NOT (approved->'values') @> (row_value->'values') THEN CONTINUE; END IF;
        matches:=matches+1; matched:=entry->>'id';
      END LOOP;
      IF matches<>1 THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      RETURN matched;
    END''')
    function('source_domain_contains', 'domain jsonb, value jsonb', 'boolean', '''
    DECLARE kind text:=domain->>'type'; s text:=value#>>'{}'; lo text:=domain->>'minimum'; hi text:=domain->>'maximum';
    BEGIN
      IF value IS NULL THEN RETURN false; END IF;
      IF value='null'::jsonb THEN RETURN domain->'nullable'='true'::jsonb; END IF;
      IF kind='number' THEN
        IF jsonb_typeof(value)<>'number' THEN RETURN false; END IF;
      ELSIF kind='decimal' THEN
        IF jsonb_typeof(value)<>'string' OR s !~ '^-?(0|[1-9][0-9]*)(\\.[0-9]+)?$' THEN RETURN false; END IF;
      ELSIF NOT gateway.source_approval_domain_type(kind,value,false) THEN RETURN false; END IF;
      IF domain->>'kind'='enum' THEN RETURN domain->'values' @> jsonb_build_array(value); END IF;
      IF kind IN ('number','decimal','integer') THEN RETURN s::numeric BETWEEN lo::numeric AND hi::numeric;
      ELSIF kind='date' THEN RETURN s::date BETWEEN lo::date AND hi::date;
      ELSIF kind='timestamp' THEN RETURN s::timestamptz BETWEEN lo::timestamptz AND hi::timestamptz;
      END IF;
      RETURN false;
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END''', immutable=True)
    function('source_field_value', 'definition jsonb, value jsonb', 'boolean', '''
    DECLARE kind text:=definition->>'type'; s text:=value#>>'{}'; fractional integer; whole integer;
    BEGIN
      IF definition IS NULL OR jsonb_typeof(definition) IS DISTINCT FROM 'object' OR value IS NULL THEN RETURN false; END IF;
      IF value='null'::jsonb THEN RETURN definition->'nullable'='true'::jsonb; END IF;
      IF kind IN ('string','date','timestamp','decimal') THEN
        IF jsonb_typeof(value)<>'string' THEN RETURN false; END IF;
        IF kind='date' AND (s !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' OR to_char(s::date,'YYYY-MM-DD')<>s) THEN RETURN false; END IF;
        IF kind='timestamp' THEN PERFORM s::timestamptz; END IF;
        IF kind='decimal' THEN
          IF s !~ '^-?(0|[1-9][0-9]*)(\\.[0-9]+)?$' THEN RETURN false; END IF;
          fractional:=CASE WHEN position('.' IN s)>0 THEN length(split_part(s,'.',2)) ELSE 0 END;
          whole:=CASE WHEN s::numeric=0 THEN 0 ELSE length(ltrim(split_part(ltrim(s,'-'),'.',1),'0')) END;
          IF fractional>(definition->>'scale')::integer OR whole>(definition->>'precision')::integer-(definition->>'scale')::integer THEN RETURN false; END IF;
        END IF;
      ELSIF kind='integer' THEN
        IF jsonb_typeof(value)<>'number' OR s !~ '^-?(0|[1-9][0-9]*)$' OR abs(s::numeric)>9007199254740991 THEN RETURN false; END IF;
      ELSIF kind='number' THEN
        IF jsonb_typeof(value)<>'number' OR abs(s::numeric)>1.7976931348623157e308::numeric THEN RETURN false; END IF;
      ELSIF kind='boolean' THEN
        IF jsonb_typeof(value)<>'boolean' THEN RETURN false; END IF;
      ELSE RETURN false; END IF;
      IF definition->'enum'<>'null'::jsonb AND NOT (definition->'enum') @> jsonb_build_array(value) THEN RETURN false; END IF;
      RETURN true;
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END''', immutable=True)
    function('source_query_reviewed', 'document jsonb, manifest jsonb, query jsonb, limits jsonb DEFAULT NULL', 'boolean', '''
    DECLARE item record; domain jsonb; clause jsonb; value jsonb; required record; span integer;
    BEGIN
      IF NOT gateway.source_approval_keys(query,ARRAY['product_id','version','inputs','filters','sort'])
        OR query->'product_id' IS DISTINCT FROM manifest->'product_id' OR query->'version' IS DISTINCT FROM manifest->'version'
        OR jsonb_typeof(query->'inputs') IS DISTINCT FROM 'object' OR jsonb_typeof(query->'filters') IS DISTINCT FROM 'array'
        OR jsonb_typeof(query->'sort') IS DISTINCT FROM 'array' OR jsonb_array_length(query->'filters')>20
        OR jsonb_array_length(query->'sort')>least(5,(manifest->'sort'->>'max_fields')::integer)
        OR (SELECT count(*) FROM jsonb_each(query->'inputs'))>50 THEN RETURN false; END IF;
      IF manifest->'date_boundaries'<>'null'::jsonb THEN
        span:=(query->'inputs'->>(manifest->'date_boundaries'->>'end_input'))::date-(query->'inputs'->>(manifest->'date_boundaries'->>'start_input'))::date;
        IF (span>0 AND span<=least((manifest->'limits'->>'max_date_range_days')::integer,coalesce((limits->>'max_date_range_days')::integer,366))) IS NOT TRUE THEN RETURN false; END IF;
      END IF;
      IF (SELECT count(*)<>count(DISTINCT v->>'field') FROM jsonb_array_elements(query->'sort') a(v)) THEN RETURN false; END IF;
      FOR clause IN SELECT v FROM jsonb_array_elements(query->'sort') a(v) LOOP
        IF NOT gateway.source_approval_keys(clause,ARRAY['field','direction'])
          OR (jsonb_typeof(clause->'field')='string' AND (manifest->'sort'->'fields') ? (clause->>'field')
            AND clause->>'direction' IN ('asc','desc')) IS NOT TRUE THEN RETURN false; END IF;
      END LOOP;
      FOR required IN SELECT e.key,e.value FROM jsonb_each(manifest->'inputs') e LOOP
        IF ((required.value->'required'='true'::jsonb OR required.value->'default'<>'null'::jsonb) AND NOT query->'inputs' ? required.key) THEN RETURN false; END IF;
      END LOOP;
      FOR item IN SELECT e.key,e.value FROM jsonb_each(query->'inputs') e LOOP
        SELECT v INTO domain FROM jsonb_array_elements(document->'input_domains') a(v) WHERE v->>'name'=item.key;
        IF domain IS NULL OR NOT gateway.source_domain_contains(domain,item.value) OR NOT gateway.source_field_value(manifest->'inputs'->item.key,item.value) THEN RETURN false; END IF;
      END LOOP;
      FOR clause IN SELECT v FROM jsonb_array_elements(query->'filters') a(v) LOOP
        SELECT v INTO domain FROM jsonb_array_elements(document->'filter_domains') a(v) WHERE v->>'name'=clause->>'field';
        IF NOT gateway.source_approval_keys(clause,ARRAY['field','operator','value']) OR domain IS NULL
          OR NOT (manifest->'filters'->(clause->>'field')->'operators' ? (clause->>'operator')) THEN RETURN false; END IF;
        IF clause->>'operator'='is_null' THEN
          IF clause->'value'<>'null'::jsonb OR domain->'nullable'<>'true'::jsonb THEN RETURN false; END IF;
        ELSIF clause->>'operator' IN ('in','not_in') THEN
          IF jsonb_typeof(clause->'value')<>'array' OR jsonb_array_length(clause->'value') NOT BETWEEN 1 AND least(100,(manifest->'filters'->(clause->>'field')->>'max_values')::integer) THEN RETURN false; END IF;
          IF (SELECT count(*)<>count(DISTINCT v) FROM jsonb_array_elements(clause->'value') a(v)) THEN RETURN false; END IF;
          FOR value IN SELECT v FROM jsonb_array_elements(clause->'value') a(v) LOOP
            IF NOT gateway.source_domain_contains(domain,value) OR NOT gateway.source_field_value(manifest->'outputs'->(clause->>'field'),value) THEN RETURN false; END IF;
          END LOOP;
        ELSIF NOT gateway.source_domain_contains(domain,clause->'value') OR NOT gateway.source_field_value(manifest->'outputs'->(clause->>'field'),clause->'value') THEN RETURN false; END IF;
      END LOOP;
      RETURN true;
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END''', immutable=True)
    function('source_execution_package', 'execution_id text, approval_id uuid', 'boolean', '''
    DECLARE execution gateway.executions%ROWTYPE; version gateway.product_versions%ROWTYPE; approval gateway.source_access_approvals%ROWTYPE;
    BEGIN
      SELECT * INTO STRICT execution FROM gateway.executions WHERE id=execution_id;
      SELECT * INTO STRICT version FROM gateway.product_versions WHERE id=execution.version_id;
      SELECT * INTO STRICT approval FROM gateway.source_access_approvals WHERE id=approval_id;
      RETURN (execution.source_access_generation=1 AND execution.context='consumer'
        AND (execution.product_id,execution.policy_id)=(version.product_id,version.policy_id)
        AND (approval.product_id,approval.product_version,approval.manifest_digest,approval.policy_id,
          approval.consumer_binding_id,approval.consumer_binding_digest)=(version.product_id,version.version,version.content_digest,
          version.policy_id,version.binding_id,version.binding_digest)
        AND version.manifest->>'binding_id'=approval.content->'bindings'->'consumer'->>'binding_id'
        AND version.manifest->'binding_revision'=approval.content->'bindings'->'consumer'->'revision'
        AND version.manifest->>'model_bundle_digest'=approval.content->'artifacts'->>'model_bundle_digest'
        AND version.manifest->>'adapter_revision'=(SELECT adapter_revision FROM gateway.binding_revisions WHERE id=version.binding_id)
        AND gateway.source_approval_package_valid(approval.content)
        AND gateway.source_json_witness(version.manifest_canonical,version.manifest-'content_digest',version.content_digest)
        AND gateway.source_json_witness(execution.query_canonical,execution.canonical_query,execution.canonical_hash)
        AND gateway.source_query_reviewed(approval.content,version.manifest,execution.canonical_query,(SELECT au.decision->'limits' FROM gateway.execution_authorizations au WHERE au.execution_id=execution.id))) IS TRUE;
    EXCEPTION WHEN NO_DATA_FOUND THEN RETURN false;
    END''')
    function('source_decision_scope', 'decision jsonb', 'text', '''
    DECLARE constraints jsonb; row_value jsonb; values jsonb; items jsonb:='[]'::jsonb;
    BEGIN
      FOR row_value IN SELECT v FROM jsonb_array_elements(decision->'row_constraints') a(v) ORDER BY v->>'member' COLLATE "C" LOOP
        SELECT jsonb_agg(v ORDER BY gateway.source_approval_canonical(v::json) COLLATE "C") INTO values
          FROM (SELECT DISTINCT v FROM jsonb_array_elements(row_value->'values') a(v)) unique_values;
        items:=items||jsonb_build_array(jsonb_build_object('member',row_value->'member','values',values));
      END LOOP;
      RETURN encode(sha256(convert_to(gateway.source_approval_canonical(jsonb_build_object('principal_id',decision->'principal_id',
        'policy_revision',decision->'policy_revision','tenant_id',decision->'tenant_id','row_constraints',items)::json),'UTF8')),'hex');
    END''', immutable=True)
    function('source_guard_execution_companion', '', 'trigger', '''
    DECLARE execution gateway.executions%ROWTYPE; auth gateway.execution_authorizations%ROWTYPE; approval gateway.source_access_approvals%ROWTYPE;
    BEGIN
      SELECT * INTO STRICT execution FROM gateway.executions WHERE id=NEW.execution_id;
      SELECT * INTO STRICT auth FROM gateway.execution_authorizations WHERE execution_id=execution.id;
      SELECT * INTO STRICT approval FROM gateway.source_access_approvals WHERE id=NEW.approval_id;
      IF pg_trigger_depth()<>2 OR execution.source_access_generation<>1
        OR NEW.principal_snapshot IS DISTINCT FROM auth.principal_snapshot
        OR NEW.principal_digest IS DISTINCT FROM encode(sha256(convert_to(gateway.source_approval_canonical(auth.principal_snapshot::json),'UTF8')),'hex')
        OR (NEW.approval_revision,NEW.approval_digest,NEW.package_pins_digest,NEW.accepted_approval_expires_at,NEW.effective_deadline_at)
          IS DISTINCT FROM (approval.revision,approval.content_digest,approval.package_pins_digest,approval.expires_at,least(execution.deadline_at,approval.expires_at))
        OR NEW.audience_entry_id IS DISTINCT FROM gateway.source_complete_audience(approval.content,auth.principal_snapshot,auth.decision,'execute') THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      NEW.created_at:=clock_timestamp(); RETURN NEW;
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501';
    END''')
    op.execute('CREATE TRIGGER source_execution_companion_insert BEFORE INSERT ON gateway.execution_source_authorities FOR EACH ROW EXECUTE FUNCTION gateway.source_guard_execution_companion()')
    function('source_accept_execution_authority', 'execution_id text', 'void', '''
    DECLARE execution gateway.executions%ROWTYPE; auth gateway.execution_authorizations%ROWTYPE; version gateway.product_versions%ROWTYPE;
      approval gateway.source_access_approvals%ROWTYPE; selected gateway.source_access_approvals%ROWTYPE; decision jsonb; entry text; matched text;
      matches integer:=0; candidates integer:=0;
    BEGIN
      SELECT * INTO STRICT execution FROM gateway.executions WHERE id=execution_id;
      IF execution.source_access_generation<>1 THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      SELECT * INTO STRICT auth FROM gateway.execution_authorizations a WHERE a.execution_id=execution.id;
      SELECT * INTO STRICT version FROM gateway.product_versions WHERE id=execution.version_id;
      decision:=gateway.source_current_decision(auth.principal_snapshot,version.manifest,'execute');
      IF decision IS DISTINCT FROM auth.decision OR gateway.source_decision_scope(auth.decision) IS DISTINCT FROM auth.scope_digest OR encode(sha256(convert_to(gateway.source_approval_canonical(auth.decision::json),'UTF8')),'hex') IS DISTINCT FROM auth.full_decision_digest THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      FOR approval IN SELECT a.* FROM gateway.source_access_approvals a JOIN gateway.source_access_approval_states s ON s.approval_id=a.id
        WHERE a.product_id=execution.product_id AND a.manifest_digest=version.content_digest AND s.state='active' AND s.state_revision=1
          AND a.expires_at>clock_timestamp() ORDER BY a.id LIMIT 101 LOOP
        candidates:=candidates+1;
        IF candidates>100 THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
        IF NOT gateway.source_execution_package(execution.id,approval.id) THEN CONTINUE; END IF;
        BEGIN entry:=gateway.source_complete_audience(approval.content,auth.principal_snapshot,decision,'execute');
        EXCEPTION WHEN insufficient_privilege THEN CONTINUE; END;
        matches:=matches+1; selected:=approval; matched:=entry;
      END LOOP;
      IF matches<>1 THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      -- The acceptance trigger already owns C/P/Q. State locking is deferred
      -- until the commit suffix, after the caller creates its state/owner rows.
      INSERT INTO gateway.execution_source_authorities(execution_id,approval_id,approval_revision,approval_digest,audience_entry_id,
        principal_snapshot,principal_digest,package_pins_digest,accepted_approval_expires_at,effective_deadline_at)
      VALUES(execution.id,selected.id,selected.revision,selected.content_digest,matched,auth.principal_snapshot,
        encode(sha256(convert_to(gateway.source_approval_canonical(auth.principal_snapshot::json),'UTF8')),'hex'),selected.package_pins_digest,
        selected.expires_at,least(execution.deadline_at,selected.expires_at));
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501';
    END''')
    function('source_after_execution_authority', '', 'trigger', '''
    BEGIN
      IF (SELECT source_access_generation FROM gateway.executions WHERE id=NEW.execution_id)=1 THEN
        PERFORM gateway.source_accept_execution_authority(NEW.execution_id);
      END IF;
      RETURN NULL;
    END''')
    function('source_execution_access', 'execution_id text, action text, lock_approval boolean', 'jsonb', '''
    DECLARE execution gateway.executions%ROWTYPE; auth gateway.execution_authorizations%ROWTYPE; accepted gateway.execution_source_authorities%ROWTYPE;
      version gateway.product_versions%ROWTYPE; approval gateway.source_access_approvals%ROWTYPE; current_decision jsonb; entry text; checked_at timestamptz;
    BEGIN
      IF execution_id IS NULL OR action IS NULL OR action NOT IN ('execute','result') OR lock_approval IS NULL THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      SELECT * INTO STRICT execution FROM gateway.executions WHERE id=execution_id;
      SELECT * INTO STRICT auth FROM gateway.execution_authorizations a WHERE a.execution_id=execution.id;
      SELECT * INTO STRICT accepted FROM gateway.execution_source_authorities a WHERE a.execution_id=execution.id;
      SELECT * INTO STRICT approval FROM gateway.source_access_approvals WHERE id=accepted.approval_id;
      SELECT * INTO STRICT version FROM gateway.product_versions WHERE id=execution.version_id;
      IF NOT gateway.source_execution_package(execution.id,approval.id)
        OR (accepted.approval_revision,accepted.approval_digest,accepted.package_pins_digest,accepted.accepted_approval_expires_at)
          IS DISTINCT FROM (approval.revision,approval.content_digest,approval.package_pins_digest,approval.expires_at)
        OR accepted.principal_snapshot IS DISTINCT FROM auth.principal_snapshot
        OR accepted.principal_digest IS DISTINCT FROM encode(sha256(convert_to(gateway.source_approval_canonical(auth.principal_snapshot::json),'UTF8')),'hex')
        OR accepted.effective_deadline_at IS DISTINCT FROM least(execution.deadline_at,approval.expires_at)
        OR accepted.audience_entry_id IS DISTINCT FROM gateway.source_complete_audience(approval.content,auth.principal_snapshot,auth.decision,'execute')
        OR NOT EXISTS(SELECT 1 FROM gateway.version_states WHERE version_id=version.id AND status IN ('published','deprecated')) THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      current_decision:=gateway.source_current_decision(auth.principal_snapshot,version.manifest,action);
      IF NOT gateway.source_query_reviewed(approval.content,version.manifest,execution.canonical_query,current_decision->'limits') THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      IF gateway.source_decision_scope(current_decision) IS DISTINCT FROM auth.scope_digest
        OR NOT (auth.decision->'visible_fields') @> (current_decision->'visible_fields')
        OR ((version.manifest->>'adapter_revision')='domo-named-v2' AND current_decision IS DISTINCT FROM auth.decision) THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      entry:=gateway.source_complete_audience(approval.content,auth.principal_snapshot,current_decision,action);
      IF lock_approval THEN PERFORM gateway.source_approval_assert(approval.id,accepted.approval_revision,accepted.approval_digest); END IF;
      checked_at:=clock_timestamp();
      IF (approval.expires_at>checked_at AND EXISTS(SELECT 1 FROM gateway.source_access_approval_states WHERE approval_id=approval.id AND state='active' AND state_revision=1)) IS NOT TRUE
        OR (action='execute' AND accepted.effective_deadline_at<=checked_at) THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      RETURN jsonb_build_object('accepted',jsonb_build_object('approval_id',accepted.approval_id,'approval_revision',accepted.approval_revision,
        'approval_digest',accepted.approval_digest,'audience_entry_id',accepted.audience_entry_id,'principal',accepted.principal_snapshot,
        'package_pins_digest',accepted.package_pins_digest,'accepted_approval_expires_at',accepted.accepted_approval_expires_at,'effective_deadline_at',accepted.effective_deadline_at),
        'projection',gateway.source_approval_projection(approval.id),'decision',current_decision,'audience_entry_id',entry,
        'admitted_decision',auth.decision,'admitted_decision_digest',auth.full_decision_digest,
        'manifest',version.manifest,'binding_digest',version.binding_digest,'version_id',version.id);
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501';
    END''')
    function('source_assert_owner_access', 'owner_kind text, owner_id text, action text', 'jsonb', '''
    BEGIN
      IF owner_kind='execution' THEN RETURN gateway.source_execution_access(owner_id,action,true);
      ELSIF owner_kind='fixture_invocation' THEN RETURN gateway.source_fixture_invocation_access(owner_id,action,true);
      ELSIF owner_kind='fixture_attempt' THEN RETURN gateway.source_fixture_attempt_access(owner_id,action,true); END IF;
      RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501';
    END''')
    function('source_read_execution_access', 'execution_id text, action text', 'jsonb', '''
    BEGIN RETURN gateway.source_execution_access(execution_id,action,false); END''')
    function('source_execution_complete', '', 'trigger', '''
    BEGIN
      IF NEW.source_access_generation=1 THEN PERFORM gateway.source_assert_owner_access('execution',NEW.id,'execute'); END IF;
      RETURN NULL;
    END''')
    op.execute('CREATE TRIGGER source_execution_authority AFTER INSERT ON gateway.execution_authorizations FOR EACH ROW EXECUTE FUNCTION gateway.source_after_execution_authority()')
    op.execute('CREATE CONSTRAINT TRIGGER source_execution_complete AFTER INSERT ON gateway.executions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.source_execution_complete()')
    op.execute('CREATE TRIGGER execution_source_authorities_immutable BEFORE UPDATE OR DELETE ON gateway.execution_source_authorities FOR EACH ROW EXECUTE FUNCTION gateway.deny_immutable_change()')
    app='"'+configured['app']+'"'
    # Grant only after the migration-wide default-ACL scrub.
    return app


def _final_access_functions():
    function('source_transient_access', 'manifest_canonical text, principal jsonb, action text', 'jsonb', '''
    DECLARE manifest jsonb; digest text; decision jsonb; approval gateway.source_access_approvals%ROWTYPE;
      selected gateway.source_access_approvals%ROWTYPE; entry text; matched text; matches integer:=0; candidates integer:=0;
    BEGIN
      IF action IS NULL OR action NOT IN ('discover','describe','author','publish','execute') OR manifest_canonical IS NULL
        OR octet_length(manifest_canonical) NOT BETWEEN 1 AND 65536 OR NOT gateway.source_json_unique(manifest_canonical::json) THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      manifest:=manifest_canonical::jsonb; digest:=encode(sha256(convert_to(manifest_canonical,'UTF8')),'hex');
      IF manifest ? 'content_digest' THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      manifest:=manifest||jsonb_build_object('content_digest',digest);
      decision:=gateway.source_current_decision(principal,manifest,action);
      FOR approval IN SELECT a.* FROM gateway.source_access_approvals a JOIN gateway.source_access_approval_states s ON s.approval_id=a.id
        WHERE a.product_id=manifest->>'product_id' AND a.product_version=manifest->>'version' AND a.manifest_digest=digest
          AND a.policy_id=manifest->>'policy_id' AND s.state='active' AND s.state_revision=1 AND a.expires_at>clock_timestamp() ORDER BY a.id LIMIT 101 LOOP
        candidates:=candidates+1;
        IF candidates>100 THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
        IF NOT gateway.source_approval_package_valid(approval.content)
          OR manifest->>'binding_id' IS DISTINCT FROM approval.content->'bindings'->'consumer'->>'binding_id'
          OR manifest->'binding_revision' IS DISTINCT FROM approval.content->'bindings'->'consumer'->'revision'
          OR manifest->>'model_bundle_digest' IS DISTINCT FROM approval.content->'artifacts'->>'model_bundle_digest'
          OR manifest->>'adapter_revision' IS DISTINCT FROM (SELECT adapter_revision FROM gateway.binding_revisions WHERE id=approval.consumer_binding_id) THEN
          RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
        BEGIN entry:=gateway.source_complete_audience(approval.content,principal,decision,action);
        EXCEPTION WHEN insufficient_privilege THEN CONTINUE; END;
        matches:=matches+1; selected:=approval; matched:=entry;
      END LOOP;
      IF matches<>1 THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      RETURN jsonb_build_object('projection',gateway.source_approval_projection(selected.id),'decision',decision,
        'audience_entry_id',matched,'binding_digest',selected.consumer_binding_digest);
    EXCEPTION WHEN invalid_text_representation OR NO_DATA_FOUND THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501';
    END''')
    function('source_finalize_access', 'requests jsonb', 'jsonb', '''
    DECLARE request jsonb; result jsonb; results jsonb:='[]'::jsonb; ids uuid[]:=ARRAY[]::uuid[]; aid uuid;
      approval gateway.source_access_approvals%ROWTYPE; checked_at timestamptz;
    BEGIN
      IF (jsonb_typeof(requests)='array' AND jsonb_array_length(requests) BETWEEN 1 AND 200 AND octet_length(requests::text)<=30000000) IS NOT TRUE THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      FOR request IN SELECT v FROM jsonb_array_elements(requests) a(v) LOOP
        IF NOT gateway.source_approval_keys(request,ARRAY['kind','owner_kind','owner_id','manifest_canonical','principal','decision','action',
          'binding_digest','approval_id','approval_revision','approval_digest','projection_digest','audience_entry_id']) THEN
          RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
        IF request->>'kind'='owner' THEN
          IF request->'manifest_canonical'<>'null'::jsonb THEN
            RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
          IF request->>'owner_kind'='execution' THEN result:=gateway.source_execution_access(request->>'owner_id',request->>'action',false);
          ELSIF request->>'owner_kind'='fixture_invocation' THEN result:=gateway.source_fixture_invocation_access(request->>'owner_id',request->>'action',false);
          ELSIF request->>'owner_kind'='fixture_attempt' THEN result:=gateway.source_fixture_attempt_access(request->>'owner_id',request->>'action',false);
          ELSE RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
          IF result->'accepted'->'principal' IS DISTINCT FROM request->'principal' THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
        ELSIF request->>'kind' IN ('transient','admission') THEN
          IF request->'owner_kind'<>'null'::jsonb OR request->'owner_id'<>'null'::jsonb
            OR (request->>'kind'='admission' AND request->>'action' IS DISTINCT FROM 'execute')
            OR (request->>'kind'='transient' AND request->>'action' NOT IN ('discover','describe','author','publish')) THEN
            RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
          result:=gateway.source_transient_access(request->>'manifest_canonical',request->'principal',request->>'action');
        ELSE RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
        -- Supplied pins are equality assertions, never authority selectors.
        IF result->'decision' IS DISTINCT FROM request->'decision' OR result->'binding_digest' IS DISTINCT FROM request->'binding_digest'
          OR result->'projection'->'approval_id' IS DISTINCT FROM request->'approval_id'
          OR result->'projection'->'approval_revision' IS DISTINCT FROM request->'approval_revision'
          OR result->'projection'->'approval_digest' IS DISTINCT FROM request->'approval_digest'
          OR result->'projection'->'projection_digest' IS DISTINCT FROM request->'projection_digest'
          OR result->'audience_entry_id' IS DISTINCT FROM request->'audience_entry_id' THEN
          RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
        ids:=array_append(ids,(result->'projection'->>'approval_id')::uuid);
        results:=results||jsonb_build_array(result);
      END LOOP;
      -- Structural evidence/completeness belongs to the protected prefix; the
      -- shared release instant must follow all nontrivial predicates and A waits.
      PERFORM gateway.source_fixture_batch_complete(requests);
      FOR aid IN SELECT DISTINCT v FROM unnest(ids) a(v) ORDER BY v LOOP
        SELECT * INTO STRICT approval FROM gateway.source_access_approvals WHERE id=aid;
        PERFORM gateway.source_approval_assert(approval.id,approval.revision,approval.content_digest);
      END LOOP;
      -- Final shared instant follows the last A wait; locks cannot freeze time.
      checked_at:=clock_timestamp();
      IF EXISTS(SELECT 1 FROM gateway.source_access_approvals a JOIN gateway.source_access_approval_states s ON s.approval_id=a.id
        WHERE a.id=ANY(ids) AND (a.expires_at>checked_at AND s.state='active' AND s.state_revision=1) IS NOT TRUE) THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      FOR request IN SELECT v FROM jsonb_array_elements(requests) a(v) LOOP
        IF request->>'kind'='owner' AND request->>'action'='execute' AND NOT (
          (request->>'owner_kind'='execution' AND EXISTS(SELECT 1 FROM gateway.execution_source_authorities
            WHERE execution_id=request->>'owner_id' AND effective_deadline_at>checked_at))
          OR (request->>'owner_kind'='fixture_attempt' AND EXISTS(SELECT 1 FROM gateway.fixture_attempt_source_authorities
            WHERE attempt_id=request->>'owner_id' AND effective_deadline_at>checked_at))) THEN
          RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      END LOOP;
      RETURN results;
    EXCEPTION WHEN invalid_text_representation OR NO_DATA_FOUND THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501';
    END''')


def _replace_existing(name, arguments, result, body):
    """Keep the exact preexisting narrow ACL; never create a missing overload."""
    types=','.join(' '.join(arg.strip().split()[1:]).split(' DEFAULT ')[0] for arg in arguments.split(',')) if arguments else ''
    op.execute(f"""DO $$ BEGIN
      IF to_regprocedure('gateway.{name}({types})') IS NULL THEN
        RAISE EXCEPTION 'Required source function is missing'; END IF;
      END $$""")
    op.execute(f"""CREATE OR REPLACE FUNCTION gateway.{name}({arguments}) RETURNS {result}
      LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,gateway
      AS $approval${body}$approval$""")


def _lifecycle_approval_functions(configured):
    function('source_owner_snapshot', 'owner_kind text, owner_id text, spec jsonb', 'jsonb', '''
    DECLARE execution gateway.executions%ROWTYPE; expected jsonb; access jsonb; accepted jsonb;
    BEGIN
      IF owner_kind IN ('fixture_invocation','fixture_attempt') THEN RETURN gateway.source_fixture_owner_snapshot(owner_kind,owner_id,spec); END IF;
      IF owner_kind IS DISTINCT FROM 'execution' THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      SELECT * INTO STRICT execution FROM gateway.executions WHERE id=owner_id;
      expected:=jsonb_build_object('principal_id',execution.principal_id,'actor_id',execution.principal_id,
        'policy_id',execution.policy_id,'version_id',execution.version_id,'draft_id',NULL,'catalog_digest',NULL);
      IF spec IS DISTINCT FROM expected THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      access:=gateway.source_execution_access(execution.id,'execute',false); accepted:=access->'accepted';
      RETURN gateway.source_current_authority(expected)||jsonb_build_object('source_access',jsonb_build_object(
        'owner_kind','execution','owner_id',execution.id,'approval_id',accepted->'approval_id',
        'approval_revision',accepted->'approval_revision','approval_digest',accepted->'approval_digest',
        'state_revision',1,'audience_entry_id',accepted->'audience_entry_id','current_audience_entry_id',access->'audience_entry_id',
        'principal_digest',encode(sha256(convert_to(gateway.source_approval_canonical((accepted->'principal')::json),'UTF8')),'hex'),
        'package_pins_digest',accepted->'package_pins_digest','accepted_approval_expires_at',accepted->'accepted_approval_expires_at',
        'effective_deadline_at',accepted->'effective_deadline_at'));
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501';
    END''')
    function('source_assert_group_approval', 'group_id uuid', 'void', '''
    DECLARE grp gateway.source_operation_groups%ROWTYPE; snapshot jsonb;
    BEGIN
      SELECT * INTO STRICT grp FROM gateway.source_operation_groups WHERE id=group_id;
      IF grp.owner_kind NOT IN ('consumer','fixture_test') THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      snapshot:=gateway.source_owner_snapshot(CASE WHEN grp.context='consumer' THEN 'execution' ELSE 'fixture_invocation' END,grp.owner_id,grp.authority_spec);
      IF snapshot IS DISTINCT FROM grp.authority_snapshot OR grp.deadline_at>(snapshot->'source_access'->>'effective_deadline_at')::timestamptz THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      PERFORM gateway.source_assert_owner_access(CASE WHEN grp.context='consumer' THEN 'execution' ELSE 'fixture_invocation' END,
        grp.owner_id,CASE WHEN grp.context='consumer' THEN 'execute' ELSE 'author' END);
      IF grp.deadline_at<=clock_timestamp() THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501';
    END''')
    function('source_assert_session_approval', 'session_id uuid', 'void', '''
    DECLARE value gateway.source_sessions%ROWTYPE; grp gateway.source_operation_groups%ROWTYPE; snapshot jsonb;
    BEGIN
      SELECT * INTO STRICT value FROM gateway.source_sessions WHERE id=session_id;
      SELECT * INTO STRICT grp FROM gateway.source_operation_groups WHERE id=value.group_id;
      IF value.context IS DISTINCT FROM grp.context OR value.deadline_at>grp.deadline_at THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      IF value.context='consumer' THEN
        IF value.owner_id IS DISTINCT FROM grp.owner_id OR value.authority_spec IS DISTINCT FROM grp.authority_spec
          OR value.authority_snapshot IS DISTINCT FROM grp.authority_snapshot THEN
          RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      ELSIF value.context='fixture_test' THEN
        snapshot:=gateway.source_owner_snapshot('fixture_attempt',value.owner_id,value.authority_spec);
        IF snapshot IS DISTINCT FROM value.authority_snapshot OR snapshot->'source_access'->>'invocation_id' IS DISTINCT FROM grp.owner_id
          OR snapshot->'source_access'->>'fixture_case_id' IS DISTINCT FROM value.case_id
          OR snapshot->'source_access'->'actor_digest' IS DISTINCT FROM grp.authority_snapshot->'source_access'->'actor_digest'
          OR snapshot->'source_access'->'approval_id' IS DISTINCT FROM grp.authority_snapshot->'source_access'->'approval_id'
          OR value.deadline_at>(snapshot->'source_access'->>'effective_deadline_at')::timestamptz THEN
          RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      ELSE RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      PERFORM gateway.source_assert_group_approval(value.group_id);
      IF value.context='fixture_test' THEN PERFORM gateway.source_assert_owner_access('fixture_attempt',value.owner_id,'execute'); END IF;
      IF value.deadline_at<=clock_timestamp() OR NOT gateway.source_check_owner(value) THEN
        RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501';
    END''')
    function('source_complete_source_approval', '', 'trigger', '''
    BEGIN
      IF TG_TABLE_NAME='source_operation_groups' THEN PERFORM gateway.source_assert_group_approval(NEW.id);
      ELSIF TG_TABLE_NAME='source_sessions' THEN PERFORM gateway.source_assert_session_approval(NEW.id);
      ELSIF TG_TABLE_NAME='source_preparations' THEN PERFORM gateway.source_assert_session_approval(NEW.session_id);
      ELSE RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      RETURN NULL;
    END''')
    for table,name in (('source_operation_groups','source_group_approval_complete'),('source_sessions','source_session_approval_complete'),('source_preparations','source_preparation_approval_complete')):
        op.execute(f'CREATE CONSTRAINT TRIGGER {name} AFTER INSERT ON gateway.{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.source_complete_source_approval()')
    op.execute('CREATE CONSTRAINT TRIGGER source_session_reclaim_approval_complete AFTER UPDATE ON gateway.source_sessions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN(OLD.generation IS DISTINCT FROM NEW.generation) EXECUTE FUNCTION gateway.source_complete_source_approval()')
    _replace_existing('source_guard_group', '', 'trigger', '''
    DECLARE profile gateway.source_profiles%ROWTYPE; settings gateway.source_runtime_settings%ROWTYPE;
      snapshot jsonb; manifest jsonb; own_count bigint; total_count bigint;
      own_limit integer; total_limit integer; seconds integer; expected_request text;
    BEGIN
      PERFORM 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE;
      SELECT * INTO STRICT settings FROM gateway.source_runtime_settings WHERE singleton;
      SELECT * INTO STRICT profile FROM gateway.source_profiles WHERE
        (source_id,revision,context)=(NEW.source_id,NEW.source_revision,NEW.context);
      PERFORM 1 FROM gateway.source_pools WHERE capacity_key=profile.capacity_key FOR UPDATE;
      snapshot:=gateway.source_owner_snapshot(CASE WHEN NEW.context='consumer' THEN 'execution' ELSE 'fixture_invocation' END,NEW.owner_id,NEW.authority_spec);
      manifest:=CASE WHEN NEW.context='consumer' THEN snapshot->'version'->'manifest' ELSE snapshot->'draft'->'manifest' END;
      expected_request:=CASE WHEN NEW.context='consumer' THEN (SELECT request_id FROM gateway.executions WHERE id=NEW.owner_id) ELSE NEW.owner_id END;
      IF NEW.authority_snapshot IS DISTINCT FROM snapshot OR NEW.capacity_key<>profile.capacity_key
        OR NEW.deployment_epoch<>settings.deployment_epoch OR NEW.owner_kind<>NEW.context
        OR NEW.deadline_at>(snapshot->'source_access'->>'effective_deadline_at')::timestamptz
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
          AND correlation_id=expected_request AND policy_revision=(snapshot->'policy'->>'revision')::integer
          AND created_transaction_id=pg_current_xact_id()
          AND source_approval_id::text=snapshot->'source_access'->>'approval_id'
          AND source_approval_revision=(snapshot->'source_access'->>'approval_revision')::integer
          AND source_approval_digest=snapshot->'source_access'->>'approval_digest' AND source_approval_state_revision=1 AND source_access_reason_code='APPROVED')
        OR (NEW.context='consumer' AND NOT EXISTS(SELECT 1 FROM gateway.executions e WHERE e.id=NEW.owner_id
          AND e.context='consumer' AND e.principal_id=NEW.actor_id AND e.version_id=NEW.authority_spec->>'version_id'
          AND e.policy_id=NEW.authority_spec->>'policy_id'
          AND NEW.deadline_at<=e.deadline_at))
        OR (NEW.context='fixture_test' AND (snapshot->'draft'->>'id' IS DISTINCT FROM snapshot->'draft'->>'head_id'
          OR snapshot->'draft'->>'revision' IS DISTINCT FROM snapshot->'draft'->>'head_revision')) THEN
        RAISE EXCEPTION 'Invalid source group authority' USING ERRCODE='23514'; END IF;
      IF (SELECT count(DISTINCT v) FROM jsonb_array_elements_text(NEW.roster) v)<>jsonb_array_length(NEW.roster)
        OR EXISTS(SELECT 1 FROM jsonb_array_elements_text(NEW.roster) v WHERE v !~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
        OR (NEW.context='consumer' AND NEW.roster<>jsonb_build_array(NEW.owner_id))
        OR (NEW.context='fixture_test' AND (NEW.roster IS DISTINCT FROM snapshot->'source_access'->'positive_roster'
          OR profile.descriptor IS DISTINCT FROM snapshot->'source_access'->'fixture_source')) THEN
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
    END''')
    _replace_existing('source_guard_session', '', 'trigger', '''
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
        OR NEW.authority_snapshot IS DISTINCT FROM gateway.source_owner_snapshot(CASE WHEN NEW.context='consumer' THEN 'execution' ELSE 'fixture_attempt' END,NEW.owner_id,NEW.authority_spec)
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
            AND a.source_invocation_id::text=grp.owner_id
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
          'scope_digest',gateway.source_fixture_scope_digest(a.id),'decision_digest',gateway.source_fixture_decision_digest(a.id),
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
        OR NEW.plan->>'scope_digest' IS DISTINCT FROM owner_pins->>'scope_digest'
        OR NEW.plan->>'decision_digest' IS DISTINCT FROM owner_pins->>'decision_digest'
        OR (NEW.context='fixture_test' AND (NEW.authority_snapshot->'source_access'->>'invocation_id' IS DISTINCT FROM grp.owner_id
          OR NEW.authority_snapshot->'source_access'->>'fixture_case_id' IS DISTINCT FROM NEW.case_id
          OR NEW.authority_snapshot->'source_access'->>'fixture_binding_digest' IS DISTINCT FROM NEW.plan->>'binding_digest'
          OR NEW.authority_snapshot->'source_access'->'fixture_source' IS DISTINCT FROM profile.descriptor))
        OR NEW.authority_spec->>'principal_id' IS DISTINCT FROM expected_principal
        OR binding->>'source_id' IS DISTINCT FROM NEW.source_id
        OR (binding->>'source_revision')::integer IS DISTINCT FROM NEW.source_revision
        OR binding->>'source_digest' IS DISTINCT FROM profile.descriptor_digest
        OR binding->>'context' IS DISTINCT FROM NEW.context
        OR NOT EXISTS(SELECT 1 FROM gateway.audit_events WHERE id=NEW.allow_audit_id AND actor_id=expected_principal
          AND decision='allow' AND action='execute' AND policy_id=NEW.authority_spec->>'policy_id'
          AND product_id=owner_pins->>'product_id' AND version_id=owner_pins->>'version_id'
          AND correlation_id=owner_pins->>'request_id' AND policy_revision=(NEW.authority_snapshot->'policy'->>'revision')::integer
          AND created_transaction_id=pg_current_xact_id()
          AND source_approval_id::text=NEW.authority_snapshot->'source_access'->>'approval_id'
          AND source_approval_revision=(NEW.authority_snapshot->'source_access'->>'approval_revision')::integer
          AND source_approval_digest=NEW.authority_snapshot->'source_access'->>'approval_digest' AND source_approval_state_revision=1 AND source_access_reason_code='APPROVED') THEN
        RAISE EXCEPTION 'Source session owner mismatch' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END''')
    _replace_existing('source_check_context', 'context jsonb, require_open boolean DEFAULT false', 'gateway.source_sessions', '''
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
        OR gateway.source_owner_snapshot(CASE WHEN value.context='consumer' THEN 'execution' ELSE 'fixture_attempt' END,value.owner_id,value.authority_spec) IS DISTINCT FROM value.authority_snapshot
        OR gateway.source_owner_snapshot(CASE WHEN grp.context='consumer' THEN 'execution' ELSE 'fixture_invocation' END,grp.owner_id,grp.authority_spec) IS DISTINCT FROM grp.authority_snapshot
        OR NOT EXISTS(SELECT 1 FROM gateway.source_runtime_settings WHERE deployment_epoch=grp.deployment_epoch) THEN
        RAISE EXCEPTION 'Source authority expired or changed' USING ERRCODE='42501'; END IF;
      RETURN value;
    END''')
    _replace_existing('source_reclaim_session', 'sid uuid, new_generation integer, new_token text, new_deadline timestamptz', 'jsonb', '''
    DECLARE value gateway.source_sessions%ROWTYPE;
    BEGIN
      IF NOT EXISTS(SELECT 1 FROM gateway.source_runtime_settings WHERE app_role=session_user) THEN
        RAISE EXCEPTION 'Source reclaim unavailable' USING ERRCODE='42501'; END IF;
      value:=gateway.source_lock_session(sid);
      IF value.sealed OR new_generation<value.generation OR new_deadline>value.deadline_at
        OR new_deadline<=clock_timestamp() OR gateway.source_owner_snapshot(CASE WHEN value.context='consumer' THEN 'execution' ELSE 'fixture_attempt' END,value.owner_id,value.authority_spec) IS DISTINCT FROM value.authority_snapshot THEN
        RAISE EXCEPTION 'Source reclaim unavailable' USING ERRCODE='42501'; END IF;
      value.generation:=new_generation; value.lease_token:=new_token; value.deadline_at:=new_deadline;
      IF NOT gateway.source_check_owner(value) THEN RAISE EXCEPTION 'Source reclaim fence mismatch' USING ERRCODE='42501'; END IF;
      UPDATE gateway.source_sessions SET generation=new_generation,lease_token=new_token,deadline_at=new_deadline WHERE id=sid;
      RETURN gateway.source_status(sid);
    END''')
    _replace_existing('source_check_attempt', 'context jsonb', 'jsonb', '''
    DECLARE value gateway.source_sessions%ROWTYPE;
    BEGIN value:=gateway.source_check_context(context,false);
      PERFORM gateway.source_assert_session_approval(value.id);
      RETURN jsonb_build_object('allowed',true,'deadline_at',value.deadline_at,'context_digest',value.context_digest);
    END''')
    _replace_existing('source_bq_lookup', 'context jsonb, semantic_key text', 'jsonb', '''
    DECLARE value gateway.source_sessions%ROWTYPE; jid text;
    BEGIN
      value:=gateway.source_check_context(context,false);
      IF semantic_key !~ '^[a-f0-9]{64}$' OR value.plan->>'provider'<>'bigquery' THEN
        RAISE EXCEPTION 'Invalid source job identity' USING ERRCODE='23514'; END IF;
      SELECT j.job_id INTO jid FROM gateway.bigquery_source_jobs j WHERE j.session_id=value.id AND j.semantic_key=source_bq_lookup.semantic_key FOR UPDATE;
      PERFORM gateway.source_assert_session_approval(value.id);
      RETURN gateway.source_job_projection(jid);
    END''')
    _replace_existing('source_bq_check_data', 'context jsonb, job_id text', 'jsonb', '''
    DECLARE value gateway.source_sessions%ROWTYPE;
    BEGIN
      value:=gateway.source_check_context(context,false);
      PERFORM 1 FROM gateway.bigquery_source_jobs j WHERE j.job_id=source_bq_check_data.job_id AND j.session_id=value.id FOR UPDATE;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'Source job unavailable' USING ERRCODE='42501'; END IF;
      PERFORM gateway.source_assert_session_approval(value.id);
      RETURN gateway.source_job_projection(job_id);
    END''')
    _replace_existing('source_bq_admit', 'context jsonb, semantic_key text, intent jsonb', 'jsonb', '''
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
        PERFORM gateway.source_assert_session_approval(value.id);
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
      PERFORM gateway.source_assert_session_approval(value.id);
      IF (intent->>'job_timeout_ms')::integer>least(120000,(profile.descriptor->>'job_timeout_ms')::integer,
        floor(extract(epoch FROM least(value.deadline_at,grp.deadline_at)-clock_timestamp())*1000)::integer) THEN
        RAISE EXCEPTION 'Source job timeout exceeds authority' USING ERRCODE='23514'; END IF;
      INSERT INTO gateway.bigquery_source_jobs(job_id,session_id,semantic_key,project_id,location,principal,intent,intent_digest)
        VALUES(jid,value.id,semantic_key,profile.descriptor->>'billing_project',profile.descriptor->>'location',
          profile.descriptor->'auth'->>'expected_principal',intent,encode(sha256(convert_to(safe_identity::text,'UTF8')),'hex'));
      UPDATE gateway.source_operation_groups SET admitted_count=admitted_count+1,reserved_bytes=reserved_bytes+bytes WHERE id=grp.id;
      UPDATE gateway.source_pools SET dispatches=dispatches+1 WHERE capacity_key=value.capacity_key;
      RETURN jsonb_build_object('submit_once',true,'job',gateway.source_job_projection(jid));
    END''')
    _replace_existing('source_domo_arm_send', 'context jsonb, plan_fingerprint text, nonce_hash text, intent jsonb', 'jsonb', '''
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
      PERFORM gateway.source_assert_session_approval(value.id);
      INSERT INTO gateway.domo_source_sends(session_id,plan_fingerprint,nonce_hash,intent) VALUES(value.id,plan_fingerprint,nonce_hash,intent);
      UPDATE gateway.source_operation_groups SET admitted_count=admitted_count+1 WHERE id=grp.id;
      UPDATE gateway.source_pools SET dispatches=dispatches+1 WHERE capacity_key=value.capacity_key;
      RETURN jsonb_build_object('armed',true,'session_id',value.id,'plan_fingerprint',plan_fingerprint);
    END''')
    _replace_existing('source_guard_owner_completion', '', 'trigger', '''
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
      IF NEW.state='succeeded' THEN
        IF TG_TABLE_NAME='execution_states' THEN PERFORM gateway.source_assert_owner_access('execution',NEW.execution_id,'execute');
        ELSE PERFORM gateway.source_assert_owner_access('fixture_attempt',NEW.attempt_id,'execute'); END IF;
      END IF;
      RETURN NEW;
    END''')
    _replace_existing('guard_source_preparation', '', 'trigger', '''
        DECLARE source gateway.source_sessions%ROWTYPE; binding jsonb; manifest jsonb; decision jsonb;
          p jsonb:=NEW.prepared_query; field text; expected_keys text[]:=ARRAY[
          'kind','context','accepted_spec_digest','binding_id','binding_revision','binding_digest','model_bundle_digest',
          'source_id','source_revision','source_digest','dataset_id','definition_digest','query_digest','scope_digest',
          'decision_digest','statement_id','statement_revision','statement_digest','response_profile','max_rows','max_bytes'];
        BEGIN
          source:=gateway.source_lock_session(NEW.session_id);
          PERFORM gateway.source_owner_snapshot(CASE WHEN source.context='consumer' THEN 'execution' ELSE 'fixture_attempt' END,source.owner_id,source.authority_spec);
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
        END ''')


def downgrade():
    raise RuntimeError('Destructive source approval downgrade is disabled')


def _retention_functions(configured):
    op.execute('CREATE INDEX ix_source_groups_allow_audit ON gateway.source_operation_groups(allow_audit_id)')
    op.execute('CREATE INDEX ix_source_sessions_allow_audit ON gateway.source_sessions(allow_audit_id)')
    function('prune_unreferenced_audits','before_at timestamptz, batch_size integer','integer',f'''    DECLARE removed integer; cutoff timestamptz;
    BEGIN
      IF session_user IS DISTINCT FROM '{configured['audit_retention']}' OR before_at IS NULL OR NOT isfinite(before_at)
        OR batch_size IS NULL OR batch_size NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'Audit retention unavailable' USING ERRCODE='42501'; END IF;
      cutoff:=least(before_at,statement_timestamp()-interval '30 days');
      DELETE FROM gateway.audit_events a WHERE a.id IN (
        SELECT eligible.id FROM gateway.audit_events eligible WHERE eligible.created_at<=cutoff
          AND NOT EXISTS(SELECT 1 FROM gateway.source_operation_groups g WHERE g.allow_audit_id=eligible.id)
          AND NOT EXISTS(SELECT 1 FROM gateway.source_sessions s WHERE s.allow_audit_id=eligible.id)
        ORDER BY eligible.created_at,eligible.id LIMIT batch_size);
      GET DIAGNOSTICS removed=ROW_COUNT;
      RETURN removed;
    END''')
