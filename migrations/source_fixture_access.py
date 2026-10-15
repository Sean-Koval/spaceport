"""Private 0007 installer for immutable fixture and publication provenance.

Not a separate Alembic revision. All functions are registered with 0007's ACL
scrubber, and only bounded checked entrypoints receive application grants.
"""
TABLES = ('fixture_source_invocations', 'fixture_source_invocation_cases',
          'fixture_attempt_source_authorities', 'fixture_test_source_authorities',
          'source_publication_operations', 'source_publication_revision_steps')


def install(op, function, configured):
    register = function
    def function(name, arguments, result, body, **options):
        # Table aliases take precedence over identically named row variables;
        # parameter comparisons use explicit function-qualified names below.
        register(name, arguments, result, '#variable_conflict use_column\n' + body, **options)
    _fixture_tables(op)
    _fixture_predicates(op, function)
    _fixture_owners(op, function)
    _fixture_evidence(op, function)
    _publication(op, function)
    for table in TABLES:
        op.execute(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON gateway.{table} FOR EACH ROW EXECUTE FUNCTION gateway.deny_immutable_change()')
    return TABLES


def grant(op, configured):
    app = '"' + configured['app'] + '"'
    op.execute(f'GRANT SELECT ON ' + ','.join('gateway.'+table for table in TABLES) + f' TO {app}')
    op.execute(f'''GRANT EXECUTE ON FUNCTION gateway.source_accept_fixture_invocation(text,jsonb,text,uuid),
        gateway.source_read_fixture_invocation(uuid),gateway.source_read_fixture_attempt(text),gateway.source_read_fixture_test(text),
        gateway.source_read_fixture_case(uuid,text),gateway.source_read_fixture_owner(text,text,text),
        gateway.source_record_publication_operation(jsonb,text,text,text,text,text,text,integer,integer,text,text) TO {app}''')
    op.execute(f'GRANT INSERT(source_invocation_id,fixture_manifest_canonical,query_canonical,decision_canonical) ON gateway.fixture_attempts TO {app}')
    op.execute(f'GRANT INSERT(manifest_canonical) ON gateway.draft_revisions TO {app}')
    op.execute(f'GRANT INSERT(evidence_canonical,source_decision_canonicals) ON gateway.test_runs TO {app}')
    for table in ('product_versions', 'version_states', 'publication_events'):
        op.execute(f'GRANT INSERT(source_operation_id) ON gateway.{table} TO {app}')
    for table in ('products', 'version_states'):
        op.execute(f'GRANT UPDATE(source_operation_id) ON gateway.{table} TO {app}')


def _fixture_tables(op):
    # 0004 allowed only successful checks to carry a digest. Retained failed
    # evidence now carries the exact bounded check digest too; historical
    # failed rows may remain NULL, but cannot satisfy a new evidence witness.
    # Preserve every other original state/completion/immutability obligation.
    op.execute('ALTER TABLE gateway.fixture_attempt_states DROP CONSTRAINT fixture_attempt_states_check')
    op.execute("""ALTER TABLE gateway.fixture_attempt_states ADD CONSTRAINT fixture_attempt_states_check CHECK(
      (state='running' AND check_digest IS NULL)
      OR (state='succeeded' AND check_digest IS NOT NULL AND check_digest ~ '^[a-f0-9]{64}$')
      OR (state IN ('failed','timed_out') AND (check_digest IS NULL OR check_digest ~ '^[a-f0-9]{64}$')))""")
    op.execute('ALTER TABLE gateway.draft_revisions ADD COLUMN manifest_canonical TEXT')
    op.execute('ALTER TABLE gateway.fixture_attempts ADD COLUMN fixture_manifest_canonical TEXT, ADD COLUMN query_canonical TEXT, ADD COLUMN decision_canonical TEXT')
    op.execute('ALTER TABLE gateway.test_runs ADD COLUMN evidence_canonical TEXT, ADD COLUMN source_decision_canonicals JSONB')
    op.execute('''CREATE TABLE gateway.fixture_source_invocations (
      id UUID PRIMARY KEY, product_id VARCHAR(128) NOT NULL, draft_id VARCHAR(128) NOT NULL, draft_digest CHAR(64) NOT NULL,
      consumer_binding_id VARCHAR(128) NOT NULL,consumer_binding_digest CHAR(64) NOT NULL,
      fixture_binding_id VARCHAR(128) NOT NULL,fixture_binding_digest CHAR(64) NOT NULL,
      catalog JSONB NOT NULL,catalog_canonical TEXT NOT NULL,catalog_digest CHAR(64) NOT NULL,
      execution_contract_digest CHAR(64) NOT NULL,actor_snapshot JSONB NOT NULL,actor_digest CHAR(64) NOT NULL,
      approval_id UUID NOT NULL,approval_revision INTEGER NOT NULL,approval_digest CHAR(64) NOT NULL,
      author_entry_id VARCHAR(128) NOT NULL,package_pins_digest CHAR(64) NOT NULL,
      positive_roster JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL,accepted_approval_expires_at TIMESTAMPTZ NOT NULL,
      effective_deadline_at TIMESTAMPTZ NOT NULL,
      FOREIGN KEY(draft_id,product_id,draft_digest) REFERENCES gateway.draft_revisions(id,product_id,content_digest),
      FOREIGN KEY(consumer_binding_id,consumer_binding_digest) REFERENCES gateway.binding_revisions(id,content_digest),
      FOREIGN KEY(fixture_binding_id,fixture_binding_digest) REFERENCES gateway.binding_revisions(id,content_digest),
      FOREIGN KEY(approval_id,approval_revision,approval_digest) REFERENCES gateway.source_access_approvals(id,revision,content_digest),
      CHECK(octet_length(catalog_canonical) BETWEEN 1 AND 65536 AND catalog=cast(catalog_canonical AS jsonb)),
      CHECK(effective_deadline_at>created_at AND effective_deadline_at<=accepted_approval_expires_at),
      CHECK(jsonb_typeof(positive_roster)='array' AND jsonb_array_length(positive_roster) BETWEEN 1 AND 8))''')
    op.execute('''CREATE TABLE gateway.fixture_source_invocation_cases (
      invocation_id UUID NOT NULL REFERENCES gateway.fixture_source_invocations(id), fixture_id VARCHAR(128) NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('known_answer','input_negative','permission_negative')),fixture_version VARCHAR(128) NOT NULL,
      case_content JSONB NOT NULL,case_digest CHAR(64) NOT NULL,principal_snapshot JSONB NOT NULL,principal_digest CHAR(64) NOT NULL,
      data_entry_id VARCHAR(128) NOT NULL,catalog_digest CHAR(64) NOT NULL,PRIMARY KEY(invocation_id,fixture_id))''')
    op.execute('ALTER TABLE gateway.fixture_attempts ADD COLUMN source_invocation_id UUID REFERENCES gateway.fixture_source_invocations(id)')
    op.execute('''CREATE TABLE gateway.fixture_attempt_source_authorities (
      attempt_id VARCHAR(128) PRIMARY KEY REFERENCES gateway.fixture_attempts(id), invocation_id UUID NOT NULL,
      fixture_id VARCHAR(128) NOT NULL,approval_id UUID NOT NULL,approval_revision INTEGER NOT NULL,approval_digest CHAR(64) NOT NULL,
      author_entry_id VARCHAR(128) NOT NULL,data_entry_id VARCHAR(128) NOT NULL,package_pins_digest CHAR(64) NOT NULL,
      accepted_approval_expires_at TIMESTAMPTZ NOT NULL,effective_deadline_at TIMESTAMPTZ NOT NULL,
      FOREIGN KEY(invocation_id,fixture_id) REFERENCES gateway.fixture_source_invocation_cases(invocation_id,fixture_id),
      FOREIGN KEY(approval_id,approval_revision,approval_digest) REFERENCES gateway.source_access_approvals(id,revision,content_digest),
      UNIQUE(invocation_id,fixture_id),CHECK(effective_deadline_at<=accepted_approval_expires_at))''')
    op.execute('''CREATE TABLE gateway.fixture_test_source_authorities (
      test_run_id VARCHAR(128) PRIMARY KEY REFERENCES gateway.test_runs(id),
      invocation_id UUID NOT NULL UNIQUE REFERENCES gateway.fixture_source_invocations(id))''')


def _fixture_predicates(op, function):
    # These are byte projections of an already digest-bound canonical witness.
    # They preserve scalar tokens; they do not serialize PostgreSQL numerics.
    function('source_fixture_omit', 'canonical text, omitted text[]', 'text', '''
    DECLARE result text;
    BEGIN
      IF canonical IS NULL OR octet_length(canonical) NOT BETWEEN 1 AND 65536
        OR json_typeof(canonical::json)<>'object' OR NOT gateway.source_json_unique(canonical::json) THEN
        RAISE EXCEPTION 'Canonical witness unavailable' USING ERRCODE='42501'; END IF;
      SELECT '{'||coalesce(string_agg(to_json(key)::text||':'||value::text,',' ORDER BY position),'')||'}'
        INTO result FROM json_each(canonical::json) WITH ORDINALITY AS e(key,value,position) WHERE NOT key=ANY(omitted);
      RETURN result;
    END''', immutable=True)
    function('source_fixture_case_text', 'canonical text, fixture_id text', 'text', '''
    DECLARE result text;
    BEGIN
      SELECT v::text INTO STRICT result FROM json_array_elements(canonical::json->'cases') AS e(v)
        WHERE v->'fixture'->>'id'=fixture_id;
      RETURN result;
    EXCEPTION WHEN NO_DATA_FOUND OR TOO_MANY_ROWS THEN RAISE EXCEPTION 'Canonical fixture unavailable' USING ERRCODE='42501'; END''', immutable=True)
    function('source_fixture_same_scope', 'first jsonb, second jsonb', 'boolean', '''
    BEGIN
      RETURN ((first->'principal_id',first->'policy_revision',first->'tenant_id')=(second->'principal_id',second->'policy_revision',second->'tenant_id')
        AND jsonb_array_length(first->'row_constraints')=jsonb_array_length(second->'row_constraints')
        AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(first->'row_constraints') a(v) WHERE NOT EXISTS(
          SELECT 1 FROM jsonb_array_elements(second->'row_constraints') b(w) WHERE v->'member'=w->'member'
            AND (v->'values') @> (w->'values') AND (w->'values') @> (v->'values')))) IS TRUE;
    END''', immutable=True)
    function('source_fixture_scope_text', 'canonical text', 'text', '''
    DECLARE value json; item record; values_text text; constraints text[]:=ARRAY[]::text[];
    BEGIN
      IF canonical IS NULL OR octet_length(canonical) NOT BETWEEN 1 AND 65536 OR NOT gateway.source_json_unique(canonical::json) THEN
        RAISE EXCEPTION 'Canonical decision unavailable' USING ERRCODE='42501'; END IF;
      value:=canonical::json;
      FOR item IN SELECT v FROM json_array_elements(value->'row_constraints') a(v) ORDER BY v->>'member' COLLATE "C" LOOP
        SELECT '['||string_agg(token,',' ORDER BY token COLLATE "C")||']' INTO values_text
          FROM (SELECT DISTINCT v::text token FROM json_array_elements(item.v->'values') a(v)) e;
        constraints:=array_append(constraints,'{"member":'||(item.v->'member')::text||',"values":'||values_text||'}');
      END LOOP;
      RETURN '{"policy_revision":'||(value->'policy_revision')::text||',"principal_id":'||(value->'principal_id')::text
        ||',"row_constraints":['||array_to_string(constraints,',')||'],"tenant_id":'||(value->'tenant_id')::text||'}';
    END''', immutable=True)
    function('source_fixture_decision_digest', 'attempt_id text', 'text', '''
    BEGIN RETURN (SELECT encode(sha256(convert_to(decision_canonical,'UTF8')),'hex') FROM gateway.fixture_attempts WHERE id=attempt_id); END''')
    function('source_fixture_scope_digest', 'attempt_id text', 'text', '''
    BEGIN RETURN (SELECT encode(sha256(convert_to(gateway.source_fixture_scope_text(decision_canonical),'UTF8')),'hex') FROM gateway.fixture_attempts WHERE id=attempt_id); END''')
    function('source_fixture_draft_witness', '', 'trigger', '''
    BEGIN
      IF NEW.manifest_canonical IS NOT NULL AND NOT gateway.source_json_witness(NEW.manifest_canonical,NEW.manifest-'content_digest',NEW.content_digest) THEN
        RAISE EXCEPTION 'Invalid protected draft witness' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END''')
    op.execute('CREATE TRIGGER source_fixture_draft_witness BEFORE INSERT ON gateway.draft_revisions FOR EACH ROW EXECUTE FUNCTION gateway.source_fixture_draft_witness()')
    function('source_fixture_principal', 'principal_id text', 'jsonb', '''
    DECLARE identity gateway.principals%ROWTYPE;
    BEGIN
      SELECT * INTO STRICT identity FROM gateway.principals WHERE id=principal_id AND active;
      IF (identity.kind='demo' AND identity.service_client_id IS NOT NULL)
        OR (identity.kind IN ('user','service') AND identity.service_client_id IS NULL) THEN
        RAISE EXCEPTION 'Source fixture configuration unavailable' USING ERRCODE='42501'; END IF;
      RETURN jsonb_build_object('id',identity.id,'subject',identity.subject,'kind',identity.kind,'service_client_id',identity.service_client_id);
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source fixture configuration unavailable' USING ERRCODE='42501'; END''')
    function('source_fixture_identity_entry', 'document jsonb, principal jsonb, fixture_id text', 'text', '''
    DECLARE entry jsonb; matched text; matches integer:=0;
    BEGIN
      FOR entry IN SELECT v FROM jsonb_array_elements(document->'audiences') a(v) LOOP
        IF (entry->>'context'='fixture_test' AND entry->>'principal_id'=principal->>'id'
          AND entry->>'principal_kind'=principal->>'kind' AND entry->'client_ids' @> jsonb_build_array(principal->'service_client_id')
          AND entry->'fixture_case_ids' ? fixture_id AND NOT entry->'actions' ?| ARRAY['author','publish']) IS TRUE THEN
          matches:=matches+1;matched:=entry->>'id'; END IF;
      END LOOP;
      IF matches=1 THEN RETURN matched; END IF;
      RETURN NULL;
    END''')
    function('source_fixture_audience', 'document jsonb, principal jsonb, decision jsonb, action text, fixture_id text DEFAULT NULL', 'text', '''
    DECLARE entry jsonb; matched text; matches integer:=0; wanted jsonb; approved jsonb; valid boolean;
    BEGIN
      FOR entry IN SELECT v FROM jsonb_array_elements(document->'audiences') a(v) LOOP
        IF (entry->>'context'='fixture_test' AND entry->>'principal_id'=principal->>'id' AND entry->>'principal_kind'=principal->>'kind'
          AND entry->'client_ids' @> jsonb_build_array(principal->'service_client_id') AND entry->'actions' ? action
          AND decision->>'principal_id'=principal->>'id' AND entry->>'tenant_id'=decision->>'tenant_id'
          AND (entry->'visible_fields') @> (decision->'visible_fields')
          AND jsonb_array_length(entry->'row_constraints')=jsonb_array_length(decision->'row_constraints')
          AND ((fixture_id IS NULL AND entry->'fixture_case_ids'='[]'::jsonb AND principal->'service_client_id'<>'null'::jsonb)
            OR (fixture_id IS NOT NULL AND entry->'fixture_case_ids' ? fixture_id))) IS NOT TRUE THEN CONTINUE; END IF;
        valid:=true;
        FOR wanted IN SELECT v FROM jsonb_array_elements(decision->'row_constraints') a(v) LOOP
          SELECT v INTO approved FROM jsonb_array_elements(entry->'row_constraints') a(v) WHERE v->'member'=wanted->'member' AND v->'operator'=wanted->'operator';
          IF approved IS NULL OR NOT (approved->'values') @> (wanted->'values')
            OR (wanted->>'operator'='eq' AND approved->'values' IS DISTINCT FROM wanted->'values') THEN valid:=false;EXIT; END IF;
        END LOOP;
        IF valid THEN matches:=matches+1;matched:=entry->>'id'; END IF;
      END LOOP;
      IF matches<>1 THEN RAISE EXCEPTION 'Source access unavailable' USING ERRCODE='42501'; END IF;
      RETURN matched;
    END''')
    function('source_fixture_catalog_valid', 'document jsonb, manifest jsonb, manifest_canonical text, catalog jsonb, canonical text', 'boolean', '''
    DECLARE fixture jsonb; input record; domain jsonb; seen text[]:=ARRAY[]::text[]; positive integer:=0; negative integer:=0;
      wanted jsonb; digest text;
    BEGIN
      IF NOT gateway.source_json_witness(manifest_canonical,manifest-'content_digest',manifest->>'content_digest')
        OR NOT gateway.source_json_witness(canonical,catalog,document->'artifacts'->>'fixture_catalog_digest')
        OR NOT gateway.source_approval_keys(catalog,ARRAY['product_id','consumer_binding','fixture_binding','consumer_source','fixture_source','execution_contract_digest','cases'])
        OR catalog->'product_id' IS DISTINCT FROM manifest->'product_id'
        OR catalog->'consumer_binding' IS DISTINCT FROM (SELECT content FROM gateway.binding_revisions WHERE binding_id=document->'bindings'->'consumer'->>'binding_id' AND revision=(document->'bindings'->'consumer'->>'revision')::integer)
        OR catalog->'fixture_binding' IS DISTINCT FROM (SELECT content FROM gateway.binding_revisions WHERE binding_id=document->'bindings'->'fixture'->>'binding_id' AND revision=(document->'bindings'->'fixture'->>'revision')::integer)
        OR catalog->'consumer_source' IS DISTINCT FROM (SELECT descriptor FROM gateway.source_profiles WHERE source_id=document->'sources'->'consumer'->>'source_id' AND revision=(document->'sources'->'consumer'->>'revision')::integer)
        OR catalog->'fixture_source' IS DISTINCT FROM (SELECT descriptor FROM gateway.source_profiles WHERE source_id=document->'sources'->'fixture'->>'source_id' AND revision=(document->'sources'->'fixture'->>'revision')::integer)
        OR jsonb_typeof(catalog->'cases') IS DISTINCT FROM 'array' OR jsonb_array_length(catalog->'cases') NOT BETWEEN 2 AND 8 THEN RETURN false; END IF;
      digest:=encode(sha256(convert_to(gateway.source_fixture_omit(manifest_canonical,ARRAY['name','description','examples','owner','domain','links','version','content_digest','fixtures']),'UTF8')),'hex');
      IF catalog->>'execution_contract_digest' IS DISTINCT FROM digest THEN RETURN false; END IF;
      FOR fixture IN SELECT v FROM jsonb_array_elements(catalog->'cases') a(v) LOOP
        IF NOT gateway.source_approval_keys(fixture,ARRAY['fixture','principal_id','kind','relative_tolerance'])
          OR fixture->>'kind' NOT IN ('known_answer','input_negative','permission_negative')
          OR fixture->'fixture'->>'id'=ANY(seen)
          OR jsonb_typeof(fixture->'fixture'->'inputs') IS DISTINCT FROM 'object'
          OR fixture->'fixture'->>'expected_error' IS DISTINCT FROM (CASE fixture->>'kind' WHEN 'input_negative' THEN 'INVALID_INPUT' WHEN 'permission_negative' THEN 'NOT_FOUND_OR_FORBIDDEN' ELSE NULL END) THEN RETURN false; END IF;
        seen:=array_append(seen,fixture->'fixture'->>'id');
        IF fixture->>'kind'='known_answer' THEN positive:=positive+1; END IF;
        IF fixture->>'kind'='permission_negative' THEN negative:=negative+1; END IF;
        FOR input IN SELECT * FROM jsonb_each(fixture->'fixture'->'inputs') LOOP
          SELECT v INTO domain FROM jsonb_array_elements(document->'input_domains') a(v) WHERE v->>'name'=input.key;
          IF domain IS NULL OR NOT gateway.source_domain_contains(domain,input.value) THEN RETURN false; END IF;
        END LOOP;
        IF fixture->>'kind'<>'permission_negative' AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(manifest->'fixtures') a(v) WHERE v=fixture->'fixture') THEN RETURN false; END IF;
      END LOOP;
      FOR wanted IN SELECT v FROM jsonb_array_elements(manifest->'fixtures') a(v) LOOP
        IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(catalog->'cases') a(v) WHERE v->'fixture'=wanted) THEN RETURN false; END IF;
      END LOOP;
      RETURN positive BETWEEN 1 AND 8 AND negative>0;
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END''')
    function('source_fixture_package', 'invocation_id uuid', 'boolean', '''
    DECLARE inv gateway.fixture_source_invocations%ROWTYPE; approval gateway.source_access_approvals%ROWTYPE; draft gateway.draft_revisions%ROWTYPE;
    BEGIN
      SELECT * INTO STRICT inv FROM gateway.fixture_source_invocations WHERE id=invocation_id;
      SELECT * INTO STRICT approval FROM gateway.source_access_approvals WHERE id=inv.approval_id;
      SELECT * INTO STRICT draft FROM gateway.draft_revisions WHERE id=inv.draft_id;
      RETURN (gateway.source_approval_package_valid(approval.content)
        AND (inv.product_id,inv.draft_digest,inv.consumer_binding_id,inv.consumer_binding_digest,inv.fixture_binding_id,inv.fixture_binding_digest,
          inv.approval_revision,inv.approval_digest,inv.package_pins_digest,inv.catalog_digest,inv.accepted_approval_expires_at)
          =(approval.product_id,approval.manifest_digest,approval.consumer_binding_id,approval.consumer_binding_digest,approval.fixture_binding_id,
            approval.fixture_binding_digest,approval.revision,approval.content_digest,approval.package_pins_digest,approval.fixture_catalog_digest,approval.expires_at)
        AND draft.manifest->>'version'=approval.product_version AND draft.manifest->>'policy_id'=approval.policy_id
        AND draft.content_digest=inv.draft_digest AND inv.catalog->>'execution_contract_digest'=inv.execution_contract_digest
        AND gateway.source_fixture_catalog_valid(approval.content,draft.manifest,draft.manifest_canonical,inv.catalog,inv.catalog_canonical)
        AND inv.actor_digest=encode(sha256(convert_to(gateway.source_approval_canonical(inv.actor_snapshot::json),'UTF8')),'hex')) IS TRUE;
    EXCEPTION WHEN NO_DATA_FOUND THEN RETURN false;
    END''')
    function('source_fixture_case_json', 'invocation_id uuid, fixture_id text', 'jsonb', '''
    DECLARE c gateway.fixture_source_invocation_cases%ROWTYPE;
    BEGIN
      SELECT * INTO STRICT c FROM gateway.fixture_source_invocation_cases c WHERE c.invocation_id=source_fixture_case_json.invocation_id AND c.fixture_id=source_fixture_case_json.fixture_id;
      RETURN jsonb_build_object('invocation_id',c.invocation_id,'fixture_id',c.fixture_id,'kind',c.kind,'fixture_version',c.fixture_version,
        'case_digest',c.case_digest,'principal',c.principal_snapshot,'case',c.case_content,'catalog_digest',c.catalog_digest);
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source fixture configuration unavailable' USING ERRCODE='42501'; END''')
    function('source_fixture_structural', 'invocation_id uuid', 'void', '''
    DECLARE inv gateway.fixture_source_invocations%ROWTYPE; approval gateway.source_access_approvals%ROWTYPE; c gateway.fixture_source_invocation_cases%ROWTYPE;
      actual integer:=0; expected jsonb; roster jsonb:='[]';
    BEGIN
      SELECT * INTO STRICT inv FROM gateway.fixture_source_invocations WHERE id=invocation_id;
      SELECT * INTO STRICT approval FROM gateway.source_access_approvals WHERE id=inv.approval_id;
      IF NOT gateway.source_fixture_package(inv.id) THEN RAISE EXCEPTION 'Source fixture configuration unavailable' USING ERRCODE='42501'; END IF;
      FOR c IN SELECT * FROM gateway.fixture_source_invocation_cases c WHERE c.invocation_id=inv.id ORDER BY fixture_id LOOP
        actual:=actual+1;
        SELECT v INTO STRICT expected FROM jsonb_array_elements(inv.catalog->'cases') a(v) WHERE v->'fixture'->>'id'=c.fixture_id;
        IF c.case_content IS DISTINCT FROM expected OR c.kind IS DISTINCT FROM expected->>'kind' OR c.fixture_version IS DISTINCT FROM expected->'fixture'->>'version'
          OR c.catalog_digest IS DISTINCT FROM inv.catalog_digest
          OR c.case_digest IS DISTINCT FROM encode(sha256(convert_to(gateway.source_fixture_case_text(inv.catalog_canonical,c.fixture_id),'UTF8')),'hex')
          OR c.principal_snapshot IS DISTINCT FROM gateway.source_fixture_principal(expected->>'principal_id')
          OR c.principal_digest IS DISTINCT FROM encode(sha256(convert_to(gateway.source_approval_canonical(c.principal_snapshot::json),'UTF8')),'hex')
          OR c.data_entry_id IS DISTINCT FROM gateway.source_fixture_identity_entry(approval.content,c.principal_snapshot,c.fixture_id) THEN
          RAISE EXCEPTION 'Source fixture configuration unavailable' USING ERRCODE='42501'; END IF;
        IF c.kind='known_answer' THEN roster:=roster||jsonb_build_array(c.fixture_id); END IF;
      END LOOP;
      IF actual<>jsonb_array_length(inv.catalog->'cases') OR roster IS DISTINCT FROM inv.positive_roster THEN
        RAISE EXCEPTION 'Source fixture configuration unavailable' USING ERRCODE='42501'; END IF;
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source fixture configuration unavailable' USING ERRCODE='42501'; END''')


def _fixture_owners(op, function):
    function('source_read_fixture_invocation', 'invocation_id uuid', 'jsonb', '''
    DECLARE inv gateway.fixture_source_invocations%ROWTYPE; draft gateway.draft_revisions%ROWTYPE; cases jsonb;
    BEGIN
      SELECT * INTO STRICT inv FROM gateway.fixture_source_invocations WHERE id=invocation_id;
      SELECT * INTO STRICT draft FROM gateway.draft_revisions WHERE id=inv.draft_id;
      SELECT jsonb_agg(gateway.source_fixture_case_json(c.invocation_id,c.fixture_id) ORDER BY c.fixture_id) INTO cases
        FROM gateway.fixture_source_invocation_cases c WHERE c.invocation_id=inv.id;
      RETURN jsonb_build_object('invocation_id',inv.id,'product',jsonb_build_object('manifest',draft.manifest,'binding_digest',inv.consumer_binding_digest,'version_id',inv.draft_id),
        'draft_id',inv.draft_id,'draft_digest',inv.draft_digest,'catalog_digest',inv.catalog_digest,'execution_contract_digest',inv.execution_contract_digest,
        'consumer_binding_digest',inv.consumer_binding_digest,'fixture_binding_digest',inv.fixture_binding_digest,'actor',inv.actor_snapshot,
        'accepted',jsonb_build_object('approval_id',inv.approval_id,'approval_revision',inv.approval_revision,'approval_digest',inv.approval_digest,
          'audience_entry_id',inv.author_entry_id,'principal',inv.actor_snapshot,'package_pins_digest',inv.package_pins_digest,
          'accepted_approval_expires_at',inv.accepted_approval_expires_at,'effective_deadline_at',inv.effective_deadline_at),
        'effective_deadline_at',inv.effective_deadline_at,'cases',cases);
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END''')
    function('source_accept_fixture_invocation', 'draft_id text, actor_snapshot jsonb, catalog_canonical text, run_id uuid', 'jsonb', '''
    DECLARE draft gateway.draft_revisions%ROWTYPE; head gateway.products%ROWTYPE; approval gateway.source_access_approvals%ROWTYPE;
      selected gateway.source_access_approvals%ROWTYPE; catalog jsonb; digest text; decision jsonb; fixture jsonb; fixed jsonb;
      entry text; chosen text; entries jsonb; selected_entries jsonb; matches integer:=0; candidates integer:=0;
      roster jsonb; now_at timestamptz; valid boolean; accepted jsonb;
    BEGIN
      IF draft_id IS NULL OR actor_snapshot IS NULL OR run_id IS NULL OR catalog_canonical IS NULL
        OR octet_length(catalog_canonical) NOT BETWEEN 1 AND 65536 OR NOT gateway.source_json_unique(catalog_canonical::json) THEN
        RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
      PERFORM 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Source capacity unavailable' USING ERRCODE='42501'; END IF;
      SELECT * INTO STRICT draft FROM gateway.draft_revisions d WHERE d.id=draft_id;
      SELECT * INTO STRICT head FROM gateway.products WHERE id=draft.product_id FOR UPDATE;
      PERFORM 1 FROM gateway.draft_revisions WHERE id=draft.id FOR UPDATE;
      PERFORM 1 FROM gateway.policies WHERE id=draft.manifest->>'policy_id' FOR UPDATE;
      IF head.latest_draft_id IS DISTINCT FROM draft.id OR head.draft_revision IS DISTINCT FROM draft.revision THEN
        RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
      decision:=gateway.source_current_decision(actor_snapshot,draft.manifest,'author');
      catalog:=catalog_canonical::jsonb;digest:=encode(sha256(convert_to(catalog_canonical,'UTF8')),'hex');
      FOR approval IN SELECT a.* FROM gateway.source_access_approvals a JOIN gateway.source_access_approval_states s ON s.approval_id=a.id
        WHERE a.product_id=draft.product_id AND a.manifest_digest=draft.content_digest AND a.product_version=draft.manifest->>'version'
          AND a.fixture_catalog_digest=digest AND s.state='active' AND s.state_revision=1 AND a.expires_at>clock_timestamp() ORDER BY a.id LIMIT 101 LOOP
        candidates:=candidates+1;
        IF candidates>100 OR NOT gateway.source_approval_package_valid(approval.content)
          OR NOT gateway.source_fixture_catalog_valid(approval.content,draft.manifest,draft.manifest_canonical,catalog,catalog_canonical) THEN
          RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
        BEGIN entry:=gateway.source_fixture_audience(approval.content,actor_snapshot,decision,'author');
        EXCEPTION WHEN insufficient_privilege THEN CONTINUE; END;
        entries:='{}';valid:=true;
        FOR fixture IN SELECT v FROM jsonb_array_elements(catalog->'cases') a(v) LOOP
          fixed:=gateway.source_fixture_principal(fixture->>'principal_id');
          chosen:=gateway.source_fixture_identity_entry(approval.content,fixed,fixture->'fixture'->>'id');
          IF chosen IS NULL THEN valid:=false;EXIT; END IF;
          entries:=entries||jsonb_build_object(fixture->'fixture'->>'id',chosen);
        END LOOP;
        IF NOT valid THEN CONTINUE; END IF;
        matches:=matches+1;selected:=approval;chosen:=entry;selected_entries:=entries;
      END LOOP;
      IF matches<>1 THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
      entry:=gateway.source_fixture_audience(selected.content,actor_snapshot,decision,'author');
      SELECT jsonb_agg(v->'fixture'->>'id' ORDER BY v->'fixture'->>'id') INTO roster FROM jsonb_array_elements(catalog->'cases') a(v) WHERE v->>'kind'='known_answer';
      now_at:=clock_timestamp();
      INSERT INTO gateway.fixture_source_invocations(id,product_id,draft_id,draft_digest,consumer_binding_id,consumer_binding_digest,
        fixture_binding_id,fixture_binding_digest,catalog,catalog_canonical,catalog_digest,execution_contract_digest,actor_snapshot,actor_digest,
        approval_id,approval_revision,approval_digest,author_entry_id,package_pins_digest,positive_roster,created_at,accepted_approval_expires_at,effective_deadline_at)
      VALUES(run_id,draft.product_id,draft.id,draft.content_digest,selected.consumer_binding_id,selected.consumer_binding_digest,
        selected.fixture_binding_id,selected.fixture_binding_digest,catalog,catalog_canonical,digest,catalog->>'execution_contract_digest',actor_snapshot,
        encode(sha256(convert_to(gateway.source_approval_canonical(actor_snapshot::json),'UTF8')),'hex'),selected.id,selected.revision,selected.content_digest,
        entry,selected.package_pins_digest,roster,now_at,selected.expires_at,least(selected.expires_at,now_at+make_interval(secs=>(decision->'limits'->>'execution_timeout_seconds')::integer)));
      FOR fixture IN SELECT v FROM jsonb_array_elements(catalog->'cases') a(v) LOOP
        fixed:=gateway.source_fixture_principal(fixture->>'principal_id');
        INSERT INTO gateway.fixture_source_invocation_cases(invocation_id,fixture_id,kind,fixture_version,case_content,case_digest,principal_snapshot,principal_digest,data_entry_id,catalog_digest)
        VALUES(run_id,fixture->'fixture'->>'id',fixture->>'kind',fixture->'fixture'->>'version',fixture,
          encode(sha256(convert_to(gateway.source_fixture_case_text(catalog_canonical,fixture->'fixture'->>'id'),'UTF8')),'hex'),fixed,
          encode(sha256(convert_to(gateway.source_approval_canonical(fixed::json),'UTF8')),'hex'),selected_entries->>(fixture->'fixture'->>'id'),digest);
      END LOOP;
      -- Caller-generated UUID is unique and now bound to immutable owner rows.
      -- This SQL-owned audit cannot be omitted by a direct application caller.
      INSERT INTO gateway.audit_events(id,actor_id,service_client_id,action,product_id,version_id,policy_id,policy_revision,decision,
        correlation_id,source_approval_id,source_approval_revision,source_approval_digest,source_approval_state_revision,source_access_reason_code)
      VALUES(gen_random_uuid()::text,actor_snapshot->>'id',actor_snapshot->>'service_client_id','author',draft.product_id,draft.id,draft.manifest->>'policy_id',
        (decision->>'policy_revision')::integer,'allow',run_id::text,selected.id,selected.revision,selected.content_digest,1,'APPROVED');
      PERFORM gateway.source_fixture_invocation_access(run_id::text,'author',true);
      IF (SELECT effective_deadline_at<=clock_timestamp() FROM gateway.fixture_source_invocations WHERE id=run_id) THEN
        RAISE EXCEPTION 'Fixture acceptance expired' USING ERRCODE='42501'; END IF;
      RETURN gateway.source_read_fixture_invocation(run_id);
    EXCEPTION WHEN invalid_text_representation OR NO_DATA_FOUND THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END''')
    function('source_fixture_invocation_access', 'invocation_id text, action text, lock_approval boolean', 'jsonb', '''
    DECLARE inv gateway.fixture_source_invocations%ROWTYPE; draft gateway.draft_revisions%ROWTYPE; approval gateway.source_access_approvals%ROWTYPE;
      decision jsonb; entry text; now_at timestamptz;
    BEGIN
      IF action IS DISTINCT FROM 'author' OR lock_approval IS NULL THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
      SELECT * INTO STRICT inv FROM gateway.fixture_source_invocations WHERE id=invocation_id::uuid;
      SELECT * INTO STRICT draft FROM gateway.draft_revisions WHERE id=inv.draft_id;
      SELECT * INTO STRICT approval FROM gateway.source_access_approvals WHERE id=inv.approval_id;
      PERFORM gateway.source_fixture_structural(inv.id);
      decision:=gateway.source_current_decision(inv.actor_snapshot,draft.manifest,'author');
      entry:=gateway.source_fixture_audience(approval.content,inv.actor_snapshot,decision,'author');
      IF entry IS DISTINCT FROM inv.author_entry_id THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
      IF EXISTS(SELECT 1 FROM gateway.fixture_test_source_authorities WHERE invocation_id=inv.id) THEN
        PERFORM gateway.source_fixture_evidence(inv.id);
      END IF;
      IF lock_approval THEN PERFORM gateway.source_approval_assert(inv.approval_id,inv.approval_revision,inv.approval_digest); END IF;
      now_at:=clock_timestamp();
      IF NOT EXISTS(SELECT 1 FROM gateway.source_access_approval_states WHERE approval_id=inv.approval_id AND state='active' AND state_revision=1)
        OR approval.expires_at<=now_at THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
      RETURN jsonb_build_object('accepted',gateway.source_read_fixture_invocation(inv.id)->'accepted',
        'projection',gateway.source_approval_projection(inv.approval_id),'decision',decision,'audience_entry_id',entry,
        'admitted_decision',NULL,'admitted_decision_digest',NULL,'manifest',draft.manifest,'binding_digest',inv.consumer_binding_digest,'version_id',inv.draft_id,
        'fixture_case_id',NULL,'invoking_actor',NULL,'invoking_actor_decision',NULL,'invoking_actor_entry_id',NULL);
    EXCEPTION WHEN invalid_text_representation OR NO_DATA_FOUND THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END''')
    function('source_read_fixture_case', 'invocation_id uuid, fixture_id text', 'jsonb', '''
    BEGIN
      PERFORM gateway.source_fixture_invocation_access(invocation_id::text,'author',false);
      RETURN gateway.source_fixture_case_json(invocation_id,fixture_id);
    END''')
    function('source_fixture_invocation_complete', '', 'trigger', '''
    BEGIN
      PERFORM gateway.source_fixture_invocation_access(NEW.id::text,'author',true);
      IF NOT EXISTS(SELECT 1 FROM gateway.fixture_test_source_authorities WHERE invocation_id=NEW.id)
        AND NEW.effective_deadline_at<=clock_timestamp() THEN
        RAISE EXCEPTION 'Fixture acceptance expired' USING ERRCODE='42501'; END IF;
      RETURN NULL;
    END''')
    op.execute('CREATE CONSTRAINT TRIGGER source_fixture_invocation_complete AFTER INSERT ON gateway.fixture_source_invocations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.source_fixture_invocation_complete()')
    function('source_fixture_attempt_prefix', '', 'trigger', '''
    BEGIN
      IF (SELECT adapter_revision FROM gateway.binding_revisions WHERE id=NEW.fixture_binding_id) IN ('cube-bigquery-v1','domo-named-v2') THEN
        IF NEW.source_invocation_id IS NULL THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
        PERFORM 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'Source capacity unavailable' USING ERRCODE='42501'; END IF;
        PERFORM 1 FROM gateway.products WHERE id=NEW.product_id FOR UPDATE;
        PERFORM 1 FROM gateway.draft_revisions WHERE id=NEW.draft_id FOR UPDATE;
        PERFORM 1 FROM gateway.policies WHERE id=NEW.policy_id FOR UPDATE;
      ELSIF NEW.source_invocation_id IS NOT NULL THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
      RETURN NEW;
    END''')
    op.execute('CREATE TRIGGER aaa_source_fixture_prefix BEFORE INSERT ON gateway.fixture_attempts FOR EACH ROW EXECUTE FUNCTION gateway.source_fixture_attempt_prefix()')
    function('source_fixture_attempt_capture', '', 'trigger', '''
    DECLARE inv gateway.fixture_source_invocations%ROWTYPE; c gateway.fixture_source_invocation_cases%ROWTYPE; approval gateway.source_access_approvals%ROWTYPE;
      draft gateway.draft_revisions%ROWTYPE; derived jsonb; decision jsonb; entry text;
    BEGIN
      IF NEW.source_invocation_id IS NULL THEN RETURN NULL; END IF;
      SELECT * INTO STRICT inv FROM gateway.fixture_source_invocations WHERE id=NEW.source_invocation_id;
      SELECT * INTO STRICT c FROM gateway.fixture_source_invocation_cases WHERE invocation_id=inv.id AND fixture_id=NEW.fixture_id;
      SELECT * INTO STRICT approval FROM gateway.source_access_approvals WHERE id=inv.approval_id;
      SELECT * INTO STRICT draft FROM gateway.draft_revisions WHERE id=inv.draft_id;
      PERFORM gateway.source_fixture_structural(inv.id);
      decision:=gateway.source_current_decision(c.principal_snapshot,draft.manifest,'execute');
      entry:=gateway.source_fixture_audience(approval.content,c.principal_snapshot,decision,'execute',c.fixture_id);
      derived:=(draft.manifest-'content_digest')||jsonb_build_object('binding_id',inv.catalog->'fixture_binding'->>'binding_id',
        'binding_revision',inv.catalog->'fixture_binding'->'revision','model_bundle_digest',inv.catalog->'fixture_binding'->>'model_bundle_digest');
      IF NOT gateway.source_json_witness(NEW.fixture_manifest_canonical,derived,NEW.fixture_manifest_digest)
        OR NOT gateway.source_json_witness(NEW.decision_canonical,decision,encode(sha256(convert_to(NEW.decision_canonical,'UTF8')),'hex')) THEN
        RAISE EXCEPTION 'Canonical fixture witness unavailable' USING ERRCODE='42501'; END IF;
      derived:=derived||jsonb_build_object('content_digest',NEW.fixture_manifest_digest);
      IF c.kind IS DISTINCT FROM 'known_answer' OR NEW.actor_id IS DISTINCT FROM inv.actor_snapshot->>'id'
        OR (NEW.product_id,NEW.draft_id,NEW.draft_digest,NEW.consumer_binding_id,NEW.consumer_binding_digest,NEW.fixture_binding_id,NEW.fixture_binding_digest,
          NEW.catalog_digest,NEW.execution_contract_digest,NEW.fixture_version)
        IS DISTINCT FROM (inv.product_id,inv.draft_id,inv.draft_digest,inv.consumer_binding_id,inv.consumer_binding_digest,inv.fixture_binding_id,inv.fixture_binding_digest,
          inv.catalog_digest,inv.execution_contract_digest,c.fixture_version)
        OR NEW.principal_snapshot IS DISTINCT FROM c.principal_snapshot OR NEW.fixed_principal_id IS DISTINCT FROM c.principal_snapshot->>'id'
        OR NEW.fixture_manifest IS DISTINCT FROM derived OR NEW.fixture_manifest_digest IS DISTINCT FROM derived->>'content_digest'
        OR NEW.decision IS DISTINCT FROM decision OR NEW.policy_id IS DISTINCT FROM draft.manifest->>'policy_id'
        OR NEW.policy_revision IS DISTINCT FROM (decision->>'policy_revision')::integer
        OR NOT gateway.source_json_witness(NEW.query_canonical,NEW.canonical_query,NEW.canonical_hash)
        OR NEW.canonical_query->'filters' IS DISTINCT FROM '[]'::jsonb OR NEW.canonical_query->'sort' IS DISTINCT FROM '[]'::jsonb
        OR NOT gateway.source_query_reviewed(approval.content,draft.manifest,NEW.canonical_query,decision->'limits')
        OR EXISTS(SELECT 1 FROM jsonb_each(c.case_content->'fixture'->'inputs') e WHERE NEW.canonical_query->'inputs'->e.key IS DISTINCT FROM e.value)
        OR EXISTS(SELECT 1 FROM jsonb_each(NEW.canonical_query->'inputs') e WHERE NOT c.case_content->'fixture'->'inputs' ? e.key
          AND e.value IS DISTINCT FROM draft.manifest->'inputs'->e.key->'default'->'value') THEN
        RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
      INSERT INTO gateway.fixture_attempt_source_authorities(attempt_id,invocation_id,fixture_id,approval_id,approval_revision,approval_digest,
        author_entry_id,data_entry_id,package_pins_digest,accepted_approval_expires_at,effective_deadline_at)
      VALUES(NEW.id,inv.id,c.fixture_id,inv.approval_id,inv.approval_revision,inv.approval_digest,inv.author_entry_id,entry,inv.package_pins_digest,
        inv.accepted_approval_expires_at,least(NEW.deadline_at,inv.effective_deadline_at,inv.accepted_approval_expires_at));
      RETURN NULL;
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END''')
    op.execute('CREATE TRIGGER source_fixture_attempt_capture AFTER INSERT ON gateway.fixture_attempts FOR EACH ROW EXECUTE FUNCTION gateway.source_fixture_attempt_capture()')
    function('source_read_fixture_attempt', 'attempt_id text', 'jsonb', '''
    DECLARE a gateway.fixture_attempts%ROWTYPE; auth gateway.fixture_attempt_source_authorities%ROWTYPE; inv gateway.fixture_source_invocations%ROWTYPE;
    BEGIN
      SELECT * INTO STRICT a FROM gateway.fixture_attempts WHERE id=attempt_id;
      SELECT * INTO STRICT auth FROM gateway.fixture_attempt_source_authorities x WHERE x.attempt_id=a.id;
      SELECT * INTO STRICT inv FROM gateway.fixture_source_invocations WHERE id=auth.invocation_id;
      RETURN jsonb_build_object('attempt_id',a.id,'invocation_id',inv.id,'fixture_id',a.fixture_id,
        'product',gateway.source_read_fixture_invocation(inv.id)->'product','principal',a.principal_snapshot,'actor',inv.actor_snapshot,'admitted_decision',a.decision,
        'accepted',jsonb_build_object('approval_id',auth.approval_id,'approval_revision',auth.approval_revision,'approval_digest',auth.approval_digest,
          'audience_entry_id',auth.data_entry_id,'principal',a.principal_snapshot,'package_pins_digest',auth.package_pins_digest,
          'accepted_approval_expires_at',auth.accepted_approval_expires_at,'effective_deadline_at',auth.effective_deadline_at),
        'effective_deadline_at',auth.effective_deadline_at);
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END''')
    function('source_fixture_attempt_access', 'attempt_id text, action text, lock_approval boolean', 'jsonb', '''
    DECLARE a gateway.fixture_attempts%ROWTYPE; auth gateway.fixture_attempt_source_authorities%ROWTYPE; inv gateway.fixture_source_invocations%ROWTYPE;
      c gateway.fixture_source_invocation_cases%ROWTYPE; approval gateway.source_access_approvals%ROWTYPE; parent jsonb; decision jsonb; entry text; now_at timestamptz;
    BEGIN
      IF action IS NULL OR action NOT IN ('execute','result') OR lock_approval IS NULL THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
      SELECT * INTO STRICT a FROM gateway.fixture_attempts WHERE id=attempt_id;
      SELECT * INTO STRICT auth FROM gateway.fixture_attempt_source_authorities x WHERE x.attempt_id=a.id;
      SELECT * INTO STRICT inv FROM gateway.fixture_source_invocations WHERE id=auth.invocation_id;
      SELECT * INTO STRICT c FROM gateway.fixture_source_invocation_cases WHERE invocation_id=inv.id AND fixture_id=auth.fixture_id;
      SELECT * INTO STRICT approval FROM gateway.source_access_approvals WHERE id=inv.approval_id;
      parent:=gateway.source_fixture_invocation_access(inv.id::text,'author',false);
      decision:=gateway.source_current_decision(a.principal_snapshot,parent->'manifest',action);
      entry:=gateway.source_fixture_audience(approval.content,a.principal_snapshot,decision,action,c.fixture_id);
      IF (auth.approval_id,auth.approval_revision,auth.approval_digest,auth.author_entry_id,auth.package_pins_digest,auth.accepted_approval_expires_at)
        IS DISTINCT FROM (inv.approval_id,inv.approval_revision,inv.approval_digest,inv.author_entry_id,inv.package_pins_digest,inv.accepted_approval_expires_at)
        OR a.source_invocation_id IS DISTINCT FROM inv.id OR a.fixture_id IS DISTINCT FROM c.fixture_id OR a.principal_snapshot IS DISTINCT FROM c.principal_snapshot
        OR auth.data_entry_id IS DISTINCT FROM gateway.source_fixture_audience(approval.content,a.principal_snapshot,a.decision,'execute',c.fixture_id)
        OR NOT gateway.source_fixture_same_scope(decision,a.decision)
        OR NOT (a.decision->'visible_fields') @> (decision->'visible_fields')
        OR (parent->'manifest'->>'adapter_revision'='domo-named-v2' AND decision IS DISTINCT FROM a.decision)
        OR auth.effective_deadline_at IS DISTINCT FROM least(a.deadline_at,inv.effective_deadline_at,inv.accepted_approval_expires_at)
        OR NOT gateway.source_query_reviewed(approval.content,parent->'manifest',a.canonical_query,decision->'limits') THEN
        RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
      IF lock_approval THEN PERFORM gateway.source_approval_assert(inv.approval_id,inv.approval_revision,inv.approval_digest); END IF;
      now_at:=clock_timestamp();
      IF approval.expires_at<=now_at OR (action='execute' AND auth.effective_deadline_at<=now_at)
        OR NOT EXISTS(SELECT 1 FROM gateway.source_access_approval_states WHERE approval_id=inv.approval_id AND state='active' AND state_revision=1) THEN
        RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
      RETURN jsonb_build_object('accepted',gateway.source_read_fixture_attempt(a.id)->'accepted','projection',parent->'projection','decision',decision,
        'audience_entry_id',entry,'admitted_decision',a.decision,'admitted_decision_digest',gateway.source_fixture_decision_digest(a.id),
        'manifest',parent->'manifest','binding_digest',inv.consumer_binding_digest,'version_id',inv.draft_id,'fixture_case_id',c.fixture_id,
        'invoking_actor',inv.actor_snapshot,'invoking_actor_decision',parent->'decision','invoking_actor_entry_id',inv.author_entry_id);
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END''')
    function('source_read_fixture_owner', 'owner_kind text, owner_id text, action text', 'jsonb', '''
    BEGIN
      IF owner_kind='fixture_invocation' THEN RETURN gateway.source_fixture_invocation_access(owner_id,action,false);
      ELSIF owner_kind='fixture_attempt' THEN RETURN gateway.source_fixture_attempt_access(owner_id,action,false); END IF;
      RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501';
    END''')
    function('source_fixture_attempt_complete', '', 'trigger', '''
    BEGIN
      IF NEW.source_invocation_id IS NOT NULL THEN PERFORM gateway.source_fixture_attempt_access(NEW.id,'execute',true); END IF;
      RETURN NULL;
    END''')
    op.execute('CREATE CONSTRAINT TRIGGER source_fixture_attempt_complete AFTER INSERT ON gateway.fixture_attempts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.source_fixture_attempt_complete()')


    function('source_fixture_owner_snapshot', 'owner_kind text, owner_id text, spec jsonb', 'jsonb', '''
    DECLARE inv gateway.fixture_source_invocations%ROWTYPE; attempt gateway.fixture_attempts%ROWTYPE;
      access jsonb; accepted jsonb; expected jsonb; principal_id text;
    BEGIN
      IF owner_kind='fixture_invocation' THEN
        SELECT * INTO STRICT inv FROM gateway.fixture_source_invocations WHERE id=owner_id::uuid;
        access:=gateway.source_fixture_invocation_access(owner_id,'author',false);
        principal_id:=inv.actor_snapshot->>'id';
      ELSIF owner_kind='fixture_attempt' THEN
        SELECT * INTO STRICT attempt FROM gateway.fixture_attempts WHERE id=owner_id;
        SELECT * INTO STRICT inv FROM gateway.fixture_source_invocations WHERE id=attempt.source_invocation_id;
        access:=gateway.source_fixture_attempt_access(owner_id,'execute',false);
        principal_id:=attempt.fixed_principal_id;
      ELSE RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
      expected:=jsonb_build_object('principal_id',principal_id,'actor_id',inv.actor_snapshot->>'id',
        'policy_id',access->'manifest'->>'policy_id','version_id',NULL,'draft_id',inv.draft_id,'catalog_digest',inv.catalog_digest);
      IF spec IS DISTINCT FROM expected THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END IF;
      accepted:=access->'accepted';
      RETURN gateway.source_current_authority(expected)||jsonb_build_object('source_access',jsonb_build_object(
        'owner_kind',owner_kind,'owner_id',owner_id,'approval_id',accepted->'approval_id','approval_revision',accepted->'approval_revision',
        'approval_digest',accepted->'approval_digest','state_revision',1,'audience_entry_id',accepted->'audience_entry_id',
        'current_audience_entry_id',access->'audience_entry_id','principal_digest',
        encode(sha256(convert_to(gateway.source_approval_canonical((accepted->'principal')::json),'UTF8')),'hex'),
        'package_pins_digest',accepted->'package_pins_digest','accepted_approval_expires_at',accepted->'accepted_approval_expires_at',
        'effective_deadline_at',accepted->'effective_deadline_at','invocation_id',inv.id,'fixture_case_id',access->'fixture_case_id',
        'actor_digest',inv.actor_digest,'actor_entry_id',inv.author_entry_id,'positive_roster',inv.positive_roster,
        'fixture_binding_digest',inv.fixture_binding_digest,'fixture_source',inv.catalog->'fixture_source'));
    EXCEPTION WHEN invalid_text_representation OR NO_DATA_FOUND THEN RAISE EXCEPTION 'Source fixture unavailable' USING ERRCODE='42501'; END''')


def _fixture_evidence(op, function):
    function('source_fixture_rows_valid', 'fields jsonb, rows_value jsonb', 'boolean', '''
    DECLARE row_value jsonb; field record; field_names text[];
    BEGIN
      IF jsonb_typeof(fields) IS DISTINCT FROM 'object' OR jsonb_typeof(rows_value) IS DISTINCT FROM 'array'
        OR jsonb_array_length(rows_value)>1000 THEN RETURN false; END IF;
      SELECT array_agg(key ORDER BY key) INTO field_names FROM jsonb_each(fields);
      IF cardinality(field_names) IS NULL OR cardinality(field_names) NOT BETWEEN 1 AND 50 THEN RETURN false; END IF;
      FOR row_value IN SELECT v FROM jsonb_array_elements(rows_value) a(v) LOOP
        IF NOT gateway.source_approval_keys(row_value,field_names) THEN RETURN false; END IF;
        FOR field IN SELECT * FROM jsonb_each(fields) LOOP
          IF NOT gateway.source_field_value(field.value,row_value->field.key) THEN RETURN false; END IF;
        END LOOP;
      END LOOP;
      RETURN true;
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END''', immutable=True)
    function('source_fixture_row_matches', 'fields jsonb, wanted jsonb, actual jsonb, absolute numeric, relative numeric', 'boolean', '''
    DECLARE field record; left_value jsonb; right_value jsonb;
    BEGIN
      FOR field IN SELECT * FROM jsonb_each(fields) LOOP
        left_value:=wanted->field.key;right_value:=actual->field.key;
        IF field.value->>'type'='decimal' AND left_value<>'null'::jsonb AND right_value<>'null'::jsonb THEN
          IF abs((left_value#>>'{}')::numeric-(right_value#>>'{}')::numeric)>greatest(absolute,relative*abs((left_value#>>'{}')::numeric)) THEN RETURN false; END IF;
        ELSIF left_value IS DISTINCT FROM right_value THEN RETURN false;
        END IF;
      END LOOP;
      RETURN true;
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END''', immutable=True)
    function('source_fixture_row_group', 'fields jsonb, row_value jsonb', 'jsonb', '''
    DECLARE decimal_names text[];
    BEGIN
      SELECT coalesce(array_agg(key),ARRAY[]::text[]) INTO decimal_names FROM jsonb_each(fields)
        WHERE value->>'type'='decimal' AND row_value->key<>'null'::jsonb;
      RETURN row_value-decimal_names;
    END''', immutable=True)
    function('source_fixture_rows_match', 'fields jsonb, wanted jsonb, actual jsonb, absolute_text text, relative_text text', 'boolean', '''
    DECLARE size integer; row_value jsonb; field record; field_names text[]; absolute numeric; relative numeric;
      left_index integer; right_index integer; candidate integer; queue_index integer; current_left integer; found_right integer; previous_right integer;
      adjacency jsonb:='[]'::jsonb; neighbors jsonb; queue integer[]; left_match integer[]; right_match integer[]; parent_right integer[];
      visited_left boolean[]; visited_right boolean[];
    BEGIN
      IF jsonb_typeof(fields) IS DISTINCT FROM 'object' OR jsonb_typeof(wanted) IS DISTINCT FROM 'array' OR jsonb_typeof(actual) IS DISTINCT FROM 'array'
        OR absolute_text IS NULL OR relative_text IS NULL
        OR absolute_text !~ '^(0|[1-9][0-9]{0,37})([.][0-9]{1,38})?$'
        OR relative_text !~ '^(0|[1-9][0-9]{0,37})([.][0-9]{1,38})?$' THEN RETURN false; END IF;
      size:=jsonb_array_length(wanted);
      IF size>1000 OR jsonb_array_length(actual)<>size THEN RETURN false; END IF;
      IF NOT gateway.source_fixture_rows_valid(fields,wanted) OR NOT gateway.source_fixture_rows_valid(fields,actual) THEN RETURN false; END IF;
      IF size=0 THEN RETURN true; END IF;
      -- Fast exact multiset path; duplicates retain their multiplicity.
      IF NOT EXISTS(SELECT v,count(*) FROM jsonb_array_elements(wanted) a(v) GROUP BY v
          EXCEPT SELECT v,count(*) FROM jsonb_array_elements(actual) a(v) GROUP BY v) THEN RETURN true; END IF;
      absolute:=absolute_text::numeric;relative:=relative_text::numeric;
      -- A complete indexed bijection is sufficient positive proof. Failure is
      -- never a negative conclusion: ambiguous permutations still use matching.
      IF NOT EXISTS(SELECT 1 FROM generate_series(0,size-1) a(i)
        WHERE NOT gateway.source_fixture_row_matches(fields,wanted->i,actual->i,absolute,relative)) THEN RETURN true; END IF;
      -- Materialize exact nondecimal keys once; the join avoids decimal work
      -- across different grains and preserves decimal NULL versus non-NULL.
      WITH wanted_rows AS MATERIALIZED (
        SELECT position,v,gateway.source_fixture_row_group(fields,v) group_value FROM jsonb_array_elements(wanted) WITH ORDINALITY a(v,position)),
      actual_rows AS MATERIALIZED (
        SELECT position,v,gateway.source_fixture_row_group(fields,v) group_value FROM jsonb_array_elements(actual) WITH ORDINALITY a(v,position)),
      joined AS (
        SELECT l.position,coalesce(jsonb_agg(r.position ORDER BY r.position) FILTER(WHERE r.position IS NOT NULL),'[]'::jsonb) neighbors
        FROM wanted_rows l LEFT JOIN actual_rows r ON l.group_value=r.group_value
          AND gateway.source_fixture_row_matches(fields,l.v,r.v,absolute,relative) GROUP BY l.position)
      SELECT jsonb_agg(neighbors ORDER BY position) INTO adjacency FROM joined;
      IF EXISTS(SELECT 1 FROM jsonb_array_elements(adjacency) a(v) WHERE jsonb_array_length(v)=0) THEN RETURN false; END IF;
      -- Bounded augmenting-path matching, not an unsound greedy row pairing.
      -- Each search visits at most 1,000 left/right vertices once.
      left_match:=array_fill(0,ARRAY[size]);right_match:=array_fill(0,ARRAY[size]);
      FOR left_index IN 1..size LOOP
        queue:=ARRAY[left_index];queue_index:=1;found_right:=0;
        visited_left:=array_fill(false,ARRAY[size]);visited_right:=array_fill(false,ARRAY[size]);parent_right:=array_fill(0,ARRAY[size]);
        visited_left[left_index]:=true;
        WHILE queue_index<=cardinality(queue) AND found_right=0 LOOP
          current_left:=queue[queue_index];queue_index:=queue_index+1;
          FOR right_index IN SELECT v::integer FROM jsonb_array_elements_text(adjacency->(current_left-1)) a(v) LOOP
            IF visited_right[right_index] THEN CONTINUE; END IF;
            visited_right[right_index]:=true;parent_right[right_index]:=current_left;
            candidate:=right_match[right_index];
            IF candidate=0 THEN found_right:=right_index;EXIT; END IF;
            IF NOT visited_left[candidate] THEN visited_left[candidate]:=true;queue:=array_append(queue,candidate); END IF;
          END LOOP;
        END LOOP;
        IF found_right=0 THEN RETURN false; END IF;
        right_index:=found_right;
        WHILE right_index<>0 LOOP
          current_left:=parent_right[right_index];previous_right:=left_match[current_left];
          left_match[current_left]:=right_index;right_match[right_index]:=current_left;right_index:=previous_right;
        END LOOP;
      END LOOP;
      RETURN true;
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END''', immutable=True)
    function('source_fixture_output_valid', 'manifest jsonb, binding jsonb, query jsonb, decision jsonb, check_value jsonb, completed_at timestamptz', 'boolean', '''
    DECLARE actual jsonb:=check_value->'actual'; row_value jsonb; member record; grain jsonb; grains jsonb:='[]';
      time_field text; start_at text; end_at text; watermark timestamptz; age numeric; freshness text; output_fields jsonb;
    BEGIN
      -- Normalization emits exactly the admitted visible projection. A narrower
      -- safe result may be retained as a failed full-schema comparison.
      SELECT jsonb_object_agg(key,value) INTO output_fields FROM jsonb_each(manifest->'outputs')
        WHERE (decision->'visible_fields') ? key;
      IF completed_at IS NULL OR NOT gateway.source_fixture_rows_valid(output_fields,actual)
        OR check_value->'actual_error' IS DISTINCT FROM 'null'::jsonb
        OR check_value->>'freshness_status' IS NULL
        OR jsonb_array_length(actual)>least(1000,(manifest->'limits'->>'max_rows')::integer,(decision->'limits'->>'max_rows')::integer)
        OR EXISTS(SELECT 1 FROM jsonb_array_elements(actual) a(v),LATERAL jsonb_object_keys(v) f(key)
          WHERE NOT (decision->'visible_fields') ? key) THEN RETURN false; END IF;
      IF binding->>'adapter_revision'='cube-bigquery-v1' THEN
        IF binding->>'freshness_mode'='unknown' AND check_value->'observed_source_load_watermark'<>'null'::jsonb THEN RETURN false; END IF;
      ELSIF binding->>'adapter_revision'='domo-named-v2' THEN
        IF binding->'checkpoint'='null'::jsonb AND check_value->'observed_source_load_watermark'<>'null'::jsonb THEN RETURN false; END IF;
      ELSE RETURN false; END IF;
      IF check_value->'observed_source_load_watermark'='null'::jsonb THEN freshness:='unknown';
      ELSE
        IF jsonb_typeof(check_value->'observed_source_load_watermark')<>'string'
          OR check_value->>'observed_source_load_watermark' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$' THEN RETURN false; END IF;
        watermark:=(check_value->>'observed_source_load_watermark')::timestamptz;
        IF binding->>'adapter_revision'='domo-named-v2' AND watermark>completed_at THEN RETURN false; END IF;
        age:=extract(epoch FROM completed_at-watermark);
        freshness:=CASE WHEN age<0 THEN 'unknown' WHEN age<=(manifest->'freshness'->>'max_age_seconds')::integer THEN 'fresh' ELSE 'stale' END;
      END IF;
      IF freshness IS DISTINCT FROM check_value->>'freshness_status'
        OR (manifest->'freshness'->'required'='true'::jsonb AND freshness<>'fresh') THEN RETURN false; END IF;
      start_at:=query->'inputs'->>(manifest->'date_boundaries'->>'start_input');end_at:=query->'inputs'->>(manifest->'date_boundaries'->>'end_input');
      FOR row_value IN SELECT v FROM jsonb_array_elements(actual) a(v) LOOP
        grain:='[]'::jsonb;
        IF binding->>'adapter_revision'='cube-bigquery-v1' THEN
          FOR member IN SELECT key,value FROM jsonb_each(binding->'fields') ORDER BY key LOOP
            IF member.value->>'kind'='time' AND (row_value->>member.key>=start_at AND row_value->>member.key<end_at) IS NOT TRUE THEN RETURN false; END IF;
            IF member.value->>'kind'<>'measure' THEN grain:=grain||jsonb_build_array(row_value->member.key); END IF;
          END LOOP;
        ELSE
          time_field:=binding->>'time_field';
          IF (row_value->>time_field>=start_at AND row_value->>time_field<end_at) IS NOT TRUE THEN RETURN false; END IF;
          FOR member IN SELECT v FROM jsonb_array_elements_text(binding->'grain_keys') a(v) LOOP grain:=grain||jsonb_build_array(row_value->member.v); END LOOP;
          FOR member IN SELECT key,value FROM jsonb_each_text(binding->'input_fields') LOOP
            IF query->'inputs'->member.key<>'null'::jsonb AND row_value->member.value IS DISTINCT FROM query->'inputs'->member.key THEN RETURN false; END IF;
          END LOOP;
        END IF;
        IF EXISTS(SELECT 1 FROM jsonb_array_elements(grains) a(v) WHERE v=grain) THEN RETURN false; END IF;
        grains:=grains||jsonb_build_array(grain);
      END LOOP;
      RETURN true;
    EXCEPTION WHEN OTHERS THEN RETURN false;
    END''')
    function('source_fixture_known_answer_valid', 'manifest jsonb, binding jsonb, query jsonb, decision jsonb, check_value jsonb, completed_at timestamptz', 'boolean', '''
    BEGIN
      RETURN gateway.source_fixture_output_valid(manifest,binding,query,decision,check_value,completed_at)
        AND gateway.source_fixture_rows_match(manifest->'outputs',check_value->'expected',check_value->'actual',
          check_value->>'absolute_tolerance',check_value->>'relative_tolerance');
    END''')
    function('source_fixture_evidence', 'invocation_id uuid', 'void', '''
    DECLARE inv gateway.fixture_source_invocations%ROWTYPE; draft gateway.draft_revisions%ROWTYPE; t gateway.test_runs%ROWTYPE;
      c gateway.fixture_source_invocation_cases%ROWTYPE; a gateway.fixture_attempts%ROWTYPE; state gateway.fixture_attempt_states%ROWTYPE;
      check_value jsonb; check_canonical text; decision jsonb; query jsonb; approval gateway.source_access_approvals%ROWTYPE; all_passed boolean:=true; count_checks integer:=0;
    BEGIN
      SELECT * INTO STRICT inv FROM gateway.fixture_source_invocations WHERE id=invocation_id;
      SELECT * INTO STRICT draft FROM gateway.draft_revisions WHERE id=inv.draft_id;
      SELECT * INTO STRICT approval FROM gateway.source_access_approvals WHERE id=inv.approval_id;
      SELECT t.* INTO STRICT t FROM gateway.test_runs t JOIN gateway.fixture_test_source_authorities a ON a.test_run_id=t.id WHERE a.invocation_id=inv.id;
      IF NOT gateway.source_json_witness(t.evidence_canonical,t.evidence,encode(sha256(convert_to(t.evidence_canonical,'UTF8')),'hex'))
        OR t.source_decision_canonicals IS NULL OR jsonb_typeof(t.source_decision_canonicals)<>'object' OR octet_length(t.source_decision_canonicals::text)>65536
        OR t.id IS DISTINCT FROM inv.id::text OR t.actor_id IS DISTINCT FROM inv.actor_snapshot->>'id'
        OR (t.product_id,t.draft_id,t.draft_digest,t.binding_id,t.binding_digest,t.fixture_version)
        IS DISTINCT FROM (inv.product_id,inv.draft_id,inv.draft_digest,inv.consumer_binding_id,inv.consumer_binding_digest,inv.catalog_digest)
        OR t.policy_id IS DISTINCT FROM draft.manifest->>'policy_id'
        OR t.policy_revision IS DISTINCT FROM (gateway.source_current_decision(inv.actor_snapshot,draft.manifest,'author')->>'policy_revision')::integer
        OR NOT gateway.source_approval_keys(t.evidence,ARRAY['schema_version','catalog_digest','execution_contract_digest','fixture_binding_digest','fixture_model_bundle_digest','checks'])
        OR t.evidence->>'schema_version' IS DISTINCT FROM '1' OR t.evidence->>'catalog_digest' IS DISTINCT FROM inv.catalog_digest
        OR t.evidence->>'execution_contract_digest' IS DISTINCT FROM inv.execution_contract_digest
        OR t.evidence->>'fixture_binding_digest' IS DISTINCT FROM inv.fixture_binding_digest
        OR t.evidence->>'fixture_model_bundle_digest' IS DISTINCT FROM inv.catalog->'fixture_binding'->>'model_bundle_digest'
        OR jsonb_typeof(t.evidence->'checks') IS DISTINCT FROM 'array'
        OR jsonb_array_length(t.evidence->'checks') IS DISTINCT FROM jsonb_array_length(inv.catalog->'cases')
        OR (SELECT count(*)<>count(DISTINCT v->>'fixture_id') FROM jsonb_array_elements(t.evidence->'checks') a(v)) THEN
        RAISE EXCEPTION 'Source fixture evidence unavailable' USING ERRCODE='42501'; END IF;
      FOR c IN SELECT * FROM gateway.fixture_source_invocation_cases c WHERE c.invocation_id=inv.id ORDER BY fixture_id LOOP
        SELECT v INTO STRICT check_value FROM jsonb_array_elements(t.evidence->'checks') a(v) WHERE v->>'fixture_id'=c.fixture_id;
        SELECT v::text INTO STRICT check_canonical FROM json_array_elements(t.evidence_canonical::json->'checks') a(v) WHERE v->>'fixture_id'=c.fixture_id;
        count_checks:=count_checks+1;
        IF NOT gateway.source_approval_keys(check_value,ARRAY['fixture_id','kind','passed','expected','actual','expected_error','actual_error',
          'absolute_tolerance','relative_tolerance','attempt_id','decision_digest','reference_watermark','observed_source_load_watermark','freshness_status'])
          OR check_value->>'kind' IS DISTINCT FROM c.kind OR jsonb_typeof(check_value->'passed') IS DISTINCT FROM 'boolean'
          OR check_value->'expected' IS DISTINCT FROM c.case_content->'fixture'->'expected_rows'
          OR check_value->'expected_error' IS DISTINCT FROM c.case_content->'fixture'->'expected_error'
          OR check_value->'absolute_tolerance' IS DISTINCT FROM c.case_content->'fixture'->'tolerance'
          OR check_value->'relative_tolerance' IS DISTINCT FROM c.case_content->'relative_tolerance'
          OR check_value->'reference_watermark' IS DISTINCT FROM c.case_content->'fixture'->'source_watermark'
          OR jsonb_typeof(check_value->'actual') IS DISTINCT FROM 'array' THEN
          RAISE EXCEPTION 'Source fixture evidence unavailable' USING ERRCODE='42501'; END IF;
        decision:=gateway.source_current_decision_or_denial(c.principal_snapshot,draft.manifest,'execute');
        IF c.kind='permission_negative' THEN
          IF check_value->'attempt_id'<>'null'::jsonb OR check_value->'actual'<>'[]'::jsonb
            OR (check_value->'passed'='true'::jsonb AND (decision IS NOT NULL OR check_value->>'actual_error' IS DISTINCT FROM 'NOT_FOUND_OR_FORBIDDEN'
              OR check_value->'decision_digest'<>'null'::jsonb)) THEN
            RAISE EXCEPTION 'Source fixture evidence unavailable' USING ERRCODE='42501'; END IF;
        ELSIF c.kind='input_negative' THEN
          query:=jsonb_build_object('product_id',draft.product_id,'version',draft.manifest->>'version','inputs',c.case_content->'fixture'->'inputs','filters','[]'::jsonb,'sort','[]'::jsonb);
          IF check_value->'attempt_id'<>'null'::jsonb OR check_value->'actual'<>'[]'::jsonb
            OR (check_value->'passed'='true'::jsonb AND (decision IS NULL OR check_value->>'actual_error' IS DISTINCT FROM 'INVALID_INPUT'
              OR gateway.source_query_reviewed(approval.content,draft.manifest,query,decision->'limits')
              OR NOT gateway.source_json_witness(t.source_decision_canonicals->>c.fixture_id,decision,check_value->>'decision_digest'))) THEN
            RAISE EXCEPTION 'Source fixture evidence unavailable' USING ERRCODE='42501'; END IF;
        ELSE
          IF check_value->'attempt_id'='null'::jsonb THEN
            IF check_value->'passed'='true'::jsonb OR check_value->'actual'<>'[]'::jsonb THEN
              RAISE EXCEPTION 'Source fixture evidence unavailable' USING ERRCODE='42501'; END IF;
          ELSE
            SELECT a.* INTO STRICT a FROM gateway.fixture_attempts a JOIN gateway.fixture_attempt_source_authorities auth ON auth.attempt_id=a.id
              WHERE a.id=check_value->>'attempt_id' AND auth.invocation_id=inv.id AND auth.fixture_id=c.fixture_id;
            SELECT * INTO STRICT state FROM gateway.fixture_attempt_states WHERE attempt_id=a.id;
            IF state.check_digest IS DISTINCT FROM encode(sha256(convert_to(check_canonical,'UTF8')),'hex')
              OR check_value->>'decision_digest' IS DISTINCT FROM gateway.source_fixture_decision_digest(a.id)
              OR (jsonb_array_length(check_value->'actual')>0 AND (state.state IS DISTINCT FROM 'succeeded' OR NOT state.source_completed))
              OR (state.state='succeeded' AND (check_value->'actual_error' IS DISTINCT FROM 'null'::jsonb
                OR NOT gateway.source_fixture_output_valid(draft.manifest,inv.catalog->'fixture_binding',a.canonical_query,a.decision,check_value,state.completed_at)))
              OR (state.state IN ('failed','timed_out') AND check_value->>'actual_error' IS DISTINCT FROM state.error_code)
              OR (check_value->'passed'='true'::jsonb AND (state.state IS DISTINCT FROM 'succeeded' OR NOT state.source_completed OR decision IS DISTINCT FROM a.decision
                OR NOT gateway.source_fixture_rows_match(draft.manifest->'outputs',check_value->'expected',check_value->'actual',
                  check_value->>'absolute_tolerance',check_value->>'relative_tolerance'))) THEN
              RAISE EXCEPTION 'Source fixture evidence unavailable' USING ERRCODE='42501'; END IF;
          END IF;
        END IF;
        IF check_value->'passed'='false'::jsonb THEN all_passed:=false; END IF;
      END LOOP;
      IF count_checks<>jsonb_array_length(t.evidence->'checks') OR t.status IS DISTINCT FROM (CASE WHEN all_passed THEN 'passed' ELSE 'failed' END) THEN
        RAISE EXCEPTION 'Source fixture evidence unavailable' USING ERRCODE='42501'; END IF;
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source fixture evidence unavailable' USING ERRCODE='42501'; END''')
    function('source_fixture_test_capture', '', 'trigger', '''
    DECLARE inv gateway.fixture_source_invocations%ROWTYPE;
    BEGIN
      IF (SELECT adapter_revision FROM gateway.binding_revisions WHERE id=NEW.binding_id) NOT IN ('cube-bigquery-v1','domo-named-v2') THEN RETURN NULL; END IF;
      SELECT * INTO STRICT inv FROM gateway.fixture_source_invocations WHERE id=NEW.id::uuid;
      IF inv.draft_id IS DISTINCT FROM NEW.draft_id THEN RAISE EXCEPTION 'Source fixture evidence unavailable' USING ERRCODE='42501'; END IF;
      INSERT INTO gateway.fixture_test_source_authorities(test_run_id,invocation_id) VALUES(NEW.id,inv.id);
      PERFORM gateway.source_fixture_structural(inv.id);
      PERFORM gateway.source_fixture_evidence(inv.id);
      RETURN NULL;
    EXCEPTION WHEN invalid_text_representation OR NO_DATA_FOUND THEN RAISE EXCEPTION 'Source fixture evidence unavailable' USING ERRCODE='42501'; END''')
    op.execute('CREATE TRIGGER source_fixture_test_capture AFTER INSERT ON gateway.test_runs FOR EACH ROW EXECUTE FUNCTION gateway.source_fixture_test_capture()')
    function('source_fixture_test_complete', '', 'trigger', '''
    DECLARE auth gateway.fixture_test_source_authorities%ROWTYPE; a gateway.fixture_attempt_source_authorities%ROWTYPE;
    BEGIN
      SELECT * INTO auth FROM gateway.fixture_test_source_authorities WHERE test_run_id=NEW.id;
      IF FOUND THEN
        PERFORM gateway.source_fixture_invocation_access(auth.invocation_id::text,'author',true);
        FOR a IN SELECT a.* FROM gateway.fixture_attempt_source_authorities a WHERE invocation_id=auth.invocation_id ORDER BY attempt_id LOOP
          PERFORM gateway.source_fixture_attempt_access(a.attempt_id,'result',true);
        END LOOP;
      END IF;
      RETURN NULL;
    END''')
    op.execute('CREATE CONSTRAINT TRIGGER source_fixture_test_complete AFTER INSERT ON gateway.test_runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.source_fixture_test_complete()')
    function('source_read_fixture_test', 'test_run_id text', 'jsonb', '''
    DECLARE inv uuid; attempts jsonb;
    BEGIN
      SELECT invocation_id INTO STRICT inv FROM gateway.fixture_test_source_authorities WHERE fixture_test_source_authorities.test_run_id=source_read_fixture_test.test_run_id;
      SELECT coalesce(jsonb_agg(gateway.source_read_fixture_attempt(attempt_id) ORDER BY attempt_id),'[]') INTO attempts FROM gateway.fixture_attempt_source_authorities WHERE invocation_id=inv;
      RETURN jsonb_build_object('invocation',gateway.source_read_fixture_invocation(inv),'attempts',attempts);
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Source fixture evidence unavailable' USING ERRCODE='42501'; END''')
    function('source_fixture_batch_complete', 'requests jsonb', 'void', '''
    DECLARE request jsonb; inv uuid; a gateway.fixture_attempt_source_authorities%ROWTYPE;
    BEGIN
      FOR request IN SELECT v FROM jsonb_array_elements(requests) a(v) WHERE v->>'kind'='owner' LOOP
        IF request->>'owner_kind'='fixture_attempt' THEN
          SELECT * INTO STRICT a FROM gateway.fixture_attempt_source_authorities WHERE attempt_id=request->>'owner_id';
          IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(requests) r(v) WHERE v->>'kind'='owner' AND v->>'owner_kind'='fixture_invocation'
            AND v->>'owner_id'=a.invocation_id::text AND v->>'action'='author') THEN
            RAISE EXCEPTION 'Original fixture author required' USING ERRCODE='42501'; END IF;
        ELSIF request->>'owner_kind'='fixture_invocation' THEN
          inv:=(request->>'owner_id')::uuid;
          IF EXISTS(SELECT 1 FROM gateway.fixture_test_source_authorities WHERE invocation_id=inv) THEN
            PERFORM gateway.source_fixture_evidence(inv);
            FOR a IN SELECT * FROM gateway.fixture_attempt_source_authorities WHERE invocation_id=inv LOOP
              IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(requests) r(v) WHERE v->>'kind'='owner' AND v->>'owner_kind'='fixture_attempt'
                AND v->>'owner_id'=a.attempt_id AND v->>'action'='result') THEN
                RAISE EXCEPTION 'Independent fixture result authority required' USING ERRCODE='42501'; END IF;
            END LOOP;
          END IF;
        END IF;
      END LOOP;
    END''')


def _publication(op, function):
    op.execute('ALTER TABLE gateway.products ADD COLUMN publication_revision INTEGER NOT NULL DEFAULT 0 CHECK(publication_revision>=0)')
    op.execute('''CREATE TABLE gateway.source_publication_operations (
      id UUID PRIMARY KEY,transaction_id xid8 NOT NULL,action TEXT NOT NULL CHECK(action IN ('publish','deprecate','withdraw','rollback')),
      product_id VARCHAR(128) NOT NULL REFERENCES gateway.products(id),version_id VARCHAR(128) NOT NULL,draft_id VARCHAR(128) NOT NULL,
      test_run_id VARCHAR(128) NOT NULL,previous_version_id VARCHAR(128),expected_product_revision INTEGER NOT NULL,
      next_product_revision INTEGER NOT NULL,expected_draft_revision INTEGER,actor_snapshot JSONB NOT NULL,actor_digest CHAR(64) NOT NULL,
      publisher_approval_id UUID NOT NULL,publisher_approval_revision INTEGER NOT NULL,publisher_approval_digest CHAR(64) NOT NULL,
      publisher_entry_id VARCHAR(128) NOT NULL,package_pins_digest CHAR(64) NOT NULL,fixture_invocation_id UUID NOT NULL REFERENCES gateway.fixture_source_invocations(id),
      fixture_approval_id UUID NOT NULL,fixture_approval_revision INTEGER NOT NULL,fixture_approval_digest CHAR(64) NOT NULL,
      target_pins JSONB NOT NULL,allow_audit_id VARCHAR(128) NOT NULL UNIQUE,allow_audit_projection JSONB NOT NULL,allow_audit_digest CHAR(64) NOT NULL,
      request_id UUID NOT NULL UNIQUE,created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY(publisher_approval_id,publisher_approval_revision,publisher_approval_digest) REFERENCES gateway.source_access_approvals(id,revision,content_digest),
      FOREIGN KEY(fixture_approval_id,fixture_approval_revision,fixture_approval_digest) REFERENCES gateway.source_access_approvals(id,revision,content_digest),
      UNIQUE(product_id,transaction_id),CHECK(expected_product_revision>=0 AND next_product_revision::bigint=expected_product_revision::bigint+1))''')
    for table in ('products', 'product_versions', 'version_states', 'publication_events'):
        op.execute(f'ALTER TABLE gateway.{table} ADD COLUMN source_operation_id UUID REFERENCES gateway.source_publication_operations(id)')
    op.execute('CREATE UNIQUE INDEX uq_publication_operation_event ON gateway.publication_events(source_operation_id) WHERE source_operation_id IS NOT NULL')
    op.execute('''CREATE TABLE gateway.source_publication_revision_steps (
      product_id VARCHAR(128) NOT NULL REFERENCES gateway.products(id),prior_revision INTEGER NOT NULL,next_revision INTEGER NOT NULL,
      transaction_id xid8 NOT NULL,version_id VARCHAR(128) NOT NULL,action TEXT NOT NULL,operation_id UUID REFERENCES gateway.source_publication_operations(id),
      PRIMARY KEY(product_id,next_revision),UNIQUE(product_id,transaction_id),
      CHECK(prior_revision>=0 AND next_revision::bigint=prior_revision::bigint+1),CHECK(action IN ('publish','deprecate','withdraw','rollback')))''')
    function('source_publication_advance', 'product_id text, version_id text, action text, operation_id uuid DEFAULT NULL', 'integer', '''
    DECLARE step gateway.source_publication_revision_steps%ROWTYPE; current_revision integer;
    BEGIN
      SELECT * INTO step FROM gateway.source_publication_revision_steps s WHERE s.product_id=source_publication_advance.product_id AND transaction_id=pg_current_xact_id();
      IF FOUND THEN
        IF (step.version_id,step.action,step.operation_id) IS DISTINCT FROM (version_id,action,operation_id) THEN
          RAISE EXCEPTION 'Publication operation conflicts' USING ERRCODE='42501'; END IF;
        RETURN step.next_revision;
      END IF;
      SELECT publication_revision INTO STRICT current_revision FROM gateway.products WHERE id=product_id;
      IF current_revision=2147483647 THEN RAISE EXCEPTION 'Publication revision exhausted' USING ERRCODE='42501'; END IF;
      INSERT INTO gateway.source_publication_revision_steps VALUES(product_id,current_revision,current_revision+1,pg_current_xact_id(),version_id,action,operation_id);
      UPDATE gateway.products SET publication_revision=current_revision+1,source_operation_id=operation_id WHERE id=product_id;
      RETURN current_revision+1;
    END''')
    function('source_publication_receipt', 'operation_id uuid, product_id text, version_id text', 'gateway.source_publication_operations', '''
    DECLARE receipt gateway.source_publication_operations%ROWTYPE;
    BEGIN
      SELECT * INTO STRICT receipt FROM gateway.source_publication_operations WHERE id=operation_id;
      IF receipt.transaction_id IS DISTINCT FROM pg_current_xact_id() OR receipt.product_id IS DISTINCT FROM product_id OR receipt.version_id IS DISTINCT FROM version_id THEN
        RAISE EXCEPTION 'Publication receipt unavailable' USING ERRCODE='42501'; END IF;
      RETURN receipt;
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Publication receipt unavailable' USING ERRCODE='42501'; END''')
    function('source_record_publication_operation', '''actor_snapshot jsonb, action text, product_id text, version_id text,
      draft_id text,test_run_id text,previous_version_id text,expected_product_revision integer,expected_draft_revision integer,
      allow_audit_id text,request_id text''', 'uuid', '''
    DECLARE head gateway.products%ROWTYPE; draft gateway.draft_revisions%ROWTYPE; target gateway.product_versions%ROWTYPE; previous gateway.product_versions%ROWTYPE;
      test gateway.test_runs%ROWTYPE; inv gateway.fixture_source_invocations%ROWTYPE; audit gateway.audit_events%ROWTYPE; projected jsonb;
      manifest jsonb; target_result jsonb; result jsonb; original jsonb; attempt gateway.fixture_attempt_source_authorities%ROWTYPE;
      pins jsonb:='[]'; ids uuid[]:=ARRAY[]::uuid[]; aid uuid; operation uuid:=gen_random_uuid(); checked_at timestamptz; target_key text;
    BEGIN
      IF actor_snapshot IS NULL OR action IS NULL OR action NOT IN ('publish','deprecate','withdraw','rollback') OR product_id IS NULL
        OR version_id IS NULL OR expected_product_revision IS NULL OR expected_product_revision<0 OR expected_product_revision=2147483647
        OR request_id IS NULL OR request_id::uuid::text IS DISTINCT FROM request_id OR allow_audit_id IS NULL THEN
        RAISE EXCEPTION 'Publication unavailable' USING ERRCODE='42501'; END IF;
      PERFORM 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Source capacity unavailable' USING ERRCODE='42501'; END IF;
      SELECT * INTO STRICT head FROM gateway.products WHERE id=product_id FOR UPDATE;
      IF head.publication_revision IS DISTINCT FROM expected_product_revision OR head.current_version_id IS DISTINCT FROM previous_version_id THEN
        RAISE EXCEPTION 'Publication revision conflicts' USING ERRCODE='42501'; END IF;
      IF action='publish' THEN
        IF draft_id IS NULL OR test_run_id IS NULL OR expected_draft_revision IS NULL OR EXISTS(SELECT 1 FROM gateway.product_versions WHERE id=version_id) THEN
          RAISE EXCEPTION 'Publication unavailable' USING ERRCODE='42501'; END IF;
        SELECT * INTO STRICT draft FROM gateway.draft_revisions WHERE id=draft_id;
        IF draft.product_id IS DISTINCT FROM product_id OR head.latest_draft_id IS DISTINCT FROM draft.id
          OR (head.draft_revision,draft.revision) IS DISTINCT FROM (expected_draft_revision,expected_draft_revision) THEN
          RAISE EXCEPTION 'Publication revision conflicts' USING ERRCODE='42501'; END IF;
        SELECT * INTO STRICT test FROM gateway.test_runs WHERE id=test_run_id;
        target_key:=draft.id;
      ELSE
        IF draft_id IS NOT NULL OR test_run_id IS NOT NULL OR expected_draft_revision IS NOT NULL THEN
          RAISE EXCEPTION 'Publication unavailable' USING ERRCODE='42501'; END IF;
        SELECT * INTO STRICT target FROM gateway.product_versions WHERE id=version_id AND product_versions.product_id=source_record_publication_operation.product_id;
        SELECT * INTO STRICT draft FROM gateway.draft_revisions WHERE id=target.draft_id;
        SELECT * INTO STRICT test FROM gateway.test_runs WHERE id=target.test_run_id;
        IF NOT EXISTS(SELECT 1 FROM gateway.version_states WHERE version_states.version_id=target.id AND status IN ('published','deprecated')) THEN
          RAISE EXCEPTION 'Publication unavailable' USING ERRCODE='42501'; END IF;
        target_key:=target.id;
      END IF;
      IF previous_version_id IS NOT NULL THEN SELECT * INTO STRICT previous FROM gateway.product_versions WHERE id=previous_version_id; END IF;
      PERFORM 1 FROM gateway.draft_revisions WHERE id IN (draft.id,previous.draft_id) ORDER BY id FOR UPDATE;
      PERFORM 1 FROM gateway.policies WHERE id IN (draft.manifest->>'policy_id',previous.policy_id) ORDER BY id FOR UPDATE;
      PERFORM 1 FROM gateway.version_states WHERE version_states.version_id IN (version_id,previous_version_id) ORDER BY version_states.version_id FOR UPDATE;
      SELECT i.* INTO STRICT inv FROM gateway.fixture_test_source_authorities a JOIN gateway.fixture_source_invocations i ON i.id=a.invocation_id WHERE a.test_run_id=test.id;
      PERFORM 1 FROM gateway.fixture_attempt_states WHERE attempt_id IN (SELECT attempt_id FROM gateway.fixture_attempt_source_authorities WHERE invocation_id=inv.id) ORDER BY attempt_id FOR UPDATE;
      manifest:=draft.manifest;
      IF test.status IS DISTINCT FROM 'passed' OR (test.draft_id,test.draft_digest,test.product_id) IS DISTINCT FROM (draft.id,draft.content_digest,product_id)
        OR test.policy_revision IS DISTINCT FROM (gateway.source_current_decision(actor_snapshot,manifest,'publish')->>'policy_revision')::integer THEN
        RAISE EXCEPTION 'Publication evidence unavailable' USING ERRCODE='42501'; END IF;
      target_result:=gateway.source_transient_access(draft.manifest_canonical,actor_snapshot,'publish');
      original:=gateway.source_fixture_invocation_access(inv.id::text,'author',false);
      pins:=pins||jsonb_build_array(jsonb_build_object('kind','publisher','version_id',target_key,'approval',target_result->'projection','decision',target_result->'decision','entry',target_result->>'audience_entry_id'));
      ids:=array_append(ids,(target_result->'projection'->>'approval_id')::uuid);ids:=array_append(ids,inv.approval_id);
      FOR attempt IN SELECT * FROM gateway.fixture_attempt_source_authorities WHERE invocation_id=inv.id ORDER BY attempt_id LOOP
        result:=gateway.source_fixture_attempt_access(attempt.attempt_id,'result',false);
        pins:=pins||jsonb_build_array(jsonb_build_object('kind','fixture_result','attempt_id',attempt.attempt_id,'approval_id',attempt.approval_id,
          'decision',result->'decision','entry',result->>'audience_entry_id'));
      END LOOP;
      IF previous.id IS NOT NULL AND previous.id IS DISTINCT FROM target.id THEN
        result:=gateway.source_transient_access(previous.manifest_canonical,actor_snapshot,'publish');
        ids:=array_append(ids,(result->'projection'->>'approval_id')::uuid);
        pins:=pins||jsonb_build_array(jsonb_build_object('kind','publisher','version_id',previous.id,'approval',result->'projection','decision',result->'decision','entry',result->>'audience_entry_id'));
      END IF;
      SELECT * INTO STRICT audit FROM gateway.audit_events WHERE id=allow_audit_id;
      IF audit.created_transaction_id IS DISTINCT FROM pg_current_xact_id() OR audit.actor_id IS DISTINCT FROM actor_snapshot->>'id'
        OR audit.service_client_id IS DISTINCT FROM actor_snapshot->>'service_client_id' OR audit.action IS DISTINCT FROM 'publish'
        OR audit.product_id IS DISTINCT FROM product_id OR audit.version_id IS DISTINCT FROM target_key
        OR audit.policy_id IS DISTINCT FROM manifest->>'policy_id' OR audit.policy_revision IS DISTINCT FROM (target_result->'decision'->>'policy_revision')::integer
        OR audit.correlation_id IS DISTINCT FROM request_id OR audit.decision IS DISTINCT FROM 'allow'
        OR audit.source_approval_id IS DISTINCT FROM (target_result->'projection'->>'approval_id')::uuid
        OR audit.source_approval_revision IS DISTINCT FROM (target_result->'projection'->>'approval_revision')::integer
        OR audit.source_approval_digest IS DISTINCT FROM target_result->'projection'->>'approval_digest'
        OR audit.source_approval_state_revision IS DISTINCT FROM 1 OR audit.source_access_reason_code IS DISTINCT FROM 'APPROVED' THEN
        RAISE EXCEPTION 'Publication audit unavailable' USING ERRCODE='42501'; END IF;
      projected:=jsonb_build_object('id',audit.id,'actor_id',audit.actor_id,'service_client_id',audit.service_client_id,'action',audit.action,
        'product_id',audit.product_id,'version_id',audit.version_id,'policy_id',audit.policy_id,'policy_revision',audit.policy_revision,'decision',audit.decision,
        'correlation_id',audit.correlation_id,'source_approval_id',audit.source_approval_id,'source_approval_revision',audit.source_approval_revision,
        'source_approval_digest',audit.source_approval_digest,'source_approval_state_revision',audit.source_approval_state_revision,'source_access_reason_code',audit.source_access_reason_code);
      FOR aid IN SELECT DISTINCT v FROM unnest(ids) a(v) ORDER BY v LOOP
        PERFORM gateway.source_approval_assert(aid,(SELECT revision FROM gateway.source_access_approvals WHERE id=aid),(SELECT content_digest FROM gateway.source_access_approvals WHERE id=aid));
      END LOOP;
      checked_at:=clock_timestamp();
      IF EXISTS(SELECT 1 FROM gateway.source_access_approvals a JOIN gateway.source_access_approval_states s ON s.approval_id=a.id WHERE a.id=ANY(ids)
        AND (a.expires_at>checked_at AND s.state='active' AND s.state_revision=1) IS NOT TRUE) THEN
        RAISE EXCEPTION 'Publication expired' USING ERRCODE='42501'; END IF;
      INSERT INTO gateway.source_publication_operations(id,transaction_id,action,product_id,version_id,draft_id,test_run_id,previous_version_id,
        expected_product_revision,next_product_revision,expected_draft_revision,actor_snapshot,actor_digest,publisher_approval_id,publisher_approval_revision,
        publisher_approval_digest,publisher_entry_id,package_pins_digest,fixture_invocation_id,fixture_approval_id,fixture_approval_revision,fixture_approval_digest,
        target_pins,allow_audit_id,allow_audit_projection,allow_audit_digest,request_id)
      VALUES(operation,pg_current_xact_id(),action,product_id,version_id,draft.id,test.id,previous_version_id,expected_product_revision,expected_product_revision+1,
        expected_draft_revision,actor_snapshot,encode(sha256(convert_to(gateway.source_approval_canonical(actor_snapshot::json),'UTF8')),'hex'),
        (target_result->'projection'->>'approval_id')::uuid,(target_result->'projection'->>'approval_revision')::integer,target_result->'projection'->>'approval_digest',
        target_result->>'audience_entry_id',inv.package_pins_digest,inv.id,inv.approval_id,inv.approval_revision,inv.approval_digest,pins,
        audit.id,projected,encode(sha256(convert_to(gateway.source_approval_canonical(projected::json),'UTF8')),'hex'),request_id::uuid);
      PERFORM gateway.source_publication_advance(product_id,version_id,action,operation);
      RETURN operation;
    EXCEPTION WHEN invalid_text_representation OR NO_DATA_FOUND THEN RAISE EXCEPTION 'Publication unavailable' USING ERRCODE='42501'; END''')
    _publication_guards(op, function)


def _publication_guards(op, function):
    op.execute('ALTER TABLE gateway.publication_events ADD COLUMN created_transaction_id xid8')
    op.execute('ALTER TABLE gateway.product_versions ADD COLUMN source_created_transaction_id xid8')
    function('source_publication_event_transaction', '', 'trigger', '''
    BEGIN NEW.created_transaction_id:=pg_current_xact_id(); RETURN NEW; END''')
    op.execute('CREATE TRIGGER aaa_source_publication_transaction BEFORE INSERT ON gateway.publication_events FOR EACH ROW EXECUTE FUNCTION gateway.source_publication_event_transaction()')
    function('guard_publication', '', 'trigger', '''
    DECLARE p gateway.products%ROWTYPE; d gateway.draft_revisions%ROWTYPE; b gateway.binding_revisions%ROWTYPE;
      t gateway.test_runs%ROWTYPE; policy_head integer; receipt gateway.source_publication_operations%ROWTYPE;
    BEGIN
      NEW.source_created_transaction_id:=pg_current_xact_id();
      SELECT * INTO STRICT b FROM gateway.binding_revisions WHERE id=NEW.binding_id;
      IF b.adapter_revision IN ('cube-bigquery-v1','domo-named-v2') THEN
        receipt:=gateway.source_publication_receipt(NEW.source_operation_id,NEW.product_id,NEW.id);
        IF receipt.action IS DISTINCT FROM 'publish' OR (receipt.draft_id,receipt.test_run_id,receipt.actor_snapshot->>'id')
          IS DISTINCT FROM (NEW.draft_id,NEW.test_run_id,NEW.actor_id) THEN RAISE EXCEPTION 'Publication receipt unavailable' USING ERRCODE='42501'; END IF;
        SELECT * INTO p FROM gateway.products WHERE id=NEW.product_id;
        SELECT current_revision INTO policy_head FROM gateway.policies WHERE id=NEW.policy_id;
      ELSE
        IF NEW.source_operation_id IS NOT NULL THEN RAISE EXCEPTION 'Publication receipt unavailable' USING ERRCODE='42501'; END IF;
        SELECT * INTO p FROM gateway.products WHERE id=NEW.product_id FOR UPDATE;
        SELECT current_revision INTO policy_head FROM gateway.policies WHERE id=NEW.policy_id FOR UPDATE;
      END IF;
      SELECT * INTO d FROM gateway.draft_revisions WHERE id=NEW.draft_id;
      SELECT * INTO t FROM gateway.test_runs WHERE id=NEW.test_run_id;
      IF p.id IS NULL OR d.id IS NULL OR t.id IS NULL OR policy_head IS NULL
        OR p.latest_draft_id IS DISTINCT FROM d.id OR p.draft_revision IS DISTINCT FROM d.revision
        OR d.product_id IS DISTINCT FROM NEW.product_id OR d.manifest IS DISTINCT FROM NEW.manifest OR d.content_digest IS DISTINCT FROM NEW.content_digest
        OR b.content_digest IS DISTINCT FROM NEW.binding_digest OR b.binding_id IS DISTINCT FROM NEW.manifest->>'binding_id'
        OR b.revision::text IS DISTINCT FROM NEW.manifest->>'binding_revision' OR b.model_bundle_digest IS DISTINCT FROM NEW.manifest->>'model_bundle_digest'
        OR b.adapter_revision IS DISTINCT FROM NEW.manifest->>'adapter_revision' OR NEW.policy_id IS DISTINCT FROM NEW.manifest->>'policy_id'
        OR NEW.policy_revision IS DISTINCT FROM policy_head OR t.status IS DISTINCT FROM 'passed' OR t.product_id IS DISTINCT FROM NEW.product_id
        OR t.draft_id IS DISTINCT FROM NEW.draft_id OR t.draft_digest IS DISTINCT FROM NEW.content_digest OR t.binding_id IS DISTINCT FROM NEW.binding_id
        OR t.binding_digest IS DISTINCT FROM NEW.binding_digest OR t.policy_id IS DISTINCT FROM NEW.policy_id OR t.policy_revision IS DISTINCT FROM NEW.policy_revision
        OR NEW.name IS DISTINCT FROM NEW.manifest->>'name' OR NEW.domain IS DISTINCT FROM NEW.manifest->>'domain'
        OR NEW.classification IS DISTINCT FROM NEW.manifest->>'classification' THEN
        RAISE EXCEPTION 'Publication requires exact current tested content' USING ERRCODE='P0001'; END IF;
      RETURN NEW;
    END''')
    function('source_publication_state_guard', '', 'trigger', '''
    DECLARE version gateway.product_versions%ROWTYPE; receipt gateway.source_publication_operations%ROWTYPE; action text; existing gateway.source_publication_revision_steps%ROWTYPE;
    BEGIN
      SELECT * INTO STRICT version FROM gateway.product_versions WHERE id=NEW.version_id;
      IF version.manifest->>'adapter_revision' IN ('cube-bigquery-v1','domo-named-v2') THEN
        receipt:=gateway.source_publication_receipt(NEW.source_operation_id,version.product_id,version.id);
        IF receipt.actor_snapshot->>'id' IS DISTINCT FROM NEW.actor_id
          OR (TG_OP='INSERT' AND receipt.action IS DISTINCT FROM 'publish')
          OR (receipt.action='publish' AND NEW.status IS DISTINCT FROM 'published')
          OR (receipt.action='deprecate' AND NEW.status IS DISTINCT FROM 'deprecated')
          OR (receipt.action='withdraw' AND NEW.status IS DISTINCT FROM 'withdrawn')
          OR (receipt.action='rollback' AND (TG_OP='INSERT' OR NEW.status IS DISTINCT FROM OLD.status)) THEN
          RAISE EXCEPTION 'Publication receipt unavailable' USING ERRCODE='42501'; END IF;
      ELSE
        IF NEW.source_operation_id IS NOT NULL THEN RAISE EXCEPTION 'Publication receipt unavailable' USING ERRCODE='42501'; END IF;
        PERFORM 1 FROM gateway.products WHERE id=version.product_id FOR UPDATE;
        SELECT * INTO existing FROM gateway.source_publication_revision_steps WHERE product_id=version.product_id AND transaction_id=pg_current_xact_id();
        action:=CASE WHEN TG_OP='INSERT' THEN 'publish' WHEN NEW.status='withdrawn' THEN 'withdraw' WHEN NEW.status='deprecated' AND OLD.status IS DISTINCT FROM NEW.status THEN 'deprecate' ELSE 'rollback' END;
        IF FOUND AND existing.version_id=version.id THEN action:=existing.action; END IF;
        PERFORM gateway.source_publication_advance(version.product_id,version.id,action,NULL);
      END IF;
      RETURN NEW;
    END''')
    op.execute('CREATE TRIGGER source_publication_state_guard BEFORE INSERT OR UPDATE ON gateway.version_states FOR EACH ROW EXECUTE FUNCTION gateway.source_publication_state_guard()')
    function('source_publication_head_guard', '', 'trigger', '''
    DECLARE step gateway.source_publication_revision_steps%ROWTYPE; receipt gateway.source_publication_operations%ROWTYPE; reviewed boolean; legacy_action text;
    BEGIN
      IF TG_OP='INSERT' THEN
        IF NEW.publication_revision<>0 OR NEW.source_operation_id IS NOT NULL THEN RAISE EXCEPTION 'Publication counter unavailable' USING ERRCODE='42501'; END IF;
        RETURN NEW;
      END IF;
      IF NEW.publication_revision IS DISTINCT FROM OLD.publication_revision THEN
        SELECT * INTO STRICT step FROM gateway.source_publication_revision_steps WHERE product_id=NEW.id AND transaction_id=pg_current_xact_id();
        IF NEW.publication_revision::bigint<>OLD.publication_revision::bigint+1 OR step.prior_revision<>OLD.publication_revision
          OR step.next_revision<>NEW.publication_revision OR step.operation_id IS DISTINCT FROM NEW.source_operation_id
          OR current_user IS DISTINCT FROM (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='gateway.products'::regclass) THEN
          RAISE EXCEPTION 'Publication counter unavailable' USING ERRCODE='42501'; END IF;
      END IF;
      IF NEW.current_version_id IS DISTINCT FROM OLD.current_version_id THEN
        SELECT coalesce(bool_or(manifest->>'adapter_revision' IN ('cube-bigquery-v1','domo-named-v2')),false) INTO reviewed FROM gateway.product_versions WHERE id IN (NEW.current_version_id,OLD.current_version_id);
        IF reviewed THEN
          receipt:=gateway.source_publication_receipt(NEW.source_operation_id,NEW.id,NEW.current_version_id);
          IF receipt.action NOT IN ('publish','rollback') OR receipt.previous_version_id IS DISTINCT FROM OLD.current_version_id
            OR NEW.publication_revision IS DISTINCT FROM receipt.next_product_revision THEN
            RAISE EXCEPTION 'Publication head unavailable' USING ERRCODE='42501'; END IF;
        ELSIF NEW.source_operation_id IS NOT NULL THEN RAISE EXCEPTION 'Publication head unavailable' USING ERRCODE='42501';
        ELSE
          SELECT CASE WHEN source_created_transaction_id=pg_current_xact_id() THEN 'publish' ELSE 'rollback' END INTO STRICT legacy_action FROM gateway.product_versions WHERE id=NEW.current_version_id;
          -- This BEFORE trigger cannot recursively update the same product row.
          SELECT * INTO step FROM gateway.source_publication_revision_steps WHERE product_id=NEW.id AND transaction_id=pg_current_xact_id();
          IF NOT FOUND THEN
            IF OLD.publication_revision=2147483647 THEN RAISE EXCEPTION 'Publication revision exhausted' USING ERRCODE='42501'; END IF;
            INSERT INTO gateway.source_publication_revision_steps VALUES(NEW.id,OLD.publication_revision,OLD.publication_revision+1,pg_current_xact_id(),NEW.current_version_id,legacy_action,NULL);
            NEW.publication_revision:=OLD.publication_revision+1;
          ELSIF (step.version_id,step.action,step.operation_id) IS DISTINCT FROM (NEW.current_version_id,legacy_action,NULL::uuid) THEN
            RAISE EXCEPTION 'Publication operation conflicts' USING ERRCODE='42501';
          ELSE NEW.publication_revision:=step.next_revision;
          END IF;
        END IF;
      ELSIF NEW.source_operation_id IS DISTINCT FROM OLD.source_operation_id AND NEW.publication_revision IS NOT DISTINCT FROM OLD.publication_revision THEN
        RAISE EXCEPTION 'Publication receipt unavailable' USING ERRCODE='42501';
      END IF;
      RETURN NEW;
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Publication unavailable' USING ERRCODE='42501'; END''')
    op.execute('CREATE TRIGGER source_publication_head_guard BEFORE INSERT OR UPDATE ON gateway.products FOR EACH ROW EXECUTE FUNCTION gateway.source_publication_head_guard()')
    function('source_publication_event_guard', '', 'trigger', '''
    DECLARE version gateway.product_versions%ROWTYPE; receipt gateway.source_publication_operations%ROWTYPE;
    BEGIN
      SELECT * INTO STRICT version FROM gateway.product_versions WHERE id=NEW.version_id;
      IF version.manifest->>'adapter_revision' IN ('cube-bigquery-v1','domo-named-v2') THEN
        receipt:=gateway.source_publication_receipt(NEW.source_operation_id,NEW.product_id,NEW.version_id);
        IF (receipt.action,receipt.actor_snapshot->>'id',receipt.request_id::text) IS DISTINCT FROM (NEW.action,NEW.actor_id,NEW.request_id) THEN
          RAISE EXCEPTION 'Publication event unavailable' USING ERRCODE='42501'; END IF;
      ELSE
        IF NEW.source_operation_id IS NOT NULL THEN RAISE EXCEPTION 'Publication event unavailable' USING ERRCODE='42501'; END IF;
        PERFORM 1 FROM gateway.products WHERE id=NEW.product_id FOR UPDATE;
        PERFORM gateway.source_publication_advance(NEW.product_id,NEW.version_id,NEW.action,NULL);
      END IF;
      IF (SELECT count(*) FROM gateway.publication_events WHERE product_id=NEW.product_id AND created_transaction_id=pg_current_xact_id())<>1 THEN
        RAISE EXCEPTION 'Duplicate publication operation' USING ERRCODE='42501'; END IF;
      RETURN NULL;
    END''')
    op.execute('CREATE TRIGGER source_publication_event_guard AFTER INSERT ON gateway.publication_events FOR EACH ROW EXECUTE FUNCTION gateway.source_publication_event_guard()')
    function('source_publication_head_complete', '', 'trigger', '''
    DECLARE step gateway.source_publication_revision_steps%ROWTYPE;
    BEGIN
      IF NEW.current_version_id IS DISTINCT FROM OLD.current_version_id THEN
        SELECT * INTO STRICT step FROM gateway.source_publication_revision_steps WHERE product_id=NEW.id AND transaction_id=pg_current_xact_id();
        IF step.version_id IS DISTINCT FROM NEW.current_version_id OR step.action NOT IN ('publish','rollback')
          OR (SELECT publication_revision FROM gateway.products WHERE id=NEW.id) IS DISTINCT FROM step.next_revision THEN
          RAISE EXCEPTION 'Publication head incomplete' USING ERRCODE='42501'; END IF;
      END IF;
      RETURN NULL;
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Publication head incomplete' USING ERRCODE='42501'; END''')
    op.execute('CREATE CONSTRAINT TRIGGER source_publication_head_complete AFTER UPDATE ON gateway.products DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.source_publication_head_complete()')
    function('source_publication_complete', '', 'trigger', '''
    DECLARE head gateway.products%ROWTYPE; version gateway.product_versions%ROWTYPE; state gateway.version_states%ROWTYPE; event gateway.publication_events%ROWTYPE;
      step gateway.source_publication_revision_steps%ROWTYPE; pin jsonb; approval_id uuid; checked_at timestamptz;
    BEGIN
      SELECT * INTO STRICT head FROM gateway.products WHERE id=NEW.product_id;
      SELECT * INTO STRICT version FROM gateway.product_versions WHERE id=NEW.version_id;
      SELECT * INTO STRICT state FROM gateway.version_states WHERE version_id=NEW.version_id;
      SELECT * INTO STRICT event FROM gateway.publication_events WHERE source_operation_id=NEW.id;
      SELECT * INTO STRICT step FROM gateway.source_publication_revision_steps WHERE product_id=NEW.product_id AND transaction_id=NEW.transaction_id;
      IF NEW.transaction_id IS DISTINCT FROM pg_current_xact_id() OR (head.publication_revision,head.source_operation_id) IS DISTINCT FROM (NEW.next_product_revision,NEW.id)
        OR (step.prior_revision,step.next_revision,step.version_id,step.action,step.operation_id)
          IS DISTINCT FROM (NEW.expected_product_revision,NEW.next_product_revision,NEW.version_id,NEW.action,NEW.id)
        OR (event.product_id,event.version_id,event.action,event.actor_id,event.request_id,event.created_transaction_id)
          IS DISTINCT FROM (NEW.product_id,NEW.version_id,NEW.action,NEW.actor_snapshot->>'id',NEW.request_id::text,NEW.transaction_id)
        OR state.source_operation_id IS DISTINCT FROM NEW.id OR state.actor_id IS DISTINCT FROM NEW.actor_snapshot->>'id'
        OR (NEW.action='publish' AND (version.source_operation_id IS DISTINCT FROM NEW.id OR version.draft_id IS DISTINCT FROM NEW.draft_id OR version.test_run_id IS DISTINCT FROM NEW.test_run_id OR state.status IS DISTINCT FROM 'published'))
        OR (NEW.action IN ('publish','rollback') AND head.current_version_id IS DISTINCT FROM NEW.version_id)
        OR (NEW.action IN ('deprecate','withdraw') AND head.current_version_id IS DISTINCT FROM NEW.previous_version_id)
        OR (NEW.action='deprecate' AND state.status IS DISTINCT FROM 'deprecated') OR (NEW.action='withdraw' AND state.status IS DISTINCT FROM 'withdrawn')
        OR (SELECT count(*) FROM gateway.publication_events WHERE product_id=NEW.product_id AND created_transaction_id=NEW.transaction_id)<>1 THEN
        RAISE EXCEPTION 'Publication operation incomplete' USING ERRCODE='42501'; END IF;
      PERFORM gateway.source_fixture_invocation_access(NEW.fixture_invocation_id::text,'author',false);
      checked_at:=clock_timestamp();
      FOR pin IN SELECT v FROM jsonb_array_elements(NEW.target_pins) a(v) WHERE v->>'kind'='publisher' LOOP
        approval_id:=(pin->'approval'->>'approval_id')::uuid;
        IF NOT EXISTS(SELECT 1 FROM gateway.source_access_approvals a JOIN gateway.source_access_approval_states s ON s.approval_id=a.id WHERE a.id=approval_id
          AND a.expires_at>checked_at AND s.state='active' AND s.state_revision=1) THEN RAISE EXCEPTION 'Publication expired' USING ERRCODE='42501'; END IF;
      END LOOP;
      RETURN NULL;
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Publication operation incomplete' USING ERRCODE='42501'; END''')
    op.execute('CREATE CONSTRAINT TRIGGER source_publication_complete AFTER INSERT ON gateway.source_publication_operations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.source_publication_complete()')

    function('source_publication_step_complete', '', 'trigger', '''
    DECLARE head gateway.products%ROWTYPE; state gateway.version_states%ROWTYPE;
    BEGIN
      SELECT * INTO STRICT head FROM gateway.products WHERE id=NEW.product_id;
      SELECT * INTO STRICT state FROM gateway.version_states WHERE version_id=NEW.version_id;
      IF NEW.transaction_id IS DISTINCT FROM pg_current_xact_id() OR head.publication_revision IS DISTINCT FROM NEW.next_revision
        OR (NEW.action IN ('publish','rollback') AND head.current_version_id IS DISTINCT FROM NEW.version_id)
        OR (NEW.action='publish' AND state.status IS DISTINCT FROM 'published')
        OR (NEW.action='deprecate' AND state.status IS DISTINCT FROM 'deprecated')
        OR (NEW.action='withdraw' AND state.status IS DISTINCT FROM 'withdrawn')
        OR (NEW.operation_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM gateway.source_publication_operations r WHERE r.id=NEW.operation_id
          AND r.transaction_id=NEW.transaction_id AND r.expected_product_revision=NEW.prior_revision AND r.next_product_revision=NEW.next_revision))
        OR (SELECT count(*) FROM gateway.publication_events WHERE product_id=NEW.product_id AND created_transaction_id=NEW.transaction_id)>1 THEN
        RAISE EXCEPTION 'Publication counter incomplete' USING ERRCODE='42501'; END IF;
      RETURN NULL;
    EXCEPTION WHEN NO_DATA_FOUND THEN RAISE EXCEPTION 'Publication counter incomplete' USING ERRCODE='42501'; END''')
    op.execute('CREATE CONSTRAINT TRIGGER source_publication_step_complete AFTER INSERT ON gateway.source_publication_revision_steps DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.source_publication_step_complete()')
