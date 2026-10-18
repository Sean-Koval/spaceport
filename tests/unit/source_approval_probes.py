"""Contract-strict in-memory sequencing probes, never database approval evidence.

These objects do not issue AccessReceipt, accept retained owners, dispatch a
source, or stand in for production finalizer/fixture/publication qualification.
"""
from uuid import uuid4

from gateway.contracts.canonical import canonical_bytes
from gateway.registry.repository import not_found
from gateway.source_access import AccessRequest
from gateway.source_access_contracts import evaluate_runtime_source_access, validate_source_approval


class TransientApprovalProbe:
    def __init__(self, registry, audit):
        self.registry, self.audit = registry, audit
        self.approvals = {}
        self.batches = []

    def install(self, document, manifest):
        approved = validate_source_approval(canonical_bytes(document))
        pins = approved.package_pins(manifest, approval_digest=approved.content_digest)
        projection = approved.runtime_projection(str(uuid4()))
        self.approvals[manifest.content_digest] = (pins, projection)
        return projection.approval_id

    async def prelock(self, uow, products, **kwargs):
        assert not uow.session.aborted

    async def match_transient(self, uow, product, principal, decision, *, action, request_id):
        assert not uow.session.aborted
        record = self.approvals.get(product.manifest.content_digest)
        if record is None:
            return None
        pins, projection = record
        if product.binding_digest != pins.bindings.consumer.content_digest:
            raise not_found()
        try:
            entry = evaluate_runtime_source_access(projection, principal, decision,
                action=action, context='consumer', pins=pins)
        except ValueError:
            return None
        data = dict.fromkeys(AccessRequest.model_fields)
        data.update(request_kind='transient', product=product, principal=principal,
            decision=decision, action=action, context='consumer', request_id=request_id,
            pins=pins, projection=projection, audience_entry_id=entry)
        return AccessRequest.model_validate(data)

    async def _deny(self, uow, principal, action, request_id, reason):
        from gateway.source_access import SourceAccessService
        await SourceAccessService(self.registry, self.audit)._deny(uow, principal, action, request_id, reason)

    async def resolve_transient(self, uow, product, principal, decision, *, action, request_id):
        request = await self.match_transient(uow, product, principal, decision,
            action=action, request_id=request_id)
        if request is None:
            await self._deny(uow, principal, action, request_id, 'AUDIENCE_DENIED')
        return request

    async def finalize(self, uow, requests):
        assert not uow.session.aborted
        assert type(requests) is tuple and 1 <= len(requests) <= 200
        checked = []
        for request in requests:
            assert request.request_kind == 'transient' and request.owner is None
            current = await self.match_transient(uow, request.product, request.principal,
                request.decision, action=request.action, request_id=request.request_id)
            if current != request:
                await self._deny(uow, request.principal, request.action, request.request_id, 'AUDIENCE_DENIED')
            checked.append(current)
        self.batches.append(tuple(checked))
        for request in checked:
            await self.audit.append(uow, self.audit.event(request.principal, request.action,
                product=request.product, policy_revision=request.decision.policy_revision,
                decision='allow', correlation_id=request.request_id,
                source_approval_id=request.projection.approval_id,
                source_approval_revision=request.projection.approval_revision,
                source_approval_digest=request.projection.approval_digest,
                source_approval_state_revision=1, source_access_reason_code='APPROVED'))
        return ()
