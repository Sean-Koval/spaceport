"""PostgreSQL queue: serialized capacity admission, durable authority and fencing.

A lease is scheduling ownership, never source-completion evidence. Every new
attempt consumes its own reservation; uncertain old attempts stay quarantined.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
from uuid import uuid4

from sqlalchemy import column, func, select, table, update, text

from gateway.adapters.base import authorization_scope_digest, check_binding
from gateway.adapters.normalize import NormalizationContext, normalize
from gateway.contracts.canonical import canonical_bytes, canonical_digest
from gateway.contracts.clock import SystemClock
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.execution import ExecutionSpec, ExecutionView, PinnedProduct, PreparedQuery
from gateway.contracts.identity import Decision, Principal
from gateway.contracts.manifest import validate_manifest
from gateway.contracts.query import QueryRequest
from gateway.registry.models import (CapacityLock, Execution, ExecutionAuthorization,
    ExecutionLease, ExecutionPreparation, ExecutionState, IdempotencyRecord,
    ProductVersion, SourceReservation)
from .service import check_current_scope

_PENDING={'accepted','running'}


def authority_deadline(accepted_at, deadline, spec, executed, current=None):
    """All tighter durations are measured from the original DB acceptance."""
    seconds=min(120,executed.limits.execution_timeout_seconds,
        spec.pinned_product.manifest.limits.execution_timeout_seconds,
        current.limits.execution_timeout_seconds if current is not None else 120)
    return min(deadline,accepted_at+timedelta(seconds=seconds))


def effective_deadline(claim, current=None):
    return authority_deadline(claim.accepted_at,claim.deadline,claim.spec,claim.executed_decision,current)

_SAFE_MESSAGES={
    ErrorCode.EXECUTION_TIMEOUT:'The execution deadline elapsed; upstream cancellation is unknown.',
    ErrorCode.NOT_FOUND_OR_FORBIDDEN:'The requested execution is unavailable.',
    ErrorCode.RESOURCE_LIMIT:'The execution exceeds its resource limit.',
    ErrorCode.BINDING_DRIFT:'The pinned execution binding is unavailable.',
    ErrorCode.INVALID_INPUT:'The execution input is invalid.',
    ErrorCode.FRESHNESS_UNKNOWN:'The required source freshness could not be established.',
    ErrorCode.STALE_DATA:'The recorded source watermark is too old.',
}


def failure(code,request_id):
    return GatewayError(code,request_id,_SAFE_MESSAGES.get(ErrorCode(code),'The execution could not complete.'))


def execution_view(execution,state):
    if state is None: raise failure(ErrorCode.BINDING_DRIFT,execution.request_id)
    return ExecutionView(execution_id=execution.id,state=state.state,accepted_at=execution.created_at,
        started_at=state.started_at,completed_at=state.completed_at,
        error=failure(state.error_code,execution.request_id).as_dict() if state.error_code else None)


@dataclass(frozen=True)
class Claim:
    execution_id: str
    lease_token: str
    generation: int
    reservation_id: str
    spec: ExecutionSpec
    principal: Principal
    executed_decision: Decision
    binding: object
    prepared: PreparedQuery | None
    deadline: datetime
    accepted_at: datetime
    started_at: datetime
    claimed_at: datetime | None = None
    monotonic_issued_at: float | None = None


@dataclass(frozen=True)
class AuthorizedExecution:
    """Internal admission/status authority; never a transport response."""
    view: ExecutionView
    spec: ExecutionSpec
    principal: Principal
    executed_decision: Decision
    current_decision: Decision
    issued_at: datetime
    monotonic_issued_at: float


async def lock_execution_capacity(uow):
    """Shared with isolated fixtures; never change the established lock key."""
    locked=await uow.session.scalar(select(CapacityLock).where(CapacityLock.scope_id=='deployment').with_for_update())
    if locked is None: raise failure(ErrorCode.UPSTREAM_UNAVAILABLE,'execution-capacity')


async def _capacity_counts(uow,name,principal_id):
    # Names below are source literals, never public input. Invoker-rights views
    # have no optional hook: later fixture migrations extend them transactionally.
    view=table(name,column('principal_id'),schema='gateway')
    result=(await uow.session.execute(select(
        func.count().filter(view.c.principal_id==principal_id),func.count()).select_from(view))).one()
    return tuple(result)


async def source_capacity_counts(uow,principal_id):
    return await _capacity_counts(uow,'held_source_capacity',principal_id)


async def pending_capacity_counts(uow,principal_id):
    return await _capacity_counts(uow,'pending_execution_capacity',principal_id)


class ExecutionQueue:
    LEASE_SECONDS=30
    def __init__(self,registry,policy,bindings,*,clock=None,source_lifecycle=None):
        self.registry,self.policy,self.bindings=registry,policy,bindings
        self.source_lifecycle=source_lifecycle
        self.clock=clock or SystemClock()

    async def lock_capacity(self,uow):
        # Order: deployment capacity -> policy -> execution state -> lease.
        await lock_execution_capacity(uow)

    async def _now(self,uow):
        return await uow.session.scalar(select(func.clock_timestamp()))

    async def _load(self,uow,execution_id):
        execution=await uow.session.get(Execution,execution_id)
        if execution is None: raise failure(ErrorCode.NOT_FOUND_OR_FORBIDDEN,'execution-status')
        authority=await uow.session.get(ExecutionAuthorization,execution_id)
        version=await uow.session.get(ProductVersion,execution.version_id)
        try:
            if authority is None or version is None: raise ValueError
            manifest=validate_manifest(version.manifest)
            product=PinnedProduct(manifest=manifest,binding_digest=version.binding_digest,version_id=version.id)
            spec=ExecutionSpec(principal_id=execution.principal_id,pinned_product=product,
                canonical_query=execution.canonical_query,canonical_hash=execution.canonical_hash,
                policy_revision=execution.policy_revision,context=execution.context,request_id=execution.request_id)
            principal=Principal.model_validate(authority.principal_snapshot)
            executed=Decision.model_validate(authority.decision)
            if (principal.id!=spec.principal_id or executed.principal_id!=spec.principal_id
                or executed.policy_revision!=spec.policy_revision
                or execution.execution_timeout_seconds!=executed.limits.execution_timeout_seconds
                or execution.deadline_at!=execution.created_at+timedelta(seconds=execution.execution_timeout_seconds)
                or authority.scope_digest!=authorization_scope_digest(executed)
                or authority.full_decision_digest!=canonical_digest(executed)
                or manifest.content_digest!=version.content_digest
                or execution.product_id!=manifest.product_id or execution.policy_id!=manifest.policy_id
                or execution.query_canonical is not None and execution.query_canonical!=canonical_bytes(spec.canonical_query).decode()): raise ValueError
            binding=await self.bindings.resolve(manifest.binding_id,manifest.binding_revision)
            check_binding(binding,manifest,digest=product.binding_digest)
            if binding.context!=execution.context or execution.context!='consumer': raise ValueError
            return execution,spec,principal,executed,binding
        except (ValueError,TypeError,AttributeError):
            raise failure(ErrorCode.BINDING_DRIFT,execution.request_id) from None

    async def deny_scope(self,uow,principal,product,decision,request_id,parameters,*,action='execute'):
        await uow.session.rollback()
        async with self.registry.transaction() as denied:
            await self.policy.audit.append(denied,self.policy.audit.event(principal,action,
                product=product,policy_revision=decision.policy_revision,decision='deny',
                parameters=parameters,correlation_id=request_id))

    async def maintain(self):
        """Capacity-only expiry phase, committed before product/policy locking."""
        async with self.registry.transaction() as uow:
            await self.lock_capacity(uow)
            await self._expire(uow)

    @staticmethod
    def _key(idempotency_key):
        return hashlib.sha256(b'dpg-idempotency-v1\0'+idempotency_key.encode()).hexdigest() if idempotency_key else None

    async def _deny_requester(self,uow,principal,*,action='execute'):
        # Never inspect or hash a retry's values before complete identity matches.
        request_id=str(uuid4())
        await self.policy._deny(uow,principal,action,product=None,policy_revision=None,correlation_id=request_id)
        raise failure(ErrorCode.NOT_FOUND_OR_FORBIDDEN,request_id)

    async def find_existing(self,uow,principal,product,idempotency_key):
        """Identity-only key lookup. It confers no status or execution authority."""
        key=self._key(idempotency_key)
        if key is None: return None
        record=await uow.session.get(IdempotencyRecord,(principal.id,product.manifest.product_id,product.version_id,key))
        if record is None: return None
        authority=await uow.session.get(ExecutionAuthorization,record.execution_id)
        try:
            requester=Principal.model_validate(principal.model_dump(mode='python'))
            accepted=Principal.model_validate(authority.principal_snapshot) if authority is not None else None
            if requester!=accepted: raise ValueError
        except (ValueError,TypeError,AttributeError):
            await self._deny_requester(uow,principal)
        return record.execution_id

    async def _prelock(self,uow,product):
        if self.policy.requires_source_approval(product):
            if self.policy.source_access is None:
                raise failure(ErrorCode.NOT_FOUND_OR_FORBIDDEN,'execution-source')
            await self.policy.source_access.prelock(uow,(product,),version_ids=(product.version_id,))
            return True
        return False

    async def _deadline(self,uow,loaded,current=None):
        execution,spec,principal,executed,_=loaded
        deadline=execution.deadline_at
        if self.policy.requires_source_approval(spec.pinned_product):
            from gateway.source_access import load_execution_access
            accepted=await load_execution_access(uow,execution.id)
            if accepted.principal!=principal or accepted.effective_deadline_at>deadline:
                raise failure(ErrorCode.NOT_FOUND_OR_FORBIDDEN,spec.request_id)
            deadline=min(deadline,accepted.effective_deadline_at)
        return authority_deadline(execution.created_at,deadline,spec,executed,current)

    @staticmethod
    def _check_claim(claim,loaded,deadline):
        """A frozen worker value is not provenance; compare immutable owner data."""
        execution,spec,principal,executed,binding=loaded
        try:
            if (type(claim) is not Claim or type(claim.spec) is not ExecutionSpec
                or type(claim.principal) is not Principal or type(claim.executed_decision) is not Decision
                or type(claim.binding) is not type(binding) or type(claim.deadline) is not datetime
                or claim.execution_id!=execution.id or claim.accepted_at!=execution.created_at
                or canonical_digest(claim.spec)!=canonical_digest(spec)
                or canonical_digest(claim.principal)!=canonical_digest(principal)
                or canonical_digest(claim.executed_decision)!=canonical_digest(executed)
                or canonical_digest(claim.binding)!=canonical_digest(binding)
                or not execution.created_at<claim.deadline<=deadline):
                raise ValueError
        except (ValueError,TypeError,AttributeError,GatewayError):
            raise failure(ErrorCode.NOT_FOUND_OR_FORBIDDEN,spec.request_id) from None

    async def _finalize(self,uow,access_request):
        if access_request is not None:
            await self.policy.source_access.finalize(uow,(access_request,))

    async def _check_tail(self,uow,deadline,lease_expires_at,request_id):
        # The final audit can consume the remaining execution/lease budget.
        # Clock-only suffix: all mutable locks and writes already precede A.
        now=await self._now(uow)
        if now>=deadline: raise failure(ErrorCode.EXECUTION_TIMEOUT,request_id)
        if now>=lease_expires_at: raise failure(ErrorCode.CONFLICT,request_id)
        return now

    async def _evaluate(self,uow,loaded,*,action='execute',initial=False,request_id=None):
        execution,spec,principal,executed,binding=loaded
        request_id=request_id or spec.request_id
        reviewed=await self._prelock(uow,spec.pinned_product)
        request=QueryRequest.model_validate(spec.canonical_query)
        access_request=None
        if reviewed:
            from gateway.source_access_contracts import validate_reviewed_values
            decision=await self.policy.evaluate(principal,action,spec.pinned_product,uow,correlation_id=request_id)
            access_request=await self.policy.source_access.resolve_owner(uow,
                {'kind':'execution','id':execution.id},principal,decision,action=action,request_id=request_id)
            try:
                validate_reviewed_values(access_request.pins,spec.pinned_product.manifest,request)
            except ValueError:
                await self.policy.source_access._deny(uow,principal,action,request_id,'INVALID_INPUT')
        else:
            decision=await self.policy.authorize(principal,action,spec.pinned_product,uow,
                parameters=dict(request.inputs),correlation_id=request_id)
        try:
            check_current_scope(spec.pinned_product,QueryRequest.model_validate(spec.canonical_query),binding,
                decision,executed,spec.request_id)
            if initial and canonical_digest(decision)!=canonical_digest(executed):
                raise failure(ErrorCode.NOT_FOUND_OR_FORBIDDEN,spec.request_id)
        except GatewayError:
            # Scope mismatch can occur despite current general authorization.
            # Abort all caller mutations, then durably record this specific deny.
            await self.deny_scope(uow,principal,spec.pinned_product,decision,spec.request_id,
                None if reviewed else dict(request.inputs),action=action)
            raise
        return decision,access_request

    async def admit(self,uow,principal,spec,decision,idempotency_key,*,access_request=None):
        # The service's capacity-only maintenance precedes its product locks.
        # Never expire unrelated executions from this authorization transaction.
        reviewed=await self._prelock(uow,spec.pinned_product)
        existing=await self.find_existing(uow,principal,spec.pinned_product,idempotency_key)
        if reviewed:
            if access_request is None:
                raise failure(ErrorCode.NOT_FOUND_OR_FORBIDDEN,spec.request_id)
            self.policy.source_access.precheck(access_request)
            if (access_request.product!=spec.pinned_product or access_request.principal!=principal
                or access_request.decision!=decision or access_request.action!='execute'
                or access_request.request_id!=spec.request_id
                or access_request.request_kind!=('owner' if existing else 'admission')
                or existing is not None and access_request.owner.id!=existing):
                raise failure(ErrorCode.NOT_FOUND_OR_FORBIDDEN,spec.request_id)
        elif access_request is not None:
            raise failure(ErrorCode.NOT_FOUND_OR_FORBIDDEN,spec.request_id)
        key_digest=self._key(idempotency_key)
        if existing is not None:
            record=await uow.session.get(IdempotencyRecord,(principal.id,spec.pinned_product.manifest.product_id,
                spec.pinned_product.version_id,key_digest))
            if record.canonical_hash!=spec.canonical_hash:
                raise failure(ErrorCode.CONFLICT,spec.request_id)
            loaded=await self._load(uow,existing)
            current,retained=await self._evaluate(uow,loaded,request_id=spec.request_id)
            state=await uow.session.scalar(select(ExecutionState).where(
                ExecutionState.execution_id==existing).with_for_update())
            now=await self._now(uow)
            if state.state in _PENDING and now>=await self._deadline(uow,loaded,current):
                state.state='timed_out';state.completed_at=now;state.error_code=ErrorCode.EXECUTION_TIMEOUT.value
            await uow.session.flush()
            await self._finalize(uow,retained)
            return self._authorized(execution_view(loaded[0],state),loaded[1],loaded[2],loaded[3],current)
        own,total=await pending_capacity_counts(uow,principal.id)
        if total>=100 or own>=10: raise failure(ErrorCode.RESOURCE_LIMIT,spec.request_id)
        execution=Execution(id=str(uuid4()),principal_id=principal.id,product_id=spec.pinned_product.manifest.product_id,
            version_id=spec.pinned_product.version_id,policy_id=spec.pinned_product.manifest.policy_id,
            policy_revision=spec.policy_revision,canonical_query=spec.model_dump(mode='json')['canonical_query'],
            query_canonical=canonical_bytes(spec.canonical_query).decode(),
            canonical_hash=spec.canonical_hash,context=spec.context,request_id=spec.request_id,deadline_at=func.clock_timestamp(),
            execution_timeout_seconds=decision.limits.execution_timeout_seconds)
        uow.session.add(execution)
        await uow.session.flush()
        await uow.session.refresh(execution)
        state=ExecutionState(execution_id=execution.id,state='accepted')
        uow.session.add(state)
        uow.session.add(ExecutionAuthorization(execution_id=execution.id,principal_snapshot=principal.model_dump(mode='json'),
            decision=decision.model_dump(mode='json'),scope_digest=authorization_scope_digest(decision),full_decision_digest=canonical_digest(decision)))
        if key_digest:
            uow.session.add(IdempotencyRecord(principal_id=principal.id,product_id=execution.product_id,
                version_id=execution.version_id,key_digest=key_digest,canonical_hash=spec.canonical_hash,execution_id=execution.id))
        await uow.session.flush()
        if reviewed:
            retained=await self.policy.source_access.resolve_owner(uow,{'kind':'execution','id':execution.id},
                principal,decision,action='execute',request_id=spec.request_id)
            if (retained.projection!=access_request.projection or retained.pins!=access_request.pins
                or retained.audience_entry_id!=access_request.audience_entry_id):
                raise failure(ErrorCode.NOT_FOUND_OR_FORBIDDEN,spec.request_id)
            await self._finalize(uow,retained)
        return self._authorized(execution_view(execution,state),spec,principal,decision,decision)

    def _authorized(self,view,spec,principal,executed,current):
        # Issuance precedes the transaction's commit/return. Strict waiting must
        # subtract that delay rather than begin a new budget afterward.
        return AuthorizedExecution(view,spec,principal,executed,current,
            self.clock.now(),asyncio.get_running_loop().time())

    async def wait_authorized(self,execution_id,*,requester):
        """Fresh status permission for the complete original authenticated actor."""
        await self.maintain()
        async with self.registry.transaction() as uow:
            await self.lock_capacity(uow)
            loaded=await self._load(uow,execution_id)
            try:
                requester=Principal.model_validate(requester.model_dump(mode='python'))
                if requester!=loaded[2]: raise ValueError
            except (ValueError,TypeError,AttributeError):
                await self._deny_requester(uow,requester,action='result')
            current,access_request=await self._evaluate(uow,loaded,action='result')
            execution,spec,principal,executed,_=loaded
            state=await uow.session.scalar(select(ExecutionState).where(
                ExecutionState.execution_id==execution_id).with_for_update())
            now=await self._now(uow)
            if state.state in _PENDING and now>=await self._deadline(uow,loaded,current):
                state.state='timed_out';state.completed_at=now;state.error_code=ErrorCode.EXECUTION_TIMEOUT.value
            await uow.session.flush()
            await self._finalize(uow,access_request)
            return self._authorized(execution_view(execution,state),spec,principal,executed,current)

    async def _expire(self,uow):
        if self.source_lifecycle is not None:
            await self.source_lifecycle._epoch(uow)
            await uow.session.execute(text("SELECT gateway.source_expire_groups(32)"))
        now=await self._now(uow)
        await uow.session.execute(update(ExecutionState).where(ExecutionState.state.in_(_PENDING),
            ExecutionState.execution_id.in_(select(Execution.id).where(Execution.deadline_at<=now))).values(
                state='timed_out',completed_at=now,error_code=ErrorCode.EXECUTION_TIMEOUT.value))

    async def view(self,execution_id):
        async with self.registry.transaction() as uow:
            await self.lock_capacity(uow)
            await self._expire(uow)
            execution=await uow.session.get(Execution,execution_id)
            state=await uow.session.get(ExecutionState,execution_id)
            if execution is None or state is None: raise failure(ErrorCode.NOT_FOUND_OR_FORBIDDEN,'execution-status')
            return execution_view(execution,state)

    async def claim(self,worker_id):
        if type(worker_id) is not str or not 1<=len(worker_id)<=128:
            raise ValueError('A bounded worker identity is required')
        # Expiry may lock states of multiple policies. Commit that maintenance
        # before taking any policy lock, preserving policy-before-state ordering.
        await self.maintain()
        # At most the bounded queue plus existing running jobs can be rejected
        # during one pass. Each denial completes its own rollback/audit boundary.
        for _ in range(110):
            target=None
            expected=None
            try:
                async with self.registry.transaction() as uow:
                    await self.lock_capacity(uow)
                    now=await self._now(uow)
                    candidates=(await uow.session.execute(select(Execution,ExecutionState,ExecutionLease).join(
                        ExecutionState,ExecutionState.execution_id==Execution.id).outerjoin(
                        ExecutionLease,ExecutionLease.execution_id==Execution.id).where(
                        ExecutionState.state.in_(_PENDING),
                        (ExecutionLease.execution_id.is_(None)) | (ExecutionLease.expires_at<=now))
                        .order_by(Execution.created_at,Execution.id).limit(110))).all()
                    for execution,state,lease in candidates:
                        target=execution.id
                        expected=lease.lease_token if lease else None
                        prepared_row=await uow.session.get(ExecutionPreparation,target)
                        try:
                            prepared=PreparedQuery.model_validate(prepared_row.prepared_query) if prepared_row else None
                        except (ValueError,TypeError):
                            raise failure(ErrorCode.BINDING_DRIFT,execution.request_id) from None
                        loaded=await self._load(uow,target)
                        source_mode=self._source_mode(loaded[4])
                        existing_group=None
                        if source_mode:
                            await self.source_lifecycle._lock_authority(uow,loaded[1].pinned_product)
                            if loaded[4].adapter_revision=='domo-named-v2':
                                from gateway.adapters.source_preparations import load_source_prepared
                                prepared=await load_source_prepared(uow,'consumer',target,loaded[4])
                            existing_group=(await uow.session.execute(text("SELECT id,closed,settled_at FROM gateway.source_operation_groups WHERE owner_kind='consumer' AND owner_id=:owner"),{'owner':target})).mappings().one_or_none()
                            if existing_group and (existing_group['closed'] or existing_group['settled_at'] is not None):
                                raise failure(ErrorCode.NOT_FOUND_OR_FORBIDDEN,execution.request_id)
                        current,access_request=await self._evaluate(uow,loaded,initial=prepared is None)
                        own,total=await source_capacity_counts(uow,execution.principal_id)
                        running=await uow.session.scalar(select(func.count()).select_from(ExecutionState).where(
                            ExecutionState.state=='running',ExecutionState.execution_id!=target))
                        own_running=await uow.session.scalar(select(func.count()).select_from(ExecutionState).join(Execution,
                            Execution.id==ExecutionState.execution_id).where(ExecutionState.state=='running',
                            ExecutionState.execution_id!=target,Execution.principal_id==execution.principal_id))
                        # A reclaim retains its one logical group, including every
                        # prior intent. It must not count its own hold twice.
                        if existing_group is not None:
                            own-=1;total-=1
                        if (total>=current.limits.deployment_concurrency or own>=current.limits.per_principal_concurrency
                            or running>=current.limits.deployment_concurrency or own_running>=current.limits.per_principal_concurrency):
                            target=None
                            continue
                        state=await uow.session.scalar(select(ExecutionState).where(
                            ExecutionState.execution_id==target).with_for_update().execution_options(populate_existing=True))
                        lease=await uow.session.scalar(select(ExecutionLease).where(
                            ExecutionLease.execution_id==target).with_for_update().execution_options(populate_existing=True))
                        now=await self._now(uow)
                        if state.state not in _PENDING or lease is not None and lease.expires_at>now:
                            return None
                        deadline=await self._deadline(uow,loaded,current)
                        if now>=deadline:
                            state.state='timed_out';state.completed_at=now;state.error_code=ErrorCode.EXECUTION_TIMEOUT.value
                            return None
                        token=str(uuid4());generation=lease.generation+1 if lease else 1
                        expiry=min(now+timedelta(seconds=self.LEASE_SECONDS),deadline)
                        if lease is None:
                            lease=ExecutionLease(execution_id=target,worker_id=worker_id,lease_token=token,generation=generation,
                                expires_at=expiry,source_reserved_until=execution.deadline_at)
                            uow.session.add(lease)
                        else:
                            lease.worker_id=worker_id;lease.lease_token=token;lease.generation=generation
                            lease.expires_at=expiry;lease.source_reserved_until=execution.deadline_at
                        state.state='running';state.started_at=state.started_at or now
                        # E/L are held and their mutation is flushed before any
                        # source pool/group/session work. Approval is last.
                        await uow.session.flush()
                        if source_mode:
                            try:
                                source_group=await self.source_lifecycle.reserve_consumer(uow,loaded,current,deadline)
                            except (ValueError,TypeError):
                                raise failure(ErrorCode.NOT_FOUND_OR_FORBIDDEN,execution.request_id) from None
                            reservation_id=source_group.group_id
                            deadline=min(deadline,source_group.deadline)
                        else:
                            reservation=SourceReservation(id=str(uuid4()),execution_id=target,principal_id=execution.principal_id,
                                lease_token=token,generation=generation)
                            uow.session.add(reservation)
                            reservation_id=reservation.id
                        await uow.session.flush()
                        await self._finalize(uow,access_request)
                        issued_at=await self._check_tail(uow,deadline,lease.expires_at,loaded[1].request_id)
                        return Claim(target,token,generation,reservation_id,loaded[1],loaded[2],loaded[3],loaded[4],
                            prepared,deadline,execution.created_at,state.started_at,
                            issued_at,asyncio.get_running_loop().time())
                    return None
            except GatewayError as error:
                if target is None: raise
                await self._reject_candidate(target,expected,error.code)
        return None

    async def _reject_candidate(self,execution_id,expected_token,code):
        async with self.registry.transaction() as uow:
            await self.lock_capacity(uow)
            state=await uow.session.scalar(select(ExecutionState).where(ExecutionState.execution_id==execution_id).with_for_update())
            lease=await uow.session.get(ExecutionLease,execution_id)
            if state is None or state.state not in _PENDING or (lease.lease_token if lease else None)!=expected_token: return
            state.state='timed_out' if code==ErrorCode.EXECUTION_TIMEOUT else 'failed'
            state.error_code=ErrorCode(code).value;state.completed_at=await self._now(uow)
            if self.source_lifecycle is not None:
                group_id=await uow.session.scalar(text("SELECT id FROM gateway.source_operation_groups WHERE owner_kind='consumer' AND owner_id=:owner"),{'owner':execution_id})
                if group_id is not None:
                    await uow.session.execute(text("SELECT gateway.source_close_group(:id,'failure')"),{'id':group_id})

    async def _fence(self,uow,claim,*,allow_expired=False,allow_timed_out=False):
        state=await uow.session.scalar(select(ExecutionState).where(ExecutionState.execution_id==claim.execution_id).with_for_update().execution_options(populate_existing=True))
        lease=await uow.session.scalar(select(ExecutionLease).where(ExecutionLease.execution_id==claim.execution_id).with_for_update().execution_options(populate_existing=True))
        now=await self._now(uow)
        if (state is None or state.state not in ({'running','timed_out'} if allow_timed_out else {'running'}) or lease is None or lease.lease_token!=claim.lease_token
            or lease.generation!=claim.generation or not allow_expired and lease.expires_at<=now):
            raise failure(ErrorCode.CONFLICT,claim.spec.request_id)
        return state,lease,now

    def _source_mode(self,binding):
        from gateway.source_lifecycle import SourceLifecycle
        if not SourceLifecycle.required(binding):
            return False
        if self.source_lifecycle is None:
            raise failure(ErrorCode.BINDING_DRIFT,'source-runtime')
        return True

    async def source_attempt(self,claim,prepared,current,session_spec):
        if not self._source_mode(claim.binding):
            raise failure(ErrorCode.BINDING_DRIFT,claim.spec.request_id)
        try:
            return await self.source_lifecycle.consumer_attempt(self,claim,prepared,current,session_spec)
        except (ValueError,TypeError):
            raise failure(ErrorCode.NOT_FOUND_OR_FORBIDDEN,claim.spec.request_id) from None

    async def _close_source(self,uow,claim,reason,*,require_terminal=False):
        # Caller has already locked and checked the exact CURRENT lease fence.
        # Old worker cleanup must never close a successor's stable source group.
        row=(await uow.session.execute(text("SELECT id FROM gateway.source_operation_groups WHERE owner_kind='consumer' AND owner_id=:owner"),
            {'owner':claim.execution_id})).one_or_none()
        if row is None or str(row.id)!=claim.reservation_id:
            raise failure(ErrorCode.BINDING_DRIFT,claim.spec.request_id)
        closed=await uow.session.scalar(text("SELECT gateway.source_close_group(:id,:reason)"),{'id':row.id,'reason':reason})
        if require_terminal:
            status=await uow.session.scalar(text("SELECT gateway.source_status(id) FROM gateway.source_sessions WHERE context='consumer' AND owner_id=:owner"),{'owner':claim.execution_id})
            if status is None or not status['sealed'] or not status['terminal'] or not closed['settled']:
                raise failure(ErrorCode.UPSTREAM_UNAVAILABLE,claim.spec.request_id)

    async def _validate_source_candidate(self,uow,claim,completed):
        if claim.binding.adapter_revision!='domo-named-v2':
            return
        from gateway.adapters.domo_completed import DomoCompleted
        from gateway.source_lifecycle import SourceLifecycle
        if type(completed) is not DomoCompleted:
            raise failure(ErrorCode.BINDING_DRIFT,claim.spec.request_id)
        row=(await uow.session.execute(text("SELECT * FROM gateway.source_sessions WHERE context='consumer' AND owner_id=:owner"),
            {'owner':claim.execution_id})).mappings().one_or_none()
        if row is None:
            raise failure(ErrorCode.BINDING_DRIFT,claim.spec.request_id)
        retained=SourceLifecycle._context(row)
        if (canonical_digest(completed.permit.context.wire_context())!=canonical_digest(retained.wire_context())
                or retained.generation!=claim.generation or retained.group_id!=claim.reservation_id):
            raise failure(ErrorCode.BINDING_DRIFT,claim.spec.request_id)
        completed.evidence()

    async def gate(self,claim):
        """Commit fresh authorization before each upstream attempt/pending poll."""
        async with self.registry.transaction() as uow:
            await self.lock_capacity(uow)
            loaded=await self._load(uow,claim.execution_id)
            if self._source_mode(loaded[4]):
                await self.source_lifecycle._lock_authority(uow,loaded[1].pinned_product)
            prepared=await uow.session.get(ExecutionPreparation,claim.execution_id)
            current,access_request=await self._evaluate(uow,loaded,initial=prepared is None)
            self._check_claim(claim,loaded,await self._deadline(uow,loaded))
            state,lease,now=await self._fence(uow,claim)
            deadline=min(claim.deadline,await self._deadline(uow,loaded,current))
            if now>=deadline: raise failure(ErrorCode.EXECUTION_TIMEOUT,claim.spec.request_id)
            lease.expires_at=min(now+timedelta(seconds=self.LEASE_SECONDS),deadline)
            await uow.session.flush()
            await self._finalize(uow,access_request)
            await self._check_tail(uow,deadline,lease.expires_at,loaded[1].request_id)
            return current

    async def save_prepared(self,claim,prepared):
        prepared=PreparedQuery.model_validate(prepared.model_dump(mode='python'))
        async with self.registry.transaction() as uow:
            await self.lock_capacity(uow)
            loaded=await self._load(uow,claim.execution_id)
            if self._source_mode(loaded[4]):
                await self.source_lifecycle._lock_authority(uow,loaded[1].pinned_product)
            current,access_request=await self._evaluate(uow,loaded)
            self._check_claim(claim,loaded,await self._deadline(uow,loaded))
            _,lease,now=await self._fence(uow,claim)
            deadline=min(claim.deadline,await self._deadline(uow,loaded,current))
            if now>=deadline:
                raise failure(ErrorCode.EXECUTION_TIMEOUT,claim.spec.request_id)
            existing=await uow.session.get(ExecutionPreparation,claim.execution_id)
            if existing:
                if canonical_digest(existing.prepared_query)!=canonical_digest(prepared): raise failure(ErrorCode.CONFLICT,claim.spec.request_id)
            else:
                if prepared.model_bundle_digest!=loaded[1].pinned_product.manifest.model_bundle_digest:
                    raise failure(ErrorCode.BINDING_DRIFT,claim.spec.request_id)
                uow.session.add(ExecutionPreparation(execution_id=claim.execution_id,prepared_query=prepared.model_dump(mode='json')))
            await uow.session.flush()
            await self._finalize(uow,access_request)
            await self._check_tail(uow,deadline,lease.expires_at,loaded[1].request_id)

    async def renew(self,claim):
        async with self.registry.transaction() as uow:
            # One-row operation, never followed by capacity/policy/state locks.
            now=await self._now(uow)
            if now>=claim.deadline: return False
            changed=await uow.session.execute(update(ExecutionLease).where(ExecutionLease.execution_id==claim.execution_id,
                ExecutionLease.lease_token==claim.lease_token,ExecutionLease.generation==claim.generation,
                ExecutionLease.expires_at>now).values(expires_at=min(now+timedelta(seconds=self.LEASE_SECONDS),claim.deadline)).returning(ExecutionLease.execution_id))
            return changed.scalar_one_or_none() is not None

    async def _release(self,uow,claim):
        if self._source_mode(claim.binding):
            await self._close_source(uow,claim,'completed',require_terminal=True)
            return
        # Trigger checks the current token/generation and supplies its own clock.
        await uow.session.execute(update(SourceReservation).where(SourceReservation.id==claim.reservation_id,
            SourceReservation.lease_token==claim.lease_token,SourceReservation.generation==claim.generation,
            SourceReservation.released_at.is_(None)).values(release_reason='completed'))

    async def fail(self,claim,code,*,source_completed=False):
        try:
            async with self.registry.transaction() as uow:
                await self.lock_capacity(uow)
                source_mode=self._source_mode(claim.binding)
                state,lease,now=await self._fence(uow,claim,allow_expired=True,allow_timed_out=source_completed or source_mode)
                if source_mode:
                    await self._close_source(uow,claim,'deadline' if code==ErrorCode.EXECUTION_TIMEOUT else 'failure')
                if state.state=='timed_out':
                    if not source_mode: await self._release(uow,claim)
                    return True
                state.state='timed_out' if code==ErrorCode.EXECUTION_TIMEOUT or now>=claim.deadline else 'failed'
                state.error_code=ErrorCode.EXECUTION_TIMEOUT.value if state.state=='timed_out' else ErrorCode(code).value
                state.completed_at=now
                if source_completed and not source_mode: await self._release(uow,claim)
                return True
        except GatewayError as error:
            if error.code==ErrorCode.CONFLICT: return False
            raise

    async def complete(self,claim,completed):
        """Data completion and metadata-only cleanup have independent commits."""
        try:
            return await self._complete(claim,completed)
        except Exception as error:
            # A denied/failed result transaction rolls its source closure back.
            # Close the exact fenced group independently. Only protected source
            # terminal evidence may settle it; an error type proves nothing.
            if self._source_mode(claim.binding):
                code=error.code if isinstance(error,GatewayError) else ErrorCode.UPSTREAM_UNAVAILABLE
                await self.fail(claim,code,source_completed=True)
            raise

    async def _complete(self,claim,completed):
        from .results import prepare_stored_result
        async with self.registry.transaction() as uow:
            await self.lock_capacity(uow)
            loaded=await self._load(uow,claim.execution_id)
            if self._source_mode(loaded[4]):
                await self.source_lifecycle._lock_authority(uow,loaded[1].pinned_product)
            current,access_request=await self._evaluate(uow,loaded,action='result')
            self._check_claim(claim,loaded,await self._deadline(uow,loaded))
            state,lease,now=await self._fence(uow,claim)
            deadline=min(claim.deadline,await self._deadline(uow,loaded,current))
            if now>=deadline:
                raise failure(ErrorCode.EXECUTION_TIMEOUT,claim.spec.request_id)
            source_mode=self._source_mode(loaded[4])
            if source_mode:
                await self._validate_source_candidate(uow,claim,completed)
                await self._close_source(uow,claim,'completed',require_terminal=True)
            execution,spec,_,executed,binding=loaded
            view=ExecutionView(execution_id=execution.id,state='succeeded',accepted_at=execution.created_at,
                started_at=state.started_at,completed_at=now)
            class CompletionClock:
                def now(self): return now
            envelope=normalize(spec.pinned_product,completed,view,context=NormalizationContext(
                spec=spec,decision=current,executed_decision=executed,binding=binding,clock=CompletionClock()))
            result=prepare_stored_result(execution,envelope,executed,now)
            # Legacy reservation release is also an earlier lifecycle operation.
            # Reviewed source closure above must never run a second time after A.
            if not source_mode: await self._release(uow,claim)
            uow.session.add(result)
            state.state='succeeded';state.completed_at=now;state.error_code=None
            await uow.session.flush()
            await self._finalize(uow,access_request)
            await self._check_tail(uow,deadline,lease.expires_at,spec.request_id)
            return view
