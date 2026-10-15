"""Genuine PostgreSQL serving ledger acceptance, never a fake-ledger substitute."""
import pytest

from tests.integration.conftest import migrate


@pytest.mark.postgres
@pytest.mark.parametrize("registry_database", [{"initial_revision": "0001_registry"}], indirect=True)
def test_source_migration_preserves_four_role_install(registry_database):
    db = registry_database
    migrate(db.urls["owner"], db.roles)
    assert db.scalar("SELECT count(*) FROM gateway.source_operation_groups") == 0
    assert db.scalar("SELECT deployment_epoch FROM gateway.source_runtime_settings WHERE singleton") == "unconfigured"
    assert db.denied_as_app("SELECT gateway.source_bq_reconcile_claim(1)") == "permission_denied"
    assert db.denied_as_source("SELECT * FROM gateway.source_sessions") == "permission_denied"
    assert db.denied_as_retention("DELETE FROM gateway.source_operation_groups") == "permission_denied"


@pytest.fixture
def source_database(registry_database):
    import secrets
    import psycopg
    from psycopg import sql
    from sqlalchemy import create_engine
    from tests.integration.conftest import connection_kwargs
    from tests.unit.test_bigquery_serving_source import source_value
    from gateway.adapters.bigquery_runtime import BigQuerySource
    from gateway.source_lifecycle import provision_sources
    db=registry_database
    created=[]
    with psycopg.connect(**connection_kwargs(db.admin_url),autocommit=True) as admin:
        for kind in ('bq_runtime','bq_observer','unmapped'):
            name='gws_'+secrets.token_hex(8)
            password=secrets.token_urlsafe(32)
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
            admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(db.urls['owner'].database),sql.Identifier(name)))
            db.roles[kind]=name;db.urls[kind]=db.urls['app'].set(username=name,password=password);created.append(name)
    source=BigQuerySource.model_validate(source_value(max_active_jobs=2,max_global_active_jobs=4))
    engine=create_engine(db.urls['owner'])
    with engine.begin() as connection:
        provision_sources(connection,[source],[dict(login_name=db.roles[role],source_id=source.source_id,revision=1,context='consumer',role_kind=kind)
            for role,kind in [('bq_runtime','runtime'),('bq_observer','observer')]],deployment_epoch='synthetic-epoch')
    engine.dispose()
    db.source=source
    try:
        yield db
    finally:
        with psycopg.connect(**connection_kwargs(db.admin_url.set(database=db.urls['owner'].database)),autocommit=True) as admin:
            for name in created:
                admin.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(name)))
        with psycopg.connect(**connection_kwargs(db.admin_url),autocommit=True) as admin:
            for name in created:
                admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))
        for kind in ('bq_runtime','bq_observer','unmapped'):
            db.roles.pop(kind);db.urls.pop(kind)


async def seed_source_session(db, *, per_principal_limit=None):
    """Fresh protected publication and accepted consumer, with retained fixture history."""
    from datetime import timedelta
    from types import SimpleNamespace
    from uuid import uuid4
    from sqlalchemy import func
    from gateway.adapters.bigquery_runtime import compile_bigquery_query
    from gateway.adapters.base import authorization_scope_digest
    from gateway.contracts.canonical import canonical_bytes,canonical_digest
    from gateway.contracts.execution import ExecutionSpec,PreparedQuery
    from gateway.registry.models import Execution,ExecutionState,ExecutionLease,ExecutionAuthorization
    from gateway.source_access import SourceAccessService,ExecutionOwner
    from gateway.source_lifecycle import SourceLifecycle,SourceSessionSpec
    from tests.contract.test_cube_adapter import synthetic
    from tests.integration.test_audit import service
    from tests.unit.test_source_approval_contracts import principal
    from tests.integration.source_approval_seed import source_database_assets
    from tests.integration.test_source_approval_transactions import consumer_setup
    original,original_product,_,request=synthetic()
    _,manifest,_,product=consumer_setup(db,original_product.manifest.model_dump(mode='json'),
        assets_factory=lambda db,raw:source_database_assets(db,raw,per_principal_limit=per_principal_limit))
    binding=db.publication_seed.bindings[0]
    actor=principal();policy=service(db);access=SourceAccessService(db.registry,policy.audit);policy.source_access=access
    sources=SimpleNamespace(resolve=lambda ref: db.source)
    lifecycle=SourceLifecycle(db.registry,policy,sources,deployment_epoch='synthetic-epoch')
    from gateway.contracts.query import validate_query
    request=validate_query(manifest,request)
    canonical=request.model_dump(mode='json',exclude={'idempotency_key'})
    spec=ExecutionSpec(principal_id=actor.id,pinned_product=product,canonical_query=canonical,canonical_hash=canonical_digest(canonical),
        policy_revision=1,context='consumer',request_id=str(uuid4()))
    async with db.registry.transaction() as uow:
        await lifecycle._lock_authority(uow,product)
        decision=await policy.evaluate(actor,'execute',product,uow,correlation_id=spec.request_id)
        execution=Execution(id=str(uuid4()),principal_id=actor.id,product_id='revenue',version_id=product.version_id,policy_id='revenue-access',
            policy_revision=1,canonical_query=canonical,query_canonical=canonical_bytes(canonical).decode(),canonical_hash=spec.canonical_hash,context='consumer',request_id=spec.request_id,
            deadline_at=func.clock_timestamp(),execution_timeout_seconds=120)
        uow.session.add(execution);await uow.session.flush();await uow.session.refresh(execution)
        uow.session.add(ExecutionAuthorization(execution_id=execution.id,principal_snapshot=actor.model_dump(mode='json'),
            decision=decision.model_dump(mode='json'),scope_digest=authorization_scope_digest(decision),full_decision_digest=canonical_digest(decision)))
        uow.session.add(ExecutionState(execution_id=execution.id,state='running',started_at=execution.created_at))
        token=str(uuid4())
        uow.session.add(ExecutionLease(execution_id=execution.id,worker_id='source-test',lease_token=token,generation=1,
            expires_at=execution.created_at+timedelta(seconds=30),source_reserved_until=execution.deadline_at))
        await uow.session.flush()
        group=await lifecycle.reserve_consumer(uow,(execution,spec,actor,decision,binding),decision,execution.deadline_at)
        prepared=PreparedQuery(endpoint_id='synthetic-source',model_bundle_digest=binding.model_bundle_digest,
            cube_query=compile_bigquery_query(binding,manifest,request,decision))
        plan=SourceSessionSpec(provider='bigquery',source_id=db.source.source_id,source_revision=1,context='consumer',
            capacity_key=db.source.capacity_pool_id,definition_digest=db.source.digest,request_digest=spec.canonical_hash,
            prepared_digest=canonical_digest(prepared.cube_query),scope_digest=authorization_scope_digest(decision),decision_digest=canonical_digest(decision),
            binding_digest=binding.digest,model_bundle_digest=binding.model_bundle_digest,catalog_digest=None,max_submissions=2,
            max_bytes_per_submission=1000,max_total_bytes=2000)
        lifecycle._validate_plan(spec,prepared,decision,binding,plan)
        context=await lifecycle._materialize(uow,group=group,owner_id=execution.id,case_id=execution.id,spec=spec,principal=actor,
            actor=actor,binding=binding,prepared=prepared,executed=decision,current=decision,plan=plan,generation=1,token=token,deadline=execution.deadline_at)
        source_request=await access.resolve_owner(uow,ExecutionOwner(kind='execution',id=execution.id),actor,decision,action='execute',request_id=spec.request_id)
        await access.finalize(uow,(source_request,))
    return SimpleNamespace(lifecycle=lifecycle,context=context,group=group,execution=execution,spec=spec,prepared=prepared,
        decision=decision,binding=binding,token=token,principal=actor)


def call(db,role,sql,*parameters):
    from psycopg.types.json import Jsonb
    return db._execute(role,sql,tuple(Jsonb(value) if isinstance(value,dict) else value for value in parameters),scalar=True)


def intent(timeout=5000,key='a'):
    return dict(query_digest=key*64,parameters_digest='b'*64,parameter_types_digest='c'*64,
        configuration_digest='d'*64,maximum_bytes_billed='1000',job_timeout_ms=timeout)


@pytest.mark.postgres
def test_source_commit_replay_seal_and_proof_only_settlement(source_database):
    import asyncio
    db=source_database;seeded=asyncio.run(seed_source_session(db));ctx=seeded.context.wire_context()
    first=call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',ctx,'e'*64,intent())
    assert first['submit_once'] is True
    replay=call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',ctx,'e'*64,intent(4000))
    assert replay['submit_once'] is False and replay['job']==first['job']
    assert db.scalar('SELECT admitted_count FROM gateway.source_operation_groups WHERE context=\'consumer\'')==1
    assert db.denied_as_app('SELECT gateway.source_bq_observe(%s,%s)',(first['job']['job_id'],'{}'))=='permission_denied'
    call(db,'bq_runtime','SELECT gateway.source_seal_attempt(%s,%s)',ctx,'completed')
    result=asyncio.run(seeded.lifecycle.close_group(seeded.group.group_id,'completed'))
    assert result=={'closed':True,'settled':False}
    evidence=dict(intent_digest=first['job']['intent_digest'],state='done',terminal_error=False)
    status=call(db,'bq_runtime','SELECT gateway.source_bq_observe(%s,%s)',first['job']['job_id'],evidence)
    assert status['terminal'] is True and status['group_settled'] is True
    assert call(db,'bq_observer','SELECT gateway.source_settle_group(%s)',seeded.group.group_id) is True
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0


@pytest.mark.postgres
def test_source_roles_cannot_widen_or_use_unbounded_null_scans(source_database):
    import asyncio,psycopg
    db=source_database;seeded=asyncio.run(seed_source_session(db));ctx=seeded.context.wire_context()
    for role,statement,parameters in [('bq_runtime','SELECT * FROM gateway.principals',()),
        ('bq_runtime','SELECT gateway.source_seal_session(%s,%s)',(seeded.context.session_id,'completed')),
        ('bq_observer','SELECT gateway.source_check_attempt(%s)',(ctx,)),
        ('unmapped','SELECT gateway.source_status(%s)',(seeded.context.session_id,))]:
        with pytest.raises(psycopg.Error) as caught:call(db,role,statement,*parameters)
        assert caught.value.sqlstate=='42501'
    for function in ('source_expire_groups','source_bq_reconcile_claim'):
        with pytest.raises(psycopg.Error) as caught:call(db,'bq_observer',f'SELECT gateway.{function}(NULL)')
        assert caught.value.sqlstate=='23514'
    assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'')==0
    assert db.scalar('SELECT closed FROM gateway.source_operation_groups WHERE context=\'consumer\'') is False


@pytest.mark.postgres
def test_old_generation_cannot_seal_successor_but_revoked_same_generation_can(source_database):
    import asyncio,psycopg
    from uuid import uuid4
    db=source_database;seeded=asyncio.run(seed_source_session(db));old=seeded.context.wire_context()
    first=call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',old,'e'*64,intent())
    token=str(uuid4())
    db.execute_app('UPDATE gateway.execution_leases SET generation=2,lease_token=%s WHERE execution_id=%s',(token,seeded.execution.id))
    with pytest.raises(psycopg.Error) as caught:call(db,'bq_runtime','SELECT gateway.source_seal_attempt(%s,%s)',old,'runtime_error')
    assert caught.value.sqlstate=='42501'  # actual lease changed before session reclaim
    call(db,'app','SELECT gateway.source_reclaim_session(%s,%s,%s,%s)',seeded.context.session_id,2,token,seeded.context.deadline)
    fresh=old|{'generation':2}
    with pytest.raises(psycopg.Error):call(db,'bq_runtime','SELECT gateway.source_seal_attempt(%s,%s)',old,'runtime_error')
    replay=call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',fresh,'e'*64,intent(3000))
    assert replay['submit_once'] is False and replay['job']==first['job']
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==1
    db.execute_owner("UPDATE gateway.principals SET active=false WHERE id='north-user'")
    with pytest.raises(psycopg.Error):call(db,'bq_runtime','SELECT gateway.source_check_attempt(%s)',fresh)
    status=call(db,'bq_runtime','SELECT gateway.source_seal_attempt(%s,%s)',fresh,'authorization')
    assert status['sealed'] and not status['terminal']
    call(db,'app','SELECT gateway.source_close_group(%s,%s)',seeded.group.group_id,'authorization')
    jobs=call(db,'bq_observer','SELECT gateway.source_bq_reconcile_claim(8)')
    assert len(jobs)==1
    assert call(db,'bq_observer','SELECT gateway.source_bq_check_observation(%s,%s)',first['job']['job_id'],jobs[0]['observation_token'])['job_id']==first['job']['job_id']
    call(db,'bq_observer','SELECT gateway.source_bq_observe(%s,%s)',first['job']['job_id'],dict(intent_digest=first['job']['intent_digest'],state='done',terminal_error=True))
    assert call(db,'bq_observer','SELECT gateway.source_settle_group(%s)',seeded.group.group_id)


@pytest.mark.postgres
def test_concurrent_intent_commit_has_one_winner_and_seal_blocks_late_admission(source_database):
    import asyncio,concurrent.futures,psycopg
    db=source_database;seeded=asyncio.run(seed_source_session(db));ctx=seeded.context.wire_context()
    def admit(timeout):return call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',ctx,'e'*64,intent(timeout))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(admit,[4000,5000]))
    assert sum(item['submit_once'] for item in results)==1
    assert results[0]['job']==results[1]['job']
    call(db,'bq_runtime','SELECT gateway.source_seal_attempt(%s,%s)',ctx,'completed')
    with pytest.raises(psycopg.Error):call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',ctx,'f'*64,intent(key='f'))
    assert db.scalar('SELECT admitted_count FROM gateway.source_operation_groups WHERE context=\'consumer\'')==1
    assert db.scalar('SELECT dispatches FROM gateway.source_pools')==1+len(db.publication_seed.job_ids)


@pytest.mark.postgres
def test_service_rejects_otherwise_valid_swapped_preparation_pins(source_database):
    import asyncio
    from dataclasses import replace
    db=source_database;seeded=asyncio.run(seed_source_session(db))
    for field in ('request_digest','prepared_digest','scope_digest','decision_digest','binding_digest','model_bundle_digest'):
        with pytest.raises(ValueError):
            seeded.lifecycle._validate_plan(seeded.spec,seeded.prepared,seeded.decision,seeded.binding,
                replace(seeded.context.spec,**{field:'f'*64}))
    with pytest.raises(ValueError):
        seeded.lifecycle._validate_plan(seeded.spec,seeded.prepared,seeded.decision,seeded.binding,
            replace(seeded.context.spec,catalog_digest='f'*64))
    assert db.scalar('SELECT count(*) FROM gateway.source_sessions WHERE context=\'consumer\'')==1


@pytest.mark.postgres
def test_unknown_intent_survives_lost_commit_reply_and_budget_never_refunds(source_database):
    import asyncio,psycopg
    db=source_database;seeded=asyncio.run(seed_source_session(db));ctx=seeded.context.wire_context()
    # The caller deliberately discards a confirmed commit response, as after a
    # network acknowledgement loss. A new connection recovers only the intent.
    call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',ctx,'e'*64,intent())
    known=call(db,'bq_runtime','SELECT gateway.source_bq_lookup(%s,%s)',ctx,'e'*64)
    assert known['state']=='intent'
    replay=call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',ctx,'e'*64,intent(3000))
    assert not replay['submit_once']
    call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',ctx,'f'*64,intent(key='f'))
    with pytest.raises(psycopg.Error) as caught:call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',ctx,'a'*64,intent(key='f'))
    assert caught.value.sqlstate=='54000'
    call(db,'app','SELECT gateway.source_close_group(%s,%s)',seeded.group.group_id,'cancelled')
    assert call(db,'bq_observer','SELECT gateway.source_settle_group(%s)',seeded.group.group_id) is False
    assert db.scalar('SELECT reserved_bytes FROM gateway.source_operation_groups WHERE context=\'consumer\'')==2000
    assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==1


@pytest.mark.postgres
def test_data_role_disable_and_profile_rotation_preserve_old_observer_settlement(source_database):
    import asyncio,psycopg
    from sqlalchemy import create_engine
    from gateway.adapters.bigquery_runtime import BigQuerySource
    from gateway.source_lifecycle import provision_sources
    db=source_database;seeded=asyncio.run(seed_source_session(db));ctx=seeded.context.wire_context()
    admitted=call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',ctx,'e'*64,intent())
    changed=db.source.model_dump(mode='json');changed.update(source_id='rotated-alias',revision=2)
    changed['auth']['expected_principal']='rotated@billing-project.iam.gserviceaccount.com'
    rotated=BigQuerySource.model_validate(changed)
    engine=create_engine(db.urls['owner'])
    with engine.begin() as connection:
        provision_sources(connection,[rotated],[dict(login_name=db.roles['bq_runtime'],source_id=rotated.source_id,revision=2,context='consumer',role_kind='runtime')],deployment_epoch='synthetic-epoch')
    engine.dispose()
    assert db.scalar('SELECT count(*) FROM gateway.source_pools')==1
    db.execute_owner('REVOKE EXECUTE ON FUNCTION gateway.source_check_attempt(jsonb),gateway.source_bq_admit(jsonb,text,jsonb) FROM "'+db.roles['bq_runtime']+'"')
    with pytest.raises(psycopg.Error):call(db,'bq_runtime','SELECT gateway.source_check_attempt(%s)',ctx)
    call(db,'app','SELECT gateway.source_close_group(%s,%s)',seeded.group.group_id,'authorization')
    jobs=call(db,'bq_observer','SELECT gateway.source_bq_reconcile_claim(8)')
    assert len(jobs)==1 and jobs[0]['source']['auth']['expected_principal']==db.source.auth.expected_principal
    call(db,'bq_observer','SELECT gateway.source_bq_observe(%s,%s)',admitted['job']['job_id'],dict(intent_digest=admitted['job']['intent_digest'],state='done',terminal_error=False))
    assert call(db,'bq_observer','SELECT gateway.source_settle_group(%s)',seeded.group.group_id)


@pytest.mark.postgres
def test_app_cannot_insert_success_without_sealed_terminal_source_proof(source_database):
    import asyncio
    db=source_database;seeded=asyncio.run(seed_source_session(db))
    # BEFORE INSERT proof check must run before uniqueness; otherwise legacy
    # initial-state permissiveness would allow a fresh owner to bypass proof.
    assert db.constraint_failure("INSERT INTO gateway.execution_states(execution_id,state,started_at,completed_at) VALUES(%s,'succeeded',clock_timestamp(),clock_timestamp())",
        (seeded.execution.id,))=='23514'
    assert db.constraint_failure("UPDATE gateway.execution_states SET state='succeeded',completed_at=clock_timestamp() WHERE execution_id=%s",
        (seeded.execution.id,))=='23514'


@pytest.mark.postgres
@pytest.mark.parametrize('field',['catalog_digest','request_digest','binding_digest','model_bundle_digest','scope_digest','decision_digest'])
def test_db_owner_pins_reject_otherwise_well_formed_session_plan(source_database,field):
    import asyncio,psycopg
    from uuid import uuid4
    from psycopg.types.json import Jsonb
    db=source_database;asyncio.run(seed_source_session(db))
    row=db.scalar('SELECT to_jsonb(s) FROM gateway.source_sessions s WHERE s.context=\'consumer\'')
    row['id']=str(uuid4());row['plan'][field]='f'*64
    with pytest.raises(psycopg.Error) as caught:
        call(db,'app','INSERT INTO gateway.source_sessions SELECT (jsonb_populate_record(NULL::gateway.source_sessions,%s)).* RETURNING id',row)
    assert caught.value.sqlstate=='23514'


@pytest.mark.postgres
@pytest.mark.parametrize('role',['bq_runtime','bq_observer'])
def test_exact_terminal_evidence_rejects_null_missing_extra_and_wrong_types(source_database,role):
    import asyncio,psycopg
    db=source_database;seeded=asyncio.run(seed_source_session(db));ctx=seeded.context.wire_context()
    job=call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',ctx,'e'*64,intent())['job']
    evidence=dict(intent_digest=job['intent_digest'],state='done',terminal_error=False)
    call(db,role,'SELECT gateway.source_bq_observe(%s,%s)',job['job_id'],evidence)
    cases=[evidence|{'state':None},evidence|{'state':1},evidence|{'terminal_error':None},
        evidence|{'terminal_error':'false'},evidence|{'extra':False},
        {key:value for key,value in evidence.items() if key!='state'}]
    for bad in cases:
        with pytest.raises(psycopg.Error) as caught:call(db,role,'SELECT gateway.source_bq_observe(%s,%s)',job['job_id'],bad)
        assert caught.value.sqlstate=='23514'
    assert db.scalar('SELECT state FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'')=='done'


@pytest.mark.postgres
def test_queue_reclaim_reuses_group_and_old_python_cleanup_cannot_close_it(source_database):
    import asyncio
    from sqlalchemy import text
    from gateway.adapters.base import StaticBindingResolver
    from gateway.contracts.errors import ErrorCode
    from gateway.execution.queue import ExecutionQueue,Claim
    db=source_database
    async def scenario():
        seeded=await seed_source_session(db,per_principal_limit=1)
        queue=ExecutionQueue(db.registry,seeded.lifecycle.policy,StaticBindingResolver([seeded.binding]),source_lifecycle=seeded.lifecycle)
        old=Claim(seeded.execution.id,seeded.token,1,seeded.group.group_id,seeded.spec,
            seeded.principal,
            seeded.decision,seeded.binding,seeded.prepared,seeded.context.deadline,seeded.execution.created_at,seeded.execution.created_at)
        db.execute_owner("UPDATE gateway.execution_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE execution_id=%s",(seeded.execution.id,))
        successor=await queue.claim('successor')
        assert successor is not None and successor.generation==2 and successor.reservation_id==seeded.group.group_id
        current=await queue.gate(successor)
        fresh=await queue.source_attempt(successor,seeded.prepared,current,seeded.context.spec)
        assert fresh.session_id==seeded.context.session_id and fresh.generation==2
        assert await queue.fail(old,ErrorCode.UPSTREAM_UNAVAILABLE,source_completed=True) is False
        assert db.scalar('SELECT closed FROM gateway.source_operation_groups WHERE context=\'consumer\'') is False
        assert db.scalar('SELECT sealed FROM gateway.source_sessions WHERE context=\'consumer\'') is False
        assert db.scalar('SELECT count(*) FROM gateway.source_reservations')==0
        assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==1
        assert await queue.fail(successor,ErrorCode.UPSTREAM_UNAVAILABLE) is True
        assert db.scalar('SELECT closed FROM gateway.source_operation_groups WHERE context=\'consumer\'') is True
        assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0  # independently zero intent
    asyncio.run(scenario())


@pytest.mark.postgres
def test_lower_retained_global_cap_applies_even_to_another_admitting_pool(source_database):
    import asyncio,psycopg
    from sqlalchemy import create_engine
    from gateway.adapters.bigquery_runtime import BigQuerySource
    from gateway.source_lifecycle import provision_sources
    db=source_database;seeded=asyncio.run(seed_source_session(db));ctx=seeded.context.wire_context()
    first=call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',ctx,'e'*64,intent())
    data=db.source.model_dump(mode='json');data.update(source_id='other-pool',billing_project='other-project',max_global_active_jobs=1,max_active_jobs=1)
    other=BigQuerySource.model_validate(data)
    engine=create_engine(db.urls['owner'])
    with engine.begin() as connection:
        provision_sources(connection,[other],[dict(login_name=db.roles['bq_runtime'],source_id=other.source_id,revision=1,context='consumer',role_kind='runtime')],deployment_epoch='synthetic-epoch')
    engine.dispose()
    assert db.scalar('SELECT count(*) FROM gateway.source_pools')==2
    assert db.scalar('SELECT gateway.source_global_limit()')==1
    # The admitting profile still says4 and its own pool allows2; only the
    # lower retained other-pool global cap can reject this second operation.
    with pytest.raises(psycopg.Error) as caught:call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',ctx,'f'*64,intent(key='f'))
    assert caught.value.sqlstate=='54000'
    assert call(db,'bq_observer','SELECT gateway.source_bq_reconcile_claim(8)')[0]['job']['job_id']==first['job']['job_id']


@pytest.mark.postgres
@pytest.mark.parametrize('registry_database',[{'initial_revision':'0006_source_preparations'}],indirect=True)
def test_expiry_batches_progress_past_old_closed_unknown_groups(source_database):
    import psycopg
    from datetime import datetime,timedelta,timezone
    from uuid import uuid4
    from psycopg.types.json import Jsonb
    from tests.integration.conftest import connection_kwargs
    from tests.integration.source_approval_seed import _insert
    db=source_database;now=datetime.now(timezone.utc);first=None
    assert db.scalar('SELECT version_num FROM public.alembic_version')=='0006_source_preparations'
    # This maintenance-only inventory predates the approval migration. It never
    # represents accepted reviewed consumers and is never given fresh authority.
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as owner:
        _insert(owner,'principals',dict(id='historical-actor',subject='historical-actor',kind='demo'))
        _insert(owner,'audit_events',dict(id='historical-audit',actor_id='historical-actor',action='execute',decision='allow',correlation_id='historical'))
        owner.execute('ALTER TABLE gateway.source_operation_groups DISABLE TRIGGER source_group_insert')
        owner.execute('ALTER TABLE gateway.source_sessions DISABLE TRIGGER source_session_insert')
        for index in range(34):
            gid,sid=str(uuid4()),str(uuid4());owner_id='historical-'+str(index)
            _insert(owner,'source_operation_groups',dict(id=gid,owner_kind='consumer',owner_id=owner_id,actor_id='historical-actor',
                source_id=db.source.source_id,source_revision=1,context='consumer',capacity_key=db.source.capacity_pool_id,
                deployment_epoch='synthetic-epoch',created_at=now-timedelta(seconds=120),deadline_at=now-timedelta(seconds=40-index),
                roster=Jsonb([owner_id]),authority_spec=Jsonb({}),authority_snapshot=Jsonb({}),allow_audit_id='historical-audit',
                max_submissions=2,max_total_bytes=2000,closed=index<32,close_reason='deadline' if index<32 else None,
                admitted_count=1 if index<32 else 0,reserved_bytes=1000 if index<32 else 0))
            if index<32:
                _insert(owner,'source_sessions',dict(id=sid,group_id=gid,case_id=owner_id,owner_id=owner_id,
                    source_id=db.source.source_id,source_revision=1,context='consumer',capacity_key=db.source.capacity_pool_id,
                    plan=Jsonb({'provider':'bigquery'}),provider_nonce='historical',authority_spec=Jsonb({}),authority_snapshot=Jsonb({}),
                    authority_stamp='a'*64,context_digest='b'*64,allow_audit_id='historical-audit',generation=1,lease_token='historical',
                    deadline_at=now-timedelta(seconds=40-index),sealed=True,seal_reason='deadline'))
                _insert(owner,'bigquery_source_jobs',dict(job_id=owner_id,session_id=sid,semantic_key='e'*64,project_id=db.source.billing_project,
                    location=db.source.location,principal=db.source.auth.expected_principal,intent=Jsonb(intent()),intent_digest='f'*64))
                if first is None:first=(gid,sid,owner_id)
        owner.execute('ALTER TABLE gateway.source_operation_groups ENABLE TRIGGER source_group_insert')
        owner.execute('ALTER TABLE gateway.source_sessions ENABLE TRIGGER source_session_insert')
    migrate(db.urls['owner'],db.roles)
    assert db.scalar('SELECT version_num FROM public.alembic_version')=='0007_source_access_approvals'
    assert db.scalar('SELECT count(*) FROM gateway.execution_source_authorities')==0
    assert db.scalar('SELECT count(*) FROM gateway.executions')==0
    assert db.denied_as_app("SELECT gateway.source_assert_owner_access('execution',%s,'execute')",(first[2],))=='permission_denied'
    with pytest.raises(psycopg.Error) as denied:
        call(db,'bq_runtime','SELECT gateway.source_check_attempt(%s)',dict(group_id=first[0],session_id=first[1],generation=1,
            authority_stamp='a'*64,context_digest='b'*64))
    assert denied.value.sqlstate=='42501'
    assert db.scalar("SELECT count(*) FROM pg_trigger WHERE tgrelid IN ('gateway.source_operation_groups'::regclass,'gateway.source_sessions'::regclass) AND tgenabled='D'")==0
    assert call(db,'bq_observer','SELECT gateway.source_expire_groups(1)')==1
    assert call(db,'bq_observer','SELECT gateway.source_expire_groups(1)')==1
    assert call(db,'bq_observer','SELECT gateway.source_expire_groups(32)')==0
    assert db.scalar("SELECT count(*) FROM gateway.source_operation_groups WHERE owner_id LIKE 'historical-%%' AND settled_at IS NOT NULL")==2
    assert db.scalar("SELECT count(*) FROM gateway.source_operation_groups WHERE owner_id LIKE 'historical-%%' AND settled_at IS NULL")==32
    assert db.scalar("SELECT count(*) FROM gateway.bigquery_source_jobs WHERE state='intent'")==32


@pytest.mark.postgres
def test_same_policy_new_grant_invalidates_queued_authority_before_rpc(source_database):
    import asyncio,psycopg
    db=source_database;seeded=asyncio.run(seed_source_session(db));ctx=seeded.context.wire_context()
    db.execute_owner("INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) VALUES('north-user','revenue-access','south','[\"consumer\"]','{\"groups\":[\"south\"]}')")
    with pytest.raises(psycopg.Error) as caught:call(db,'bq_runtime','SELECT gateway.source_check_attempt(%s)',ctx)
    assert caught.value.sqlstate=='42501'
    with pytest.raises(psycopg.Error):call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',ctx,'e'*64,intent())
    assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'')==0


@pytest.mark.postgres
@pytest.mark.parametrize('terminal',[True,False])
def test_real_worker_bq_http_completion_cannot_replace_provider_proof(source_database,terminal):
    import asyncio,json,httpx,jwt
    from types import SimpleNamespace
    from gateway.adapters.base import StaticBindingResolver
    from gateway.adapters.bigquery_serving import BigQueryServingAdapter
    from gateway.adapters.dispatch import SourceDispatchAdapter
    from gateway.execution.queue import ExecutionQueue
    from gateway.execution.worker import Worker
    from tests.contract.test_cube_adapter import upstream_response
    db=source_database;calls=[]
    async def scenario():
        seeded=await seed_source_session(db)
        bindings=StaticBindingResolver([seeded.binding])
        queue=ExecutionQueue(db.registry,seeded.lifecycle.policy,bindings,source_lifecycle=seeded.lifecycle)
        endpoint=SimpleNamespace(endpoint_id='synthetic-source',bundle_digest=seeded.binding.model_bundle_digest,
            adapter_revision='cube-bigquery-v1',context='consumer',url=db.source.cube_endpoint,secret_ref='/run/secrets/signing')
        catalog=SimpleNamespace(resolve=lambda digest:endpoint,resolve_query=lambda endpoint_id,digest:endpoint)
        def handler(request):
            calls.append(request)
            claims=jwt.decode(request.headers['Authorization'].removeprefix('Bearer '),'a'*43,algorithms=['HS256'],audience='dpg-cube-source-v1')
            context=claims['source_attempt']
            assert context['generation']==2
            assert claims['cube_query_digest']==__import__('gateway.contracts.canonical',fromlist=['canonical_digest']).canonical_digest(json.loads(request.content)['query'])
            job=call(db,'bq_runtime','SELECT gateway.source_bq_admit(%s,%s,%s)',context,'e'*64,intent())['job']
            if terminal:
                call(db,'bq_runtime','SELECT gateway.source_bq_observe(%s,%s)',job['job_id'],dict(intent_digest=job['intent_digest'],state='done',terminal_error=False))
            return upstream_response(200,json={'data':[]})
        def factory():
            adapter=BigQueryServingAdapter(sources=seeded.lifecycle.sources,catalog=catalog,bindings=bindings,
                secret_resolver=lambda ref:'a'*43,transport=httpx.MockTransport(handler))
            return SourceDispatchAdapter(bindings=bindings,bigquery_adapter=adapter)
        db.execute_owner("UPDATE gateway.execution_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE execution_id=%s",(seeded.execution.id,))
        assert await Worker(queue,adapter_factory=factory,worker_id='actual-python-worker').run_once()
        assert len(calls)==1
        assert db.scalar('SELECT state FROM gateway.execution_states')==('succeeded' if terminal else 'failed')
        assert db.scalar('SELECT count(*) FROM gateway.results')==int(terminal)
        assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==int(not terminal)
        assert db.scalar('SELECT count(*) FROM gateway.source_reservations')==0
        if terminal:
            assert db.scalar("SELECT envelope->'source_watermark' FROM gateway.results") is None
    asyncio.run(scenario())


@pytest.mark.postgres
def test_first_source_claim_reserves_one_group_and_no_legacy_attempt_hold(source_database):
    import asyncio
    from uuid import uuid4
    from gateway.adapters.base import StaticBindingResolver
    from gateway.execution.queue import ExecutionQueue
    db=source_database
    async def scenario():
        seeded=await seed_source_session(db)
        eid=str(uuid4());request_id=str(uuid4())
        import psycopg
        from tests.integration.conftest import connection_kwargs
        with psycopg.connect(**connection_kwargs(db.urls['app'])) as conn:
            conn.execute('''INSERT INTO gateway.executions(id,principal_id,product_id,version_id,policy_id,policy_revision,
                canonical_query,query_canonical,canonical_hash,context,request_id,deadline_at,execution_timeout_seconds)
                SELECT %s,principal_id,product_id,version_id,policy_id,policy_revision,canonical_query,query_canonical,canonical_hash,context,%s,
                    clock_timestamp(),execution_timeout_seconds FROM gateway.executions WHERE id=%s''',(eid,request_id,seeded.execution.id))
            # Probe the real newly inserted owner under a savepoint, before its
            # initial state exists; a uniqueness failure cannot satisfy this.
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute("INSERT INTO gateway.execution_states(execution_id,state,started_at,completed_at) VALUES(%s,'succeeded',clock_timestamp(),clock_timestamp())",(eid,))
            conn.execute("INSERT INTO gateway.execution_states(execution_id,state) VALUES(%s,'accepted')",(eid,))
            conn.execute('''INSERT INTO gateway.execution_authorizations(execution_id,principal_snapshot,decision,scope_digest,full_decision_digest)
                SELECT %s,principal_snapshot,decision,scope_digest,full_decision_digest FROM gateway.execution_authorizations WHERE execution_id=%s''',(eid,seeded.execution.id))
        queue=ExecutionQueue(db.registry,seeded.lifecycle.policy,StaticBindingResolver([seeded.binding]),source_lifecycle=seeded.lifecycle)
        claim=await queue.claim('first-source-owner')
        assert claim.execution_id==eid and claim.generation==1 and claim.prepared is None
        assert claim.reservation_id!=seeded.group.group_id
        assert db.scalar('SELECT count(*) FROM gateway.source_operation_groups WHERE context=\'consumer\'')==2
        assert db.scalar('SELECT count(*) FROM gateway.source_reservations')==0
        assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==2
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize('widen',['schema','table','function'])
def test_provisioner_rejects_preexisting_unreviewed_provider_privileges(source_database,widen):
    from sqlalchemy import create_engine
    from gateway.source_lifecycle import provision_sources
    db=source_database;role=db.roles['bq_runtime']
    grants={'schema':'CREATE ON SCHEMA gateway','table':'SELECT ON TABLE gateway.principals',
        'function':'EXECUTE ON FUNCTION gateway.source_current_authority(jsonb)'}
    db.execute_owner('GRANT '+grants[widen]+' TO "'+role+'"')
    engine=create_engine(db.urls['owner'])
    with pytest.raises(ValueError):
        with engine.begin() as connection:
            provision_sources(connection,[db.source],[dict(login_name=role,source_id=db.source.source_id,revision=1,context='consumer',role_kind='runtime')],deployment_epoch='synthetic-epoch')
    engine.dispose()


@pytest.mark.postgres
def test_source_transaction_overrides_role_async_commit_default_before_permit(source_database):
    import asyncio,psycopg
    from psycopg import sql
    from psycopg.types.json import Jsonb
    from tests.integration.conftest import connection_kwargs
    db=source_database;seeded=asyncio.run(seed_source_session(db));ctx=seeded.context.wire_context()
    with psycopg.connect(**connection_kwargs(db.admin_url),autocommit=True) as admin:
        admin.execute(sql.SQL("ALTER ROLE {} SET synchronous_commit=off").format(sql.Identifier(db.roles['bq_runtime'])))
    with psycopg.connect(**connection_kwargs(db.urls['bq_runtime'])) as runtime:
        assert runtime.execute('SHOW synchronous_commit').fetchone()[0]=='off'
        result=runtime.execute('SELECT gateway.source_bq_admit(%s,%s,%s)',(Jsonb(ctx),'e'*64,Jsonb(intent()))).fetchone()[0]
        assert result['submit_once'] is True
        assert runtime.execute('SHOW synchronous_commit').fetchone()[0]=='on'
        assert runtime.execute('SHOW fsync').fetchone()[0]=='on'
        assert runtime.execute('SHOW full_page_writes').fetchone()[0]=='on'
    assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'')==1


@pytest.mark.postgres
@pytest.mark.parametrize('setting',['fsync','full_page_writes'])
def test_source_epoch_fails_closed_when_disposable_server_durability_is_off(source_database,setting):
    import asyncio,psycopg,time
    from psycopg import sql
    from tests.integration.conftest import connection_kwargs
    db=source_database
    from tests.integration.test_execution_queue import setup
    from tests.integration.test_audit import principal
    from gateway.contracts.errors import ErrorCode
    async def legacy_owner():
        service,request=await setup(db)
        await service.submit(principal(),request)
        return service.queue,await service.queue.claim('legacy-durability-scope')
    legacy_queue,legacy_claim=asyncio.run(legacy_owner())
    # Only the generated, explicitly authorized loopback cluster is changed;
    # restore in finally before any later test and the launcher removes it.
    with psycopg.connect(**connection_kwargs(db.admin_url),autocommit=True) as admin:
        assert admin.execute(sql.SQL('SHOW {}').format(sql.Identifier(setting))).fetchone()[0]=='on'
        try:
            admin.execute(sql.SQL('ALTER SYSTEM SET {}=off').format(sql.Identifier(setting)))
            admin.execute('SELECT pg_reload_conf()')
            for _ in range(100):
                if call(db,'bq_runtime','SELECT current_setting(%s)',setting)=='off':break
                time.sleep(.01)
            else:raise AssertionError('Disposable durability setting did not reload')
            with pytest.raises(psycopg.Error) as caught:call(db,'bq_runtime','SELECT gateway.source_check_epoch(%s)','synthetic-epoch')
            assert caught.value.sqlstate=='42501'
            assert asyncio.run(legacy_queue.fail(legacy_claim,ErrorCode.UPSTREAM_UNAVAILABLE)) is True
            assert db.scalar('SELECT state FROM gateway.execution_states')=='failed'
            assert db.scalar('SELECT count(*) FROM gateway.source_operation_groups WHERE context=\'consumer\'')==0
        finally:
            admin.execute(sql.SQL('ALTER SYSTEM RESET {}').format(sql.Identifier(setting)))
            admin.execute('SELECT pg_reload_conf()')
            for _ in range(100):
                if call(db,'bq_runtime','SELECT current_setting(%s)',setting)=='on':break
                time.sleep(.01)
            else:raise AssertionError('Disposable durability setting did not restore')
