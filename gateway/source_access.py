"""Private source access transaction contracts.

A preliminary match is never release authority. Finalization is the approval-lock
suffix of a caller-owned, capacity-first transaction. No public route accepts
these types and no provider receives the registry's audience or review content.
"""
from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from gateway.contracts.canonical import AwareTimestamp, Digest, Identifier, canonical_digest, canonical_bytes
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.errors import GatewayError
from gateway.contracts.identity import Decision, Principal
from gateway.registry.models import BindingRevision, ProductRow, VersionState
from gateway.registry.repository import not_found
from gateway.source_access_contracts import (ApprovalModel, ReviewedPackagePins, RuntimeApprovalProjection,
    evaluate_runtime_source_access, _fresh, _plain, _rows)


class ExecutionOwner(ApprovalModel):
    kind: Literal['execution']
    id: Identifier


class FixtureInvocationOwner(ApprovalModel):
    kind: Literal['fixture_invocation']
    id: Identifier


class FixtureAttemptOwner(ApprovalModel):
    kind: Literal['fixture_attempt']
    id: Identifier


OwnerAccessSelector = Annotated[ExecutionOwner | FixtureInvocationOwner | FixtureAttemptOwner, Field(discriminator='kind')]
OwnerAction = Literal['execute','result','author']
TransientAction = Literal['discover','describe','author','publish']


def validate_owner_operation(owner, action):
    if type(owner) is dict:
        value=_plain(owner)
    elif any(type(owner) is cls for cls in (ExecutionOwner,FixtureInvocationOwner,FixtureAttemptOwner)):
        data=owner.__dict__
        if (type(data) is not dict or any(type(key) is not str for key in data)
            or owner.__pydantic_extra__ is not None or set(data)!=set(type(owner).model_fields)):
            raise ValueError('Invalid source operation')
        value=_plain(data)
    else:
        raise ValueError('Invalid source operation')
    owner=TypeAdapter(OwnerAccessSelector).validate_python(value)
    if type(action) is not str or action not in {
        'execution':('execute','result'), 'fixture_invocation':('author',), 'fixture_attempt':('execute','result'),
    }[owner.kind]:
        raise ValueError('Invalid source operation')
    return owner


def validate_transient_action(action):
    if type(action) is not str or action not in ('discover','describe','author','publish'):
        raise ValueError('Invalid source operation')
    return action


def _uuid(value):
    if type(value) is not str or len(value)!=36 or str(UUID(value))!=value:
        raise ValueError('Invalid source operation identity')
    return value


class AcceptedApprovalRef(ApprovalModel):
    approval_id: str
    approval_revision: int = Field(ge=1,le=2**31-1)
    approval_digest: Digest
    audience_entry_id: Identifier
    principal: Principal
    package_pins_digest: Digest
    accepted_approval_expires_at: AwareTimestamp
    effective_deadline_at: AwareTimestamp
    _identity=field_validator('approval_id')(_uuid)

    @model_validator(mode='after')
    def capped(self):
        if self.effective_deadline_at>self.accepted_approval_expires_at:
            raise ValueError('Invalid accepted source deadline')
        return self


class AccessRequest(ApprovalModel):
    """Closed private variants; constructors confer no database provenance."""
    request_kind: Literal['owner','transient','admission']
    owner: OwnerAccessSelector | None
    product: PinnedProduct
    principal: Principal
    decision: Decision
    action: Literal['discover','describe','execute','result','author','publish']
    context: Literal['consumer','fixture_test']
    request_id: str
    pins: ReviewedPackagePins
    projection: RuntimeApprovalProjection
    audience_entry_id: Identifier
    accepted: AcceptedApprovalRef | None
    admitted_decision: Decision | None
    admitted_decision_digest: Digest | None
    fixture_case_id: Identifier | None
    invoking_actor: Principal | None
    invoking_actor_decision: Decision | None
    invoking_actor_entry_id: Identifier | None
    _identity=field_validator('request_id')(_uuid)

    @model_validator(mode='after')
    def closed_operation(self):
        if self.request_kind=='owner':
            if self.owner is None or self.accepted is None:
                raise ValueError('A retained owner is required')
            validate_owner_operation(self.owner,self.action)
            expected_context='consumer' if self.owner.kind=='execution' else 'fixture_test'
            if self.context!=expected_context:
                raise ValueError('Source context differs from owner')
            if self.owner.kind!='fixture_invocation' and (self.admitted_decision is None or self.admitted_decision_digest is None):
                raise ValueError('Immutable accepted decision is required')
            if self.admitted_decision is not None and canonical_digest(self.admitted_decision)!=self.admitted_decision_digest:
                raise ValueError('Accepted decision digest differs')
            if (self.accepted.principal!=self.principal or self.accepted.approval_id!=self.projection.approval_id
                or self.accepted.approval_revision!=self.projection.approval_revision
                or self.accepted.approval_digest!=self.projection.approval_digest
                or self.accepted.package_pins_digest!=self.pins.content_digest):
                raise ValueError('Retained approval identity differs')
            if self.owner.kind=='fixture_attempt' and (self.fixture_case_id is None or self.invoking_actor is None
                or self.invoking_actor_decision is None or self.invoking_actor_entry_id is None):
                raise ValueError('Original fixture actor and case are required')
        else:
            if any(value is not None for value in (self.owner,self.accepted,self.admitted_decision,self.admitted_decision_digest,
                self.fixture_case_id,self.invoking_actor,self.invoking_actor_decision,self.invoking_actor_entry_id)):
                raise ValueError('Transient access cannot carry accepted owner authority')
            if self.context!='consumer': raise ValueError('Transient context is invalid')
            if self.request_kind=='transient': validate_transient_action(self.action)
            elif self.action!='execute': raise ValueError('Admission is execute-only')
        if self.pins.approval_digest!=self.projection.approval_digest:
            raise ValueError('Protected source digest differs')
        if (self.product.manifest.content_digest!=self.pins.product.manifest_digest
            or self.product.manifest.product_id!=self.pins.product.product_id
            or self.product.manifest.version!=self.pins.product.version):
            raise ValueError('Exact product witness is required')
        return self


@dataclass(frozen=True, init=False)
class AccessReceipt:
    """Ephemeral transaction assertion, never a credential or reusable permit."""
    approval_id: str
    approval_revision: int
    approval_digest: str
    audience_entry_id: str
    projection_digest: str
    request_id: str
    _uow: object = field(repr=False,compare=False)
    _transaction: object = field(repr=False,compare=False)

    @classmethod
    def _issue(cls,uow,**values):
        transaction=uow.session.get_transaction()
        if transaction is None: raise ValueError('An active source transaction is required')
        result=object.__new__(cls)
        for name,value in values.items(): object.__setattr__(result,name,value)
        object.__setattr__(result,'_uow',uow);object.__setattr__(result,'_transaction',transaction)
        return result

    def assert_uow(self,uow):
        if uow is not self._uow or uow.session.get_transaction() is not self._transaction:
            raise ValueError('The source receipt is outside its transaction')


def _service_plain(value, *, depth=0):
    """Revalidate closed private types before any caller-controlled serializer."""
    if depth > 16:
        raise ValueError("Invalid source request")
    private_models = (AccessRequest, AcceptedApprovalRef, PinnedProduct,
        ExecutionOwner, FixtureInvocationOwner, FixtureAttemptOwner)
    if any(type(value) is model for model in private_models):
        data = value.__dict__
        extra = value.__pydantic_extra__
        if (type(data) is not dict or any(type(key) is not str for key in data)
                or set(data) != set(type(value).model_fields)
                or (extra is not None and (type(extra) is not dict or extra))):
            raise ValueError("Invalid source request")
        return {key: _service_plain(item, depth=depth+1) for key, item in data.items()}
    if type(value) is datetime:
        return value
    if type(value) in (tuple, list):
        if len(value) > 4096:
            raise ValueError("Invalid source request")
        return [_service_plain(item, depth=depth+1) for item in value]
    return _plain(value)


def _fresh_request(request):
    if type(request) is not AccessRequest:
        raise ValueError("Invalid source request")
    return AccessRequest.model_validate(_service_plain(request))


class SourceAccessService:
    def __init__(self,registry,audit):
        self.registry,self.audit=registry,audit

    async def prelock(self,uow,products:tuple[PinnedProduct,...],*,draft_ids:tuple[str,...]=(),version_ids:tuple[str,...]=(),policy_ids:tuple[str,...]=())->None:
        """Earlier phase only: C, sorted P/D/Q/V, never an approval row."""
        from gateway.execution.queue import lock_execution_capacity
        try:
            if type(products) is not tuple or not 1 <= len(products) <= 200:
                raise ValueError("Invalid source packages")
            if any(type(product) is not PinnedProduct for product in products):
                raise ValueError("Invalid source packages")
            products = tuple(PinnedProduct.model_validate(_service_plain(product)) for product in products)
            for values in (draft_ids, version_ids, policy_ids):
                if type(values) is not tuple or len(values) > 200:
                    raise ValueError("Invalid source dependencies")
                for value in values:
                    TypeAdapter(Identifier).validate_python(value, strict=True)
        except (ValueError, TypeError, AttributeError, GatewayError):
            raise not_found() from None
        await lock_execution_capacity(uow)
        for product_id in sorted({p.manifest.product_id for p in products}):
            # A canonical approval may precede first draft/product persistence.
            await uow.session.scalar(select(ProductRow).where(ProductRow.id==product_id).with_for_update())
        for draft_id in sorted(set(draft_ids)): await uow.lock_draft(draft_id)
        for policy_id in sorted({p.manifest.policy_id for p in products} | set(policy_ids)): await uow.lock_policy(policy_id)
        for version_id in sorted(set(version_ids)):
            await uow.session.scalar(select(VersionState).where(VersionState.version_id==version_id).with_for_update())

    async def scope_fingerprint(self, uow) -> str:
        """Opaque conservative cursor generation; never grants product access."""
        value = await uow.session.scalar(text("SELECT gateway.source_approval_scope_fingerprint()"))
        return TypeAdapter(Digest).validate_python(value, strict=True)

    def precheck(self,request:AccessRequest)->str:
        try:
            request=_fresh_request(request)
            matched=evaluate_runtime_source_access(request.projection,request.principal,request.decision,
                action=request.action,context=request.context,pins=request.pins,fixture_case_id=request.fixture_case_id)
            if matched!=request.audience_entry_id: raise ValueError
            if request.invoking_actor is not None:
                actor_match=evaluate_runtime_source_access(request.projection,request.invoking_actor,request.invoking_actor_decision,
                    action='author',context='fixture_test',pins=request.pins)
                if actor_match!=request.invoking_actor_entry_id: raise ValueError
            return matched
        except (ValueError,TypeError,AttributeError):
            raise not_found() from None

    async def resolve_owner(self,uow,owner:OwnerAccessSelector,principal:Principal,decision:Decision,*,action:OwnerAction,request_id:str)->AccessRequest:
        try:
            owner=validate_owner_operation(owner,action);_uuid(request_id)
            principal=_fresh(principal,Principal);decision=_fresh(decision,Decision)
            _rows(decision.row_constraints)
        except (ValueError,TypeError,AttributeError,GatewayError):
            raise not_found() from None
        try:
            if owner.kind=='execution':
                value=await uow.session.scalar(text('SELECT gateway.source_read_execution_access(:id,:action)'),
                    {'id':owner.id,'action':action})
            else:
                value=await uow.session.scalar(text('SELECT gateway.source_read_fixture_owner(:kind,:id,:action)'),
                    {'kind':owner.kind,'id':owner.id,'action':action})
            request=_owner_request(value,owner,principal,decision,action,request_id)
            self.precheck(request)
            return request
        except SQLAlchemyError:
            await self._deny(uow,principal,action,request_id,'UNAVAILABLE')
        except (ValueError,TypeError,AttributeError,KeyError,GatewayError):
            await self._deny(uow,principal,action,request_id,'AUDIENCE_DENIED')

    async def resolve_transient(self,uow,product:PinnedProduct,principal:Principal,decision:Decision,*,action:TransientAction,request_id:str)->AccessRequest:
        validate_transient_action(action)
        return await self._resolve_package(uow,product,principal,decision,action=action,request_id=request_id,kind='transient')

    async def match_transient(self,uow,product:PinnedProduct,principal:Principal,decision:Decision,*,action:TransientAction,request_id:str)->AccessRequest | None:
        """Non-authoritative action probe; never audits, rolls back or grants release.

        Only ordinary absent/ambiguous/audience-denied matches return None.
        Invalid models, protected projections and unavailable registry stay errors.
        """
        validate_transient_action(action)
        return await self._resolve_package(uow,product,principal,decision,action=action,request_id=request_id,kind='transient',probe=True)

    async def resolve_admission(self,uow,product:PinnedProduct,principal:Principal,decision:Decision,*,request_id:str)->AccessRequest:
        return await self._resolve_package(uow,product,principal,decision,action='execute',request_id=request_id,kind='admission')

    async def _deny(self,uow,principal,action,request_id,reason):
        """Rollback first, then commit one target-free source denial separately."""
        from gateway.policy.audit import audit_unavailable
        try:
            await uow.session.rollback()
            event=self.audit.event(principal,action,decision='deny',correlation_id=request_id,
                source_access_reason_code=reason)
            async with self.registry.transaction() as denied:
                await self.audit.append(denied,event)
        except Exception:
            raise audit_unavailable() from None
        denied=not_found()
        raise GatewayError(denied.code,request_id,denied.safe_message)

    async def _resolve_package(self,uow,product,principal,decision,*,action,request_id,kind,probe=False):
        """Preliminary protected match only. No lock or reusable allow is issued."""
        try:
            _uuid(request_id)
            if type(product) is not PinnedProduct: raise ValueError('Invalid product')
            product=PinnedProduct.model_validate(_service_plain(product))
            principal=_fresh(principal,Principal)
            decision=_fresh(decision,Decision)
            _rows(decision.row_constraints)
        except (ValueError,TypeError,AttributeError,GatewayError):
            # Invalid in-process objects have no trusted actor to audit.
            raise not_found() from None
        reason='PACKAGE_MISMATCH'
        try:
            manifest=product.manifest
            binding=await uow.session.scalar(select(BindingRevision).where(
                BindingRevision.binding_id==manifest.binding_id,
                BindingRevision.revision==manifest.binding_revision))
            if (binding is None or binding.content_digest!=product.binding_digest
                or binding.adapter_revision!=manifest.adapter_revision
                or binding.model_bundle_digest!=manifest.model_bundle_digest):
                raise ValueError('Protected binding differs')
            rows=await uow.session.scalars(text('SELECT gateway.source_approval_candidates(:product,:version,:digest)'),
                {'product':manifest.product_id,'version':manifest.version,'digest':manifest.content_digest})
            matches=[];reason='MISSING'
            for raw in rows:
                projected=RuntimeApprovalProjection.model_validate(_plain(raw))
                pins=ReviewedPackagePins.model_validate(projected._package_content() |
                    {'manifest':_plain(manifest),'approval_digest':projected.approval_digest})
                if pins.bindings.consumer.content_digest!=product.binding_digest:
                    raise ValueError('Protected approval binding differs')
                reason='AUDIENCE_DENIED'
                try:
                    entry=evaluate_runtime_source_access(projected,principal,decision,
                        action=action,context='consumer',pins=pins)
                except ValueError:
                    continue
                values=dict.fromkeys(AccessRequest.model_fields)
                values.update(request_kind=kind,product=product,principal=principal,decision=decision,
                    action=action,context='consumer',request_id=request_id,pins=pins,
                    projection=projected,audience_entry_id=entry)
                matches.append(AccessRequest.model_validate(values))
            if len(matches)!=1:
                if probe: return None
                if len(matches)>1:reason='AMBIGUOUS'
                raise ValueError('No unique protected tuple')
            return matches[0]
        except SQLAlchemyError:
            if probe: raise not_found() from None
            await self._deny(uow,principal,action,request_id,'UNAVAILABLE')
        except (ValueError,TypeError,AttributeError,KeyError,GatewayError):
            if probe: raise not_found() from None
            await self._deny(uow,principal,action,request_id,reason)

    async def finalize(self,uow,requests:tuple[AccessRequest,...])->tuple[AccessReceipt,...]:
        try:
            if type(requests) is not tuple or not 1<=len(requests)<=200:
                raise ValueError('Invalid source requests')
            requests=tuple(_fresh_request(request) for request in requests)
        except (ValueError,TypeError,AttributeError,GatewayError):
            raise not_found() from None
        first=requests[0]
        try:
            for request in requests: self.precheck(request)
            wire=[_final_wire(request) for request in requests]
            # Flush the entire earlier phase before taking any A lock. Audit
            # writes below only insert new rows, with no mutable owner locks.
            await uow.session.flush()
            statement=text('SELECT gateway.source_finalize_access(CAST(:requests AS jsonb))')
            params={'requests':json.dumps(wire)}
            checked=await uow.session.scalar(statement,params)
            if type(checked) is not list or len(checked)!=len(requests): raise ValueError('Invalid final source response')
            for request,value in zip(requests,checked,strict=True):
                if request.owner is not None:
                    protected=_owner_request(value,request.owner,request.principal,request.decision,request.action,request.request_id)
                    if protected!=request: raise ValueError('Retained source request differs')
                event=self.audit.event(request.principal,request.action,product=request.product,
                    policy_revision=request.decision.policy_revision,decision='allow',correlation_id=request.request_id,
                    source_approval_id=request.projection.approval_id,source_approval_revision=request.projection.approval_revision,
                    source_approval_digest=request.projection.approval_digest,source_approval_state_revision=1,
                    source_access_reason_code='APPROVED')
                await self.audit.append(uow,event)
            await uow.session.flush()
            # An audit flush can take time even though A locks block revocation.
            # Recheck all requests at one fresh DB instant after that flush.
            rechecked=await uow.session.scalar(statement,params)
            if rechecked!=checked: raise ValueError('Source authority changed')
            return tuple(AccessReceipt._issue(uow,approval_id=request.projection.approval_id,
                approval_revision=request.projection.approval_revision,approval_digest=request.projection.approval_digest,
                audience_entry_id=request.audience_entry_id,projection_digest=request.projection.projection_digest,
                request_id=request.request_id) for request in requests)
        except SQLAlchemyError:
            await self._deny(uow,first.principal,first.action,first.request_id,'UNAVAILABLE')
        except (ValueError,TypeError,AttributeError,KeyError,GatewayError):
            await self._deny(uow,first.principal,first.action,first.request_id,'AUDIENCE_DENIED')


    async def load_fixture_invocation_access(self,uow,invocation_id):
        return await load_fixture_invocation_access(uow,invocation_id)

    async def load_fixture_attempt_access(self,uow,attempt_id):
        return await load_fixture_attempt_access(uow,attempt_id)

    async def load_fixture_case_review(self,uow,*,invocation_id,fixture_id,request_id):
        return await load_fixture_case_review(uow,invocation_id=invocation_id,fixture_id=fixture_id,request_id=request_id)

    async def load_fixture_test_access(self,uow,test_run_id):
        return await load_fixture_test_access(uow,test_run_id)

    async def accept_fixture_invocation(self,uow,*,draft_id,actor,catalog_canonical,run_id,request_id):
        from gateway.source_fixture_access import accept_fixture_invocation
        return await accept_fixture_invocation(self,uow,draft_id=draft_id,actor=actor,
            catalog_canonical=catalog_canonical,run_id=run_id,request_id=request_id)

    async def record_publication_operation(self,uow,*,actor,action,product_id,version_id,draft_id,test_run_id,
            previous_version_id,expected_product_revision,expected_draft_revision,allow_audit_id,request_id):
        from gateway.source_fixture_access import record_publication_operation
        return await record_publication_operation(self,uow,actor=actor,action=action,product_id=product_id,
            version_id=version_id,draft_id=draft_id,test_run_id=test_run_id,previous_version_id=previous_version_id,
            expected_product_revision=expected_product_revision,expected_draft_revision=expected_draft_revision,
            allow_audit_id=allow_audit_id,request_id=request_id)


def _owner_request(value,owner,principal,decision,action,request_id):
    if owner.kind=='execution':
        return _execution_request(value,owner,principal,decision,action,request_id)
    from gateway.source_fixture_access import fixture_owner_request
    return fixture_owner_request(value,owner,principal,decision,action,request_id)


def _execution_request(value,owner,principal,decision,action,request_id):
    value=_plain(value)
    if type(value) is not dict or set(value)!={'accepted','projection','decision','audience_entry_id',
        'admitted_decision','admitted_decision_digest','manifest','binding_digest','version_id'}:
        raise ValueError('Invalid retained source authority')
    accepted=AcceptedApprovalRef.model_validate(value['accepted'])
    current=Decision.model_validate(value['decision'])
    if accepted.principal!=principal or current!=decision:
        raise ValueError('Retained requester or current decision differs')
    projection=RuntimeApprovalProjection.model_validate(value['projection'])
    pins=ReviewedPackagePins.model_validate(projection._package_content()|
        {'manifest':value['manifest'],'approval_digest':projection.approval_digest})
    product=PinnedProduct.model_validate({key:value[key] for key in ('manifest','binding_digest','version_id')})
    values=dict.fromkeys(AccessRequest.model_fields)
    values.update(request_kind='owner',owner=owner,product=product,principal=principal,decision=current,
        action=action,context='consumer',request_id=request_id,pins=pins,projection=projection,
        audience_entry_id=value['audience_entry_id'],accepted=accepted,
        admitted_decision=Decision.model_validate(value['admitted_decision']),admitted_decision_digest=value['admitted_decision_digest'])
    return AccessRequest.model_validate(values)


def _final_wire(request):
    return {'kind':request.request_kind,'owner_kind':request.owner.kind if request.owner else None,
        'owner_id':request.owner.id if request.owner else None,
        'manifest_canonical':None if request.owner else canonical_bytes(request.product.manifest.model_dump(mode='python',exclude={'content_digest'})).decode(),
        'principal':request.principal.model_dump(mode='json'),'decision':request.decision.model_dump(mode='json'),
        'action':request.action,'binding_digest':request.product.binding_digest,
        'approval_id':request.projection.approval_id,'approval_revision':request.projection.approval_revision,
        'approval_digest':request.projection.approval_digest,'projection_digest':request.projection.projection_digest,
        'audience_entry_id':request.audience_entry_id}


async def load_execution_access(uow,execution_id:str)->AcceptedApprovalRef:
    try:
        TypeAdapter(Identifier).validate_python(execution_id,strict=True)
        # Loading immutable pins is not an action gate. In particular an
        # execute-only approval must not need a result audience to be scheduled.
        value=await uow.session.scalar(text("""SELECT jsonb_build_object(
            'approval_id',approval_id,'approval_revision',approval_revision,'approval_digest',approval_digest,
            'audience_entry_id',audience_entry_id,'principal',principal_snapshot,'package_pins_digest',package_pins_digest,
            'accepted_approval_expires_at',accepted_approval_expires_at,'effective_deadline_at',effective_deadline_at)
            FROM gateway.execution_source_authorities WHERE execution_id=:id"""),{'id':execution_id})
        return AcceptedApprovalRef.model_validate(_plain(value))
    except (ValueError,TypeError,AttributeError,KeyError,GatewayError,SQLAlchemyError):
        raise not_found() from None


async def load_fixture_invocation_access(uow,invocation_id:str):
    from gateway.source_fixture_access import load_fixture_invocation_access as load
    return await load(uow,invocation_id)


async def load_fixture_attempt_access(uow,attempt_id:str):
    from gateway.source_fixture_access import load_fixture_attempt_access as load
    return await load(uow,attempt_id)


async def load_fixture_case_review(uow,*,invocation_id,fixture_id,request_id):
    from gateway.source_fixture_access import load_fixture_case_review as load
    return await load(uow,invocation_id=invocation_id,fixture_id=fixture_id,request_id=request_id)


async def load_fixture_test_access(uow,test_run_id):
    from gateway.source_fixture_access import load_fixture_test_access as load
    return await load(uow,test_run_id)


async def validate_registry_packages(registry,packages:tuple[ReviewedPackagePins,...])->None:
    """Check every configured package against current protected registry records.

    This is readiness only, not a reusable allow or provider permission claim.
    Request release still needs its own retained-owner final transaction check.
    """
    try:
        if type(packages) is not tuple or not 1 <= len(packages) <= 100:
            raise ValueError("Invalid configured packages")
        packages = tuple(_fresh(package, ReviewedPackagePins) for package in packages)
        if any(package.approval_digest is None for package in packages):
            raise ValueError("Explicit configured approval required")
        expected = [{"product_id": package.product.product_id, "version": package.product.version,
            "manifest_digest": package.product.manifest_digest, "package_pins_digest": package.package_pins_digest,
            "approval_digest": package.approval_digest} for package in packages]
        async with registry.transaction() as uow:
            current = await uow.session.scalar(text("SELECT gateway.source_approval_check_packages(CAST(:expected AS jsonb))"),
                {"expected": json.dumps(expected)})
            if current is not True:
                raise ValueError("No exact active configured approval")
    except (ValueError, TypeError, AttributeError, KeyError, GatewayError):
        raise not_found() from None
