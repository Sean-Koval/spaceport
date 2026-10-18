"""Real disposable PostgreSQL proof of protected source approval state."""
import pytest

pytestmark = pytest.mark.postgres

TABLES = ('source_access_approvals', 'source_access_approval_states', 'source_access_admin_events')


def test_approval_migration_is_additive_and_installs_nothing(registry_database):
    db = registry_database
    assert db.migration_head == '0007_source_access_approvals'
    for table in TABLES:
        assert db.scalar(f'SELECT count(*) FROM gateway.{table}') == 0
    assert db.first_migration_snapshot == db.second_migration_snapshot


@pytest.mark.parametrize('registry_database', [{}, {'unsafe_default_grants': True}], indirect=True)
def test_approval_acl_scrubs_default_grants_and_owner_only_admin(registry_database):
    db = registry_database
    for role in ('app', 'audit_retention', 'source_reader'):
        for table in TABLES:
            assert not db.scalar('SELECT has_table_privilege(%s,%s,%s)', (db.roles[role], 'gateway.'+table, 'SELECT,INSERT,UPDATE,DELETE'))
        for function in ('source_approval_install(text)', 'source_approval_revoke(uuid,text,text)',
                         'source_approval_assert(uuid,integer,text)'):
            assert not db.scalar('SELECT has_function_privilege(%s,%s,%s)', (db.roles[role], 'gateway.'+function, 'EXECUTE'))


def test_approval_time_sensitive_assertions_are_volatile(registry_database):
    db = registry_database
    for function in ('source_approval_install', 'source_approval_revoke', 'source_approval_assert'):
        assert db.scalar("SELECT provolatile FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='gateway' AND p.proname=%s", (function,)) == 'v'


def test_direct_null_malformed_or_expired_approval_denies_safely(registry_database):
    db = registry_database
    for value in (None, '{}', 'null', '{"mode":"shared_non_pii","mode":"shared_non_pii"}', 'x'*65537):
        assert db.denied_as_owner('SELECT gateway.source_approval_install(%s)', (value,)) == '23514'
    assert db.denied_as_owner('SELECT gateway.source_approval_revoke(NULL,NULL,NULL)') == '23514'
    assert db.denied_as_owner('SELECT gateway.source_approval_assert(NULL,NULL,NULL)') == 'permission_denied'


def approval_assets(db, valid_manifest):
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


def install(db, doc):
    from gateway.contracts.canonical import canonical_bytes
    return db.scalar('SELECT gateway.source_approval_install(%s)',(canonical_bytes(doc).decode(),))


def test_canonical_install_digest_replay_immutable_pins_and_terminal_revoke(registry_database,valid_manifest):
    from gateway.contracts.canonical import canonical_bytes
    from gateway.source_access_contracts import validate_source_approval
    db=registry_database;doc,manifest,bindings,sources=approval_assets(db,valid_manifest)
    parsed=validate_source_approval(canonical_bytes(doc))
    receipt=install(db,doc)
    assert receipt['content_digest']==parsed.content_digest
    assert receipt['state']=='active' and receipt['replayed'] is False
    assert db.scalar('SELECT package_pins_digest FROM gateway.source_access_approvals WHERE id=%s',(receipt['id'],))==parsed.package_pins_digest
    replay=install(db,doc)
    assert replay | {'replayed':False} == receipt
    assert db.scalar('SELECT count(*) FROM gateway.source_access_admin_events')==1
    assert db.denied_as_owner("UPDATE gateway.source_access_approvals SET expires_at=expires_at+interval '1 day'")=='permission_denied'
    assert db.denied_as_owner('DELETE FROM gateway.source_access_approvals')=='permission_denied'
    assert db.denied_as_owner('DELETE FROM gateway.source_access_admin_events')=='permission_denied'
    revoked=db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(receipt['id'],receipt['content_digest'],'synthetic-review-ended'))
    assert revoked['state']=='revoked' and revoked['state_revision']==2
    assert db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(receipt['id'],receipt['content_digest'],'synthetic-review-ended'))['replayed'] is True
    assert install(db,doc)['id']==receipt['id'] and install(db,doc)['state']=='revoked'
    assert db.scalar('SELECT count(*) FROM gateway.source_access_admin_events')==2
    assert db.denied_as_owner("UPDATE gateway.source_access_approval_states SET state='active',state_revision=1,revocation_reason_code=NULL")=='23514'
    assert db.denied_as_owner('SELECT gateway.source_approval_assert(%s,%s,%s)',(receipt['id'],1,receipt['content_digest']))=='permission_denied'


def test_expired_exact_import_returns_original_but_new_expired_document_denies(registry_database,valid_manifest):
    from datetime import timedelta, timezone
    from gateway.contracts.canonical import canonical_bytes
    import psycopg
    from tests.integration.conftest import connection_kwargs
    db=registry_database;doc,*_=approval_assets(db,valid_manifest)
    expires=db.scalar("SELECT clock_timestamp()+interval '0.35 second'")
    doc['expires_at']=expires.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    receipt=install(db,doc)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as owner:
        # Database time, not Python time or a mutated immutable expiry.
        owner.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))',(expires,))
        assert owner.execute('SELECT clock_timestamp()>%s',(expires,)).fetchone()[0]
    replay=install(db,doc)
    assert replay['id']==receipt['id'] and replay['replayed'] is True
    doc['review']['review_id']='distinct-expired-document'
    assert db.denied_as_owner('SELECT gateway.source_approval_install(%s)',(canonical_bytes(doc).decode(),))=='23514'
    assert db.denied_as_owner('SELECT gateway.source_approval_assert(%s,%s,%s)',(receipt['id'],1,receipt['content_digest']))=='permission_denied'


def test_required_administrative_receipt_failure_rolls_back_install_and_revoke(registry_database,valid_manifest):
    import psycopg
    from tests.integration.conftest import connection_kwargs
    from gateway.contracts.canonical import canonical_bytes
    db=registry_database;doc,*_=approval_assets(db,valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as owner:
        owner.execute("CREATE FUNCTION gateway.synthetic_reject_receipt() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic receipt failure'; END $$")
        owner.execute('CREATE TRIGGER synthetic_receipt_failure BEFORE INSERT ON gateway.source_access_admin_events FOR EACH ROW EXECUTE FUNCTION gateway.synthetic_reject_receipt()')
    assert db.denied_as_owner('SELECT gateway.source_approval_install(%s)',(canonical_bytes(doc).decode(),))=='P0001'
    assert db.scalar('SELECT count(*) FROM gateway.source_access_approvals')==0
    db.execute_owner('DROP TRIGGER synthetic_receipt_failure ON gateway.source_access_admin_events')
    receipt=install(db,doc)
    db.execute_owner('CREATE TRIGGER synthetic_receipt_failure BEFORE INSERT ON gateway.source_access_admin_events FOR EACH ROW EXECUTE FUNCTION gateway.synthetic_reject_receipt()')
    assert db.denied_as_owner('SELECT gateway.source_approval_revoke(%s,%s,%s)',(receipt['id'],receipt['content_digest'],'synthetic-failure'))=='P0001'
    assert db.scalar('SELECT state FROM gateway.source_access_approval_states')=='active'


@pytest.mark.parametrize('purpose', ['hidden_scope','grouping','source_input','exposed'])
def test_exact_complete_lineage_inventory_cannot_be_omitted(registry_database,valid_manifest,purpose):
    from gateway.contracts.canonical import canonical_bytes
    db=registry_database;doc,*_=approval_assets(db,valid_manifest)
    doc['lineage']=[entry for entry in doc['lineage'] if entry['purpose']!=purpose]
    assert db.denied_as_owner('SELECT gateway.source_approval_install(%s)',(canonical_bytes(doc).decode(),))=='23514'
    assert db.scalar('SELECT count(*) FROM gateway.source_access_approvals')==0


def test_recomputed_source_or_binding_pins_cannot_override_protected_registry(registry_database,valid_manifest):
    import copy
    from gateway.contracts.canonical import canonical_bytes
    db=registry_database;doc,*_=approval_assets(db,valid_manifest)
    for section,kind,field in [('sources','consumer','descriptor_digest'),('sources','fixture','physical_identity_digest'),
                               ('bindings','consumer','content_digest'),('bindings','fixture','content_digest')]:
        changed=copy.deepcopy(doc);changed[section][kind][field]='0'*64
        assert db.denied_as_owner('SELECT gateway.source_approval_install(%s)',(canonical_bytes(changed).decode(),))=='23514'


def test_expiry_after_real_approval_lock_wait_denies_without_json_mutation(registry_database,valid_manifest):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timezone
    from queue import Queue
    import time
    import psycopg
    from tests.integration.conftest import connection_kwargs
    db=registry_database;doc,*_=approval_assets(db,valid_manifest)
    expires=db.scalar("SELECT clock_timestamp()+interval '1.5 second'")
    doc['expires_at']=expires.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    receipt=install(db,doc);pids=Queue()
    def check():
        try:
            with psycopg.connect(**connection_kwargs(db.urls['owner'])) as connection:
                connection.execute("SET LOCAL statement_timeout='6s'")
                pids.put(connection.info.backend_pid)
                connection.execute("SELECT 1 FROM gateway.capacity_locks WHERE scope_id='deployment' FOR UPDATE")
                connection.execute('SELECT gateway.source_approval_assert(%s,%s,%s)',(receipt['id'],1,receipt['content_digest']))
            return 'allowed'
        except psycopg.Error as error:
            return error.sqlstate
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as blocker, ThreadPoolExecutor(max_workers=1) as workers:
        blocker.execute('SELECT 1 FROM gateway.source_access_approval_states WHERE approval_id=%s FOR UPDATE',(receipt['id'],))
        future=workers.submit(check);pid=pids.get(timeout=2);deadline=time.monotonic()+4
        while True:
            blocked=blocker.execute('SELECT %s=ANY(pg_blocking_pids(%s))',(blocker.info.backend_pid,pid)).fetchone()[0]
            if blocked: break
            assert time.monotonic()<deadline,'The final assertion never reached its real approval lock wait'
            blocker.execute('SELECT pg_sleep(0.01)')
        blocker.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))',(expires,))
        assert blocker.execute('SELECT clock_timestamp()>%s',(expires,)).fetchone()[0]
        blocker.commit()
        assert future.result(timeout=3)=='42501'
    assert db.scalar('SELECT content_digest FROM gateway.source_access_approvals')==receipt['content_digest']
    assert db.scalar('SELECT state FROM gateway.source_access_approval_states')=='active'


def test_app_projection_omits_all_operator_evidence_and_matches_python_digest(registry_database,valid_manifest):
    import json
    from gateway.contracts.canonical import canonical_bytes
    from gateway.source_access_contracts import validate_source_approval,RuntimeApprovalProjection
    db=registry_database;doc,manifest,*_=approval_assets(db,valid_manifest);receipt=install(db,doc)
    projected=db._execute('app','SELECT gateway.source_approval_candidates(%s,%s,%s)',
        (doc['product']['product_id'],doc['product']['version'],doc['product']['manifest_digest']),scalar=True)
    expected=validate_source_approval(canonical_bytes(doc)).runtime_projection(receipt['id'])
    assert RuntimeApprovalProjection.model_validate(projected)==expected
    serialized=json.dumps(projected)
    for secret in [*[key for key in doc['review'] if key!='purpose'],doc['review']['purpose'],doc['review']['review_id'],doc['review']['operator_id'],doc['review']['evidence_ref'],doc['review']['evidence_digest']]:
        assert secret not in serialized
    assert db.denied_as_app('SELECT * FROM gateway.source_access_approvals')=='permission_denied'


def test_sql_document_schema_rejects_malformed_closed_shapes_and_duplicate_names(registry_database,valid_manifest):
    import copy
    from gateway.contracts.canonical import canonical_bytes
    db=registry_database;doc,*_=approval_assets(db,valid_manifest)
    mutations=[lambda d:d.update(schema_version=True),lambda d:d.update(revision=1.5),lambda d:d.update(expires_at=None),
        lambda d:d['review'].update(samples=['private-canary']),lambda d:d['review'].update(lineage_reviewed=False),
        lambda d:d['lineage'][0].update(classification='unknown'),lambda d:d['audiences'][0].update(client_ids=[None]),
        lambda d:d['audiences'][0]['row_constraints'].append(d['audiences'][0]['row_constraints'][0]),
        lambda d:d['audiences'][0]['row_constraints'][1].update(values=[True,1]),
        lambda d:d['input_domains'][2].update(kind='range',values=[],minimum='A',maximum='Z')]
    for mutate in mutations:
        changed=copy.deepcopy(doc);mutate(changed)
        assert db.denied_as_owner('SELECT gateway.source_approval_install(%s)',(canonical_bytes(changed).decode(),))=='23514'
    canonical=canonical_bytes(doc).decode()
    for duplicate in [canonical.replace('"schema_version":1','"schema_version":1,"schema_version":1'),
                      canonical.replace('"classification":"non-personal"','"classification":"non-personal","classification":"non-personal"',1)]:
        assert db.denied_as_owner('SELECT gateway.source_approval_install(%s)',(duplicate,))=='23514'


def test_audit_marker_is_server_owned_and_source_references_are_coherent(registry_database):
    import psycopg
    from tests.integration.conftest import connection_kwargs
    db=registry_database
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as app:
        app.execute("INSERT INTO gateway.audit_events(id,actor_id,action,decision,correlation_id) VALUES('synthetic-audit','synthetic-user','result','allow','synthetic-request')")
        assert app.execute("SELECT created_transaction_id=pg_current_xact_id() FROM gateway.audit_events WHERE id='synthetic-audit'").fetchone()[0]
    assert db.denied_as_app("INSERT INTO gateway.audit_events(id,actor_id,action,decision,correlation_id,created_transaction_id) VALUES('forged-audit','synthetic-user','result','allow','synthetic-request',pg_current_xact_id())")=='permission_denied'
    assert db.denied_as_app("INSERT INTO gateway.audit_events(id,actor_id,action,decision,correlation_id,source_access_reason_code) VALUES('partial-audit','synthetic-user','result','allow','synthetic-request','APPROVED')")=='23514'
    assert db.denied_as_app("INSERT INTO gateway.audit_events(id,actor_id,action,decision,correlation_id,source_approval_revision) VALUES('partial-audit','synthetic-user','result','deny','synthetic-request',1)")=='23514'


@pytest.mark.parametrize('missing', ['source_approval_id','source_approval_revision','source_approval_digest','source_approval_state_revision'])
def test_partial_approval_audit_reference_always_denies(registry_database,missing):
    """A missing state revision must not turn the CHECK into SQL UNKNOWN."""
    from uuid import uuid4
    values={'source_approval_id':str(uuid4()),'source_approval_revision':1,
        'source_approval_digest':'a'*64,'source_approval_state_revision':1}
    values[missing]=None
    names=tuple(values)
    statement=('INSERT INTO gateway.audit_events(id,actor_id,action,decision,correlation_id,'
        +','.join(names)+",source_access_reason_code) VALUES('partial-ref','synthetic-user','result','deny','synthetic-request',"
        +','.join(['%s']*len(names))+",'AUDIENCE_DENIED')")
    assert registry_database.denied_as_app(statement,tuple(values.values()))=='23514'


def test_credential_pin_is_mandatory_and_cannot_override_protected_descriptor(registry_database,valid_manifest):
    import copy
    from gateway.contracts.canonical import canonical_bytes
    db=registry_database;doc,*_=approval_assets(db,valid_manifest)
    missing=copy.deepcopy(doc)
    for name in ('consumer','fixture'):missing['sources'][name].pop('credential_binding_digest')
    assert db.denied_as_owner('SELECT gateway.source_approval_install(%s)',(canonical_bytes(missing).decode(),))=='23514'
    changed=copy.deepcopy(doc);changed['sources']['consumer']['credential_binding_digest']='f'*64
    assert db.denied_as_owner('SELECT gateway.source_approval_install(%s)',(canonical_bytes(changed).decode(),))=='23514'
    assert install(db,doc)['state']=='active'


def test_sql_credential_binding_digest_matches_python_and_rejects_aliases(registry_database):
    import copy
    from psycopg.types.json import Jsonb
    from gateway.source_access_contracts import source_credential_binding_digest,physical_source_identity_digest
    from gateway.contracts.canonical import canonical_digest
    from gateway.adapters.bigquery_runtime import BigQuerySource
    from tests.unit.test_bigquery_serving_source import source_value
    from tests.unit.test_source_credential_binding import versioned_source
    db=registry_database
    for source in (BigQuerySource.model_validate(source_value()),versioned_source()):
        descriptor=source.model_dump(mode='json')
        assert db.scalar('SELECT gateway.source_approval_credential_digest(%s)',(Jsonb(descriptor),))==source_credential_binding_digest(source)
        assert canonical_digest(db.scalar('SELECT gateway.source_approval_physical(%s)',(Jsonb(descriptor),)))==physical_source_identity_digest(source)
    descriptor=versioned_source().model_dump(mode='json')
    for value in (None,'projects/123456/secrets/domo_secret/versions/latest','projects/123456/secrets/domo_secret/versions/01'):
        changed=copy.deepcopy(descriptor);changed['auth']['client_secret_version']=value
        assert db.denied_as_owner('SELECT gateway.source_approval_credential_digest(%s)',(Jsonb(changed),))=='23514'
    for changed in (None,{}, {'provider':'bigquery','auth':None}):
        assert db.denied_as_owner('SELECT gateway.source_approval_credential_digest(%s)',(Jsonb(changed),))=='23514'


@pytest.mark.parametrize('field',['review_id','operator_id','evidence_ref','purpose'])
def test_sql_rejects_known_secret_in_every_approval_string(registry_database,valid_manifest,field):
    from gateway.contracts.canonical import canonical_bytes
    from gateway.source_access_contracts import validate_source_approval
    db=registry_database;doc,*_=approval_assets(db,valid_manifest)
    doc['review'][field]='sk-review-canary'
    wire=canonical_bytes(doc)
    with pytest.raises(ValueError):validate_source_approval(wire)
    assert db.denied_as_owner('SELECT gateway.source_approval_install(%s)',(wire.decode(),))=='23514'


def test_registry_readiness_requires_every_exact_active_package_without_provider_io(registry_database,valid_manifest):
    import asyncio
    from gateway.contracts.canonical import canonical_bytes
    from gateway.contracts.errors import GatewayError
    from gateway.source_access import validate_registry_packages
    from gateway.source_access_contracts import validate_source_approval
    db=registry_database;doc,manifest,*_=approval_assets(db,valid_manifest);receipt=install(db,doc)
    approval=validate_source_approval(canonical_bytes(doc))
    pins=approval.package_pins(manifest,approval_digest=approval.content_digest)
    async def scenario():
        await validate_registry_packages(db.registry,(pins,))
        with pytest.raises(GatewayError):await validate_registry_packages(db.registry,(pins,pins.model_copy(update={'approval_digest':'f'*64})))
        with pytest.raises(GatewayError):await validate_registry_packages(db.registry,(pins.model_copy(update={'approval_digest':None}),))
        db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(receipt['id'],receipt['content_digest'],'synthetic-readiness-revoke'))
        with pytest.raises(GatewayError):await validate_registry_packages(db.registry,(pins,))
    asyncio.run(scenario())


def package_expectation(doc,receipt):
    from gateway.source_access_contracts import validate_source_approval
    from gateway.contracts.canonical import canonical_bytes
    return {'approval_digest':receipt['content_digest'],'package_pins_digest':
        validate_source_approval(canonical_bytes(doc)).package_pins_digest,
        'product_id':doc['product']['product_id'],'version':doc['product']['version'],'manifest_digest':doc['product']['manifest_digest']}


def test_readiness_batch_rechecks_database_clock_after_all_state_locks(registry_database,valid_manifest):
    import copy,time
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    import psycopg
    from psycopg.types.json import Jsonb
    from tests.integration.conftest import connection_kwargs
    db=registry_database;doc,*_=approval_assets(db,valid_manifest)
    expiry=db.scalar("SELECT clock_timestamp()+interval '1.5 second'")
    from datetime import timezone
    doc['expires_at']=expiry.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    first=install(db,doc);other=copy.deepcopy(doc);other['review']['review_id']='synthetic-second';second=install(db,other)
    expected=[package_expectation(doc,first),package_expectation(other,second)]
    pid=Queue()
    def check():
        try:
            with psycopg.connect(**connection_kwargs(db.urls['app'])) as app:
                app.execute("SET LOCAL lock_timeout='5s'")
                pid.put(app.execute('SELECT pg_backend_pid()').fetchone()[0])
                app.execute('SELECT gateway.source_approval_check_packages(%s)',(Jsonb(expected),))
            return 'allowed'
        except psycopg.Error as error:return error.sqlstate
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as blocker, ThreadPoolExecutor(1) as pool:
        blocker.execute('SELECT 1 FROM gateway.source_access_approval_states WHERE approval_id=%s FOR UPDATE',(max(first['id'],second['id']),))
        future=pool.submit(check);target=pid.get(timeout=2);deadline=time.monotonic()+3
        while not db.scalar('SELECT cardinality(pg_blocking_pids(%s))>0',(target,)):
            assert time.monotonic()<deadline,'batch never reached the real state-lock barrier'
            db.scalar('SELECT pg_sleep(0.005)')
        blocker.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))',(expiry,))
        blocker.commit()
        assert future.result(timeout=3)=='42501'
    assert db.scalar('SELECT count(*) FROM gateway.source_access_approval_states WHERE state=%s',('active',))==2


def test_readiness_batch_and_global_scope_fingerprint_are_safe_app_only(registry_database,valid_manifest):
    from psycopg.types.json import Jsonb
    db=registry_database;doc,*_=approval_assets(db,valid_manifest)
    empty=db._execute('app','SELECT gateway.source_approval_scope_fingerprint()',scalar=True)
    receipt=install(db,doc);expected=package_expectation(doc,receipt)
    assert db._execute('app','SELECT gateway.source_approval_check_packages(%s)',(Jsonb([expected]),),scalar=True) is True
    active=db._execute('app','SELECT gateway.source_approval_scope_fingerprint()',scalar=True)
    assert len(active)==64 and active!=empty
    for value in (None,{},[],[None],[expected|{'unexpected':True}]):
        assert db.denied_as_app('SELECT gateway.source_approval_check_packages(%s)',(Jsonb(value),))=='permission_denied'
    assert db.denied_as_source('SELECT gateway.source_approval_scope_fingerprint()')=='permission_denied'
    db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(receipt['id'],receipt['content_digest'],'synthetic-fingerprint-revoke'))
    assert db._execute('app','SELECT gateway.source_approval_scope_fingerprint()',scalar=True)==empty


def test_readiness_serializes_with_revoke_then_later_checks_deny(registry_database,valid_manifest):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    from threading import Event
    import psycopg
    from psycopg.types.json import Jsonb
    from tests.integration.conftest import connection_kwargs
    db=registry_database;doc,*_=approval_assets(db,valid_manifest);receipt=install(db,doc)
    expected=[package_expectation(doc,receipt)];checked=Event();commit=Event();pid=Queue()
    def ready():
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as app:
            assert app.execute('SELECT gateway.source_approval_check_packages(%s)',(Jsonb(expected),)).fetchone()[0]
            checked.set()
            assert commit.wait(4)
        return True
    def revoke():
        with psycopg.connect(**connection_kwargs(db.urls['owner'])) as owner:
            owner.execute("SET LOCAL lock_timeout='5s'")
            pid.put(owner.execute('SELECT pg_backend_pid()').fetchone()[0])
            return owner.execute('SELECT gateway.source_approval_revoke(%s,%s,%s)',
                (receipt['id'],receipt['content_digest'],'synthetic-batch-revoke')).fetchone()[0]
    with ThreadPoolExecutor(2) as pool:
        readiness=pool.submit(ready)
        try:
            assert checked.wait(3)
            revocation=pool.submit(revoke);target=pid.get(timeout=2);deadline=time.monotonic()+3
            while not db.scalar('SELECT cardinality(pg_blocking_pids(%s))>0',(target,)):
                assert time.monotonic()<deadline
                db.scalar('SELECT pg_sleep(0.005)')
        finally:commit.set()
        assert readiness.result(timeout=3) is True
        assert revocation.result(timeout=3)['state']=='revoked'
    assert db.denied_as_app('SELECT gateway.source_approval_check_packages(%s)',(Jsonb(expected),))=='permission_denied'


def test_preliminary_admission_resolves_only_unique_exact_protected_tuple(registry_database,valid_manifest):
    import asyncio,copy
    from uuid import uuid4
    from gateway.source_access import SourceAccessService
    from gateway.contracts.execution import PinnedProduct
    from gateway.contracts.errors import GatewayError
    from tests.unit.test_source_approval_contracts import principal,decision
    db=registry_database;doc,manifest,bindings,*_=approval_assets(db,valid_manifest)
    doc['audiences'][0]['actions'].append('describe')
    receipt=install(db,doc)
    product=PinnedProduct(manifest=manifest,binding_digest=bindings[0].digest,version_id='inert-version')
    from gateway.policy.audit import AuditWriter
    service=SourceAccessService(db.registry,AuditWriter(b'synthetic-test-audit-key-32-bytes!!'))
    async def scenario():
        async with db.registry.transaction() as uow:
            request=await service.resolve_admission(uow,product,principal(),decision(),request_id=str(uuid4()))
            assert request.request_kind=='admission' and request.owner is None and request.accepted is None
            assert request.projection.approval_id==receipt['id']
            assert service.precheck(request)=='north-consumer'
            transient=await service.resolve_transient(uow,product,principal(),decision(),action='describe',request_id=str(uuid4()))
            assert transient.request_kind=='transient' and transient.action=='describe'
        assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow'")==0
        for candidate,actor in [(product,principal().model_copy(update={'service_client_id':'other-client'})),
            (product.model_copy(update={'binding_digest':'f'*64}),principal())]:
            async with db.registry.transaction() as uow:
                with pytest.raises(GatewayError):await service.resolve_admission(uow,candidate,actor,decision(),request_id=str(uuid4()))
        duplicate=copy.deepcopy(doc);duplicate['review']['review_id']='synthetic-overlapping-review';install(db,duplicate)
        async with db.registry.transaction() as uow:
            with pytest.raises(GatewayError):await service.resolve_admission(uow,product,principal(),decision(),request_id=str(uuid4()))
    asyncio.run(scenario())
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE source_access_reason_code IN ('AUDIENCE_DENIED','PACKAGE_MISMATCH','AMBIGUOUS')")==3
    assert db.scalar("SELECT bool_and(product_id IS NULL AND version_id IS NULL AND parameter_names='[]'::jsonb) FROM gateway.audit_events") is True


def test_transient_match_is_non_aborting_non_authoritative_and_strict(registry_database,valid_manifest):
    import asyncio
    from uuid import uuid4
    from sqlalchemy import text
    from gateway.source_access import SourceAccessService
    from gateway.contracts.execution import PinnedProduct
    from gateway.contracts.errors import GatewayError
    from tests.unit.test_source_approval_contracts import principal,decision
    db=registry_database;doc,manifest,bindings,*_=approval_assets(db,valid_manifest)
    doc['audiences'][0]['actions'].append('describe');install(db,doc)
    product=PinnedProduct(manifest=manifest,binding_digest=bindings[0].digest,version_id='unsaved-version')
    service=SourceAccessService(db.registry,None)
    async def scenario():
        async with db.registry.transaction() as uow:
            transaction=uow.session.get_transaction()
            assert await service.match_transient(uow,product,principal(),decision(),action='author',request_id=str(uuid4())) is None
            assert await service.match_transient(uow,product,principal(),decision(),action='describe',request_id=str(uuid4())) is not None
            assert uow.session.get_transaction() is transaction
            with pytest.raises(GatewayError):
                await service.match_transient(uow,product.model_copy(update={'binding_digest':'f'*64}),principal(),decision(),action='describe',request_id=str(uuid4()))
            assert uow.session.get_transaction() is transaction
            assert await uow.session.scalar(text('SELECT 1'))==1
    asyncio.run(scenario())
    assert db.scalar('SELECT count(*) FROM gateway.audit_events')==0
