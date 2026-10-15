"""Clock/orchestration tests. Fakes here prove no database/source race guarantees."""
import asyncio
from datetime import timedelta
import importlib
import pickle
from types import SimpleNamespace

import pytest

from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import ExecutionView
from gateway.contracts.identity import Principal
from tests.contract.test_cube_adapter import synthetic

pytestmark = pytest.mark.unit


def api():
    try:
        return SimpleNamespace(**{name: importlib.import_module('gateway.execution.' + name)
                                  for name in ('service', 'queue', 'worker')})
    except ModuleNotFoundError as error:
        pytest.fail(f'Durable execution implementation is missing: {error.name}')


def test_backoff_is_bounded_and_never_crosses_original_deadline(clock):
    backoff = api().worker.backoff_seconds
    deadline = clock.now() + timedelta(seconds=120)
    assert [backoff(i, clock.now(), deadline) for i in range(6)] == [.25, .5, 1, 2, 2, 2]
    assert backoff(99, deadline-timedelta(seconds=.1), deadline) == .1
    assert backoff(0, deadline, deadline) == 0
    assert backoff(0, deadline+timedelta(seconds=1), deadline) == 0


@pytest.mark.parametrize('seconds', [5, 100, .1, 0])
def test_wait_is_capped_without_changing_running_state(clock, seconds):
    view = ExecutionView(execution_id='e-1', state='running', accepted_at=clock.now(), started_at=clock.now())
    async def read(): return view
    async def scenario():
        start=clock.now()
        result=await api().service.wait_for_view(read, clock=clock, seconds=seconds, initial=view)
        assert result == view
        assert (clock.now()-start).total_seconds() == min(seconds, 5)
    asyncio.run(scenario())


def test_wait_ends_when_execution_finishes(clock):
    start=clock.now()
    async def read():
        done=(clock.now()-start).total_seconds()>=.5
        return ExecutionView(execution_id='e-1', state='succeeded' if done else 'running',
            accepted_at=start, started_at=start, completed_at=clock.now() if done else None)
    assert asyncio.run(api().service.wait_for_view(read, clock=clock)).state == 'succeeded'
    assert sum(clock.sleeps) == .5


def test_wait_bounds_a_blocked_database_read(clock):
    async def read(): await asyncio.sleep(30)
    async def scenario():
        with pytest.raises(GatewayError) as caught:
            await api().service.wait_for_view(read, clock=clock, seconds=.01)
        assert caught.value.code == 'UPSTREAM_UNAVAILABLE'
    asyncio.run(scenario())


@pytest.mark.parametrize('elapsed', [1, 1.01])
def test_wait_does_not_start_a_read_after_monotonic_deadline(clock, monkeypatch, elapsed):
    view=ExecutionView(execution_id='e-1',state='running',accepted_at=clock.now(),started_at=clock.now())
    async def scenario():
        loop=asyncio.get_running_loop()
        monotonic_start=loop.time()
        monotonic_now=monotonic_start
        async def delayed_sleep(seconds):
            nonlocal monotonic_now
            # Wall time can lag the monotonic budget after scheduler delay or
            # a wall-clock adjustment. No timer callback has run yet.
            clock.instant+=timedelta(seconds=seconds)
            monotonic_now=monotonic_start+elapsed
        async def forbidden_read():
            raise AssertionError('An expired monotonic budget must not start polling I/O')
        with monkeypatch.context() as patch:
            patch.setattr(loop,'time',lambda:monotonic_now)
            patch.setattr(clock,'sleep',delayed_sleep)
            result=await api().service.wait_for_view(forbidden_read,clock=clock,seconds=1,initial=view,
                monotonic_started_at=monotonic_start)
        assert result==view
    asyncio.run(scenario())


def test_wait_fails_closed_when_in_time_read_crosses_deadline(clock, monkeypatch):
    view=ExecutionView(execution_id='e-1',state='running',accepted_at=clock.now(),started_at=clock.now())
    async def scenario():
        loop=asyncio.get_running_loop()
        monotonic_start=loop.time()
        monotonic_now=monotonic_start
        reads=[]
        def advance(seconds):
            nonlocal monotonic_now
            clock.instant+=timedelta(seconds=seconds)
            monotonic_now+=seconds
        async def sleep(seconds): advance(seconds)
        async def read():
            reads.append((clock.now()-view.accepted_at).total_seconds())
            advance(.1)
            # Yield to the real asyncio timeout so the last read is cancelled
            # after it began inside the one-second budget, before returning.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return view
        with monkeypatch.context() as patch:
            patch.setattr(loop,'time',lambda:monotonic_now)
            patch.setattr(clock,'sleep',sleep)
            with pytest.raises(GatewayError) as caught:
                await api().service.wait_for_view(read,clock=clock,seconds=1,initial=view,
                    monotonic_started_at=monotonic_start)
        assert caught.value.code=='UPSTREAM_UNAVAILABLE'
        assert reads==[.25,.6,.95]
    asyncio.run(scenario())


@pytest.mark.parametrize('seconds', [-1, float('nan'), float('inf'), True, '5'])
def test_wait_rejects_invalid_budget(clock, seconds):
    async def read(): raise AssertionError('No read for an invalid budget')
    with pytest.raises(GatewayError) as caught:
        asyncio.run(api().service.wait_for_view(read, clock=clock, seconds=seconds))
    assert caught.value.code == 'INVALID_INPUT'


def test_scope_guard_rejects_same_revision_entitlement_change():
    binding, product, executed, request = synthetic()
    changed=executed.model_copy(update={'row_constraints': (
        executed.row_constraints[0], executed.row_constraints[1].model_copy(update={'values': ('south',)}))})
    with pytest.raises(GatewayError) as caught:
        api().service.check_current_scope(product, request, binding, changed, executed, 'test-1')
    assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'


@pytest.mark.parametrize('changed', ['tenant', 'policy', 'principal', 'wider_visibility', 'hidden_grain', 'hidden_filter', 'date_limit'])
def test_current_scope_and_saved_query_dependencies_are_guarded(changed):
    binding, product, executed, request = synthetic()
    current=executed
    if changed=='tenant': current=executed.model_copy(update={'tenant_id':'south'})
    elif changed=='policy': current=executed.model_copy(update={'policy_revision':2})
    elif changed=='principal': current=executed.model_copy(update={'principal_id':'south-user'})
    elif changed=='wider_visibility':
        current=executed
        executed=executed.model_copy(update={'visible_fields':('day','segment')})
    elif changed=='hidden_grain': current=executed.model_copy(update={'visible_fields':('day','revenue')})
    elif changed=='hidden_filter':
        request=request.model_copy(update={'inputs':{**request.inputs,'segment':'A'}})
        current=executed.model_copy(update={'visible_fields':('day','revenue')})
    else: current=executed.model_copy(update={'limits':executed.limits.model_copy(update={'max_date_range_days':2})})
    with pytest.raises(GatewayError):
        api().service.check_current_scope(product, request, binding, current, executed, 'test-1')


def test_current_scope_permits_tighter_uninvolved_measure_and_limits():
    binding, product, executed, request = synthetic()
    current=executed.model_copy(update={'visible_fields':('day','segment'),
        'limits':executed.limits.model_copy(update={'max_rows':1,'page_size':1})})
    assert api().service.check_current_scope(product, request, binding, current, executed, 'test-1') == current


def test_fixture_capability_cannot_be_constructed_or_pickled():
    capability=api().service.FixtureCapability
    with pytest.raises(TypeError): capability()
    with pytest.raises(TypeError): capability(approved=True)
    forged=object.__new__(capability)
    with pytest.raises(TypeError): pickle.dumps(forged)


class UnitQueue:
    """In-memory orchestration probe, never used as concurrency/DB evidence."""
    def __init__(self, clock, *, duration=120):
        binding,product,decision,request=synthetic()
        from gateway.contracts.execution import ExecutionSpec
        from gateway.contracts.canonical import canonical_digest
        from gateway.contracts.query import validate_query
        canonical=validate_query(product.manifest,request).model_dump(mode='json',exclude={'idempotency_key'})
        spec=ExecutionSpec(principal_id=decision.principal_id,pinned_product=product,
            canonical_query=canonical,canonical_hash=canonical_digest(canonical),policy_revision=1,context='consumer',request_id='request-1')
        self.claimed=SimpleNamespace(execution_id='execution-1',lease_token='lease-1',generation=1,
            reservation_id='reservation-1',spec=spec,executed_decision=decision,binding=binding,
            prepared=None,accepted_at=clock.now(),deadline=clock.now()+timedelta(seconds=duration))
        self.clock=clock;self.events=[];self.failures=[];self.completed=[]
        self.denied_after=None
    async def claim(self,worker_id): self.events.append('committed-claim-audit'); return self.claimed
    async def gate(self,claim):
        self.events.append('committed-poll-audit')
        if self.denied_after is not None and self.events.count('committed-poll-audit')>self.denied_after:
            raise GatewayError('NOT_FOUND_OR_FORBIDDEN','request-1','Unavailable')
        return claim.executed_decision
    async def save_prepared(self,claim,prepared): self.events.append('persisted-preparation')
    async def renew(self,claim): return True
    async def fail(self,claim,code,*,source_completed=False): self.failures.append((code,source_completed)); return True
    async def complete(self,claim,completed): self.completed.append(completed)


class UnitAdapter:
    def __init__(self, queue, *, complete_after=None, late=False, blocked=False):
        self.queue=queue;self.complete_after=complete_after;self.late=late;self.blocked=blocked
        self.deadlines=[];self.polls=0;self.closed=False
    async def prepare(self,product,request,decision,*,context,deadline,request_id):
        from gateway.contracts.execution import PreparedQuery
        self.queue.events.append('prepare')
        self.deadlines.append(deadline)
        if self.blocked: await asyncio.sleep(30)
        return PreparedQuery(endpoint_id='synthetic-consumer',model_bundle_digest=product.manifest.model_bundle_digest,cube_query={'limit':1001})
    async def poll(self,prepared,decision,*,context,deadline,request_id):
        from gateway.contracts.execution import Pending,Completed
        self.queue.events.append('poll');self.deadlines.append(deadline);self.polls+=1
        if self.late: await self.queue.clock.sleep(120)
        if self.complete_after is not None and self.polls>=self.complete_after:
            return Completed(raw_rows=[],source_watermark=self.queue.clock.now())
        return Pending(retry_after_seconds=30)
    async def close(self): self.closed=True


def test_pending_worker_stops_at_120_and_keeps_original_deadline(clock):
    queue=UnitQueue(clock)
    adapter=UnitAdapter(queue)
    start=clock.now()
    def factory():
        assert queue.events==['committed-claim-audit']
        return adapter
    assert asyncio.run(api().worker.Worker(queue,adapter_factory=factory,worker_id='worker-1',clock=clock).run_once())
    assert (clock.now()-start).total_seconds()==120
    assert clock.sleeps[:4]==[.25,.5,1,2] and max(clock.sleeps)<=2
    assert set(adapter.deadlines)=={queue.claimed.deadline}
    assert queue.failures==[('EXECUTION_TIMEOUT',False)] and not queue.completed
    assert adapter.closed


def test_pending_reauthorization_stops_subsequent_calls(clock):
    queue=UnitQueue(clock);queue.denied_after=2
    adapter=UnitAdapter(queue)
    asyncio.run(api().worker.Worker(queue,adapter_factory=lambda:adapter,worker_id='worker-1',clock=clock).run_once())
    assert adapter.polls==1
    assert queue.failures==[('NOT_FOUND_OR_FORBIDDEN',False)]


def test_late_completed_rows_are_not_released(clock):
    queue=UnitQueue(clock);adapter=UnitAdapter(queue,complete_after=1,late=True)
    asyncio.run(api().worker.Worker(queue,adapter_factory=lambda:adapter,worker_id='worker-1',clock=clock).run_once())
    assert not queue.completed and queue.failures==[('EXECUTION_TIMEOUT',True)]


def test_worker_independently_times_out_blocked_prepare(clock):
    queue=UnitQueue(clock,duration=.01);adapter=UnitAdapter(queue,blocked=True)
    async def bounded():
        await asyncio.wait_for(api().worker.Worker(queue,adapter_factory=lambda:adapter,worker_id='worker-1',clock=clock).run_once(),timeout=.1)
    asyncio.run(bounded())
    assert queue.failures==[('EXECUTION_TIMEOUT',False)]
    assert adapter.closed


def test_completed_rows_are_handed_to_persistence(clock):
    queue=UnitQueue(clock);adapter=UnitAdapter(queue,complete_after=2)
    asyncio.run(api().worker.Worker(queue,adapter_factory=lambda:adapter,worker_id='worker-1',clock=clock).run_once())
    assert len(queue.completed)==1 and not queue.failures
    assert adapter.polls==2


def test_worker_cancellation_preserves_uncertain_attempt(clock):
    queue=UnitQueue(clock);adapter=UnitAdapter(queue,blocked=True)
    async def scenario():
        task=asyncio.create_task(api().worker.Worker(queue,adapter_factory=lambda:adapter,worker_id='worker-1',clock=clock).run_once())
        await asyncio.sleep(.01);task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(scenario())
    assert adapter.closed and not queue.failures and not queue.completed


@pytest.mark.parametrize('limit_source', ['product', 'decision'])
def test_worker_pending_honors_tighter_immutable_execution_budget(clock, limit_source):
    queue=UnitQueue(clock)
    claim=queue.claimed
    if limit_source=='decision':
        claim.executed_decision=claim.executed_decision.model_copy(update={
            'limits':claim.executed_decision.limits.model_copy(update={'execution_timeout_seconds':10})})
    else:
        product=claim.spec.pinned_product
        claim.spec=claim.spec.model_copy(update={'pinned_product':product.model_copy(update={
            'manifest':product.manifest.model_copy(update={'limits':product.manifest.limits.model_copy(
                update={'execution_timeout_seconds':10})})})})
    claim.accepted_at=clock.now()
    adapter=UnitAdapter(queue)
    start=clock.now()
    asyncio.run(api().worker.Worker(queue,adapter_factory=lambda:adapter,worker_id='worker-1',clock=clock).run_once())
    assert (clock.now()-start).total_seconds()==10
    assert max(adapter.deadlines)==start+timedelta(seconds=10)
    assert queue.failures==[('EXECUTION_TIMEOUT',False)] and not queue.completed


def test_current_tighter_timeout_is_anchored_to_original_acceptance(clock):
    queue=UnitQueue(clock)
    queue.claimed.accepted_at=clock.now()
    current=queue.claimed.executed_decision.model_copy(update={
        'limits':queue.claimed.executed_decision.limits.model_copy(update={'execution_timeout_seconds':10})})
    async def gate(claim): return current
    queue.gate=gate
    asyncio.run(clock.sleep(8))
    adapter=UnitAdapter(queue)
    asyncio.run(api().worker.Worker(queue,adapter_factory=lambda:adapter,worker_id='worker-1',clock=clock).run_once())
    assert clock.now()==queue.claimed.accepted_at+timedelta(seconds=10)
    assert set(adapter.deadlines)=={queue.claimed.accepted_at+timedelta(seconds=10)}
    assert queue.failures==[('EXECUTION_TIMEOUT',False)]


def test_completed_after_tight_timeout_keeps_rows_unreleased(clock):
    queue=UnitQueue(clock)
    queue.claimed.accepted_at=clock.now()
    queue.claimed.executed_decision=queue.claimed.executed_decision.model_copy(update={
        'limits':queue.claimed.executed_decision.limits.model_copy(update={'execution_timeout_seconds':10})})
    adapter=UnitAdapter(queue,complete_after=1)
    original=adapter.poll
    async def late(*args, **kwargs):
        await clock.sleep(11)
        return await original(*args, **kwargs)
    adapter.poll=late
    asyncio.run(api().worker.Worker(queue,adapter_factory=lambda:adapter,worker_id='worker-1',clock=clock).run_once())
    assert not queue.completed and queue.failures==[('EXECUTION_TIMEOUT',True)]


def test_blocked_prepare_uses_tight_decision_budget(clock):
    queue=UnitQueue(clock)
    queue.claimed.accepted_at=clock.now()
    queue.claimed.executed_decision=queue.claimed.executed_decision.model_copy(update={
        'limits':queue.claimed.executed_decision.limits.model_copy(update={
            'execution_timeout_seconds':1,'sync_wait_seconds':1})})
    adapter=UnitAdapter(queue,blocked=True)
    async def bounded():
        await asyncio.wait_for(api().worker.Worker(queue,adapter_factory=lambda:adapter,
            worker_id='worker-1',clock=clock).run_once(),timeout=1.5)
    asyncio.run(bounded())
    assert queue.failures==[('EXECUTION_TIMEOUT',False)] and adapter.closed


def test_zero_wait_without_fresh_view_never_reads(clock):
    reads=[]
    async def read():
        reads.append(True)
        return ExecutionView(execution_id='e-1',state='accepted',accepted_at=clock.now())
    with pytest.raises(GatewayError):
        asyncio.run(api().service.wait_for_view(read,clock=clock,seconds=0))
    assert not reads and not clock.sleeps


def test_zero_wait_reuses_fresh_view_without_read(clock):
    view=ExecutionView(execution_id='e-1',state='accepted',accepted_at=clock.now())
    async def read(): raise AssertionError('A zero budget must not do I/O')
    result=asyncio.run(api().service.wait_for_view(read,clock=clock,seconds=0,initial=view))
    assert result==view and not clock.sleeps


def waiting_service(clock, *, saved=5, current=5, product=5):
    service=object.__new__(api().service.ExecutionService)
    service.clock=clock
    claim=UnitQueue(clock).claimed
    def decision(seconds):
        return claim.executed_decision.model_copy(update={
            'limits':claim.executed_decision.limits.model_copy(update={'sync_wait_seconds':seconds})})
    pinned=claim.spec.pinned_product
    spec=claim.spec.model_copy(update={'pinned_product':pinned.model_copy(update={
        'manifest':pinned.manifest.model_copy(update={
            'limits':pinned.manifest.limits.model_copy(update={'sync_wait_seconds':product})})})})
    authorized=SimpleNamespace(view=ExecutionView(execution_id='execution-1',state='accepted',accepted_at=clock.now()),
        principal=Principal(
            id=spec.principal_id,subject='synthetic-north',kind='user',service_client_id='test-client'),
        spec=spec,executed_decision=decision(saved),current_decision=decision(current),
        issued_at=clock.now(),monotonic_issued_at=__import__('time').monotonic())
    class StatusQueue:
        calls=0
        next_cap=current
        blocked=False
        async def wait_authorized(self,execution_id,*,requester):
            assert requester==authorized.principal
            self.calls+=1
            if self.blocked: await asyncio.sleep(30)
            return SimpleNamespace(**{**vars(authorized),'current_decision':decision(self.next_cap)})
    service.queue=StatusQueue()
    async def submit(principal,request): return authorized
    service._submit=submit
    return service,authorized


@pytest.mark.parametrize(('saved','current','product','caller','elapsed'),[
    (1,5,5,5,1),(5,1,5,100,1),(5,5,1,5,1),(0,5,5,100,0),
    (5,0,5,5,0),(5,5,0,5,0),(5,5,5,0,0),(5,5,5,.1,.1),
])
def test_submit_and_wait_uses_all_authorized_wait_caps(clock,saved,current,product,caller,elapsed):
    service,authorized=waiting_service(clock,saved=saved,current=current,product=product)
    start=clock.now()
    result=asyncio.run(service.submit_and_wait(None,None,seconds=caller))
    assert result==authorized.view
    assert (clock.now()-start).total_seconds()==elapsed
    if elapsed==0: assert service.queue.calls==0


def test_positive_wait_reauthorization_can_tighten_original_budget(clock):
    service,_=waiting_service(clock)
    service.queue.next_cap=1
    start=clock.now()
    asyncio.run(service.submit_and_wait(None,None,seconds=100))
    assert (clock.now()-start).total_seconds()==1
    assert service.queue.calls>0


def test_wait_without_fresh_authority_fails_immediately(clock):
    service,_=waiting_service(clock)
    with pytest.raises(GatewayError): asyncio.run(service.wait('execution-1'))
    assert service.queue.calls==0 and not clock.sleeps


def test_blocked_positive_status_read_respects_saved_budget(clock):
    service,_=waiting_service(clock,saved=1)
    service.queue.blocked=True
    async def bounded():
        with pytest.raises(GatewayError) as caught:
            await asyncio.wait_for(service.submit_and_wait(None,None),timeout=1.2)
        assert caught.value.code=='UPSTREAM_UNAVAILABLE'
    asyncio.run(bounded())


def test_elapsed_current_timeout_prevents_preparation(clock):
    queue=UnitQueue(clock)
    current=queue.claimed.executed_decision.model_copy(update={
        'limits':queue.claimed.executed_decision.limits.model_copy(update={'execution_timeout_seconds':10})})
    async def gate(claim): return current
    queue.gate=gate
    asyncio.run(clock.sleep(11))
    adapter=UnitAdapter(queue)
    asyncio.run(api().worker.Worker(queue,adapter_factory=lambda:adapter,worker_id='worker-1',clock=clock).run_once())
    assert 'prepare' not in queue.events and adapter.polls==0
    assert queue.failures==[('EXECUTION_TIMEOUT',False)]


@pytest.mark.parametrize('misuse',['foreign_service','wrong_id','reused','stale','stale_monotonic','serialized'])
def test_wait_authority_rejects_misuse_before_any_poll(clock,misuse):
    service,_=waiting_service(clock,saved=0)
    foreign,_=waiting_service(clock)
    original=service.wait
    async def intercepted(execution_id,seconds=5,*,authority=None):
        if misuse=='serialized':
            with pytest.raises(TypeError): pickle.dumps(authority)
        elif misuse=='foreign_service':
            with pytest.raises(GatewayError): await foreign.wait(execution_id,authority=authority)
        elif misuse=='wrong_id':
            with pytest.raises(GatewayError): await original('wrong-execution',authority=authority)
        elif misuse in {'stale','stale_monotonic'}:
            if misuse=='stale': await clock.sleep(6)
            else: authority.authorized.monotonic_issued_at-=6
            with pytest.raises(GatewayError): await original(execution_id,authority=authority)
            return authority.authorized.view
        result=await original(execution_id,seconds=seconds,authority=authority)
        if misuse=='reused':
            with pytest.raises(GatewayError): await original(execution_id,authority=authority)
        return result
    service.wait=intercepted
    asyncio.run(service.submit_and_wait(None,None))
    assert service.queue.calls==0 and foreign.queue.calls==0


def test_wait_budget_subtracts_commit_return_and_scheduling_delay(clock):
    service,authorized=waiting_service(clock,saved=1)
    async def delayed_submit(principal,request):
        # Issuance occurs before commit/return, exactly as in ExecutionQueue.
        await clock.sleep(.75)
        return authorized
    service._submit=delayed_submit
    start=clock.now()
    asyncio.run(service.submit_and_wait(None,None))
    assert (clock.now()-start).total_seconds()==1
    assert clock.sleeps==[.75,.25] and service.queue.calls==0


@pytest.mark.parametrize('blocked_stage',['save_prepared','complete'])
def test_current_tighter_timeout_bounds_following_database_work(clock,blocked_stage):
    queue=UnitQueue(clock)
    current=queue.claimed.executed_decision.model_copy(update={
        'limits':queue.claimed.executed_decision.limits.model_copy(update={
            'execution_timeout_seconds':1,'sync_wait_seconds':1})})
    async def gate(claim): return current
    async def blocked(*args): await asyncio.sleep(30)
    queue.gate=gate
    setattr(queue,blocked_stage,blocked)
    adapter=UnitAdapter(queue,complete_after=1)
    async def bounded():
        assert await asyncio.wait_for(api().worker.Worker(queue,adapter_factory=lambda:adapter,
            worker_id='worker-1',clock=clock).run_once(),timeout=1.25)
    asyncio.run(bounded())
    assert queue.failures==[('EXECUTION_TIMEOUT',blocked_stage=='complete')]
    assert adapter.closed and not queue.completed


def test_claim_reservation_orm_insert_omits_server_owned_release_columns():
    """Observe real ORM SQL emission only, not PostgreSQL privilege enforcement."""
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session
    from gateway.registry.models import SourceReservation

    class StopBeforeExecution(Exception):
        pass

    engine=create_engine('sqlite://')
    inserted=[]
    @event.listens_for(engine,'before_cursor_execute')
    def capture_insert(connection,cursor,statement,parameters,context,many):
        assert context.isinsert
        inserted.extend(context.compiled_parameters)
        # Never execute this PostgreSQL mapping against SQLite. The ORM planner
        # is the boundary under test; integration tests enforce actual GRANTs.
        raise StopBeforeExecution
    try:
        with Session(engine) as session:
            session.add(SourceReservation(id='reservation-1',execution_id='execution-1',
                principal_id='principal-1',lease_token='lease-1',generation=1))
            with pytest.raises(StopBeforeExecution):
                session.flush()
        assert inserted==[{'id':'reservation-1','execution_id':'execution-1',
            'principal_id':'principal-1','lease_token':'lease-1','generation':1}]
    finally:
        engine.dispose()
