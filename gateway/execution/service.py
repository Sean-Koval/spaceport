"""Authenticated consumer admission and bounded internal status waiting.

Only QueryRequest is public. ExecutionSpec, decisions, fixture authority, and
unscoped wait/view are internal; transports authorize ownership through results.
"""
import asyncio
import math
from contextvars import ContextVar
from typing import Awaitable, Callable
from uuid import uuid4

from gateway.adapters.base import (BindingResolver, authorization_scope_digest,
    check_binding, validate_data_decision, validate_query_visibility)
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.clock import Clock, SystemClock
from gateway.contracts.errors import ErrorCode, GatewayError, invalid_input
from gateway.contracts.execution import ExecutionSpec, ExecutionView
from gateway.contracts.identity import Decision, Principal
from gateway.contracts.manifest import iso_date
from gateway.contracts.query import QueryRequest, validate_query


def unavailable(request_id='execution-status'):
    return GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE,request_id,'Execution state is unavailable.')


def check_current_scope(product, request, binding, current, executed, request_id):
    """Typed row scope cannot change; current projection/limits may only tighten.

    A changed policy revision, tenant or same-revision entitlement is a different
    execution. Full grouping and saved selection/order dependencies stay visible.
    """
    current=validate_data_decision(current,request_id,binding.constraint_members)
    executed=validate_data_decision(executed,request_id,binding.constraint_members)
    if (authorization_scope_digest(current)!=authorization_scope_digest(executed)
        or not set(current.visible_fields)<=set(executed.visible_fields)
        or not set(executed.visible_fields)<=set(product.manifest.outputs)
        or not {name for name,item in binding.fields.items() if item.kind!='measure'}<=set(current.visible_fields)):
        raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN,request_id,'The current scope no longer matches this execution.')
    validate_query_visibility(binding,request,current,request_id)
    bounds=product.manifest.date_boundaries
    if bounds and (iso_date(request.inputs[bounds.end_input])-iso_date(request.inputs[bounds.start_input])).days>current.limits.max_date_range_days:
        raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN,request_id,'The current date limit no longer permits this execution.')
    return current


def wait_budget(seconds):
    if type(seconds) not in {int,float} or not math.isfinite(seconds) or seconds<0:
        raise invalid_input('The wait budget must be finite and nonnegative.')
    return min(float(seconds),5.0)


async def wait_for_view(read: Callable[[], Awaitable[ExecutionView]], *, clock: Clock,
                        seconds: float=5, initial: ExecutionView | None=None,
                        started_at=None, monotonic_started_at=None, budget_limit=None) -> ExecutionView:
    """Internal status-only wait; an effective zero never performs polling I/O."""
    maximum=wait_budget(seconds)
    started=started_at if started_at is not None else clock.now()
    loop=asyncio.get_running_loop()
    monotonic_start=monotonic_started_at if monotonic_started_at is not None else loop.time()
    last=initial
    reading=False
    def budget(): return min(maximum,wait_budget(budget_limit()) if budget_limit else maximum)
    if budget()==0:
        if last is not None: return last
        raise unavailable()
    try:
        async with asyncio.timeout_at(monotonic_start+budget()) as timeout:
            while True:
                cap=budget()
                timeout.reschedule(monotonic_start+cap)
                remaining=cap-(clock.now()-started).total_seconds()
                if last is not None and (last.state not in {'accepted','running'} or remaining<=0
                    or loop.time()>=monotonic_start+cap): return last
                if last is not None:
                    await clock.sleep(min(.25,remaining))
                    if cap-(clock.now()-started).total_seconds()<=0: return last
                reading=True
                last=await read()
                reading=False
    except TimeoutError:
        if last is not None and not reading: return last
        raise unavailable() from None


_WAIT_OPERATION=ContextVar('gateway_internal_wait_operation',default=None)


class _WaitAuthority:
    """Single-use, task/service/operation-bound handle; no serialized authority."""
    __slots__=('owner','operation','task','authorized','consumed')
    def __init__(self,owner,authorized):
        self.owner=owner;self.authorized=authorized;self.operation=_WAIT_OPERATION.get()
        self.task=asyncio.current_task();self.consumed=False
    def __reduce__(self): raise TypeError('Wait authority cannot be serialized')
    def __reduce_ex__(self,protocol): raise TypeError('Wait authority cannot be serialized')
    def consume(self,owner,execution_id):
        authorized=self.authorized
        elapsed=asyncio.get_running_loop().time()-authorized.monotonic_issued_at
        clock_elapsed=(owner.clock.now()-authorized.issued_at).total_seconds()
        if (self.owner is not owner or self.operation is None or self.operation is not _WAIT_OPERATION.get()
            or self.task is not asyncio.current_task() or self.consumed or authorized.view.execution_id!=execution_id
            or not math.isfinite(elapsed) or elapsed<0 or elapsed>5
            or not math.isfinite(clock_elapsed) or clock_elapsed<0 or clock_elapsed>5):
            raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN,'execution-wait','Fresh wait authority is required.')
        self.consumed=True
        return authorized


class FixtureCapability:
    """Unforgeable-by-serialization boundary reserved for Task9's fixture runner.

    Task9 must bind issuance to an approved draft/run, exact isolated binding and
    inputs, fixed test principal, and current author permission. Consumer storage
    intentionally cannot host prepublication drafts. No issuer exists yet.
    """
    __slots__=()
    def __new__(cls,*args,**kwargs):
        raise TypeError('Fixture capabilities are issued only by the authorized fixture runner')
    def __reduce__(self): raise TypeError('Fixture capabilities cannot be serialized')
    def __reduce_ex__(self,protocol): raise TypeError('Fixture capabilities cannot be serialized')


class ExecutionService:
    def __init__(self, *, registry, policy, bindings: BindingResolver, clock: Clock | None=None):
        from .queue import ExecutionQueue
        self.registry,self.policy,self.bindings=registry,policy,bindings
        self.clock=clock or SystemClock()
        self.queue=ExecutionQueue(registry,policy,bindings,clock=self.clock)

    async def submit(self, principal: Principal, request: QueryRequest) -> ExecutionView:
        return (await self._submit(principal,request)).view

    async def _submit(self, principal: Principal, request: QueryRequest):
        request_id=str(uuid4())
        try:
            principal=Principal.model_validate(principal.model_dump(mode='python'))
            request=QueryRequest.model_validate(request.model_dump(mode='python'))
        except (ValueError,TypeError,AttributeError):
            raise invalid_input('A verified principal and valid query are required.') from None
        product=await self.registry.resolve(request.product_id,request.version)
        async with self.registry.transaction() as uow:
            await self.queue.lock_capacity(uow)
            decision=await self.policy.authorize(principal,'execute',product,uow,
                parameters=dict(request.inputs),correlation_id=request_id)
            request=validate_query(product.manifest,request)
            binding=await self.bindings.resolve(product.manifest.binding_id,product.manifest.binding_revision)
            check_binding(binding,product.manifest,digest=product.binding_digest)
            if binding.context!='consumer':
                raise GatewayError(ErrorCode.BINDING_DRIFT,request_id,'The consumer binding is unavailable.')
            try:
                check_current_scope(product,request,binding,decision,decision,request_id)
            except GatewayError:
                await self.queue.deny_scope(uow,principal,product,decision,request_id,dict(request.inputs))
                raise
            canonical={**request.model_dump(mode='json',exclude={'idempotency_key'}),'version':product.manifest.version}
            spec=ExecutionSpec(principal_id=principal.id,pinned_product=product,canonical_query=canonical,
                canonical_hash=canonical_digest(canonical),policy_revision=decision.policy_revision,
                context='consumer',request_id=request_id)
            return await self.queue.admit(uow,principal,spec,decision,request.idempotency_key)

    async def submit_fixture(self, principal: Principal, spec: ExecutionSpec, capability: FixtureCapability) -> ExecutionView:
        # Explicit dependency transferred to Task9; never convert author rights
        # into a data grant or weaken the consumer ProductVersion foreign key.
        raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN,'fixture-execution',
            'An approved isolated fixture runner is required.')

    async def submit_and_wait(self, principal: Principal, request: QueryRequest, seconds: float=5) -> ExecutionView:
        wait_budget(seconds)
        token=_WAIT_OPERATION.set(object())
        try:
            authorized=await self._submit(principal,request)
            return await self.wait(authorized.view.execution_id,seconds=seconds,
                authority=_WaitAuthority(self,authorized))
        finally:
            _WAIT_OPERATION.reset(token)

    async def wait(self, execution_id: str, seconds: float=5, *, authority=None) -> ExecutionView:
        maximum=wait_budget(seconds)
        if not isinstance(authority,_WaitAuthority):
            raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN,'execution-wait','Fresh wait authority is required.')
        authorized=authority.consume(self,execution_id)
        def cap(context):
            return min(maximum,context.executed_decision.limits.sync_wait_seconds,
                context.spec.pinned_product.manifest.limits.sync_wait_seconds,
                context.current_decision.limits.sync_wait_seconds,5)
        current_cap=cap(authorized)
        async def read():
            nonlocal current_cap
            current=await self.queue.wait_authorized(execution_id)
            current_cap=min(current_cap,cap(current))
            return current.view
        return await wait_for_view(read,clock=self.clock,seconds=maximum,initial=authorized.view,
            started_at=authorized.issued_at,monotonic_started_at=authorized.monotonic_issued_at,
            budget_limit=lambda:current_cap)
