"""Approved retained consumer source sessions and narrow provider boundaries."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.integration.test_source_approval_transactions import consumer_setup,insert_consumer
from tests.integration.test_source_lifecycle import call,intent

pytestmark=pytest.mark.postgres


@pytest.fixture
def approved_source_database(registry_database,valid_manifest,request):
    import secrets,psycopg
    from psycopg import sql
    from sqlalchemy import create_engine
    from gateway.adapters.bigquery_runtime import BigQuerySource,BigQueryBinding
    from gateway.source_lifecycle import provision_sources
    from tests.integration.conftest import connection_kwargs
    db=registry_database;valid_manifest['freshness']['required']=False
    expires=None
    if getattr(request,'param',{}).get('expires_seconds'):
        from datetime import timezone
        expires=db.scalar("SELECT clock_timestamp()+make_interval(secs=>%s)",(request.param['expires_seconds'],)).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    domo=getattr(request,'param',{}).get('provider')=='domo'
    document,manifest,approval,product=consumer_setup(db,valid_manifest,expires=expires,assets_factory=domo_approval_assets if domo else None,deployment_epoch='approval-lifecycle-test')
    if domo:
        from gateway.adapters.domo_runtime import DomoSource,DomoRuntimeBinding
        source_type,binding_type=DomoSource,DomoRuntimeBinding
    else:source_type,binding_type=BigQuerySource,BigQueryBinding
    source=source_type.model_validate(db.scalar("SELECT descriptor FROM gateway.source_profiles WHERE source_id='consumer-source'"))
    binding=binding_type.model_validate(db.scalar("SELECT content FROM gateway.binding_revisions WHERE id='consumer-binding-row'"))
    roles=[('domo_runtime','runtime')] if domo else [('bq_runtime','runtime'),('bq_observer','observer')]
    created=[]
    with psycopg.connect(**connection_kwargs(db.admin_url),autocommit=True) as admin:
        for kind,_ in roles:
            name='gws_'+secrets.token_hex(8);password=secrets.token_urlsafe(32)
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
            admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(db.urls['owner'].database),sql.Identifier(name)))
            db.roles[kind]=name;db.urls[kind]=db.urls['app'].set(username=name,password=password);created.append(name)
    engine=create_engine(db.urls['owner'])
    with engine.begin() as connection:
        provision_sources(connection,[source],[dict(login_name=db.roles[role],source_id=source.source_id,revision=1,context='consumer',role_kind=kind)
            for role,kind in roles],deployment_epoch='approval-lifecycle-test')
    engine.dispose()
    db.approved=SimpleNamespace(document=document,manifest=manifest,approval=approval,product=product,source=source,binding=binding)
    try:yield db
    finally:
        with psycopg.connect(**connection_kwargs(db.admin_url.set(database=db.urls['owner'].database)),autocommit=True) as admin:
            for name in created:admin.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(name)))
        with psycopg.connect(**connection_kwargs(db.admin_url),autocommit=True) as admin:
            for name in created:admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))
        for kind,_ in roles:db.roles.pop(kind);db.urls.pop(kind)


def seed_approved_source_session(db, *, delay_commit=False, existing=False, deadline_seconds=None, phase_probe=None):
    import psycopg
    from gateway.adapters.base import authorization_scope_digest
    from gateway.adapters.bigquery_runtime import compile_bigquery_query
    from gateway.contracts.canonical import canonical_digest
    from gateway.contracts.execution import ExecutionSpec,PreparedQuery
    from gateway.contracts.identity import Decision,Principal
    from gateway.contracts.query import QueryRequest
    from gateway.registry.models import Execution
    from gateway.source_access import SourceAccessService,ExecutionOwner
    from gateway.source_lifecycle import SourceLifecycle,SourceSessionSpec
    from tests.integration.conftest import connection_kwargs
    from tests.integration.test_audit import service as policy_service
    data=db.approved;token=str(uuid4())
    with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
        if existing:
            actor,decision=conn.execute("SELECT principal_snapshot,decision FROM gateway.execution_authorizations WHERE execution_id='execution-1'").fetchone()
        else:
            actor,decision=insert_consumer(conn,data.manifest,**(dict(query=db.domo.query.model_dump(mode='json',exclude={'idempotency_key'}),decision_override=db.domo.decision.model_dump(mode='json')) if data.source.provider=='domo' else {}))
        conn.execute("UPDATE gateway.execution_states SET state='running',started_at=clock_timestamp() WHERE execution_id='execution-1'")
        conn.execute("INSERT INTO gateway.execution_leases(execution_id,worker_id,lease_token,generation,expires_at,source_reserved_until) SELECT id,'approved-test',%s,1,clock_timestamp()+interval '30 seconds',deadline_at FROM gateway.executions",(token,))
    actor=Principal.model_validate(actor);decision=Decision.model_validate(decision)
    policy=policy_service(db);access=SourceAccessService(db.registry,policy.audit)
    policy.source_access=access
    lifecycle=SourceLifecycle(db.registry,policy,SimpleNamespace(resolve=lambda ref:data.source),deployment_epoch='approval-lifecycle-test')
    async def make():
        async with db.registry.transaction() as uow:
            execution=await uow.session.get(Execution,'execution-1')
            spec=ExecutionSpec(principal_id=actor.id,pinned_product=data.product,canonical_query=execution.canonical_query,
                canonical_hash=execution.canonical_hash,policy_revision=1,context='consumer',request_id=str(uuid4()))
            # Group provenance binds the immutable execution's own request ID.
            spec=spec.model_copy(update={'request_id':execution.request_id})
            await lifecycle._lock_authority(uow,data.product)
            deadline=execution.deadline_at
            if deadline_seconds is not None:
                from datetime import timedelta
                deadline=min(deadline,await uow.session.scalar(text("SELECT clock_timestamp()"))+timedelta(seconds=deadline_seconds))
            group=await lifecycle.reserve_consumer(uow,(execution,spec,actor,decision,data.binding),decision,deadline)
            if data.source.provider=='domo':
                from gateway.adapters.domo_runtime import compile_domo_query
                prepared=compile_domo_query(data.binding,data.product,QueryRequest.model_validate(spec.canonical_query),decision,
                    db.domo.catalog,source=data.source,request_id=spec.request_id)
                prepared_digest=prepared.digest
            else:
                prepared=PreparedQuery(endpoint_id='approved-test',model_bundle_digest=data.binding.model_bundle_digest,
                    cube_query=compile_bigquery_query(data.binding,data.manifest,QueryRequest.model_validate(spec.canonical_query),decision))
                prepared_digest=canonical_digest(prepared.cube_query)
            plan=SourceSessionSpec(provider=data.source.provider,source_id=data.source.source_id,source_revision=1,context='consumer',
                capacity_key=data.source.capacity_pool_id,definition_digest=data.source.digest,request_digest=spec.canonical_hash,
                prepared_digest=prepared_digest,scope_digest=authorization_scope_digest(decision),decision_digest=canonical_digest(decision),
                binding_digest=data.binding.digest,model_bundle_digest=data.binding.model_bundle_digest,catalog_digest=None,
                max_submissions=1 if data.source.provider=='domo' else 2,max_bytes_per_submission=None if data.source.provider=='domo' else 1000,
                max_total_bytes=None if data.source.provider=='domo' else 2000)
            context=await lifecycle._materialize(uow,group=group,owner_id=execution.id,case_id=execution.id,spec=spec,principal=actor,
                actor=actor,binding=data.binding,prepared=prepared,executed=decision,current=decision,plan=plan,generation=1,token=token,deadline=deadline)
            request=await access.resolve_owner(uow,ExecutionOwner(kind='execution',id=execution.id),actor,decision,action='execute',request_id=str(uuid4()))
            await access.finalize(uow,(request,))
            if phase_probe is not None:phase_probe.append('finalized')
            if delay_commit:
                await uow.session.scalar(text("SELECT pg_sleep(greatest(0,extract(epoch FROM (CAST(:expiry AS timestamptz)-clock_timestamp()))+0.02))"),{'expiry':data.document['expires_at']})
        return SimpleNamespace(lifecycle=lifecycle,context=context,group=group,execution=execution,spec=spec,principal=actor,decision=decision,token=token,prepared=prepared)
    return asyncio.run(make())


def revoke(db):
    item=db.approved.approval
    db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(item['id'],item['content_digest'],'synthetic-lifecycle-revoke'))


@pytest.mark.parametrize('operation',['attempt','lookup','admit','data'])
def test_provider_data_gates_require_original_retained_approval(approved_source_database,operation):
    import psycopg
    db=approved_source_database;seeded=seed_approved_source_session(db);context=seeded.context.wire_context()
    job=call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',context,'e'*64,intent())['job'] if operation=='data' else None
    revoke(db)
    statement,parameters={
        'attempt':('SELECT gateway.source_check_attempt(%s)',(context,)),
        'lookup':('SELECT gateway.source_bq_lookup(%s,%s)',(context,'e'*64)),
        'admit':('SELECT gateway.source_bq_admit(%s,%s,%s)',(context,'e'*64,intent())),
        'data':('SELECT gateway.source_bq_check_data(%s,%s)',(context,job['job_id'] if job else 'unused'))}[operation]
    with pytest.raises(psycopg.Error) as caught:call(db,'bq_runtime',statement,*parameters)
    assert caught.value.sqlstate=='42501'
    assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'')==int(operation=='data')


def test_revocation_preserves_unknown_hold_and_exact_metadata_settlement(approved_source_database):
    db=approved_source_database;seeded=seed_approved_source_session(db);context=seeded.context.wire_context()
    job=call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',context,'e'*64,intent())['job']
    revoke(db)
    call(db,'bq_runtime','SELECT gateway.source_seal_attempt(%s,%s)',context,'authorization')
    assert asyncio.run(seeded.lifecycle.close_group(seeded.group.group_id,'authorization'))=={'closed':True,'settled':False}
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==1
    claims=call(db,'bq_observer','SELECT gateway.source_bq_reconcile_claim(8)')
    assert len(claims)==1 and claims[0]['job']['job_id']==job['job_id']
    call(db,'bq_observer','SELECT gateway.source_bq_check_observation(%s,%s)',job['job_id'],claims[0]['observation_token'])
    call(db,'bq_observer','SELECT gateway.source_bq_observe(%s,%s)',job['job_id'],dict(intent_digest=job['intent_digest'],state='done',terminal_error=True))
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
    assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'')==1


def test_new_source_snapshot_retains_exact_approval_and_client_digest(approved_source_database):
    from gateway.contracts.canonical import canonical_digest
    db=approved_source_database;seeded=seed_approved_source_session(db)
    for table in ('source_operation_groups','source_sessions'):
        snapshot=db.scalar(f"SELECT authority_snapshot FROM gateway.{table} WHERE context='consumer'")
        pins=snapshot['source_access']
        assert pins['approval_id']==db.approved.approval['id']
        assert pins['approval_digest']==db.approved.approval['content_digest']
        assert pins['principal_digest']==canonical_digest(seeded.principal)
        assert 'audiences' not in pins and 'review' not in pins
    assert 'source_access' not in seeded.context.wire_context()


@pytest.mark.parametrize('approved_source_database',[{'expires_seconds':3}],indirect=True)
def test_new_session_and_group_cannot_commit_after_original_approval_expires(approved_source_database):
    from gateway.contracts.errors import GatewayError
    db=approved_source_database;phases=[]
    with pytest.raises(GatewayError):seed_approved_source_session(db,delay_commit=True,phase_probe=phases)
    assert phases==['finalized']
    assert db.scalar('SELECT count(*) FROM gateway.source_operation_groups WHERE context=\'consumer\'')==0
    assert db.scalar('SELECT count(*) FROM gateway.source_sessions WHERE context=\'consumer\'')==0
    assert db.scalar('SELECT count(*) FROM gateway.execution_source_authorities')==1


@pytest.mark.parametrize('approved_source_database',[{'expires_seconds':3}],indirect=True)
def test_intent_lock_wait_rechecks_original_approval_before_replay_release(approved_source_database):
    import time,psycopg
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    from psycopg.types.json import Jsonb
    from tests.integration.conftest import connection_kwargs
    db=approved_source_database;seeded=seed_approved_source_session(db);context=seeded.context.wire_context()
    job=call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',context,'e'*64,intent(1000))['job']
    pid=Queue()
    def replay():
        try:
            with psycopg.connect(**connection_kwargs(db.urls['bq_runtime'])) as conn:
                pid.put(conn.execute('SELECT pg_backend_pid()').fetchone()[0])
                conn.execute('SELECT gateway.source_bq_admit(%s,%s,%s)',(Jsonb(context),'e'*64,Jsonb(intent(1000))))
            return 'allowed'
        except psycopg.Error as error:return error.sqlstate
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as blocker,ThreadPoolExecutor(1) as pool:
        blocker.execute('SELECT 1 FROM gateway.bigquery_source_jobs WHERE job_id=%s FOR UPDATE',(job['job_id'],))
        future=pool.submit(replay);target=pid.get(timeout=2);limit=time.monotonic()+2
        while not db.scalar('SELECT cardinality(pg_blocking_pids(%s))>0',(target,)):
            assert time.monotonic()<limit,'provider never reached intent lock'
            db.scalar('SELECT pg_sleep(0.005)')
        blocker.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM ((SELECT expires_at FROM gateway.source_access_approvals WHERE id=%s)-clock_timestamp()))+0.02))',(db.approved.approval['id'],))
        blocker.commit();assert future.result(timeout=3)=='42501'
    assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'')==1
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==1


@pytest.mark.parametrize('revoked',[False,True])
def test_reclaim_keeps_original_snapshot_and_requires_active_approval(approved_source_database,revoked):
    import psycopg
    from tests.integration.conftest import connection_kwargs
    db=approved_source_database;seeded=seed_approved_source_session(db);token=str(uuid4())
    before=db.scalar('SELECT authority_snapshot FROM gateway.source_sessions WHERE context=\'consumer\'')
    if revoked:revoke(db)
    def claim():
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
            conn.execute("UPDATE gateway.execution_leases SET generation=2,lease_token=%s,expires_at=clock_timestamp()+interval '30 seconds' WHERE execution_id='execution-1'",(token,))
            return conn.execute('SELECT gateway.source_reclaim_session(%s,2,%s,%s)',(seeded.context.session_id,token,seeded.context.deadline)).fetchone()[0]
    if revoked:
        with pytest.raises(psycopg.Error) as caught:claim()
        assert caught.value.sqlstate=='42501'
        assert db.scalar('SELECT generation FROM gateway.source_sessions WHERE context=\'consumer\'')==1
    else:
        status=claim();assert status['session_id']==seeded.context.session_id
        assert db.scalar('SELECT generation FROM gateway.source_sessions WHERE context=\'consumer\'')==2
    assert db.scalar('SELECT authority_snapshot FROM gateway.source_sessions WHERE context=\'consumer\'')==before
    assert db.scalar('SELECT count(*) FROM gateway.source_sessions WHERE context=\'consumer\'')==1


@pytest.mark.parametrize('approved_source_database',[{'expires_seconds':3}],indirect=True)
def test_reclaim_cannot_commit_after_original_approval_expiry(approved_source_database):
    import psycopg
    from tests.integration.conftest import connection_kwargs
    db=approved_source_database;seeded=seed_approved_source_session(db);token=str(uuid4())
    with pytest.raises(psycopg.Error) as caught:
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
            conn.execute("UPDATE gateway.execution_leases SET generation=2,lease_token=%s,expires_at=clock_timestamp()+interval '30 seconds' WHERE execution_id='execution-1'",(token,))
            result=conn.execute('SELECT gateway.source_reclaim_session(%s,2,%s,%s)',(seeded.context.session_id,token,seeded.context.deadline)).fetchone()[0]
            assert result['session_id']==seeded.context.session_id
            conn.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))',(db.approved.document['expires_at'],))
    assert caught.value.sqlstate=='42501'
    assert db.scalar('SELECT generation FROM gateway.source_sessions WHERE context=\'consumer\'')==1
    assert db.scalar('SELECT generation FROM gateway.execution_leases')==1


@pytest.mark.parametrize('boundary',['short_session','lease','new_job_timeout'])
def test_provider_final_suffix_rechecks_tighter_clocks(approved_source_database,boundary):
    import time,psycopg
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    from psycopg.types.json import Jsonb
    from tests.integration.conftest import connection_kwargs
    db=approved_source_database;seeded=seed_approved_source_session(db,deadline_seconds=3 if boundary!='lease' else None)
    context=seeded.context.wire_context();pid=Queue()
    if boundary=='lease':db.execute_owner("UPDATE gateway.execution_leases SET expires_at=clock_timestamp()+interval '2 seconds'")
    if boundary!='new_job_timeout':
        job=call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',context,'e'*64,intent(500))['job']
    def release():
        try:
            with psycopg.connect(**connection_kwargs(db.urls['bq_runtime'])) as conn:
                pid.put(conn.execute('SELECT pg_backend_pid()').fetchone()[0])
                if boundary=='new_job_timeout':conn.execute('SELECT gateway.source_bq_admit(%s,%s,%s)',(Jsonb(context),'e'*64,Jsonb(intent(2000))))
                else:conn.execute('SELECT gateway.source_bq_lookup(%s,%s)',(Jsonb(context),'e'*64))
            return 'allowed'
        except psycopg.Error as error:return error.sqlstate
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as blocker,ThreadPoolExecutor(1) as pool:
        if boundary=='new_job_timeout':blocker.execute('SELECT 1 FROM gateway.source_access_approval_states WHERE approval_id=%s FOR UPDATE',(db.approved.approval['id'],))
        else:blocker.execute('SELECT 1 FROM gateway.bigquery_source_jobs WHERE job_id=%s FOR UPDATE',(job['job_id'],))
        future=pool.submit(release);target=pid.get(timeout=2);limit=time.monotonic()+2
        while not db.scalar('SELECT cardinality(pg_blocking_pids(%s))>0',(target,)):
            assert time.monotonic()<limit,'provider did not reach expected final lock'
            db.scalar('SELECT pg_sleep(0.005)')
        if boundary=='new_job_timeout':blocker.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))-1))',(seeded.context.deadline,))
        elif boundary=='lease':blocker.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM ((SELECT expires_at FROM gateway.execution_leases)-clock_timestamp()))+0.02))')
        else:blocker.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))',(seeded.context.deadline,))
        blocker.commit();assert future.result(timeout=3) in ('42501','23514')
    assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'')==int(boundary!='new_job_timeout')


def domo_approval_assets(db,valid_manifest):
    """Explicit synthetic owner review of a finite Domo package, no transport."""
    import copy,psycopg
    from psycopg.types.json import Jsonb
    from gateway.contracts.canonical import canonical_digest
    from gateway.source_access_contracts import physical_source_identity_digest,source_credential_binding_digest
    from tests.contract.test_domo_runtime import setup
    from tests.unit.test_source_approval_contracts import package,domain,audience
    from tests.unit.test_policy import rules
    from tests.integration.conftest import connection_kwargs
    api,original_source,original_binding,product,query,decision,catalog=setup(valid_manifest)
    manifest=product.manifest;doc,_=package.__wrapped__(copy.deepcopy(valid_manifest))
    policy={'rules':rules(tenant_constraint=dict(member='tenant_id',operator='eq',source='grant.tenant_id'),
        entitlement_constraint=dict(member='access_group',operator='in',source='grant.entitlements.groups'),
        visible_fields=list(manifest.outputs)), 'reason':'Synthetic finite Domo review'}
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:
        conn.execute('INSERT INTO gateway.policies(id,current_revision) VALUES(%s,1)',(manifest.policy_id,))
        conn.execute("INSERT INTO gateway.policy_revisions(id,policy_id,revision,content,content_digest,actor_id) VALUES('policy-1',%s,1,%s,%s,'operator')",(manifest.policy_id,Jsonb(policy),canonical_digest(policy)))
        conn.execute("INSERT INTO gateway.principals(id,subject,kind,service_client_id) VALUES('north-user','synthetic-north','user','test-client')")
        conn.execute("INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) VALUES('north-user',%s,'north','[\"consumer\",\"author\",\"publisher\"]','{\"groups\":[\"core\"]}')",(manifest.policy_id,))
    doc['product']=dict(product_id=manifest.product_id,version=manifest.version,manifest_digest=manifest.content_digest,policy_id=manifest.policy_id)
    doc['input_domains']=[domain('start','date',minimum='2026-10-01',maximum='2026-11-01'),domain('end','date',minimum='2026-10-01',maximum='2026-11-01'),domain('account','string',values=('acct-a','acct-b'),nullable=True)]
    doc['filter_domains']=[];doc['lineage']=[]
    doc['audiences']=[audience(visible_fields=list(decision.visible_fields),row_constraints=[row.model_dump(mode='json') for row in decision.row_constraints])]
    db.fixture_fixed_principal='seed-domo-positive'
    original_statement=catalog.resolve(original_binding.variants[0].statement_id,1,original_binding.variants[0].statement_digest)
    fixture_text=original_statement.text+'\n'
    import hashlib
    fixture_statement=api.DomoStatement(statement_id='fixture-core-week',revision=1,text=fixture_text,sha256=hashlib.sha256(fixture_text.encode()).hexdigest())
    fixture_catalog=api.DomoStatementCatalog([fixture_statement]);db.fixture_statement_catalog=fixture_catalog
    bindings=[];sources=[]
    for kind,context in [('consumer','consumer'),('fixture','fixture_test')]:
        raw=original_source.model_dump(mode='json');raw.update(source_id=kind+'-source',context=context)
        raw['auth'].update(client_id_version=f'projects/synthetic-project/secrets/{kind}_id/versions/1',client_secret_version=f'projects/synthetic-project/secrets/{kind}_secret/versions/1')
        if kind=='fixture':
            raw['auth'].update(client_id_ref='/run/secrets/fixture_id',client_secret_ref='/run/secrets/fixture_secret',expected_customer='synthetic-fixture',expected_user_id=456,
                client_id_sha256=hashlib.sha256(b'explicit-isolated-fixture-client').hexdigest())
            raw['dataset_ids']=['00000000-0000-0000-0000-000000000002']
        source=api.DomoSource.model_validate(raw)
        value=original_binding.model_dump(mode='json');value.update(context=context,source_id=source.source_id,source_digest=source.digest,dataset_id=source.dataset_ids[0])
        if kind=='fixture':
            value.update(binding_id='fixture-domo-binding',model_bundle_digest=fixture_catalog.digest,
                variants=[value['variants'][0]|dict(statement_id=fixture_statement.statement_id,statement_digest=fixture_statement.sha256,
                    decision_digest=canonical_digest(decision.model_copy(update={'principal_id':db.fixture_fixed_principal})),
                    scope_digest=__import__('gateway.adapters.base',fromlist=['authorization_scope_digest']).authorization_scope_digest(decision.model_copy(update={'principal_id':db.fixture_fixed_principal})))])
        binding=api.DomoRuntimeBinding.model_validate(value)
        db.execute_owner('INSERT INTO gateway.source_pools(capacity_key,provider,physical_identity,max_active,max_dispatches) VALUES(%s,\'domo\',%s,2,10)',(source.capacity_pool_id,Jsonb(dict(provider='domo',customer=source.auth.expected_customer))))
        db.execute_owner('INSERT INTO gateway.source_profiles(source_id,revision,context,provider,capacity_key,descriptor_digest,descriptor) VALUES(%s,1,%s,\'domo\',%s,%s,%s)',(source.source_id,context,source.capacity_pool_id,source.digest,Jsonb(source.model_dump(mode='json'))))
        db.execute_owner('INSERT INTO gateway.binding_revisions(id,binding_id,revision,content,content_digest,model_bundle_digest,adapter_revision,actor_id) VALUES(%s,%s,1,%s,%s,%s,\'domo-named-v2\',\'operator\')',(kind+'-binding-row',binding.binding_id,Jsonb(binding.model_dump(mode='json')),binding.digest,binding.model_bundle_digest))
        doc['bindings'][kind]=dict(binding_id=binding.binding_id,revision=1,content_digest=binding.digest)
        doc['sources'][kind]=dict(source_id=source.source_id,revision=1,provider='domo',context=context,descriptor_digest=source.digest,physical_identity_digest=physical_source_identity_digest(source),credential_binding_digest=source_credential_binding_digest(source))
        doc['lineage']+=db.scalar('SELECT gateway.source_approval_binding_inventory(%s,%s)',(Jsonb(binding.model_dump(mode='json')),Jsonb(doc['input_domains'])))
        bindings.append(binding);sources.append(source)
    doc['artifacts'].update(model_bundle_digest=catalog.digest,fixture_model_bundle_digest=fixture_catalog.digest,statement_catalog_digest=catalog.digest,fixture_statement_catalog_digest=fixture_catalog.digest)
    db.domo=SimpleNamespace(query=query,decision=decision,catalog=catalog)
    return doc,manifest,bindings,sources


def domo_intent(prepared):
    from gateway.contracts.canonical import canonical_digest
    return dict(statement_id=prepared.statement_id,statement_digest=prepared.statement_digest,dataset_id=prepared.dataset_id,
        api_origin_digest=canonical_digest('https://api.domo.com'),response_profile_digest=canonical_digest('domo-query-positional-v1'))


@pytest.mark.parametrize('approved_source_database',[{'provider':'domo'}],indirect=True)
@pytest.mark.parametrize('revoke_before_arm',[False,True])
def test_domo_original_approval_gates_arm_but_not_exact_unknown_send_settlement(approved_source_database,revoke_before_arm):
    import hashlib,psycopg
    from gateway.contracts.canonical import canonical_bytes
    db=approved_source_database;seeded=seed_approved_source_session(db);context=seeded.context.wire_context();prepared=seeded.prepared
    assert db.scalar('SELECT prepared_canonical FROM gateway.source_preparations WHERE owner_kind=\'consumer\'')==canonical_bytes(prepared).decode()
    nonce='synthetic-original-process-nonce';nonce_hash=hashlib.sha256(nonce.encode()).hexdigest();intent_value=domo_intent(prepared)
    if revoke_before_arm:
        revoke(db)
        with pytest.raises(psycopg.Error) as caught:call(db,'domo_runtime','SELECT gateway.source_domo_arm_send(%s,%s,%s,%s)',context,prepared.digest,nonce_hash,intent_value)
        assert caught.value.sqlstate=='42501'
        assert db.scalar('SELECT count(*) FROM gateway.domo_source_sends j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'')==0
        return
    armed=call(db,'domo_runtime','SELECT gateway.source_domo_arm_send(%s,%s,%s,%s)',context,prepared.digest,nonce_hash,intent_value)
    assert armed['armed'] is True
    revoke(db)
    with pytest.raises(psycopg.Error):call(db,'domo_runtime','SELECT gateway.source_domo_arm_send(%s,%s,%s,%s)',context,prepared.digest,nonce_hash,intent_value)
    call(db,'domo_runtime','SELECT gateway.source_seal_attempt(%s,%s)',context,'authorization')
    assert asyncio.run(seeded.lifecycle.close_group(seeded.group.group_id,'authorization'))=={'closed':True,'settled':False}
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==1
    evidence=dict(plan_fingerprint=prepared.digest,response_profile_digest=intent_value['response_profile_digest'],response_sha256='e'*64,status_code=200,
        media_type='application/json',completed_at=db.scalar('SELECT clock_timestamp()').astimezone(__import__('datetime').timezone.utc).isoformat(),row_count=1,response_bytes=100)
    with pytest.raises(psycopg.Error):call(db,'domo_runtime','SELECT gateway.source_domo_observe(%s,%s,%s)',seeded.context.session_id,'wrong-nonce',evidence)
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==1
    assert call(db,'domo_runtime','SELECT gateway.source_domo_observe(%s,%s,%s)',seeded.context.session_id,nonce,evidence)['group_settled'] is True
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
    assert db.scalar('SELECT count(*) FROM gateway.domo_source_sends j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'')==1


@pytest.mark.parametrize('approved_source_database',[{'provider':'domo','expires_seconds':3}],indirect=True)
def test_domo_preparation_session_and_group_abort_on_deferred_approval_expiry(approved_source_database):
    from gateway.contracts.errors import GatewayError
    db=approved_source_database;phases=[]
    with pytest.raises(GatewayError):seed_approved_source_session(db,delay_commit=True,phase_probe=phases)
    assert phases==['finalized']
    for table in ('source_preparations','source_sessions','source_operation_groups','domo_source_sends'):
        query=(f"SELECT count(*) FROM gateway.{table} j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context='consumer'"
            if table=='domo_source_sends' else f"SELECT count(*) FROM gateway.{table} WHERE {'owner_kind' if table=='source_preparations' else 'context'}='consumer'")
        assert db.scalar(query)==0
    assert db.scalar('SELECT count(*) FROM gateway.execution_source_authorities')==1


@pytest.mark.parametrize('approved_source_database',[{'provider':'domo'}],indirect=True)
def test_domo_arm_rechecks_shorter_session_deadline_after_approval_lock(approved_source_database):
    import hashlib,time,psycopg
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    from psycopg.types.json import Jsonb
    from tests.integration.conftest import connection_kwargs
    db=approved_source_database;seeded=seed_approved_source_session(db,deadline_seconds=3);pid=Queue()
    def arm():
        try:
            with psycopg.connect(**connection_kwargs(db.urls['domo_runtime'])) as conn:
                pid.put(conn.execute('SELECT pg_backend_pid()').fetchone()[0])
                conn.execute('SELECT gateway.source_domo_arm_send(%s,%s,%s,%s)',(Jsonb(seeded.context.wire_context()),seeded.prepared.digest,
                    hashlib.sha256(b'synthetic-nonce').hexdigest(),Jsonb(domo_intent(seeded.prepared))))
            return 'allowed'
        except psycopg.Error as error:return error.sqlstate
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as blocker,ThreadPoolExecutor(1) as pool:
        blocker.execute('SELECT 1 FROM gateway.source_access_approval_states WHERE approval_id=%s FOR UPDATE',(db.approved.approval['id'],))
        future=pool.submit(arm);target=pid.get(timeout=2);limit=time.monotonic()+2
        while not db.scalar('SELECT cardinality(pg_blocking_pids(%s))>0',(target,)):
            assert time.monotonic()<limit,'Domo arm did not reach approval suffix'
            db.scalar('SELECT pg_sleep(0.005)')
        blocker.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))',(seeded.context.deadline,))
        blocker.commit();assert future.result(timeout=3)=='42501'
    assert db.scalar('SELECT count(*) FROM gateway.domo_source_sends j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'')==0


@pytest.mark.parametrize('approved_source_database',[{'provider':'domo','expires_seconds':3}],indirect=True)
def test_expired_domo_unknown_send_keeps_hold_until_exact_terminal_proof(approved_source_database):
    import hashlib,psycopg
    from datetime import timezone
    db=approved_source_database;seeded=seed_approved_source_session(db);context=seeded.context.wire_context();prepared=seeded.prepared
    nonce='synthetic-retained-original-nonce';intent_value=domo_intent(prepared)
    call(db,'domo_runtime','SELECT gateway.source_domo_arm_send(%s,%s,%s,%s)',context,prepared.digest,hashlib.sha256(nonce.encode()).hexdigest(),intent_value)
    db.scalar('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))',(db.approved.document['expires_at'],))
    with pytest.raises(psycopg.Error):call(db,'domo_runtime','SELECT gateway.source_check_attempt(%s)',context)
    call(db,'domo_runtime','SELECT gateway.source_seal_attempt(%s,%s)',context,'deadline')
    assert asyncio.run(seeded.lifecycle.close_group(seeded.group.group_id,'deadline'))=={'closed':True,'settled':False}
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==1
    evidence=dict(plan_fingerprint=prepared.digest,response_profile_digest=intent_value['response_profile_digest'],response_sha256='e'*64,status_code=200,
        media_type='application/json',completed_at=db.scalar('SELECT clock_timestamp()').astimezone(timezone.utc).isoformat(),row_count=1,response_bytes=100)
    assert call(db,'domo_runtime','SELECT gateway.source_domo_observe(%s,%s,%s)',seeded.context.session_id,nonce,evidence)['group_settled']
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
    assert db.scalar('SELECT count(*) FROM gateway.domo_source_sends j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'')==1


@pytest.mark.parametrize('boundary',['lease','claim','retained_session'])
def test_consumer_attempt_rechecks_fresh_budgets_after_final_audit(registry_database,valid_manifest,boundary):
    """Preserve reviewer lease probe and cover both tighter source budgets."""
    from dataclasses import replace
    from datetime import timedelta,timezone
    from gateway.contracts.errors import GatewayError
    from gateway.source_lifecycle import SourceSessionSpec
    from sqlalchemy import text
    from tests.integration.test_source_approval_execution_queue import setup_queue,materialized_claim
    async def scenario():
        db=registry_database;seeded=await setup_queue(db,valid_manifest);queue=seeded.service.queue
        if boundary=='lease':queue.LEASE_SECONDS=2
        original_attempt=queue.source_attempt
        if boundary=='retained_session':
            async def narrow(claim,prepared,current,plan):
                deadline=db.scalar("SELECT clock_timestamp()+interval '2 seconds'").astimezone(timezone.utc)
                return await original_attempt(replace(claim,deadline=deadline),prepared,current,plan)
            queue.source_attempt=narrow
        claim,prepared,context=await materialized_claim(seeded)
        queue.source_attempt=original_attempt
        if boundary=='claim':
            claim=replace(claim,deadline=db.scalar("SELECT clock_timestamp()+interval '2 seconds'").astimezone(timezone.utc))
        current=await queue.gate(claim)
        plan=SourceSessionSpec(**db.scalar('SELECT plan FROM gateway.source_sessions WHERE context=\'consumer\''))
        original=seeded.policy.audit.append;reached=[]
        before=db.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='execute' AND decision='allow'")
        async def delayed(uow,event):
            await original(uow,event)
            if event.action=='execute' and event.decision=='allow':
                reached.append(True)
                if boundary=='lease':
                    await uow.session.execute(text("SELECT pg_sleep(greatest(0,extract(epoch FROM ((SELECT expires_at FROM gateway.execution_leases)-clock_timestamp()))+0.03))"))
                else:
                    target=context.deadline if boundary=='retained_session' else claim.deadline
                    await uow.session.execute(text("SELECT pg_sleep(greatest(0,extract(epoch FROM (CAST(:deadline AS timestamptz)-clock_timestamp()))+0.03))"),{'deadline':target})
        seeded.policy.audit.append=delayed
        with pytest.raises(GatewayError) as caught:
            await queue.source_attempt(claim,prepared,current,plan)
        assert caught.value.code==('CONFLICT' if boundary=='lease' else 'EXECUTION_TIMEOUT')
        assert reached==[True]
        assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='execute' AND decision='allow'")==before
        assert db.scalar("SELECT count(*) FROM gateway.source_sessions WHERE context='consumer'")==1
        assert db.scalar("SELECT count(*) FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context='consumer'")==0
    asyncio.run(scenario())
