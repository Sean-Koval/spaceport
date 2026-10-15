"""Real source finalization, protected provenance, clock and denial audit proof."""
import asyncio
from uuid import uuid4
import pytest
from sqlalchemy import text

from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import PinnedProduct
from gateway.source_access import ExecutionOwner,SourceAccessService
from tests.integration.test_audit import service as policy_service
from tests.integration.test_source_approval_registry import approval_assets,install
from tests.unit.test_source_approval_contracts import principal

pytestmark=pytest.mark.postgres


def transient_setup(db,valid_manifest):
    doc,manifest,bindings,*_=approval_assets(db,valid_manifest)
    doc['audiences'][0]['actions']+=['author','describe']
    receipt=install(db,doc)
    policy=policy_service(db)
    access=SourceAccessService(db.registry,policy.audit)
    product=PinnedProduct(manifest=manifest,binding_digest=bindings[0].digest,version_id='unsaved-version')
    return doc,receipt,policy,access,product


async def transient_request(db,policy,access,product,uow,request_id):
    await access.prelock(uow,(product,))
    decision=await policy.evaluate(principal(),'author',product,uow,correlation_id=request_id)
    return await access.resolve_transient(uow,product,principal(),decision,action='author',request_id=request_id)


def test_transient_finalization_requires_real_protected_tuple_and_durable_audit(registry_database,valid_manifest):
    db=registry_database;doc,installed,policy,access,product=transient_setup(db,valid_manifest)
    async def scenario():
        async with db.registry.transaction() as uow:
            request=await transient_request(db,policy,access,product,uow,str(uuid4()))
            receipt,=await access.finalize(uow,(request,))
            receipt.assert_uow(uow)
            assert receipt.approval_id==installed['id']
        with pytest.raises(ValueError):receipt.assert_uow(uow)
        return request.request_id
    request_id=asyncio.run(scenario())
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow' AND correlation_id=%s AND source_approval_id=%s",(request_id,installed['id']))==1
    assert db.scalar('SELECT count(*) FROM gateway.executions')==0


def test_revoked_finalization_rolls_back_then_audits_one_exact_correlated_denial(registry_database,valid_manifest):
    db=registry_database;doc,installed,policy,access,product=transient_setup(db,valid_manifest)
    async def prepare():
        async with db.registry.transaction() as uow:return await transient_request(db,policy,access,product,uow,str(uuid4()))
    request=asyncio.run(prepare())
    db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(installed['id'],installed['content_digest'],'synthetic-ended'))
    async def denied():
        async with db.registry.transaction() as uow:
            await access.prelock(uow,(product,))
            with pytest.raises(GatewayError) as caught:await access.finalize(uow,(request,))
            assert caught.value.request_id==request.request_id
    asyncio.run(denied())
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='deny' AND correlation_id=%s",(request.request_id,))==1
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow'")==0


def test_owner_resolution_does_not_lend_identity_or_client_and_audits_once(registry_database,valid_manifest):
    import psycopg
    from gateway.contracts.identity import Decision
    from tests.integration.conftest import connection_kwargs
    from tests.integration.test_source_approval_transactions import consumer_setup,insert_consumer
    db=registry_database;_,manifest,_,product=consumer_setup(db,valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:_,decision=insert_consumer(conn,manifest)
    access=SourceAccessService(db.registry,policy_service(db).audit)
    request_id=str(uuid4())
    before=db.scalar('SELECT count(*) FROM gateway.audit_events')
    async def scenario():
        async with db.registry.transaction() as uow:
            await access.prelock(uow,(product,))
            with pytest.raises(GatewayError) as caught:
                await access.resolve_owner(uow,ExecutionOwner(kind='execution',id='execution-1'),
                    principal().model_copy(update={'service_client_id':'another-client'}),Decision.model_validate(decision),
                    action='result',request_id=request_id)
            assert caught.value.request_id==request_id
        async with db.registry.transaction() as uow:
            with pytest.raises(GatewayError) as malformed:
                await access.resolve_owner(uow,ExecutionOwner(kind='execution',id='execution-1'),object(),Decision.model_validate(decision),
                    action='result',request_id=request_id)
            assert malformed.value.request_id!=request_id
    asyncio.run(scenario())
    assert db.scalar('SELECT count(*) FROM gateway.audit_events')==before+1
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='deny' AND correlation_id=%s",(request_id,))==1


def test_owner_finalization_returns_only_original_protected_reference(registry_database,valid_manifest):
    import psycopg
    from gateway.contracts.identity import Decision
    from tests.integration.conftest import connection_kwargs
    from tests.integration.test_source_approval_transactions import consumer_setup,insert_consumer
    db=registry_database;document,manifest,installed,product=consumer_setup(db,valid_manifest)
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as conn:_,decision=insert_consumer(conn,manifest)
    access=SourceAccessService(db.registry,policy_service(db).audit)
    audit_sql="SELECT coalesce(jsonb_agg(to_jsonb(e) ORDER BY id),'[]'::jsonb) FROM gateway.audit_events e"
    prior_audits=db.scalar(audit_sql)
    prior_ids={event['id'] for event in prior_audits}
    actor=principal()
    request_id=str(uuid4())
    async def scenario():
        async with db.registry.transaction() as uow:
            await access.prelock(uow,(product,))
            request=await access.resolve_owner(uow,ExecutionOwner(kind='execution',id='execution-1'),actor,Decision.model_validate(decision),action='result',request_id=request_id)
            receipt,=await access.finalize(uow,(request,))
            assert receipt.approval_id==installed['id']
    asyncio.run(scenario())
    current_audits=db.scalar(audit_sql)
    assert [event for event in current_audits if event['id'] in prior_ids]==prior_audits
    new_events=[event for event in current_audits if event['id'] not in prior_ids]
    assert len(new_events)==1
    event=new_events[0]
    assert (event['actor_id'],event['service_client_id'],event['action'],event['correlation_id'],event['decision']) == (
        actor.id,actor.service_client_id,'result',request_id,'allow')
    assert (event['source_approval_id'],event['source_approval_revision'],event['source_approval_digest']) == (
        installed['id'],document['revision'],installed['content_digest'])


def test_final_time_check_after_allow_audit_discards_allow_and_records_one_deny(registry_database,valid_manifest):
    from datetime import timezone
    db=registry_database
    doc,manifest,bindings,*_=approval_assets(db,valid_manifest)
    doc['audiences'][0]['actions'].append('author')
    expiry=db.scalar("SELECT clock_timestamp()+interval '1.2 seconds'")
    doc['expires_at']=expiry.astimezone(timezone.utc).isoformat().replace('+00:00','Z');install(db,doc)
    policy=policy_service(db)
    original=policy.audit.append
    async def delayed(uow,event):
        result=await original(uow,event)
        if event.decision=='allow':
            await uow.session.scalar(text("SELECT pg_sleep(greatest(0,extract(epoch FROM (CAST(:expiry AS timestamptz)-clock_timestamp()))+0.02))"),{'expiry':expiry})
        return result
    policy.audit.append=delayed
    access=SourceAccessService(db.registry,policy.audit)
    product=PinnedProduct(manifest=manifest,binding_digest=bindings[0].digest,version_id='unsaved-version')
    request_id=str(uuid4())
    async def scenario():
        async with db.registry.transaction() as uow:
            request=await transient_request(db,policy,access,product,uow,request_id)
            with pytest.raises(GatewayError) as caught:await access.finalize(uow,(request,))
            assert caught.value.request_id==request_id
    asyncio.run(scenario())
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow'")==0
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='deny' AND correlation_id=%s",(request_id,))==1


def test_failed_deny_audit_does_not_return_correlated_denial_receipt(registry_database,valid_manifest):
    db=registry_database;doc,installed,policy,access,product=transient_setup(db,valid_manifest)
    async def prepare():
        async with db.registry.transaction() as uow:return await transient_request(db,policy,access,product,uow,str(uuid4()))
    request=asyncio.run(prepare())
    db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(installed['id'],installed['content_digest'],'synthetic-ended'))
    async def reject(*args):raise RuntimeError('Synthetic unavailable audit')
    policy.audit.append=reject
    async def scenario():
        async with db.registry.transaction() as uow:
            await access.prelock(uow,(product,))
            with pytest.raises(GatewayError) as caught:await access.finalize(uow,(request,))
            assert caught.value.request_id!=request.request_id
            assert caught.value.code=='UPSTREAM_UNAVAILABLE'
    asyncio.run(scenario())
    assert db.scalar('SELECT count(*) FROM gateway.audit_events')==0


def test_direct_finalizer_null_and_non_app_calls_deny(registry_database):
    db=registry_database
    for value in (None,'null','[]','{}','[null]'):
        assert db.denied_as_app('SELECT gateway.source_finalize_access(%s::jsonb)',(value,))=='permission_denied'
    for role in ('source_reader','audit_retention'):
        assert not db.scalar('SELECT has_function_privilege(%s,%s,%s)',(db.roles[role],'gateway.source_finalize_access(jsonb)','EXECUTE'))


def test_batch_finalizer_rechecks_earlier_expiry_after_later_unexpired_lock(registry_database,valid_manifest):
    import copy,time,psycopg
    from datetime import timezone
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    from tests.integration.conftest import connection_kwargs
    db=registry_database;original,manifest,bindings,*_=approval_assets(db,valid_manifest)
    for attempt in range(64):
        short=copy.deepcopy(original);long=copy.deepcopy(original)
        expiry=db.scalar("SELECT clock_timestamp()+interval '2 seconds'")
        short['expires_at']=expiry.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
        short['audiences'][0]['actions']=['author'];long['audiences'][0]['actions']=['publish']
        short['review']['review_id']=f'short-final-{attempt}';long['review']['review_id']=f'long-final-{attempt}'
        first,second=install(db,short),install(db,long)
        if first['id']<second['id']:break
        for item in (first,second):db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',(item['id'],item['content_digest'],'synthetic-order'))
    else:pytest.fail('Could not select database-generated short-before-long UUID order')
    policy=policy_service(db);access=SourceAccessService(db.registry,policy.audit)
    product=PinnedProduct(manifest=manifest,binding_digest=bindings[0].digest,version_id='unsaved-version')
    pid=Queue();request_id=str(uuid4())
    def check():
        async def scenario():
            async with db.registry.transaction() as uow:
                await access.prelock(uow,(product,))
                requests=[]
                for action in ('author','publish'):
                    decision=await policy.evaluate(principal(),action,product,uow,correlation_id=request_id)
                    requests.append(await access.resolve_transient(uow,product,principal(),decision,action=action,request_id=request_id))
                pid.put(await uow.session.scalar(text('SELECT pg_backend_pid()')))
                with pytest.raises(GatewayError) as caught:await access.finalize(uow,tuple(requests))
                assert caught.value.request_id==request_id
        asyncio.run(scenario())
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as blocker,ThreadPoolExecutor(1) as pool:
        blocker.execute('SELECT 1 FROM gateway.source_access_approval_states WHERE approval_id=%s FOR UPDATE',(second['id'],))
        future=pool.submit(check);target=pid.get(timeout=3);limit=time.monotonic()+3
        while not db.scalar('SELECT cardinality(pg_blocking_pids(%s))>0',(target,)):
            assert time.monotonic()<limit,'finalizer never reached approval barrier'
            db.scalar('SELECT pg_sleep(0.005)')
        blocker.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))',(expiry,))
        assert blocker.execute('SELECT expires_at>clock_timestamp() FROM gateway.source_access_approvals WHERE id=%s',(second['id'],)).fetchone()[0] is True
        blocker.commit();future.result(timeout=4)
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='allow'")==0
    assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='deny' AND correlation_id=%s",(request_id,))==1
