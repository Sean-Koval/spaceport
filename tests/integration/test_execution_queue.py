"""Genuine PostgreSQL admission, process races, fencing and durable completion.

RecordingAdapter is only a source-call probe; these cases do not prove Cube
cancellation or real upstream queue lifetime. Database effects are never mocked.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib
import multiprocessing
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from gateway.adapters.base import StaticBindingResolver
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import Completed, Pending, PreparedQuery
from gateway.registry.models import BindingRevision, TestRun as RegistryTestRun
from tests.contract.test_cube_adapter import synthetic
from tests.integration.test_audit import seed as seed_policy, service as policy_service, principal
from tests.integration.test_registry import publish

pytestmark=pytest.mark.postgres


def api():
    try:
        return SimpleNamespace(**{name:importlib.import_module('gateway.execution.'+name)
                                  for name in ('service','queue','worker')})
    except ModuleNotFoundError as error:
        pytest.fail(f'Durable execution implementation is missing: {error.name}')


async def setup(database, *, product_timeout=None, product_wait=None, policy_limits=None):
    seed_policy(database, roles=('consumer',))
    binding, product, decision, request=synthetic()
    policy_revision=1
    if policy_limits is not None:
        policy_revision=install_policy(database,policy_limits)
    if product_timeout is not None or product_wait is not None:
        from gateway.contracts.manifest import Manifest
        data=product.manifest.model_dump(mode='json')
        if product_timeout is not None: data['limits']['execution_timeout_seconds']=product_timeout
        if product_wait is not None: data['limits']['sync_wait_seconds']=product_wait
        data['content_digest']=None
        product=product.model_copy(update={'manifest':Manifest.model_validate(data)})
    registry=database.registry
    async with registry.transaction() as uow:
        uow.session.add(BindingRevision(id='binding-1', binding_id=binding.binding_id, revision=binding.revision,
            content=binding.model_dump(mode='json'), content_digest=binding.digest,
            model_bundle_digest=binding.model_bundle_digest,adapter_revision=binding.adapter_revision,actor_id='operator'))
    draft=await registry.save_draft('revenue',product.manifest,0,'editor')
    async with registry.transaction() as uow:
        uow.session.add(RegistryTestRun(id='test-1',product_id='revenue',draft_id=draft.id,
            draft_digest=draft.content_digest,binding_id='binding-1',binding_digest=binding.digest,
            policy_id='revenue-access',policy_revision=policy_revision,fixture_version='1',evidence={'synthetic':'passed'},
            status='passed',actor_id='tester'))
    await publish(registry,draft)
    return make_service(database),request


def install_policy(database,limits):
    from tests.unit.test_policy import rules
    from psycopg.types.json import Jsonb
    revision=database.scalar("SELECT current_revision FROM gateway.policies WHERE id='revenue-access'")+1
    content={'rules':rules(limits=limits),'reason':'Reviewed tighter synthetic budget'}
    database.execute_owner("INSERT INTO gateway.policy_revisions(id,policy_id,revision,content,content_digest,actor_id) VALUES (%s,'revenue-access',%s,%s,%s,'operator')",
        (f'policy-{revision}',revision,Jsonb(content),canonical_digest(content)))
    database.execute_owner("UPDATE gateway.policies SET current_revision=%s WHERE id='revenue-access'",(revision,))
    return revision


def make_service(database):
    return api().service.ExecutionService(registry=database.registry,policy=policy_service(database),
        bindings=StaticBindingResolver([synthetic()[0]]))


class RecordingAdapter:
    def __init__(self, *, poll=None, prepare=None):
        self.calls=[]
        self._poll=poll
        self._prepare=prepare
    async def prepare(self, product, request, decision, *, context, deadline, request_id):
        self.calls.append(('prepare',deadline,decision))
        if self._prepare: await self._prepare()
        return PreparedQuery(endpoint_id='synthetic-consumer',model_bundle_digest=product.manifest.model_bundle_digest,
            cube_query={'measures':['Orders.revenue'],'dimensions':['Orders.segment'],
                'timeDimensions':[{'dimension':'Orders.ordered_at','granularity':'day'}],
                'filters':[],'order':[],'timezone':'America/New_York','limit':1001})
    async def poll(self, query, decision, *, context, deadline, request_id):
        self.calls.append(('poll',deadline,decision))
        if self._poll: return await self._poll()
        return Completed(raw_rows=[], source_watermark=datetime.now(timezone.utc))
    async def close(self): pass


def test_same_key_same_input_replays_and_changed_input_conflicts(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        request=request.model_copy(update={'idempotency_key':'one'})
        first=await service.submit(principal(),request)
        second=await service.submit(principal(),request)
        assert first.execution_id==second.execution_id
        with pytest.raises(GatewayError) as caught:
            await service.submit(principal(),request.model_copy(update={'inputs':{'start':'2024-03-10','end':'2024-03-12'}}))
        assert caught.value.code=='CONFLICT'
        assert registry_database.scalar('SELECT count(*) FROM gateway.executions')==1
    asyncio.run(scenario())


@pytest.mark.parametrize('source',['default','product','policy'])
def test_acceptance_uses_one_database_instant_even_after_transaction_delay(registry_database,source):
    async def scenario():
        options={'product_timeout':10} if source=='product' else {'policy_limits':{'execution_timeout_seconds':10}} if source=='policy' else {}
        expected=120 if source=='default' else 10
        service,request=await setup(registry_database,**options)
        transaction_time=None
        authorize=service.policy.authorize
        async def delayed_authorize(*args, **kwargs):
            nonlocal transaction_time
            from sqlalchemy import func
            uow=args[3]
            decision=await authorize(*args, **kwargs)
            transaction_time=await uow.session.scalar(select(func.now()))
            await uow.session.scalar(select(func.pg_sleep(.03)))
            return decision
        service.policy.authorize=delayed_authorize
        view=await service.submit(principal(),request)
        assert view.accepted_at-transaction_time>=timedelta(seconds=.03)
        assert registry_database.scalar('SELECT deadline_at-created_at FROM gateway.executions WHERE id=%s',(view.execution_id,))==timedelta(seconds=expected)
        assert registry_database.scalar('SELECT created_at FROM gateway.executions WHERE id=%s',(view.execution_id,))==view.accepted_at
    asyncio.run(scenario())


def test_pending_principal_capacity_is_ten_and_replay_does_not_consume_slot(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        views=[]
        for i in range(10): views.append(await service.submit(principal(),request.model_copy(update={'idempotency_key':str(i)})))
        replay=await service.submit(principal(),request.model_copy(update={'idempotency_key':'0'}))
        assert replay.execution_id==views[0].execution_id
        with pytest.raises(GatewayError) as caught: await service.submit(principal(),request)
        assert caught.value.code=='RESOURCE_LIMIT'
    asyncio.run(scenario())


def test_claim_reserves_source_without_server_owned_insert_privileges(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        view=await service.submit(principal(),request)
        for column in ('released_at','release_reason','created_at'):
            assert registry_database.scalar(
                "SELECT has_column_privilege(%s,'gateway.source_reservations',%s,'INSERT')",
                (registry_database.roles['app'],column)) is False
        claim=await service.queue.claim('worker-1')
        assert claim is not None, (await service.queue.view(view.execution_id)).model_dump(mode='json')
        assert claim.execution_id==view.execution_id
        assert registry_database.scalar(
            'SELECT released_at IS NULL AND release_reason IS NULL FROM gateway.source_reservations WHERE id=%s',
            (claim.reservation_id,)) is True
        assert registry_database.denied_as_app(
            'UPDATE gateway.source_reservations SET released_at=clock_timestamp() WHERE id=%s',
            (claim.reservation_id,))=='permission_denied'
    asyncio.run(scenario())


def test_worker_persists_complete_result_and_releases_only_own_source_reservation(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        view=await service.submit(principal(),request)
        adapter=RecordingAdapter()
        worker=api().worker.Worker(service.queue,adapter_factory=lambda:adapter,worker_id='worker-1')
        assert await worker.run_once() is True
        assert (await service.queue.view(view.execution_id)).state=='succeeded'
        assert registry_database.scalar('SELECT envelope->>\'state\' FROM gateway.results WHERE execution_id=%s',(view.execution_id,))=='succeeded'
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==0
        assert len({call[1] for call in adapter.calls})==1
        assert [call[0] for call in adapter.calls]==['prepare','poll']
    asyncio.run(scenario())


def test_dead_worker_reclaim_fences_old_completion_and_retains_old_source_slot(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        view=await service.submit(principal(),request)
        old=await service.queue.claim('dead-worker')
        registry_database.execute_owner("UPDATE gateway.execution_leases SET expires_at=clock_timestamp()-interval '1 second'")
        new=await service.queue.claim('replacement-worker')
        assert new.execution_id==old.execution_id
        assert new.generation==old.generation+1 and new.lease_token!=old.lease_token
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==2
        assert await service.queue.fail(old,'EXECUTION_TIMEOUT',source_completed=True) is False
        assert (await service.queue.view(view.execution_id)).state=='running'
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==2
        assert await service.queue.fail(new,'UPSTREAM_UNAVAILABLE',source_completed=True) is True
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==1
    asyncio.run(scenario())


def test_gateway_timeout_never_releases_uncertain_source_capacity(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        view=await service.submit(principal(),request)
        claim=await service.queue.claim('worker-1')
        assert await service.queue.fail(claim,'EXECUTION_TIMEOUT')
        assert (await service.queue.view(view.execution_id)).state=='timed_out'
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==1
    asyncio.run(scenario())


def test_immutable_authority_and_prepared_query_privileges(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        await service.submit(principal(),request)
        assert registry_database.denied_as_app("UPDATE gateway.execution_authorizations SET decision='{}'")=='permission_denied'
        assert registry_database.denied_as_app('DELETE FROM gateway.execution_authorizations')=='permission_denied'
        assert registry_database.denied_as_app("UPDATE gateway.execution_preparations SET prepared_query='{}'")=='permission_denied'
        assert registry_database.denied_as_app('DELETE FROM gateway.source_reservations')=='permission_denied'
        assert registry_database.denied_as_source('SELECT * FROM gateway.execution_authorizations')=='permission_denied'
    asyncio.run(scenario())


def _claim_process(url, start, output, worker_id):
    from gateway.registry.repository import Registry
    from gateway.policy.service import PolicyService
    from gateway.policy.audit import AuditWriter
    from gateway.auth.principals import PrincipalStore
    async def run():
        registry=Registry(url)
        policy=PolicyService(registry,PrincipalStore(registry.sessions),AuditWriter(b'synthetic-operator-key-32-bytes!!!'))
        queue=api().queue.ExecutionQueue(registry,policy,StaticBindingResolver([synthetic()[0]]))
        claimed=[]
        for _ in range(20):
            item=await queue.claim(worker_id)
            if item is None: break
            claimed.append(item.execution_id)
        await registry.close()
        return claimed
    start.wait(15)
    try: output.send(('ok',asyncio.run(run())))
    except Exception as error: output.send(('error',type(error).__name__))
    finally: output.close()


def test_three_worker_processes_enforce_two_principal_ten_deployment(registry_database):
    async def seed():
        service,request=await setup(registry_database)
        from gateway.contracts.identity import Principal
        for index in range(6):
            name=f'principal-{index}'
            registry_database.execute_owner("INSERT INTO gateway.principals(id,subject,kind) VALUES (%s,%s,'demo')",(name,name))
            registry_database.execute_owner("INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) VALUES (%s,'revenue-access','north','[\"consumer\"]','{\"groups\":[\"north\"]}')",(name,))
            for _ in range(3):
                await service.submit(Principal(id=name,subject=name,kind='demo',service_client_id=None),request)
    asyncio.run(seed())
    context=multiprocessing.get_context('spawn')
    start=context.Event()
    children=[]
    for index in range(3):
        receiver,sender=context.Pipe(duplex=False)
        process=context.Process(target=_claim_process,args=(registry_database.urls['app'].render_as_string(hide_password=False),start,sender,f'worker-{index}'))
        process.start(); sender.close(); children.append((process,receiver))
    start.set()
    results=[]
    try:
        for process,receiver in children:
            assert receiver.poll(45),'Worker process did not return'
            status,ids=receiver.recv(); assert status=='ok',ids
            results.extend(ids)
            process.join(5); assert process.exitcode==0
    finally:
        for process,receiver in children:
            if process.is_alive(): process.terminate(); process.join(5)
            receiver.close()
    assert len(results)==len(set(results))==10
    assert registry_database.scalar("SELECT max(n) FROM (SELECT principal_id,count(*) n FROM gateway.source_reservations WHERE released_at IS NULL GROUP BY principal_id) s")==2
    assert registry_database.scalar("SELECT count(*) FROM gateway.execution_states WHERE state='running'")==10


def test_direct_sql_acceptance_clock_is_database_owned_and_bounded(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        await service.submit(principal(),request)
        assert registry_database.denied_as_app("UPDATE gateway.executions SET created_at=clock_timestamp()")=='permission_denied'
        registry_database.execute_app("INSERT INTO gateway.executions(id,principal_id,product_id,version_id,policy_id,policy_revision,canonical_query,canonical_hash,context,request_id,deadline_at) SELECT 'direct-copy',principal_id,product_id,version_id,policy_id,policy_revision,canonical_query,canonical_hash,context,'direct-request',clock_timestamp()+interval '1 year' FROM gateway.executions LIMIT 1")
        assert registry_database.scalar("SELECT deadline_at-created_at FROM gateway.executions WHERE id='direct-copy'")==timedelta(seconds=120)
    asyncio.run(scenario())


def test_reservation_principal_fence_and_one_way_release_are_database_enforced(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        await service.submit(principal(),request)
        claim=await service.queue.claim('worker-1')
        registry_database.execute_owner("INSERT INTO gateway.principals(id,subject,kind) VALUES ('other','other','demo')")
        assert registry_database.constraint_failure("INSERT INTO gateway.source_reservations(id,execution_id,principal_id,lease_token,generation) VALUES ('forged',%s,'other','forged-token',2)",(claim.execution_id,))=='23503'
        assert registry_database.denied_as_app("UPDATE gateway.source_reservations SET lease_token='forged'")=='permission_denied'
        assert registry_database.denied_as_app('UPDATE gateway.source_reservations SET created_at=clock_timestamp()')=='permission_denied'
        registry_database.execute_owner("UPDATE gateway.execution_leases SET lease_token='new-token',generation=generation+1")
        assert registry_database.constraint_failure("UPDATE gateway.source_reservations SET release_reason='completed'")=='23514'
        registry_database.execute_owner('UPDATE gateway.execution_leases SET lease_token=%s,generation=%s',(claim.lease_token,claim.generation))
        registry_database.execute_app("UPDATE gateway.source_reservations SET release_reason='completed'")
        assert registry_database.scalar('SELECT released_at IS NOT NULL FROM gateway.source_reservations')
        assert registry_database.constraint_failure("UPDATE gateway.source_reservations SET release_reason='completed'")=='23514'
    asyncio.run(scenario())


def test_concurrent_same_key_admission_has_one_durable_execution(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        request=request.model_copy(update={'idempotency_key':'concurrent'})
        views=await asyncio.gather(*(service.submit(principal(),request) for _ in range(8)))
        assert len({view.execution_id for view in views})==1
        assert registry_database.scalar('SELECT count(*) FROM gateway.executions')==1
        assert registry_database.scalar('SELECT count(*) FROM gateway.execution_authorizations')==1
    asyncio.run(scenario())


def test_product_pointer_changes_do_not_repin_accepted_work(registry_database):
    async def scenario():
        from gateway.contracts.manifest import Manifest
        service,request=await setup(registry_database)
        accepted=await service.submit(principal(),request.model_copy(update={'version':None}))
        binding,old,_,_=synthetic()
        data=old.manifest.model_dump(mode='json');data.update(version='1.0.1',content_digest=None)
        new=Manifest.model_validate(data)
        draft=await registry_database.registry.save_draft('revenue',new,1,'editor')
        async with registry_database.registry.transaction() as uow:
            uow.session.add(RegistryTestRun(id='test-2',product_id='revenue',draft_id=draft.id,draft_digest=draft.content_digest,
                binding_id='binding-1',binding_digest=binding.digest,policy_id='revenue-access',policy_revision=1,
                fixture_version='1',evidence={'synthetic':'passed'},status='passed',actor_id='tester'))
        await publish(registry_database.registry,draft,version_id='version-2',test_run_id='test-2')
        assert (await registry_database.registry.resolve('revenue')).version_id=='version-2'
        claim=await service.queue.claim('worker-1')
        assert claim.execution_id==accepted.execution_id
        assert claim.spec.pinned_product.version_id=='version-1'
        assert claim.spec.canonical_query['version']=='1.0.0'
    asyncio.run(scenario())


def test_pending_deployment_capacity_is_one_hundred(registry_database):
    async def scenario():
        from gateway.contracts.identity import Principal
        service,request=await setup(registry_database)
        for index in range(11):
            name=f'principal-{index}'
            registry_database.execute_owner("INSERT INTO gateway.principals(id,subject,kind) VALUES (%s,%s,'demo')",(name,name))
            registry_database.execute_owner("INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) VALUES (%s,'revenue-access','north','[\"consumer\"]','{\"groups\":[\"north\"]}')",(name,))
            actor=Principal(id=name,subject=name,kind='demo',service_client_id=None)
            if index<10:
                for _ in range(10): await service.submit(actor,request)
            else:
                with pytest.raises(GatewayError) as caught: await service.submit(actor,request)
                assert caught.value.code=='RESOURCE_LIMIT'
        assert registry_database.scalar("SELECT count(*) FROM gateway.execution_states WHERE state='accepted'")==100
    asyncio.run(scenario())


def test_completed_after_terminal_timeout_releases_only_current_attempt_without_rows(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        await service.submit(principal(),request)
        claim=await service.queue.claim('worker-1')
        assert await service.queue.fail(claim,'EXECUTION_TIMEOUT')
        assert await service.queue.fail(claim,'EXECUTION_TIMEOUT',source_completed=True)
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==0
        assert registry_database.scalar('SELECT count(*) FROM gateway.results')==0
        assert registry_database.scalar('SELECT state FROM gateway.execution_states')=='timed_out'
    asyncio.run(scenario())


def test_reclaimed_attempt_reuses_immutable_prepared_query(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        await service.submit(principal(),request)
        old=await service.queue.claim('worker-1')
        adapter=RecordingAdapter()
        prepared=await adapter.prepare(old.spec.pinned_product,request,old.executed_decision,
            context='consumer',deadline=old.deadline,request_id=old.spec.request_id)
        await service.queue.save_prepared(old,prepared)
        registry_database.execute_owner("UPDATE gateway.execution_leases SET expires_at=clock_timestamp()-interval '1 second'")
        replacement=await service.queue.claim('worker-2')
        assert replacement.prepared==prepared
        assert replacement.deadline==old.deadline
        assert replacement.spec==old.spec
    asyncio.run(scenario())


def test_shared_capacity_views_are_invoker_only_and_include_held_attempts(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        await service.submit(principal(),request)
        async with registry_database.registry.transaction() as uow:
            await api().queue.lock_execution_capacity(uow)
            assert await api().queue.pending_capacity_counts(uow,'north-user')==(1,1)
            assert await api().queue.source_capacity_counts(uow,'north-user')==(0,0)
        claim=await service.queue.claim('worker-1')
        await service.queue.fail(claim,'EXECUTION_TIMEOUT')
        async with registry_database.registry.transaction() as uow:
            await api().queue.lock_execution_capacity(uow)
            assert await api().queue.pending_capacity_counts(uow,'north-user')==(0,0)
            assert await api().queue.source_capacity_counts(uow,'north-user')==(1,1)
        assert registry_database.scalar("SELECT bool_and('security_invoker=true'=ANY(reloptions)) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='gateway' AND c.relname IN ('held_source_capacity','pending_execution_capacity')")
        assert registry_database.denied_as_source('SELECT * FROM gateway.held_source_capacity')=='permission_denied'
        assert registry_database.denied_as_retention('SELECT * FROM gateway.pending_execution_capacity')=='permission_denied'
    asyncio.run(scenario())


from tests.integration.test_cube_compatibility import cube_stack


@pytest.mark.cube
def test_actual_worker_approved_orders_persists_lossless_complete_result(registry_database,cube_stack):
    """Real registry+adapter+Cube composition, no guaranteed slow/pending response."""
    from decimal import Decimal
    from tests.integration.test_cube_adapter_runtime import configured
    async def scenario():
        service,request=await setup(registry_database)
        accepted=await service.submit(principal(),request)
        client,_,_,_,_=configured(cube_stack)
        class ObservedAdapter:
            pending=0
            async def prepare(self,*args,**kwargs): return await client.prepare(*args,**kwargs)
            async def poll(self,*args,**kwargs):
                value=await client.poll(*args,**kwargs)
                if isinstance(value,Pending): self.pending+=1
                return value
            async def close(self): await client.close()
        adapter=ObservedAdapter()
        await api().worker.Worker(service.queue,adapter_factory=lambda:adapter,worker_id='actual-orders-worker').run_once()
        view=await service.queue.view(accepted.execution_id)
        assert view.state=='succeeded',view.error
        envelope=registry_database.scalar('SELECT envelope FROM gateway.results WHERE execution_id=%s',(accepted.execution_id,))
        assert envelope['row_count']==3 and envelope['truncated'] is False
        assert sum((Decimal(row['revenue']) for row in envelope['rows']),Decimal(0))==Decimal('9007199254741023.31')
        assert envelope['source_watermark'] is not None and envelope['freshness']['status']=='fresh'
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==0
        cube_stack.capture('task7-worker-orders.json',{'execution_id':accepted.execution_id,'state':view.state,
            'pending_count':adapter.pending,'row_count':envelope['row_count'],'rows':envelope['rows'],
            'source_watermark':envelope['source_watermark'],'truncated':envelope['truncated'],
            'live_pending_observed':adapter.pending>0})
    asyncio.run(scenario())


def _die_after_source_submission_process(url,output):
    from gateway.registry.repository import Registry
    from gateway.policy.service import PolicyService
    from gateway.policy.audit import AuditWriter
    from gateway.auth.principals import PrincipalStore
    async def run():
        registry=Registry(url)
        policy=PolicyService(registry,PrincipalStore(registry.sessions),AuditWriter(b'synthetic-operator-key-32-bytes!!!'))
        queue=api().queue.ExecutionQueue(registry,policy,StaticBindingResolver([synthetic()[0]]))
        from gateway.registry.models import ExecutionLease, ExecutionPreparation
        class SubmittedAdapter(RecordingAdapter):
            async def poll(self,query,decision,**kwargs):
                # A synthetic source-call probe, not proof of Cube admission or
                # cancellation. The real worker already committed preparation.
                async with registry.transaction() as uow:
                    lease=await uow.session.scalar(select(ExecutionLease))
                    prepared=await uow.session.get(ExecutionPreparation,lease.execution_id)
                    assert prepared is not None
                    output.send((lease.execution_id,lease.lease_token,lease.generation))
                await asyncio.sleep(60)
                raise AssertionError('The parent must terminate this process')
        await api().worker.Worker(queue,adapter_factory=SubmittedAdapter,worker_id='doomed-worker').run_once()
    asyncio.run(run())


def test_real_worker_process_death_preserves_committed_attempt_for_recovery(registry_database):
    async def seed():
        service,request=await setup(registry_database)
        return (await service.submit(principal(),request)).execution_id
    execution_id=asyncio.run(seed())
    context=multiprocessing.get_context('spawn')
    receiver,sender=context.Pipe(duplex=False)
    process=context.Process(target=_die_after_source_submission_process,args=(registry_database.urls['app'].render_as_string(hide_password=False),sender))
    process.start();sender.close()
    try:
        assert receiver.poll(30)
        old_id,old_token,generation=receiver.recv()
        assert old_id==execution_id
        assert registry_database.scalar('SELECT count(*) FROM gateway.execution_preparations')==1
        process.terminate();process.join(10)
        assert process.exitcode is not None and process.exitcode!=0
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==1
        # Owner fixture accelerates the DB lease clock; it does not claim source
        # completion, cancellation, or a real 30-second elapsed wait.
        registry_database.execute_owner("UPDATE gateway.execution_leases SET expires_at=clock_timestamp()-interval '1 second'")
        async def reclaim(): return await make_service(registry_database).queue.claim('recovered-worker')
        replacement=asyncio.run(reclaim())
        assert replacement.generation==generation+1 and replacement.lease_token!=old_token
        assert replacement.prepared is not None
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==2
    finally:
        if process.is_alive():process.terminate();process.join(10)
        receiver.close()


def rewind_execution(database, execution_id, *, seconds):
    """Owner-only historical fixture; preserve the accepted/deadline interval."""
    import psycopg
    from tests.integration.conftest import connection_kwargs
    delta=timedelta(seconds=seconds)
    # Bound parameters use PostgreSQL's extended protocol, which accepts only
    # one command per execute. Keep all three commands in one transaction:
    # success restores the trigger, and any failure rolls its disable back.
    with psycopg.connect(**connection_kwargs(database.urls['owner'])) as connection:
        connection.execute('ALTER TABLE gateway.executions DISABLE TRIGGER immutable_content')
        connection.execute('UPDATE gateway.executions SET created_at=created_at-%s, '
            'deadline_at=deadline_at-%s WHERE id=%s',(delta,delta,execution_id))
        connection.execute('ALTER TABLE gateway.executions ENABLE TRIGGER immutable_content')


def test_expiry_commits_before_claim_policy_lock_to_avoid_result_read_deadlock(registry_database):
    async def scenario():
        from gateway.registry.models import ExecutionState
        service,request=await setup(registry_database)
        expired=await service.submit(principal(),request)
        live=await service.submit(principal(),request)
        # Disposable-owner-only historical fixture. No app privilege is widened;
        # keep the accepted/deadline interval exactly120s and restore the trigger.
        rewind_execution(registry_database,expired.execution_id,seconds=121)
        policy_locked=asyncio.Event();expired_state_locked=asyncio.Event();continue_claim=asyncio.Event()
        actual_expire=service.queue._expire
        async def coordinated_expire(uow):
            await actual_expire(uow)
            expired_state_locked.set()
            await continue_claim.wait()
        service.queue._expire=coordinated_expire
        async def result_reader():
            async with registry_database.registry.transaction() as uow:
                await uow.lock_policy('revenue-access')
                policy_locked.set()
                await expired_state_locked.wait()
                continue_claim.set()
                state=await uow.session.scalar(select(ExecutionState).where(ExecutionState.execution_id==expired.execution_id).with_for_update())
                assert state.state=='timed_out'
        async def claimant():
            await policy_locked.wait()
            claim=await service.queue.claim('worker-1')
            assert claim.execution_id==live.execution_id
        await asyncio.wait_for(asyncio.gather(result_reader(),claimant()),timeout=10)
    asyncio.run(scenario())


def test_idempotent_replay_observes_elapsed_database_deadline(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        request=request.model_copy(update={'idempotency_key':'elapsed'})
        accepted=await service.submit(principal(),request)
        rewind_execution(registry_database,accepted.execution_id,seconds=121)
        replay=await service.submit(principal(),request)
        assert replay.execution_id==accepted.execution_id and replay.state=='timed_out'
        assert replay.error.code=='EXECUTION_TIMEOUT'
    asyncio.run(scenario())


def test_source_release_locks_lease_fence_until_competing_reclaim_commits(registry_database):
    async def scenario():
        import psycopg
        from gateway.registry.models import ExecutionLease
        from tests.integration.conftest import connection_kwargs
        service,request=await setup(registry_database)
        await service.submit(principal(),request)
        claim=await service.queue.claim('worker-1')
        release_pid=asyncio.Future()
        async def direct_release():
            try:
                async with await psycopg.AsyncConnection.connect(**connection_kwargs(registry_database.urls['app'])) as connection:
                    cursor=await connection.execute('SELECT pg_backend_pid()')
                    release_pid.set_result((await cursor.fetchone())[0])
                    await connection.execute("UPDATE gateway.source_reservations SET release_reason='completed' WHERE execution_id=%s",(claim.execution_id,))
                return 'unexpectedly_allowed'
            except psycopg.Error as error: return error.sqlstate
        async with registry_database.registry.transaction() as changing:
            lease=await changing.session.scalar(select(ExecutionLease).where(ExecutionLease.execution_id==claim.execution_id).with_for_update())
            lease.lease_token='replacement-token';lease.generation+=1
            await changing.session.flush()
            from sqlalchemy import func
            blocker=await changing.session.scalar(select(func.pg_backend_pid()))
            release=asyncio.create_task(direct_release())
            pid=await release_pid
            observed=False
            for _ in range(100):
                if registry_database.scalar('SELECT %s=ANY(pg_blocking_pids(%s))',(blocker,pid)):
                    observed=True;break
                if release.done():break
                await asyncio.sleep(.05)
            assert observed,'Source release did not lock the lease fence'
        assert await asyncio.wait_for(release,timeout=10)=='23514'
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==1
    asyncio.run(scenario())


@pytest.mark.parametrize('source',['product','policy'])
def test_tight_execution_budget_is_database_owned_and_survives_reclaim(registry_database,source):
    async def scenario():
        options={'product_timeout':10} if source=='product' else {'policy_limits':{'execution_timeout_seconds':10}}
        service,request=await setup(registry_database,**options)
        accepted=await service.submit(principal(),request.model_copy(update={'idempotency_key':'tight'}))
        assert registry_database.scalar('SELECT execution_timeout_seconds FROM gateway.executions')==10
        assert registry_database.scalar('SELECT deadline_at-created_at FROM gateway.executions')==timedelta(seconds=10)
        original=await service.queue.claim('worker-1')
        registry_database.execute_owner("UPDATE gateway.execution_leases SET expires_at=clock_timestamp()-interval '1 second'")
        recovered=await service.queue.claim('worker-2')
        assert recovered.execution_id==accepted.execution_id
        assert recovered.deadline==original.deadline==accepted.accepted_at+timedelta(seconds=10)
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==2
    asyncio.run(scenario())


def test_tight_policy_cap_clamps_direct_sql_default_and_explicit_extension(registry_database):
    async def scenario():
        service,request=await setup(registry_database,policy_limits={'execution_timeout_seconds':10})
        await service.submit(principal(),request)
        for name,column,value in [('omitted','',''),('explicit',',execution_timeout_seconds',',120')]:
            registry_database.execute_app(f"INSERT INTO gateway.executions(id,principal_id,product_id,version_id,policy_id,policy_revision,canonical_query,canonical_hash,context,request_id,deadline_at{column}) SELECT %s,principal_id,product_id,version_id,policy_id,policy_revision,canonical_query,canonical_hash,context,'direct-request',clock_timestamp()+interval '1 year'{value} FROM gateway.executions WHERE id=(SELECT id FROM gateway.executions ORDER BY created_at LIMIT 1)",(name,))
            assert registry_database.scalar('SELECT execution_timeout_seconds FROM gateway.executions WHERE id=%s',(name,))==10
            assert registry_database.scalar('SELECT deadline_at-created_at FROM gateway.executions WHERE id=%s',(name,))==timedelta(seconds=10)
        assert registry_database.denied_as_app('UPDATE gateway.executions SET execution_timeout_seconds=120')=='permission_denied'
        assert registry_database.denied_as_app("UPDATE gateway.executions SET deadline_at=created_at+interval '120 seconds'")=='permission_denied'
    asyncio.run(scenario())


@pytest.mark.parametrize('limits',[{'execution_timeout_seconds':True},{'execution_timeout_seconds':1.5},
    {'execution_timeout_seconds':'10'},{'execution_timeout_seconds':None},
    {'execution_timeout_seconds':0},{'execution_timeout_seconds':121},
    {'unknown_timeout':10},True,1.5])
def test_direct_sql_rejects_malformed_policy_time_budget(registry_database,limits):
    async def scenario():
        service,request=await setup(registry_database)
        await service.submit(principal(),request)
        revision=install_policy(registry_database,limits)
        assert registry_database.constraint_failure("INSERT INTO gateway.executions(id,principal_id,product_id,version_id,policy_id,policy_revision,canonical_query,canonical_hash,context,request_id,deadline_at,execution_timeout_seconds) SELECT 'malformed',principal_id,product_id,version_id,policy_id,%s,canonical_query,canonical_hash,context,'direct-request',clock_timestamp()+interval '120 seconds',120 FROM gateway.executions LIMIT 1",(revision,))=='23514'
    asyncio.run(scenario())


def test_direct_sql_stale_policy_revision_cannot_bypass_tighter_cap(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        await service.submit(principal(),request)
        install_policy(registry_database,{'execution_timeout_seconds':10})
        assert registry_database.constraint_failure("INSERT INTO gateway.executions(id,principal_id,product_id,version_id,policy_id,policy_revision,canonical_query,canonical_hash,context,request_id,deadline_at,execution_timeout_seconds) SELECT 'stale',principal_id,product_id,version_id,policy_id,policy_revision,canonical_query,canonical_hash,context,'direct-request',clock_timestamp()+interval '120 seconds',120 FROM gateway.executions LIMIT 1")=='23514'
    asyncio.run(scenario())


def test_tight_timeout_replay_expires_at_original_acceptance(registry_database):
    async def scenario():
        service,request=await setup(registry_database,product_timeout=10)
        request=request.model_copy(update={'idempotency_key':'tight-elapsed'})
        accepted=await service.submit(principal(),request)
        claim=await service.queue.claim('worker-1')
        # Historical disposable-owner fixture, preserving the exact ten seconds.
        rewind_execution(registry_database,accepted.execution_id,seconds=11)
        replay=await service.submit(principal(),request)
        assert replay.execution_id==accepted.execution_id and replay.state=='timed_out'
        assert replay.error.code=='EXECUTION_TIMEOUT'
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==1
        assert await service.queue.claim('worker-2') is None
    asyncio.run(scenario())


def test_zero_authorized_wait_replay_does_not_poll_or_release_capacity(registry_database):
    async def scenario():
        service,request=await setup(registry_database,product_wait=0)
        request=request.model_copy(update={'idempotency_key':'zero-wait'})
        accepted=await service.submit(principal(),request)
        claim=await service.queue.claim('worker-1')
        async def forbidden_poll(execution_id): raise AssertionError('Zero wait must do no polling I/O')
        service.queue.wait_authorized=forbidden_poll
        replay=await service.submit_and_wait(principal(),request,seconds=100)
        assert replay.execution_id==accepted.execution_id and replay.state=='running'
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==1
        assert registry_database.scalar('SELECT lease_token FROM gateway.execution_leases')==claim.lease_token
    asyncio.run(scenario())


def test_positive_authorized_wait_rechecks_revocation_without_releasing_reservation(registry_database):
    async def scenario():
        service,request=await setup(registry_database,product_wait=1)
        request=request.model_copy(update={'idempotency_key':'revoked-wait'})
        accepted=await service.submit(principal(),request)
        await service.queue.claim('worker-1')
        original=service.queue.wait_authorized
        async def revoke_then_read(execution_id):
            registry_database.execute_owner('UPDATE gateway.principal_grants SET active=false')
            return await original(execution_id)
        service.queue.wait_authorized=revoke_then_read
        with pytest.raises(GatewayError) as caught: await service.submit_and_wait(principal(),request)
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==1
        assert registry_database.scalar('SELECT state FROM gateway.execution_states')=='running'
    asyncio.run(scenario())


@pytest.mark.parametrize('source',['product','policy'])
def test_one_second_wait_caps_new_submission_and_replay_without_capacity_release(registry_database,source):
    async def scenario():
        from time import monotonic
        options={'product_wait':1} if source=='product' else {'policy_limits':{'sync_wait_seconds':1}}
        service,request=await setup(registry_database,**options)
        request=request.model_copy(update={'idempotency_key':'one-second'})
        start=monotonic()
        accepted=await service.submit_and_wait(principal(),request,seconds=100)
        # Includes ordinary local admission overhead; polling itself is bounded
        # from pre-commit authority issuance, verified separately in unit tests.
        assert monotonic()-start<1.5
        claim=await service.queue.claim('worker-1')
        start=monotonic()
        replay=await service.submit_and_wait(principal(),request)
        assert monotonic()-start<1.5
        assert replay.execution_id==accepted.execution_id and replay.state=='running'
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==1
        assert registry_database.scalar('SELECT lease_token FROM gateway.execution_leases')==claim.lease_token
    asyncio.run(scenario())


def test_authority_duration_must_match_database_clamped_execution(registry_database):
    async def scenario():
        service,request=await setup(registry_database,policy_limits={'execution_timeout_seconds':10})
        await service.submit(principal(),request)
        registry_database.execute_app("INSERT INTO gateway.executions(id,principal_id,product_id,version_id,policy_id,policy_revision,canonical_query,canonical_hash,context,request_id,deadline_at,execution_timeout_seconds) SELECT 'authority-copy',principal_id,product_id,version_id,policy_id,policy_revision,canonical_query,canonical_hash,context,'direct-request',clock_timestamp()+interval '120 seconds',120 FROM gateway.executions LIMIT 1")
        assert registry_database.constraint_failure("INSERT INTO gateway.execution_authorizations(execution_id,principal_snapshot,decision,scope_digest,full_decision_digest) SELECT 'authority-copy',principal_snapshot,jsonb_set(decision,'{limits,execution_timeout_seconds}','120'),scope_digest,full_decision_digest FROM gateway.execution_authorizations LIMIT 1")=='23514'
        assert registry_database.scalar("SELECT execution_timeout_seconds FROM gateway.executions WHERE id='authority-copy'")==10
    asyncio.run(scenario())


def test_direct_sql_cannot_use_unrelated_wider_policy_for_product(registry_database):
    async def scenario():
        service,request=await setup(registry_database,policy_limits={'execution_timeout_seconds':10})
        await service.submit(principal(),request)
        from gateway.registry.models import PolicyRow,PolicyRevision
        from tests.unit.test_policy import rules
        content={'rules':rules(),'reason':'Separate synthetic product policy'}
        async with registry_database.registry.transaction() as uow:
            uow.session.add(PolicyRow(id='other-access',current_revision=1))
            await uow.session.flush()
            uow.session.add(PolicyRevision(id='other-policy-1',policy_id='other-access',revision=1,
                content=content,content_digest=canonical_digest(content),actor_id='operator'))
        assert registry_database.constraint_failure("INSERT INTO gateway.executions(id,principal_id,product_id,version_id,policy_id,policy_revision,canonical_query,canonical_hash,context,request_id,deadline_at,execution_timeout_seconds) SELECT 'wrong-policy',principal_id,product_id,version_id,'other-access',1,canonical_query,canonical_hash,context,'direct-request',clock_timestamp()+interval '120 seconds',120 FROM gateway.executions LIMIT 1")=='23514'
    asyncio.run(scenario())
