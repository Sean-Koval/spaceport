"""Real PostgreSQL fixture invocation and publication provenance, no provider I/O."""
from copy import deepcopy
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from gateway.contracts.canonical import canonical_bytes, canonical_digest
from gateway.contracts.manifest import Fixture
from gateway.publishing.test_runner import ApprovedFixture, BigQueryFixtureCatalog, execution_contract_digest
from tests.integration.conftest import connection_kwargs
from tests.integration.test_source_approval_registry import approval_assets, install
from tests.unit.test_source_approval_contracts import audience, principal

pytestmark = pytest.mark.postgres


def isolated_approval_assets(db, valid_manifest):
    """Explicit synthetic owner setup; never an app/bootstrap approval path."""
    from psycopg.types.json import Jsonb
    from gateway.contracts.canonical import canonical_digest
    from gateway.adapters.bigquery_runtime import BigQueryBinding, BigQuerySource
    from gateway.source_access_contracts import physical_source_identity_digest, source_credential_binding_digest
    from tests.unit.test_source_approval_contracts import package
    from tests.unit.test_bigquery_serving_source import source_value
    from tests.contract.test_cube_adapter import synthetic
    from tests.integration.test_audit import seed
    doc, manifest = package.__wrapped__(valid_manifest)
    seed(db, roles=('consumer','author','publisher'))
    db.execute_owner("UPDATE gateway.principals SET kind='user',service_client_id='test-client' WHERE id='north-user'")
    original = synthetic()[0].model_dump(mode='json')
    for key in ('constraint_members','watermark_member'):
        original.pop(key)
    bindings=[]; sources=[]; lineage=[]
    for kind, context in (('consumer','consumer'),('fixture','fixture_test')):
        model = manifest.model_bundle_digest if kind=='consumer' else '1'*64
        source = BigQuerySource.model_validate(source_value(source_id=kind+'-source',context=context,model_bundle_digest=model,
            auth={'mode':'gke-workload-identity','expected_principal':kind+'@billing-project.iam.gserviceaccount.com'},
            data_project=kind+'-data-project',dataset=kind+'_dataset',cube_endpoint='https://'+kind+'.synthetic.svc.cluster.local'))
        source_pin=dict(source_id=source.source_id,revision=source.revision,provider=source.provider,context=context,
            descriptor_digest=source.digest,physical_identity_digest=physical_source_identity_digest(source),credential_binding_digest=source_credential_binding_digest(source))
        binding=BigQueryBinding.model_validate(original | dict(context=context,binding_id=doc['bindings'][kind]['binding_id'],
            adapter_revision='cube-bigquery-v1',source_id=source.source_id,source_revision=source.revision,source_digest=source.digest,
            model_bundle_digest=model,scope={'tenant_member':'Orders.tenant_id','access_group_member':'Orders.access_group'},
            freshness_mode='unknown',checkpoint_member=None))
        physical={'provider':'bigquery','billing_project':source.billing_project,'location':source.location}
        db.execute_owner('''INSERT INTO gateway.source_pools(capacity_key,provider,physical_identity,max_active,max_dispatches)
            VALUES(%s,'bigquery',%s,2,8000) ON CONFLICT(capacity_key) DO NOTHING''',(source.capacity_pool_id,Jsonb(physical)))
        db.execute_owner('''INSERT INTO gateway.source_profiles(source_id,revision,context,provider,capacity_key,descriptor_digest,descriptor)
            VALUES(%s,1,%s,'bigquery',%s,%s,%s)''',(source.source_id,context,source.capacity_pool_id,source.digest,Jsonb(source.model_dump(mode='json'))))
        db.execute_owner('''INSERT INTO gateway.binding_revisions(id,binding_id,revision,content,content_digest,model_bundle_digest,adapter_revision,actor_id)
            VALUES(%s,%s,1,%s,%s,%s,'cube-bigquery-v1','synthetic-operator')''',
            (kind+'-binding-row',binding.binding_id,Jsonb(binding.model_dump(mode='json')),binding.digest,model))
        doc['bindings'][kind]={'binding_id':binding.binding_id,'revision':1,'content_digest':binding.digest}
        doc['sources'][kind]=source_pin
        for name,field in binding.fields.items():
            lineage.append(dict(id=name,source_context=context,source_member=field.member,purpose='exposed',classification='non-personal'))
            if field.kind!='measure':
                lineage.append(dict(id=name,source_context=context,source_member=field.member,purpose='grouping',classification='non-personal'))
        for name in binding.constraint_members:
            lineage.append(dict(id=name,source_context=context,source_member=name,purpose='hidden_scope',classification='non-personal'))
        for name in manifest.inputs:
            member=binding.fields[binding.input_fields[name]].member if name in binding.input_fields else next(f.member for f in binding.fields.values() if f.kind=='time')
            lineage.append(dict(id=name,source_context=context,source_member=member,purpose='source_input',classification='non-personal'))
        bindings.append(binding);sources.append(source)
    doc['lineage']=lineage
    return doc,manifest,bindings,sources


def fixture_setup(db, valid_manifest, *, omit_case=False, expires_seconds=None, without_result=False):
    raw = deepcopy(valid_manifest)
    raw['freshness']['required'] = False
    doc, manifest, bindings, sources = isolated_approval_assets(db, raw)
    for name in ('fixed-positive', 'fixed-negative'):
        db.execute_owner('INSERT INTO gateway.principals(id,subject,kind) VALUES(%s,%s,\'demo\')', (name, name))
    db.execute_owner("INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) VALUES('fixed-positive','revenue-access','north','[\"consumer\"]','{\"groups\":[\"north\"]}')")
    negative = Fixture.model_validate(dict(id='permission-denied', version='1', inputs=dict(manifest.fixtures[0].inputs), expected_error='NOT_FOUND_OR_FORBIDDEN'))
    catalog = BigQueryFixtureCatalog(product_id=manifest.product_id, consumer_binding=bindings[0], fixture_binding=bindings[1], consumer_source=sources[0], fixture_source=sources[1], execution_contract_digest=execution_contract_digest(manifest), cases=(
        *[ApprovedFixture(fixture=fixture, principal_id='fixed-positive', kind='input_negative' if fixture.expected_error=='INVALID_INPUT' else 'known_answer') for fixture in manifest.fixtures],
        ApprovedFixture(fixture=negative, principal_id='fixed-negative', kind='permission_negative')))
    doc['artifacts']['fixture_catalog_digest'] = catalog.digest
    doc['audiences'][0]['actions'] = ['author', 'publish']
    doc['audiences'].append(audience(id='invoking-author', context='fixture_test', actions=['author']))
    for case in catalog.cases:
        if omit_case and case.kind == 'permission_negative':
            continue
        doc['audiences'].append(audience(id=case.principal_id+'-'+case.fixture.id, context='fixture_test', principal_id=case.principal_id, principal_kind='demo', client_ids=[None], actions=['execute', 'result'], fixture_case_ids=[case.fixture.id]))
    if expires_seconds is not None:
        from datetime import timezone
        doc['expires_at']=db.scalar("SELECT clock_timestamp()+make_interval(secs=>%s)",(expires_seconds,)).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    if without_result:
        for entry in doc['audiences']:
            if entry['principal_id']=='fixed-positive':entry['actions']=['execute']
    installed = install(db, doc)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
        conn.execute("INSERT INTO gateway.products(id) VALUES('revenue')")
        conn.execute("INSERT INTO gateway.draft_revisions(id,product_id,revision,manifest,manifest_canonical,content_digest,actor_id) VALUES('fixture-draft','revenue',1,%s,%s,%s,'north-user')", (Jsonb(manifest.model_dump(mode='json')), canonical_bytes(manifest.model_dump(mode='python',exclude={'content_digest'})).decode(), manifest.content_digest))
        conn.execute("UPDATE gateway.products SET draft_revision=1,latest_draft_id='fixture-draft' WHERE id='revenue'")
    return doc, manifest, catalog, installed


def accept(conn, catalog, *, actor=None, run_id=None):
    return conn.execute('SELECT gateway.source_accept_fixture_invocation(%s,%s,%s,%s)', ('fixture-draft', Jsonb((actor or principal()).model_dump(mode='json')), canonical_bytes(catalog.model_dump(mode='python')).decode(), run_id or str(uuid4()))).fetchone()[0]


def test_fixture_invocation_is_generated_complete_and_audited(registry_database, valid_manifest):
    db = registry_database
    _, manifest, catalog, installed = fixture_setup(db, valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        snapshot = accept(conn, catalog)
    from gateway.source_fixture_access import _snapshot,FixtureInvocationSnapshot
    parsed=_snapshot(snapshot,FixtureInvocationSnapshot)
    assert len(parsed.cases)==2
    assert snapshot['actor'] == principal().model_dump(mode='json')
    assert snapshot['accepted']['approval_id'] == installed['id']
    assert snapshot['product']['manifest']['content_digest'] == manifest.content_digest
    assert {case['fixture_id'] for case in snapshot['cases']} == {case.fixture.id for case in catalog.cases}
    assert db.scalar('SELECT count(*) FROM gateway.fixture_source_invocations') == 1
    assert db.scalar('SELECT count(*) FROM gateway.fixture_source_invocation_cases') == 2
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow' AND source_approval_id=%s", (installed['id'],)) == 1


@pytest.mark.parametrize('registry_database', [{}, {'unsafe_default_grants': True}], indirect=True)
def test_fixture_protected_rows_and_private_predicates_are_not_app_authority(registry_database):
    db = registry_database
    for table in ('fixture_source_invocations', 'fixture_source_invocation_cases', 'fixture_attempt_source_authorities', 'fixture_test_source_authorities', 'source_publication_operations'):
        assert not db.scalar('SELECT has_table_privilege(%s,%s,%s)', (db.roles['app'], 'gateway.'+table, 'INSERT,UPDATE,DELETE'))
    for function in ('source_fixture_invocation_access(text,text,boolean)', 'source_fixture_batch_complete(jsonb)'):
        assert not db.scalar('SELECT has_function_privilege(%s,%s,%s)', (db.roles['app'], 'gateway.'+function, 'EXECUTE'))


def test_missing_negative_review_never_accepts_invocation(registry_database, valid_manifest):
    db = registry_database
    _, _, catalog, _ = fixture_setup(db, valid_manifest, omit_case=True)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error):
            accept(conn, catalog)
    assert db.scalar('SELECT count(*) FROM gateway.fixture_source_invocations') == 0
    assert db.scalar('SELECT count(*) FROM gateway.audit_events') == 0


@pytest.mark.parametrize('change', ['client', 'catalog', 'binding'])
def test_invocation_rejects_changed_client_catalog_or_binding(registry_database, valid_manifest, change):
    db = registry_database
    _, _, catalog, _ = fixture_setup(db, valid_manifest)
    actor = principal()
    if change == 'client':
        actor = actor.model_copy(update={'service_client_id': 'another-client'})
    else:
        raw = catalog.model_dump(mode='json')
        if change == 'catalog':
            raw['cases'][0]['fixture']['version'] = '2'
        else:
            raw['fixture_binding']['binding_id'] = 'substituted-fixture'
        class Changed:
            def model_dump(self, **_): return raw
        catalog = Changed()
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error):
            accept(conn, catalog, actor=actor)
    assert db.scalar('SELECT count(*) FROM gateway.fixture_source_invocations') == 0


def test_original_invocation_cannot_borrow_replacement_approval(registry_database, valid_manifest):
    db = registry_database
    doc, _, catalog, installed = fixture_setup(db, valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        snapshot = accept(conn, catalog)
    db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)', (installed['id'], installed['content_digest'], 'fixture-ended'))
    replacement = deepcopy(doc)
    replacement['review']['review_id'] = 'new-fixture-review'
    install(db, replacement)
    assert db.denied_as_owner("SELECT gateway.source_fixture_invocation_access(%s,'author',true)", (snapshot['invocation_id'],)) == 'permission_denied'


def test_publication_revision_is_protected_and_historical_zero(registry_database):
    db = registry_database
    db.execute_owner("INSERT INTO gateway.products(id) VALUES('historical-product')")
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='historical-product'") == 0
    assert db.denied_as_app("UPDATE gateway.products SET publication_revision=1 WHERE id='historical-product'") == 'permission_denied'


@pytest.mark.parametrize('registry_database', [{'initial_revision': '0001_registry'}], indirect=True)
def test_fixture_migration_reports_safe_sql_errors(registry_database):
    """Same disposable owner upgrade in-process; expose no URL or parameters."""
    from tests.integration.registry_migration_pair import collect_owned_pair
    from sqlalchemy.exc import SQLAlchemyError
    db = registry_database
    try:
        receipt = collect_owned_pair(db.urls['owner'], db.roles, 'head')
    except SQLAlchemyError as error:
        original = error.orig
        pytest.fail(f'SQLSTATE={getattr(original, "sqlstate", "unknown")}; {getattr(getattr(original, "diag", None), "message_primary", "Migration unavailable")}', pytrace=False)
    assert receipt['first'] == receipt['second']


def insert_attempt(conn, manifest, catalog, invocation_id, *, attempt_id='source-attempt', principal_override=None, fixed=None, decision=None):
    from gateway.contracts.query import QueryRequest, validate_query
    case = catalog.cases[0]
    fixed = fixed or conn.execute("SELECT gateway.source_fixture_principal('fixed-positive')").fetchone()[0]
    decision = decision or conn.execute("SELECT gateway.source_current_decision(%s,%s,'execute')", (Jsonb(fixed), Jsonb(manifest.model_dump(mode='json')))).fetchone()[0]
    query = validate_query(manifest, QueryRequest(product_id=manifest.product_id, version=manifest.version, inputs=case.fixture.inputs)).model_dump(mode='json', exclude={'idempotency_key'})
    derived = catalog.execution_manifest(manifest)
    values = dict(id=attempt_id,product_id=manifest.product_id,draft_id='fixture-draft',draft_digest=manifest.content_digest,
        consumer_binding_id='consumer-binding-row',consumer_binding_digest=catalog.consumer_binding.digest,
        fixture_binding_id='fixture-binding-row',fixture_binding_digest=catalog.fixture_binding.digest,
        fixture_manifest=Jsonb(derived.model_dump(mode='json')),fixture_manifest_digest=derived.content_digest,
        execution_contract_digest=catalog.execution_contract_digest,policy_id=manifest.policy_id,policy_revision=decision['policy_revision'],
        catalog_digest=catalog.digest,fixture_id=case.fixture.id,fixture_version=case.fixture.version,actor_id='north-user',
        fixed_principal_id='fixed-positive',principal_snapshot=Jsonb(principal_override or fixed),decision=Jsonb(decision),
        canonical_query=Jsonb(query),canonical_hash=canonical_digest(query),runner_token=str(uuid4()),request_id=str(uuid4()),
        source_invocation_id=invocation_id,fixture_manifest_canonical=canonical_bytes(derived.model_dump(mode='python',exclude={'content_digest'})).decode(),
        query_canonical=canonical_bytes(query).decode(),decision_canonical=canonical_bytes(decision).decode())
    columns=','.join(values)
    placeholders=','.join(['%s']*len(values))
    conn.execute(f'INSERT INTO gateway.fixture_attempts({columns},deadline_at) VALUES({placeholders},clock_timestamp())',tuple(values.values()))
    conn.execute("INSERT INTO gateway.fixture_attempt_states(attempt_id,state,source_completed) VALUES(%s,'running',false)",(attempt_id,))
    return decision


def insert_failed_test(conn, manifest, catalog, invocation_id, *, passing_negative=True):
    from gateway.publishing.test_runner import FixtureCheck, FixtureEvidence
    checks=[]
    for case in catalog.cases:
        passed = case.kind=='permission_negative' and passing_negative
        checks.append(FixtureCheck(fixture_id=case.fixture.id,kind=case.kind,passed=passed,
            expected=case.fixture.expected_rows,expected_error=case.fixture.expected_error,
            actual_error='NOT_FOUND_OR_FORBIDDEN' if passed else 'RESOURCE_LIMIT',
            absolute_tolerance=case.fixture.tolerance,relative_tolerance=case.relative_tolerance,reference_watermark=case.fixture.source_watermark))
    evidence=FixtureEvidence(catalog_digest=catalog.digest,execution_contract_digest=catalog.execution_contract_digest,
        fixture_binding_digest=catalog.fixture_binding.digest,fixture_model_bundle_digest=catalog.fixture_binding.model_bundle_digest,checks=checks)
    conn.execute('''INSERT INTO gateway.test_runs(id,product_id,draft_id,draft_digest,binding_id,binding_digest,policy_id,policy_revision,fixture_version,evidence,evidence_canonical,source_decision_canonicals,status,actor_id)
        VALUES(%s,%s,'fixture-draft',%s,'consumer-binding-row',%s,%s,1,%s,%s,%s,'{}','failed','north-user')''',
        (invocation_id,manifest.product_id,manifest.content_digest,catalog.consumer_binding.digest,manifest.policy_id,catalog.digest,Jsonb(evidence.model_dump(mode='json')),canonical_bytes(evidence).decode()))


def test_positive_attempt_pins_original_owner_and_separate_result_audience(registry_database, valid_manifest):
    db=registry_database
    _, manifest, catalog, installed=fixture_setup(db, valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
        invocation=accept(conn,catalog)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
        decision=insert_attempt(conn,manifest,catalog,invocation['invocation_id'])
    value=db.scalar("SELECT gateway.source_fixture_attempt_access('source-attempt','result',false)")
    assert value['decision']==decision
    assert value['accepted']['approval_id']==installed['id']
    assert value['invoking_actor']==principal().model_dump(mode='json')
    assert value['manifest']['content_digest']==manifest.content_digest
    assert value['binding_digest']==catalog.consumer_binding.digest
    assert db.scalar('SELECT count(*) FROM gateway.fixture_attempt_source_authorities')==1


def test_same_invocation_case_cannot_accept_duplicate_positive_attempt(registry_database,valid_manifest):
    db=registry_database
    _,manifest,catalog,_=fixture_setup(db,valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn: invocation=accept(conn,catalog)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn: insert_attempt(conn,manifest,catalog,invocation['invocation_id'])
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
        with pytest.raises(psycopg.Error): insert_attempt(conn,manifest,catalog,invocation['invocation_id'],attempt_id='duplicate-case')
    assert db.scalar('SELECT count(*) FROM gateway.fixture_attempts')==1


def test_invocation_uuid_collision_does_not_replay_or_duplicate_audit(registry_database,valid_manifest):
    db=registry_database
    _,_,catalog,_=fixture_setup(db,valid_manifest)
    run_id=str(uuid4())
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn: accept(conn,catalog,run_id=run_id)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error): accept(conn,catalog,run_id=run_id)
    assert db.scalar('SELECT count(*) FROM gateway.fixture_source_invocations')==1
    assert db.scalar('SELECT count(*) FROM gateway.audit_events')==1


def test_fixture_capacity_mutex_cannot_be_deleted(registry_database,valid_manifest):
    db=registry_database
    _,_,catalog,_=fixture_setup(db,valid_manifest)
    assert db.denied_as_owner('DELETE FROM gateway.capacity_locks') == 'permission_denied'
    assert db.scalar("SELECT count(*) FROM gateway.capacity_locks WHERE scope_id='deployment'")==1


def test_fixed_identity_client_may_not_borrow_invoker_client(registry_database,valid_manifest):
    db=registry_database
    _,_,catalog,_=fixture_setup(db,valid_manifest)
    db.execute_owner("UPDATE gateway.principals SET kind='user',service_client_id=NULL WHERE id='fixed-negative'")
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error): accept(conn,catalog)
    assert db.scalar('SELECT count(*) FROM gateway.fixture_source_invocations')==0


def test_structural_negative_evidence_requires_genuine_current_policy_denial(registry_database,valid_manifest):
    db=registry_database
    _,manifest,catalog,_=fixture_setup(db,valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn: invocation=accept(conn,catalog)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn: insert_failed_test(conn,manifest,catalog,invocation['invocation_id'])
    assert db.scalar('SELECT count(*) FROM gateway.fixture_test_source_authorities')==1
    db.execute_owner("INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) VALUES('fixed-negative','revenue-access','north','[\"consumer\"]','{\"groups\":[\"north\"]}')")
    assert db.denied_as_owner("SELECT gateway.source_fixture_invocation_access(%s,'author',false)",(invocation['invocation_id'],))=='permission_denied'


def test_malformed_policy_cannot_be_structural_negative_success(registry_database,valid_manifest):
    db=registry_database
    _,manifest,catalog,_=fixture_setup(db,valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn: invocation=accept(conn,catalog)
    db.execute_owner("UPDATE gateway.principal_grants SET entitlements='{}'::jsonb||jsonb_build_object('groups','[]'::jsonb) WHERE principal_id='fixed-positive'")
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error): insert_failed_test(conn,manifest,catalog,invocation['invocation_id'])
    assert db.scalar('SELECT count(*) FROM gateway.test_runs')==0


def test_invocation_audit_delay_rechecks_real_database_expiry(registry_database,valid_manifest):
    db=registry_database
    doc,_,catalog,installed=fixture_setup(db,valid_manifest)
    # A distinct explicit short-lived review; revoke the old candidate first.
    db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(installed['id'],installed['content_digest'],'short-fixture-review'))
    expiry=db.scalar("SELECT clock_timestamp()+interval '1 second'")
    from datetime import timezone
    doc['expires_at']=expiry.astimezone(timezone.utc).isoformat().replace('+00:00','Z');doc['review']['review_id']='short-fixture-review'
    install(db,doc)
    db.execute_owner("CREATE FUNCTION gateway.fixture_slow_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(1.05); RETURN NEW; END $$")
    db.execute_owner('CREATE TRIGGER fixture_slow_audit BEFORE INSERT ON gateway.audit_events FOR EACH ROW EXECUTE FUNCTION gateway.fixture_slow_audit()')
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error): accept(conn,catalog)
    assert db.scalar('SELECT count(*) FROM gateway.fixture_source_invocations')==0
    assert db.scalar('SELECT count(*) FROM gateway.audit_events')==0


def legacy_seed(db,valid_manifest):
    import asyncio
    from tests.integration.test_registry import seed,publish
    async def make():
        manifest,draft=await seed(db,valid_manifest)
        await publish(db.registry,draft)
        return manifest,draft
    return asyncio.run(make())


def test_legacy_publish_and_each_transition_advance_exactly_once(registry_database,valid_manifest):
    db=registry_database
    legacy_seed(db,valid_manifest)
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==1
    db.execute_owner("INSERT INTO gateway.principals(id,subject,kind) VALUES('publisher','publisher','demo')")
    for revision,action,status in [(2,'deprecate','deprecated'),(3,'withdraw','withdrawn')]:
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
            conn.execute("UPDATE gateway.version_states SET status=%s,actor_id='publisher',reason='Explicit synthetic transition' WHERE version_id='version-1'",(status,))
            conn.execute("INSERT INTO gateway.publication_events(id,product_id,version_id,action,actor_id,reason,request_id) VALUES(%s,'revenue','version-1',%s,'publisher','Explicit synthetic transition',%s)",(str(uuid4()),action,str(uuid4())))
        assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==revision
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_revision_steps')==3


def test_legacy_duplicate_events_and_rollback_leave_counter_unchanged(registry_database,valid_manifest):
    db=registry_database
    legacy_seed(db,valid_manifest)
    db.execute_owner("INSERT INTO gateway.principals(id,subject,kind) VALUES('publisher','publisher','demo')")
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute("UPDATE gateway.version_states SET status='deprecated' WHERE version_id='version-1'")
            for _ in range(2):
                conn.execute("INSERT INTO gateway.publication_events(id,product_id,version_id,action,actor_id,reason,request_id) VALUES(%s,'revenue','version-1','deprecate','publisher','Explicit synthetic transition',%s)",(str(uuid4()),str(uuid4())))
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==1
    assert db.scalar("SELECT status FROM gateway.version_states WHERE version_id='version-1'")=='published'
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_revision_steps')==1


def test_legacy_head_only_rollback_advances_and_rejects_aba(registry_database,valid_manifest):
    import asyncio
    from gateway.contracts.manifest import validate_manifest
    from gateway.registry.models import TestRun
    from tests.integration.test_registry import publish
    db=registry_database
    _,draft=legacy_seed(db,valid_manifest)
    second=deepcopy(valid_manifest);second['version']='2.0.0';second=validate_manifest(second)
    async def second_version():
        newer=await db.registry.save_draft('revenue',second,1,'editor')
        async with db.registry.transaction() as uow:
            uow.session.add(TestRun(id='test-2',product_id='revenue',draft_id=newer.id,draft_digest=second.content_digest,
                binding_id='binding-1',binding_digest='b'*64,policy_id=second.policy_id,policy_revision=1,fixture_version='1',
                evidence={'known_answer':'pass'},status='passed',actor_id='tester'))
        await publish(db.registry,newer,version_id='version-2',test_run_id='test-2')
    asyncio.run(second_version())
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==2
    db.execute_owner("UPDATE gateway.products SET current_version_id='version-1' WHERE id='revenue'")
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==3
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute("UPDATE gateway.products SET current_version_id='version-2' WHERE id='revenue'")
            conn.execute("UPDATE gateway.products SET current_version_id='version-1' WHERE id='revenue'")
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==3
    assert db.scalar("SELECT current_version_id FROM gateway.products WHERE id='revenue'")=='version-1'


def test_publication_recorder_rejects_missing_or_overflow_revision_without_mutation(registry_database,valid_manifest):
    db=registry_database
    _,_,catalog,_=fixture_setup(db,valid_manifest)
    for expected in (None,-1,2147483647):
        assert db.denied_as_app('SELECT gateway.source_record_publication_operation(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (Jsonb(principal().model_dump(mode='json')),'publish','revenue','new-version','fixture-draft',str(uuid4()),None,expected,1,str(uuid4()),str(uuid4())))=='permission_denied'
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==0
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations')==0


def test_legacy_event_cannot_turn_deprecation_into_unrecorded_withdrawal(registry_database,valid_manifest):
    db=registry_database
    legacy_seed(db,valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error):
            with conn.transaction():
                conn.execute("UPDATE gateway.version_states SET status='deprecated' WHERE version_id='version-1'")
                conn.execute("UPDATE gateway.version_states SET status='withdrawn' WHERE version_id='version-1'")
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==1
    assert db.scalar("SELECT status FROM gateway.version_states WHERE version_id='version-1'")=='published'


def test_review_invocation_acceptance_rechecks_execution_budget_after_allow_audit(registry_database,valid_manifest):
    db=registry_database
    valid_manifest['limits']={'execution_timeout_seconds':1,'sync_wait_seconds':0}
    _,_,catalog,_=fixture_setup(db,valid_manifest)
    db.execute_owner("CREATE FUNCTION gateway.review_slow_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(1.05); RETURN NEW; END $$")
    db.execute_owner('CREATE TRIGGER review_slow_audit BEFORE INSERT ON gateway.audit_events FOR EACH ROW EXECUTE FUNCTION gateway.review_slow_audit()')
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error):accept(conn,catalog)
    assert db.scalar('SELECT count(*) FROM gateway.fixture_source_invocations')==0


def test_review_invocation_acceptance_rechecks_approval_expiry_at_commit(registry_database,valid_manifest):
    from datetime import timezone
    db=registry_database
    doc,_,catalog,installed=fixture_setup(db,valid_manifest)
    db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(installed['id'],installed['content_digest'],'review-replace'))
    expiry=db.scalar("SELECT clock_timestamp()+interval '2 seconds'")
    doc['expires_at']=expiry.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    doc['review']['review_id']='review-short-invocation';install(db,doc)
    reached=[]
    with pytest.raises(psycopg.Error):
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
            accept(conn,catalog);reached.append('accepted')
            conn.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.03))',(expiry,))
    assert reached==['accepted']
    assert db.scalar('SELECT count(*) FROM gateway.fixture_source_invocations')==0


@pytest.mark.parametrize('number',[1e-7,-0.0,1.25])
def test_isolated_fixture_canonical_byte_projections_preserve_numbers(registry_database,valid_manifest,number):
    from gateway.contracts.manifest import validate_manifest
    from gateway.publishing.test_runner import FixtureCheck
    # This is an isolated canonical-predicate test, not provider acceptance:
    # BigQuery's serving binding supports decimal measures, not number outputs.
    valid_manifest['outputs']['revenue']={'type':'number','enum':[number],'default':{'value':number}}
    for row in valid_manifest['fixtures'][0]['expected_rows']:row['revenue']=number
    manifest=validate_manifest(valid_manifest)
    text=canonical_bytes(manifest.model_dump(mode='python',exclude={'content_digest'})).decode()
    omitted=['name','description','examples','owner','domain','links','version','content_digest','fixtures']
    projected=registry_database.scalar('SELECT gateway.source_fixture_omit(%s,%s)',(text,omitted))
    import hashlib
    assert hashlib.sha256(projected.encode()).hexdigest()==execution_contract_digest(manifest)
    case=ApprovedFixture(fixture=manifest.fixtures[0],principal_id='fixed-positive',kind='known_answer')
    catalog_text=canonical_bytes(dict(cases=[case.model_dump(mode='python')])).decode()
    case_text=registry_database.scalar('SELECT gateway.source_fixture_case_text(%s,%s)',(catalog_text,case.fixture.id))
    assert hashlib.sha256(case_text.encode()).hexdigest()==canonical_digest(case)
    check=FixtureCheck(fixture_id=case.fixture.id,kind=case.kind,passed=True,expected=case.fixture.expected_rows,actual=case.fixture.expected_rows)
    for value in (manifest.model_dump(mode='python',exclude={'content_digest'}),{'inputs':{'number':number}},check.model_dump(mode='python')):
        canonical=canonical_bytes(value).decode()
        assert registry_database.scalar('SELECT gateway.source_json_witness(%s,%s,%s)',(canonical,Jsonb(value),canonical_digest(value))) is True


def test_out_of_domain_input_negative_never_accepts_invocation(registry_database,valid_manifest):
    valid_manifest['fixtures'].append(dict(id='invalid-number',version='1',inputs=dict(start='2024-03-09',end='2024-03-12',segment=1e-7),expected_error='INVALID_INPUT'))
    db=registry_database
    _,_,catalog,_=fixture_setup(db,valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error):accept(conn,catalog)
    assert db.scalar('SELECT count(*) FROM gateway.fixture_source_invocations')==0


@pytest.mark.parametrize('number',[1e-7,-0.0,1.25])
def test_decision_scope_byte_projection_preserves_fractional_scalars(registry_database,number):
    from gateway.adapters.base import authorization_scope_digest
    from tests.unit.test_source_approval_contracts import decision
    value=decision(row_constraints=[{'member':'Orders.access_group','operator':'in','values':[number,'north']}])
    canonical=canonical_bytes(value).decode()
    projected=registry_database.scalar('SELECT gateway.source_fixture_scope_text(%s)',(canonical,))
    import hashlib
    assert hashlib.sha256(projected.encode()).hexdigest()==authorization_scope_digest(value)
    assert registry_database.scalar('SELECT gateway.source_fixture_same_scope(%s,%s)',(Jsonb(value.model_dump(mode='json')),Jsonb(value.model_dump(mode='json')))) is True


@pytest.mark.parametrize('column',['fixture_manifest_canonical','query_canonical','decision_canonical'])
def test_attempt_requires_exact_canonical_text_before_authority(registry_database,valid_manifest,column):
    db=registry_database
    _,manifest,catalog,_=fixture_setup(db,valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn: invocation=accept(conn,catalog)
    db.execute_owner(f"CREATE FUNCTION gateway.omit_fixture_witness() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.{column}:=NULL; RETURN NEW; END $$")
    db.execute_owner('CREATE TRIGGER omit_fixture_witness BEFORE INSERT ON gateway.fixture_attempts FOR EACH ROW EXECUTE FUNCTION gateway.omit_fixture_witness()')
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
        with pytest.raises(psycopg.Error): insert_attempt(conn,manifest,catalog,invocation['invocation_id'])
    assert db.scalar('SELECT count(*) FROM gateway.fixture_attempt_source_authorities')==0


@pytest.fixture
def reviewed_fixture_database(registry_database, valid_manifest, request):
    """Explicit reviewed BQ fixture assets and disposable narrow provider roles."""
    import secrets
    from types import SimpleNamespace
    from psycopg import sql
    from sqlalchemy import create_engine
    from gateway.source_lifecycle import provision_sources
    db=registry_database
    options=getattr(request,'param',{})
    if 'large_rows' in options:
        valid_manifest['fixtures'][0]['expected_rows']=[dict(day='2024-03-09',segment=str(i),revenue=f'{i}.00') for i in range(options['large_rows'])]
        valid_manifest['fixtures'][0]['tolerance']='0.01'
    if 'execution_seconds' in options:valid_manifest['limits']={'execution_timeout_seconds':options['execution_seconds'],'sync_wait_seconds':0}
    document,manifest,catalog,approval=fixture_setup(db,valid_manifest,expires_seconds=options.get('expires_seconds'),without_result=options.get('without_result',False))
    created=[]
    with psycopg.connect(**connection_kwargs(db.admin_url),autocommit=True) as admin:
        for kind in ('fixture_runtime','fixture_observer'):
            name='gws_'+secrets.token_hex(8);password=secrets.token_urlsafe(32)
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
            admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(db.urls['owner'].database),sql.Identifier(name)))
            db.roles[kind]=name;db.urls[kind]=db.urls['app'].set(username=name,password=password);created.append(name)
    engine=create_engine(db.urls['owner'])
    with engine.begin() as connection:
        provision_sources(connection,[catalog.fixture_source],[dict(login_name=db.roles[role],source_id=catalog.fixture_source.source_id,
            revision=1,context='fixture_test',role_kind=kind) for role,kind in [('fixture_runtime','runtime'),('fixture_observer','observer')]],
            deployment_epoch='fixture-owner-tests')
    engine.dispose()
    db.fixture=SimpleNamespace(document=document,manifest=manifest,catalog=catalog,approval=approval)
    try:yield db
    finally:
        with psycopg.connect(**connection_kwargs(db.admin_url.set(database=db.urls['owner'].database)),autocommit=True) as admin:
            for name in created:admin.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(name)))
        with psycopg.connect(**connection_kwargs(db.admin_url),autocommit=True) as admin:
            for name in created:admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))
        for kind in ('fixture_runtime','fixture_observer'):db.roles.pop(kind);db.urls.pop(kind)


def fixture_owner_request(conn,kind,owner_id,action):
    value=conn.execute('SELECT gateway.source_read_fixture_owner(%s,%s,%s)',(kind,owner_id,action)).fetchone()[0]
    projection=value['projection']
    return dict(kind='owner',owner_kind=kind,owner_id=owner_id,manifest_canonical=None,principal=value['accepted']['principal'],
        decision=value['decision'],action=action,binding_digest=value['binding_digest'],approval_id=projection['approval_id'],
        approval_revision=projection['approval_revision'],approval_digest=projection['approval_digest'],projection_digest=projection['projection_digest'],
        audience_entry_id=value['audience_entry_id'])


def append_fixture_allow(conn,request,correlation_id,*,version_id='fixture-draft'):
    audit_id=str(uuid4());actor=request['principal']
    conn.execute('''INSERT INTO gateway.audit_events(id,actor_id,service_client_id,action,product_id,version_id,policy_id,policy_revision,
        decision,correlation_id,source_approval_id,source_approval_revision,source_approval_digest,source_approval_state_revision,source_access_reason_code)
        VALUES(%s,%s,%s,%s,'revenue',%s,'revenue-access',%s,'allow',%s,%s,%s,%s,1,'APPROVED')''',
        (audit_id,actor['id'],actor['service_client_id'],request['action'],version_id,request['decision']['policy_revision'],correlation_id,
         request['approval_id'],request['approval_revision'],request['approval_digest']))
    return audit_id


def prelock_fixture(conn):
    conn.execute("SELECT 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE")
    conn.execute("SELECT 1 FROM gateway.products WHERE id='revenue' FOR UPDATE")
    conn.execute("SELECT 1 FROM gateway.draft_revisions WHERE id='fixture-draft' FOR UPDATE")
    conn.execute("SELECT 1 FROM gateway.policies WHERE id='revenue-access' FOR UPDATE")


def seed_fixture_session(db):
    """Real app admission/group/session; synthetic plan, with no provider network."""
    from types import SimpleNamespace
    import secrets
    from gateway.adapters.base import authorization_scope_digest
    from gateway.contracts.identity import Decision
    data=db.fixture;source=data.catalog.fixture_source
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn: invocation=accept(conn,data.catalog)
    invocation_id=invocation['invocation_id'];group_id=str(uuid4())
    actor_spec=dict(principal_id='north-user',actor_id='north-user',policy_id=data.manifest.policy_id,version_id=None,draft_id='fixture-draft',catalog_digest=data.catalog.digest)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        prelock_fixture(conn)
        request=fixture_owner_request(conn,'fixture_invocation',invocation_id,'author')
        snapshot=conn.execute('SELECT gateway.source_owner_snapshot(%s,%s,%s)',('fixture_invocation',invocation_id,Jsonb(actor_spec))).fetchone()[0]
        audit=append_fixture_allow(conn,request,invocation_id)
        conn.execute('''INSERT INTO gateway.source_operation_groups(id,owner_kind,owner_id,actor_id,source_id,source_revision,context,capacity_key,
            deployment_epoch,deadline_at,roster,authority_spec,authority_snapshot,allow_audit_id,max_submissions,max_total_bytes)
            VALUES(%s,'fixture_test',%s,'north-user',%s,1,'fixture_test',%s,'fixture-owner-tests',%s,%s,%s,%s,%s,2,2000)''',
            (group_id,invocation_id,source.source_id,source.capacity_pool_id,invocation['effective_deadline_at'],
             Jsonb(snapshot['source_access']['positive_roster']),Jsonb(actor_spec),Jsonb(snapshot),audit))
        conn.execute('SELECT gateway.source_finalize_access(%s)',(Jsonb([request]),))
    # Owner reads prepare synthetic inputs. The following app INSERT revalidates
    # the complete fixed identity/Decision against protected current records.
    fixed=db.scalar("SELECT gateway.source_fixture_principal('fixed-positive')")
    decision=db.scalar("SELECT gateway.source_current_decision(%s,%s,'execute')",(Jsonb(fixed),Jsonb(data.manifest.model_dump(mode='json'))))
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        prelock_fixture(conn)
        insert_attempt(conn,data.manifest,data.catalog,invocation_id,fixed=fixed,decision=decision)
        request=fixture_owner_request(conn,'fixture_attempt','source-attempt','execute')
        original=fixture_owner_request(conn,'fixture_invocation',invocation_id,'author')
        attempt=conn.execute("SELECT runner_token,request_id,canonical_hash,deadline_at FROM gateway.fixture_attempts WHERE id='source-attempt'").fetchone()
        spec=actor_spec|dict(principal_id=fixed['id'])
        snapshot,stamp=conn.execute('''WITH snapshot AS MATERIALIZED(SELECT gateway.source_owner_snapshot('fixture_attempt','source-attempt',%s) value)
            SELECT value,encode(sha256(convert_to(value::text,'UTF8')),'hex') FROM snapshot''',(Jsonb(spec),)).fetchone()
        audit=append_fixture_allow(conn,request,attempt[1])
        session_id=str(uuid4())
        plan=dict(provider='bigquery',source_id=source.source_id,source_revision=1,context='fixture_test',capacity_key=source.capacity_pool_id,
            definition_digest=source.digest,request_digest=attempt[2],prepared_digest='9'*64,scope_digest=authorization_scope_digest(Decision.model_validate(decision)),
            decision_digest=canonical_digest(decision),binding_digest=data.catalog.fixture_binding.digest,model_bundle_digest=data.catalog.fixture_binding.model_bundle_digest,
            catalog_digest=data.catalog.digest,max_submissions=2,max_bytes_per_submission=1000,max_total_bytes=2000)
        digest=canonical_digest(dict(group_id=group_id,session_id=session_id,plan=plan,authority_stamp=stamp))
        deadline=invocation['effective_deadline_at']
        conn.execute('''INSERT INTO gateway.source_sessions(id,group_id,case_id,owner_id,source_id,source_revision,context,capacity_key,plan,provider_nonce,
            authority_spec,authority_snapshot,authority_stamp,context_digest,allow_audit_id,generation,lease_token,deadline_at)
            VALUES(%s,%s,%s,'source-attempt',%s,1,'fixture_test',%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s)''',
            (session_id,group_id,data.catalog.cases[0].fixture.id,source.source_id,source.capacity_pool_id,Jsonb(plan),secrets.token_urlsafe(32),Jsonb(spec),Jsonb(snapshot),stamp,digest,audit,attempt[0],deadline))
        conn.execute('SELECT gateway.source_finalize_access(%s)',(Jsonb([original,request]),))
    context=plan|dict(group_id=group_id,session_id=session_id,generation=1,authority_stamp=stamp,context_digest=digest,deadline_at=deadline)
    return SimpleNamespace(invocation=invocation,group_id=group_id,session_id=session_id,context=context,decision=decision)


def complete_fixture_evidence(db,seeded,*,settle=True,mutate_checks=None,terminal_state="succeeded",error_code=None):
    """Actual admitted synthetic intent + exact runtime terminal proof, no HTTP."""
    from tests.integration.test_source_lifecycle import call,intent
    from gateway.publishing.test_runner import FixtureCheck,FixtureEvidence
    data=db.fixture
    job=call(db,'fixture_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',seeded.context,'e'*64,intent(timeout=100))['job']
    if not settle:return job
    call(db,'fixture_runtime','SELECT gateway.source_bq_observe(%s,%s)',job['job_id'],dict(intent_digest=job['intent_digest'],state='done',terminal_error=False))
    call(db,'fixture_runtime','SELECT gateway.source_seal_attempt(%s,%s)',seeded.context,'completed')
    call(db,'app','SELECT gateway.source_close_group(%s,%s)',seeded.group_id,'completed')
    checks=[]
    for case in data.catalog.cases:
        known=case.kind=='known_answer'
        checks.append(FixtureCheck(fixture_id=case.fixture.id,kind=case.kind,passed=True,expected=case.fixture.expected_rows,
            actual=case.fixture.expected_rows if known else (),expected_error=case.fixture.expected_error,
            actual_error=None if known else 'NOT_FOUND_OR_FORBIDDEN',absolute_tolerance=case.fixture.tolerance,
            relative_tolerance=case.relative_tolerance,attempt_id='source-attempt' if known else None,
            decision_digest=canonical_digest(seeded.decision) if known else None,reference_watermark=case.fixture.source_watermark,
            freshness_status='unknown' if known else None))
    if mutate_checks is not None:checks=mutate_checks(checks)
    evidence=FixtureEvidence(catalog_digest=data.catalog.digest,execution_contract_digest=data.catalog.execution_contract_digest,
        fixture_binding_digest=data.catalog.fixture_binding.digest,fixture_model_bundle_digest=data.catalog.fixture_binding.model_bundle_digest,checks=checks)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        prelock_fixture(conn)
        conn.execute("SELECT 1 FROM gateway.fixture_attempt_states WHERE attempt_id='source-attempt' FOR UPDATE")
        conn.execute("UPDATE gateway.fixture_attempt_states SET state=%s,source_completed=%s,error_code=%s,check_digest=%s WHERE attempt_id='source-attempt'",(terminal_state,terminal_state=="succeeded",error_code,canonical_digest(checks[0])))
        conn.execute('''INSERT INTO gateway.test_runs(id,product_id,draft_id,draft_digest,binding_id,binding_digest,policy_id,policy_revision,fixture_version,
            evidence,evidence_canonical,source_decision_canonicals,status,actor_id) VALUES(%s,'revenue','fixture-draft',%s,'consumer-binding-row',%s,
            'revenue-access',%s,%s,%s,%s,'{}',%s,'north-user')''',(seeded.invocation['invocation_id'],data.manifest.content_digest,data.catalog.consumer_binding.digest,
            seeded.decision['policy_revision'],data.catalog.digest,Jsonb(evidence.model_dump(mode='json')),canonical_bytes(evidence).decode(),'passed' if all(c.passed for c in checks) else 'failed'))
        original=fixture_owner_request(conn,'fixture_invocation',seeded.invocation['invocation_id'],'author')
        result=fixture_owner_request(conn,'fixture_attempt','source-attempt','result')
        append_fixture_allow(conn,original,str(uuid4()));append_fixture_allow(conn,result,str(uuid4()))
        conn.execute('SELECT gateway.source_finalize_access(%s)',(Jsonb([original,result]),))
    return evidence


def test_real_fixture_group_session_and_terminal_evidence(reviewed_fixture_database):
    db=reviewed_fixture_database;seeded=seed_fixture_session(db)
    assert db.scalar('SELECT count(*) FROM gateway.source_sessions')==1
    complete_fixture_evidence(db,seeded)
    assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs WHERE terminal_at IS NOT NULL')==1
    assert db.scalar("SELECT state='succeeded' AND source_completed FROM gateway.fixture_attempt_states") is True
    assert db.scalar("SELECT status FROM gateway.test_runs")=='passed'
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0


def test_fixture_final_batch_cannot_omit_original_invoking_author(reviewed_fixture_database):
    db=reviewed_fixture_database;seeded=seed_fixture_session(db)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        request=fixture_owner_request(conn,'fixture_attempt','source-attempt','execute')
        with pytest.raises(psycopg.Error):conn.execute('SELECT gateway.source_finalize_access(%s)',(Jsonb([request]),))
    assert db.scalar('SELECT count(*) FROM gateway.source_sessions')==1


def test_fixture_revocation_keeps_unknown_capacity_until_exact_terminal_metadata(reviewed_fixture_database):
    from tests.integration.test_source_lifecycle import call
    db=reviewed_fixture_database;seeded=seed_fixture_session(db)
    job=complete_fixture_evidence(db,seeded,settle=False)
    approval=db.fixture.approval
    db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(approval['id'],approval['content_digest'],'fixture-test-revoke'))
    with pytest.raises(psycopg.Error):call(db,'fixture_runtime','SELECT gateway.source_bq_check_data(%s,%s)',seeded.context,job['job_id'])
    call(db,'fixture_runtime','SELECT gateway.source_seal_attempt(%s,%s)',seeded.context,'authorization')
    assert call(db,'app','SELECT gateway.source_close_group(%s,%s)',seeded.group_id,'authorization')=={'closed':True,'settled':False}
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==1
    call(db,'fixture_observer','SELECT gateway.source_bq_observe(%s,%s)',job['job_id'],dict(intent_digest=job['intent_digest'],state='done',terminal_error=True))
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0


def record_fixture_publication(conn,db,seeded,*,action='publish',version_id='reviewed-version',expected_revision=0,previous=None,request_id=None,audit_id=None):
    data=db.fixture;actor=principal().model_dump(mode='json');request_id=request_id or str(uuid4())
    projection=conn.execute('SELECT gateway.source_approval_read(%s)',(data.approval['id'],)).fetchone()[0]
    # The app only supplies equality pins; recorder independently recomputes
    # publisher/original author and every fixed result owner in one batch.
    request=dict(principal=actor,action='publish',decision={'policy_revision':1},approval_id=data.approval['id'],
        approval_revision=data.approval['revision'],approval_digest=data.approval['content_digest'])
    audit_id=audit_id or append_fixture_allow(conn,request,request_id,version_id='fixture-draft' if action=='publish' else version_id)
    operation=conn.execute('SELECT gateway.source_record_publication_operation(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (Jsonb(actor),action,'revenue',version_id,'fixture-draft' if action=='publish' else None,
         seeded.invocation['invocation_id'] if action=='publish' else None,previous,expected_revision,1 if action=='publish' else None,audit_id,request_id)).fetchone()[0]
    return str(operation),request_id,audit_id


def consume_fixture_publication(conn,db,seeded,operation,request_id,*,version_id='reviewed-version',omit=None):
    data=db.fixture;manifest=data.manifest
    if omit!='version':
        conn.execute('''INSERT INTO gateway.product_versions(id,product_id,version,draft_id,manifest,manifest_canonical,content_digest,binding_id,binding_digest,
            policy_id,policy_revision,test_run_id,actor_id,name,domain,classification,source_operation_id)
            VALUES(%s,'revenue',%s,'fixture-draft',%s,%s,%s,'consumer-binding-row',%s,'revenue-access',1,%s,'north-user',%s,%s,%s,%s)''',
            (version_id,manifest.version,Jsonb(manifest.model_dump(mode='json')),canonical_bytes(manifest.model_dump(mode='python',exclude={'content_digest'})).decode(),
             manifest.content_digest,data.catalog.consumer_binding.digest,seeded.invocation['invocation_id'],manifest.name,manifest.domain,manifest.classification,operation))
    if omit!='state':conn.execute("INSERT INTO gateway.version_states(version_id,status,actor_id,reason,source_operation_id) VALUES(%s,'published','north-user','Explicit synthetic publication',%s)",(version_id,operation))
    if omit!='head':conn.execute("UPDATE gateway.products SET current_version_id=%s,source_operation_id=%s WHERE id='revenue'",(version_id,operation))
    if omit!='event':conn.execute("INSERT INTO gateway.publication_events(id,product_id,version_id,action,actor_id,reason,request_id,source_operation_id) VALUES(%s,'revenue',%s,'publish','north-user','Explicit synthetic publication',%s,%s)",(str(uuid4()),version_id,request_id,operation))


def seed_fixture_publication(db):
    seeded=seed_fixture_session(db);complete_fixture_evidence(db,seeded)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        operation,request_id,_=record_fixture_publication(conn,db,seeded)
        consume_fixture_publication(conn,db,seeded,operation,request_id)
    seeded.operation_id=operation;seeded.version_id='reviewed-version'
    return seeded


def test_reviewed_publication_links_full_real_source_evidence_and_counter(reviewed_fixture_database):
    db=reviewed_fixture_database;seeded=seed_fixture_publication(db)
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==1
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations')==1
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_revision_steps')==1
    assert db.scalar('SELECT source_operation_id::text FROM gateway.product_versions')==seeded.operation_id
    assert db.scalar('SELECT source_operation_id::text FROM gateway.publication_events')==seeded.operation_id


@pytest.mark.parametrize('omit',['state','head','event'])
def test_reviewed_publication_missing_link_rolls_back_receipt_and_counter(reviewed_fixture_database,omit):
    db=reviewed_fixture_database;seeded=seed_fixture_session(db);complete_fixture_evidence(db,seeded)
    with pytest.raises(psycopg.Error):
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
            operation,request_id,_=record_fixture_publication(conn,db,seeded)
            consume_fixture_publication(conn,db,seeded,operation,request_id,omit=omit)
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==0
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations')==0
    assert db.scalar('SELECT count(*) FROM gateway.product_versions')==0


def transition_fixture_publication(conn,db,seeded,action,revision,*,duplicate_event=False):
    operation,request_id,audit=record_fixture_publication(conn,db,seeded,action=action,expected_revision=revision,previous=seeded.version_id)
    status={'deprecate':'deprecated','withdraw':'withdrawn','rollback':'deprecated'}[action]
    conn.execute("UPDATE gateway.version_states SET status=%s,actor_id='north-user',reason='Explicit synthetic transition',source_operation_id=%s WHERE version_id=%s",(status,operation,seeded.version_id))
    if action=='rollback':conn.execute("UPDATE gateway.products SET current_version_id=%s,source_operation_id=%s WHERE id='revenue'",(seeded.version_id,operation))
    for _ in range(2 if duplicate_event else 1):
        conn.execute("INSERT INTO gateway.publication_events(id,product_id,version_id,action,actor_id,reason,request_id,source_operation_id) VALUES(%s,'revenue',%s,%s,'north-user','Explicit synthetic transition',%s,%s)",
            (str(uuid4()),seeded.version_id,action,request_id,operation))
    return operation,request_id,audit


def test_reviewed_deprecate_rollback_withdraw_each_advance_exactly_once(reviewed_fixture_database):
    db=reviewed_fixture_database;seeded=seed_fixture_publication(db)
    for revision,action in enumerate(('deprecate','rollback','withdraw'),start=1):
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:operation,_,_=transition_fixture_publication(conn,db,seeded,action,revision)
        assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==revision+1
        assert db.scalar("SELECT source_operation_id::text FROM gateway.products WHERE id='revenue'")==operation
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations')==4
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_revision_steps')==4


def test_reviewed_duplicate_event_and_stale_revision_rollback_atomically(reviewed_fixture_database):
    db=reviewed_fixture_database;seeded=seed_fixture_publication(db)
    with pytest.raises(psycopg.Error):
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:transition_fixture_publication(conn,db,seeded,'deprecate',1,duplicate_event=True)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error):record_fixture_publication(conn,db,seeded,action='deprecate',expected_revision=0,previous=seeded.version_id)
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==1
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations')==1
    assert db.scalar("SELECT status FROM gateway.version_states")=='published'


def test_publication_cannot_replay_prior_receipt_or_prior_audit(reviewed_fixture_database):
    db=reviewed_fixture_database;seeded=seed_fixture_publication(db)
    old_audit=db.scalar('SELECT allow_audit_id FROM gateway.source_publication_operations')
    old_request=db.scalar('SELECT request_id::text FROM gateway.source_publication_operations')
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error):record_fixture_publication(conn,db,seeded,action='deprecate',expected_revision=1,previous=seeded.version_id,request_id=old_request,audit_id=old_audit)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error):conn.execute("UPDATE gateway.version_states SET status='deprecated',source_operation_id=%s WHERE version_id=%s",(seeded.operation_id,seeded.version_id))
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==1


def test_publication_unused_receipt_cannot_increment_counter(reviewed_fixture_database):
    db=reviewed_fixture_database;seeded=seed_fixture_session(db);complete_fixture_evidence(db,seeded)
    reached=[]
    with pytest.raises(psycopg.Error):
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
            record_fixture_publication(conn,db,seeded);reached.append('recorded')
    assert reached==['recorded']
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations')==0
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_revision_steps')==0
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==0


@pytest.mark.parametrize('reviewed_fixture_database',[{'expires_seconds':5}],indirect=True)
def test_publication_delayed_commit_rechecks_original_approval_expiry(reviewed_fixture_database):
    db=reviewed_fixture_database;seeded=seed_fixture_session(db);complete_fixture_evidence(db,seeded)
    reached=[]
    with pytest.raises(psycopg.Error):
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
            operation,request_id,_=record_fixture_publication(conn,db,seeded)
            consume_fixture_publication(conn,db,seeded,operation,request_id)
            reached.append('complete-uncommitted')
            conn.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.03))',(db.fixture.document['expires_at'],))
    assert reached==['complete-uncommitted']
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations')==0
    assert db.scalar('SELECT count(*) FROM gateway.product_versions')==0
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==0


@pytest.mark.parametrize('reviewed_fixture_database',[{'execution_seconds':2}],indirect=True)
def test_completed_fixture_evidence_and_publication_survive_execution_budget(reviewed_fixture_database):
    db=reviewed_fixture_database;seeded=seed_fixture_session(db);complete_fixture_evidence(db,seeded)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        conn.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.03))',(seeded.invocation['effective_deadline_at'],))
        original=fixture_owner_request(conn,'fixture_invocation',seeded.invocation['invocation_id'],'author')
        result=fixture_owner_request(conn,'fixture_attempt','source-attempt','result')
        conn.execute('SELECT gateway.source_finalize_access(%s)',(Jsonb([original,result]),))
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        operation,request_id,_=record_fixture_publication(conn,db,seeded)
        consume_fixture_publication(conn,db,seeded,operation,request_id)
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==1


@pytest.mark.parametrize('reviewed_fixture_database',[{'without_result':True}],indirect=True)
def test_execute_audience_does_not_authorize_fixture_result_or_publication(reviewed_fixture_database):
    db=reviewed_fixture_database;seeded=seed_fixture_session(db)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error):fixture_owner_request(conn,'fixture_attempt','source-attempt','result')
    with pytest.raises(psycopg.Error):complete_fixture_evidence(db,seeded)
    assert db.scalar('SELECT count(*) FROM gateway.test_runs')==0
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations')==0
    assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs WHERE terminal_at IS NOT NULL')==1


@pytest.mark.parametrize('forgery',['rows','error','freshness','watermark','duplicate','extra_field','missing_field','wrong_type'])
def test_app_cannot_commit_forged_passing_answer_or_freshness(reviewed_fixture_database,forgery):
    db=reviewed_fixture_database;seeded=seed_fixture_session(db)
    def mutate(checks):
        value=checks[0].model_dump(mode='python')
        if forgery=='rows':value['actual'][0]['revenue']='99.99'
        elif forgery=='error':value['actual_error']='STALE_DATA'
        elif forgery=='freshness':value['freshness_status']='fresh'
        elif forgery=='watermark':value['observed_source_load_watermark']='2024-03-12T00:00:00Z'
        elif forgery=='extra_field':value['actual'][0]['extra']='secret'
        elif forgery=='missing_field':del value['actual'][0]['segment']
        elif forgery=='wrong_type':value['actual'][0]['revenue']=True
        else:value['actual']=[value['actual'][0],value['actual'][0],value['actual'][2]]
        return [type(checks[0]).model_validate(value),*checks[1:]]
    with pytest.raises(psycopg.Error):complete_fixture_evidence(db,seeded,mutate_checks=mutate)
    assert db.scalar('SELECT count(*) FROM gateway.test_runs')==0
    assert db.scalar("SELECT state FROM gateway.fixture_attempt_states")=='running'


@pytest.mark.parametrize('wanted,actual,absolute,relative,expected',[
    (['0.00','1.50'],['1.00','0.00'],'1','0',True),
    (['0.00','0.00','2.00'],['0.00','2.00','2.00'],'0','0',False),
    (['0.00','0.00'],['0.00','0.00'],'0','0',True),
    (['100.00'],['111.00'],'0','0.1',False),
    (['111.00'],['100.00'],'0','0.1',True),
    (['9007199254740993.01'],['9007199254740993.02'],'0.01','0',True),
    (['9007199254740993.01'],['9007199254740993.03'],'0.01','0',False),
    ([None],[None],'0','0',True),
    ([None],['0.00'],'1','0',False),
])
def test_sql_decimal_matching_matches_python_whole_row_semantics(registry_database,wanted,actual,absolute,relative,expected):
    from types import SimpleNamespace
    from gateway.contracts.manifest import FieldSchema
    from gateway.publishing.test_runner import compare_rows
    fields={'value':FieldSchema(type='decimal',precision=38,scale=2,nullable=True)}
    left=[{'value':value} for value in wanted];right=[{'value':value} for value in actual]
    python=compare_rows(SimpleNamespace(outputs=fields),left,right,absolute=absolute,relative=relative).passed
    assert python is expected
    assert registry_database.scalar('SELECT gateway.source_fixture_rows_match(%s,%s,%s,%s,%s)',
        (Jsonb({k:v.model_dump(mode='json') for k,v in fields.items()}),Jsonb(left),Jsonb(right),absolute,relative)) is expected


def test_sql_row_matching_is_not_independent_column_matching(registry_database):
    fields={key:{'type':'decimal','precision':38,'scale':2,'nullable':False,'enum':None} for key in ('x','y')}
    wanted=[{'x':'0','y':'2'},{'x':'2','y':'0'}];actual=[{'x':'0','y':'0'},{'x':'2','y':'2'}]
    assert registry_database.scalar('SELECT gateway.source_fixture_rows_match(%s,%s,%s,%s,%s)',(Jsonb(fields),Jsonb(wanted),Jsonb(actual),'0','0')) is False


@pytest.mark.parametrize('value',[1e-7,-0.0,1.25])
def test_nondecimal_scalar_matching_is_exact_and_ignores_tolerance(registry_database,value):
    fields={'value':{'type':'number','nullable':False,'enum':None}}
    assert registry_database.scalar('SELECT gateway.source_fixture_rows_match(%s,%s,%s,%s,%s)',
        (Jsonb(fields),Jsonb([{'value':value}]),Jsonb([{'value':value}]),'100','100')) is True
    assert registry_database.scalar('SELECT gateway.source_fixture_rows_match(%s,%s,%s,%s,%s)',
        (Jsonb(fields),Jsonb([{'value':value}]),Jsonb([{'value':value+1}]),'100','100')) is False


@pytest.mark.parametrize('field,value', [('service_client_id','other-client'), ('action','author'), ('version_id','wrong-target'), ('source_approval_digest','f'*64)])
def test_review_publication_rejects_mismatched_same_transaction_audit(reviewed_fixture_database,field,value):
    from psycopg import sql
    db=reviewed_fixture_database
    seeded=seed_fixture_session(db);complete_fixture_evidence(db,seeded)
    request_id=str(uuid4())
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        request=dict(principal=principal().model_dump(mode='json'),action='publish',decision={'policy_revision':1},
            approval_id=db.fixture.approval['id'],approval_revision=db.fixture.approval['revision'],approval_digest=db.fixture.approval['content_digest'])
        valid=append_fixture_allow(conn,request,request_id)
        columns=['id','actor_id','service_client_id','action','product_id','version_id','policy_id','policy_revision','decision','correlation_id',
            'source_approval_id','source_approval_revision','source_approval_digest','source_approval_state_revision','source_access_reason_code']
        invalid=str(uuid4())
        expressions=[sql.Placeholder() if name in ('id',field) else sql.Identifier(name) for name in columns]
        values=[invalid if name=='id' else value for name in columns if name in ('id',field)]
        conn.execute(sql.SQL('INSERT INTO gateway.audit_events({}) SELECT {} FROM gateway.audit_events WHERE id=%s').format(
            sql.SQL(',').join(map(sql.Identifier,columns)),sql.SQL(',').join(expressions)),(*values,valid))
        with pytest.raises(psycopg.Error):
            record_fixture_publication(conn,db,seeded,request_id=request_id,audit_id=invalid)
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations')==0
    assert db.scalar("SELECT publication_revision FROM gateway.products WHERE id='revenue'")==0




@pytest.mark.parametrize('required,watermark,status,mode,expected',[
    (False,None,'unknown','unknown',True),(True,None,'unknown','unknown',False),
    (False,'2024-03-10T00:00:00Z','stale','same-query-checkpoint',True),
    (True,'2024-03-10T00:00:00Z','stale','same-query-checkpoint',False),
    (True,'2024-03-12T00:00:00Z','fresh','same-query-checkpoint',True),
    (False,'2024-03-12T00:00:00Z','fresh','unknown',False),
    (False,'2024-03-13T00:00:00Z','unknown','same-query-checkpoint',True),
])
def test_isolated_freshness_predicate_uses_protected_completion_instant(registry_database,valid_manifest,required,watermark,status,mode,expected):
    from gateway.contracts.query import QueryRequest,validate_query
    from gateway.publishing.test_runner import FixtureCheck
    db=registry_database
    _,manifest,catalog,_=fixture_setup(db,valid_manifest)
    case=catalog.cases[0]
    definition=manifest.model_dump(mode='json');definition['freshness']['required']=required
    binding=catalog.fixture_binding.model_dump(mode='json');binding['freshness_mode']=mode
    binding['checkpoint_member']='Watermark.loaded_at' if mode!='unknown' else None
    decision=db.scalar("SELECT gateway.source_current_decision(gateway.source_fixture_principal('fixed-positive'),%s,'execute')",(Jsonb(manifest.model_dump(mode='json')),))
    query=validate_query(manifest,QueryRequest(product_id=manifest.product_id,version=manifest.version,inputs=case.fixture.inputs)).model_dump(mode='json',exclude={'idempotency_key'})
    check=FixtureCheck(fixture_id=case.fixture.id,kind=case.kind,passed=True,expected=case.fixture.expected_rows,actual=case.fixture.expected_rows,
        absolute_tolerance=case.fixture.tolerance,relative_tolerance=case.relative_tolerance,observed_source_load_watermark=watermark,freshness_status=status)
    assert db.scalar('SELECT gateway.source_fixture_known_answer_valid(%s,%s,%s,%s,%s,%s::timestamptz)',
        (Jsonb(definition),Jsonb(binding),Jsonb(query),Jsonb(decision),Jsonb(check.model_dump(mode='json')),'2024-03-12T00:00:00Z')) is expected


@pytest.mark.parametrize('passed',[True,False])
def test_app_passing_fixture_cannot_include_originally_hidden_measure(reviewed_fixture_database,passed):
    db=reviewed_fixture_database
    content=db.scalar("SELECT content FROM gateway.policy_revisions WHERE id='policy-1'")
    content['rules']['visible_fields']=['day','segment']
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
        conn.execute("INSERT INTO gateway.policy_revisions(id,policy_id,revision,content,content_digest,actor_id) VALUES('policy-2','revenue-access',2,%s,%s,'operator')",(Jsonb(content),canonical_digest(content)))
        conn.execute("UPDATE gateway.policies SET current_revision=2 WHERE id='revenue-access'")
    seeded=seed_fixture_session(db)
    assert seeded.decision['visible_fields']==['day','segment']
    def mutate(checks):
        value=checks[0].model_dump(mode='python');value['passed']=passed
        return [type(checks[0]).model_validate(value),*checks[1:]]
    with pytest.raises(psycopg.Error):complete_fixture_evidence(db,seeded,mutate_checks=mutate)
    assert db.scalar('SELECT count(*) FROM gateway.test_runs')==0


def test_dense_nonexact_tolerance_at_canonical_evidence_bound(registry_database,capsys):
    import time
    # Isolated comparator cost at both accepted scalar-byte and 1,000-row bounds.
    # Serving grain checks remain an independent prerequisite for publication.
    fields={'d':{'type':'date','nullable':False,'enum':None},'v':{'type':'decimal','precision':38,'scale':2,'nullable':False,'enum':None}}
    wanted=[{'d':'2024-03-09','v':str(i)} for i in range(1000)]
    actual=[{'d':'2024-03-09','v':str(i)+'.01'} for i in range(1000)]
    assert len(canonical_bytes(dict(expected=wanted,actual=actual)))<=65536
    started=time.monotonic()
    result=registry_database.scalar('SELECT gateway.source_fixture_rows_match(%s,%s,%s,%s,%s)',(Jsonb(fields),Jsonb(wanted),Jsonb(actual),'1000','0'))
    elapsed=time.monotonic()-started
    with capsys.disabled():print(f'\nDense 1000-row non-exact comparator: {elapsed:.3f}s; {len(canonical_bytes(dict(expected=wanted,actual=actual)))} canonical bytes')
    assert result is True


@pytest.mark.parametrize('reviewed_fixture_database',[{'large_rows':578}],indirect=True)
def test_supported_evidence_bound_reordered_tolerance_through_publication(reviewed_fixture_database,capsys):
    import time
    from decimal import Decimal
    db=reviewed_fixture_database;seeded=seed_fixture_session(db)
    def mutate(checks):
        value=checks[0].model_dump(mode='python')
        value['actual']=[row | {'revenue':str(Decimal(row['revenue'])+Decimal('0.01'))} for row in reversed(value['actual'])]
        return [type(checks[0]).model_validate(value),*checks[1:]]
    started=time.monotonic()
    evidence=complete_fixture_evidence(db,seeded,mutate_checks=mutate)
    evidence_elapsed=time.monotonic()-started
    started=time.monotonic()
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        operation,request_id,_=record_fixture_publication(conn,db,seeded)
        consume_fixture_publication(conn,db,seeded,operation,request_id)
    publication_elapsed=time.monotonic()-started
    size=len(canonical_bytes(evidence))
    with capsys.disabled():print(f'\nSupported 578-row reversed non-exact evidence: {evidence_elapsed:.3f}s; publication: {publication_elapsed:.3f}s; {size} canonical bytes')
    assert 65000<size<=65536
    over_bound=evidence.model_dump(mode='python')
    over_bound['checks'][0]['expected']=(*over_bound['checks'][0]['expected'],over_bound['checks'][0]['expected'][-1])
    over_bound['checks'][0]['actual']=(*over_bound['checks'][0]['actual'],over_bound['checks'][0]['actual'][-1])
    assert len(canonical_bytes(over_bound))>65536
    assert db.scalar("SELECT status FROM gateway.test_runs")=='passed'
    assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs WHERE terminal_at IS NOT NULL')==1
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations')==1


@pytest.mark.parametrize('state,error',[('failed','BINDING_DRIFT'),('timed_out','EXECUTION_TIMEOUT')])
def test_failed_attempt_check_digest_is_retained_without_passing_authority(reviewed_fixture_database,state,error):
    db=reviewed_fixture_database;seeded=seed_fixture_session(db)
    def mutate(checks):
        value=checks[0].model_dump(mode='python')
        value.update(passed=False,actual=[],actual_error=error,freshness_status=None)
        return [type(checks[0]).model_validate(value),*checks[1:]]
    evidence=complete_fixture_evidence(db,seeded,mutate_checks=mutate,terminal_state=state,error_code=error)
    assert db.scalar('SELECT check_digest FROM gateway.fixture_attempt_states')==canonical_digest(evidence.checks[0])
    assert db.scalar('SELECT status FROM gateway.test_runs')=='failed'
    assert db.scalar('SELECT source_completed FROM gateway.fixture_attempt_states') is False
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error):record_fixture_publication(conn,db,seeded)
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        with pytest.raises(psycopg.Error):conn.execute("UPDATE gateway.fixture_attempt_states SET state='succeeded',error_code=NULL,source_completed=true WHERE attempt_id='source-attempt'")
    assert db.scalar('SELECT count(*) FROM gateway.source_publication_operations')==0


@pytest.mark.parametrize('defect',['wrong_error','passing_failed','malformed_digest','missing_success_digest','running_digest'])
def test_failed_digest_upgrade_preserves_state_and_evidence_obligations(reviewed_fixture_database,defect):
    db=reviewed_fixture_database;seeded=seed_fixture_session(db)
    if defect in ('wrong_error','passing_failed'):
        def mutate(checks):
            value=checks[0].model_dump(mode='python')
            value.update(passed=defect=='passing_failed',actual=[],actual_error='BINDING_DRIFT',freshness_status=None)
            return [type(checks[0]).model_validate(value),*checks[1:]]
        with pytest.raises(psycopg.Error):
            complete_fixture_evidence(db,seeded,mutate_checks=mutate,terminal_state='failed',error_code='UPSTREAM_UNAVAILABLE' if defect=='wrong_error' else 'BINDING_DRIFT')
    else:
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
            with pytest.raises(psycopg.Error):
                if defect=='malformed_digest':conn.execute("UPDATE gateway.fixture_attempt_states SET state='failed',error_code='BINDING_DRIFT',check_digest='invalid' WHERE attempt_id='source-attempt'")
                elif defect=='missing_success_digest':conn.execute("UPDATE gateway.fixture_attempt_states SET state='succeeded',source_completed=true WHERE attempt_id='source-attempt'")
                else:conn.execute("UPDATE gateway.fixture_attempt_states SET check_digest=%s WHERE attempt_id='source-attempt'",('a'*64,))
    assert db.scalar('SELECT count(*) FROM gateway.test_runs')==0
    assert db.scalar('SELECT state FROM gateway.fixture_attempt_states')=='running'


@pytest.mark.parametrize('value',['1e999','"NaN"','"Infinity"','true','null'])
def test_numeric_predicate_rejects_nonfinite_and_wrong_scalar_shape(registry_database,value):
    fields={'value':{'type':'number','nullable':False,'enum':None}}
    assert registry_database.scalar('SELECT gateway.source_fixture_rows_match(%s,%s::jsonb,%s::jsonb,%s,%s)',
        (Jsonb(fields),'[{"value":'+value+'}]','[{"value":'+value+'}]','0','0')) is False


@pytest.mark.parametrize('forgery',[None,'extra_field','missing_field','wrong_type','freshness','grain'])
def test_failed_comparison_requires_normalized_visible_rows(reviewed_fixture_database,forgery):
    db=reviewed_fixture_database;seeded=seed_fixture_session(db)
    def mutate(checks):
        value=checks[0].model_dump(mode='python');value['passed']=False
        value['actual'][0]['revenue']='99.99'
        if forgery=='extra_field':value['actual'][0]['extra']='secret'
        elif forgery=='missing_field':del value['actual'][0]['segment']
        elif forgery=='wrong_type':value['actual'][0]['revenue']=True
        elif forgery=='freshness':value['freshness_status']='fresh'
        elif forgery=='grain':value['actual']=[value['actual'][0],value['actual'][0],value['actual'][2]]
        return [type(checks[0]).model_validate(value),*checks[1:]]
    if forgery is None:
        complete_fixture_evidence(db,seeded,mutate_checks=mutate)
        assert db.scalar('SELECT status FROM gateway.test_runs')=='failed'
        assert db.scalar("SELECT state FROM gateway.fixture_attempt_states")=='succeeded'
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
            with pytest.raises(psycopg.Error):record_fixture_publication(conn,db,seeded)
    else:
        with pytest.raises(psycopg.Error):complete_fixture_evidence(db,seeded,mutate_checks=mutate)
        assert db.scalar('SELECT count(*) FROM gateway.test_runs')==0


def test_narrow_visible_projection_remains_safe_failed_comparison_evidence(reviewed_fixture_database):
    db=reviewed_fixture_database
    content=db.scalar("SELECT content FROM gateway.policy_revisions WHERE id='policy-1'")
    content['rules']['visible_fields']=['day','segment']
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
        conn.execute("INSERT INTO gateway.policy_revisions(id,policy_id,revision,content,content_digest,actor_id) VALUES('policy-2','revenue-access',2,%s,%s,'operator')",(Jsonb(content),canonical_digest(content)))
        conn.execute("UPDATE gateway.policies SET current_revision=2 WHERE id='revenue-access'")
    seeded=seed_fixture_session(db)
    assert seeded.decision['visible_fields']==['day','segment']
    def mutate(checks):
        value=checks[0].model_dump(mode='python');value['passed']=False
        value['actual']=[{name:row[name] for name in seeded.decision['visible_fields']} for row in value['actual']]
        return [type(checks[0]).model_validate(value),*checks[1:]]
    evidence=complete_fixture_evidence(db,seeded,mutate_checks=mutate)
    assert db.scalar('SELECT status FROM gateway.test_runs')=='failed'
    assert all('revenue' not in row for row in evidence.checks[0].actual)
    assert db.scalar('SELECT count(*) FROM gateway.fixture_test_source_authorities')==1


def test_review_sql_comparator_matches_python_on_bounded_ambiguous_multisets(registry_database, valid_manifest):
    """Independent deterministic parity over directional decimal/null/group edges."""
    import random
    from copy import deepcopy
    from psycopg.types.json import Jsonb
    from gateway.contracts.manifest import Manifest
    from gateway.publishing.test_runner import compare_rows
    randomizer=random.Random(45192)
    raw=deepcopy(valid_manifest)
    manifest=Manifest.model_validate(raw)
    decimal=next(value for value in manifest.outputs.values() if value.type=='decimal').model_copy(update={'nullable':True})
    string=next(value for value in manifest.outputs.values() if value.type=='string').model_copy(update={'nullable':True,'enum':None})
    manifest=manifest.model_copy(update={'outputs':{'value':decimal,'group':string}})
    fields={key:value.model_dump(mode='json') for key,value in manifest.outputs.items()}
    for index in range(160):
        count=randomizer.randrange(0,8)
        def row():
            value=randomizer.choice([None,-200,-101,-100,-1,0,1,99,100,101,200])
            return {'value':None if value is None else f'{value/100:.2f}', 'group':randomizer.choice([None,'a','b'])}
        wanted=[row() for _ in range(count)]
        actual=deepcopy(wanted) if index%3==0 else [row() for _ in range(count)]
        if index%3==0:
            randomizer.shuffle(actual)
            if actual and index%2==0 and actual[0]['value'] is not None:
                actual[0]['value']='1.01'
        absolute=randomizer.choice(['0','0.01','1','2'])
        relative=randomizer.choice(['0','0.01','0.5','1'])
        expected=compare_rows(manifest,wanted,actual,absolute=absolute,relative=relative).passed
        observed=registry_database.scalar('SELECT gateway.source_fixture_rows_match(%s,%s,%s,%s,%s)',
            (Jsonb(fields),Jsonb(wanted),Jsonb(actual),absolute,relative))
        assert observed is expected, (index,wanted,actual,absolute,relative)
