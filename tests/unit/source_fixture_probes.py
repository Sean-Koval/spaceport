"""Contract-strict retained fixture sequencing probes, never SQL authority.

This probe models an immutable invocation/attempt roster and accepts only exact
validated AccessRequest values. It creates no AccessReceipt, provider operation,
publication witness or database owner. Genuine PostgreSQL lanes qualify those.
"""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

from gateway.contracts.canonical import canonical_digest
from gateway.contracts.identity import Principal
from gateway.contracts.errors import GatewayError
from gateway.registry.repository import not_found
from gateway.source_access import AccessRequest, AcceptedApprovalRef, validate_owner_operation
from gateway.source_access_contracts import evaluate_runtime_source_access
from tests.unit.source_approval_probes import TransientApprovalProbe


class RetainedFixtureProbe(TransientApprovalProbe):
    def retain(self, product, catalog, test, actor, decisions):
        """Explicit test setup only; never the production acceptance entrypoint."""
        self.product, self.catalog = product, catalog
        self.catalog_digest = catalog.digest
        self.case_digests = {case.fixture.id:canonical_digest(case) for case in catalog.cases}
        assert len(self.case_digests)==len(catalog.cases)
        self.decisions = deepcopy(decisions)
        self.test_pins = {name:getattr(test,name) for name in
            ('id','product_id','draft_id','draft_digest','actor_id','fixture_version','policy_id','policy_revision')}
        pins, projection = self.approvals[product.manifest.content_digest]
        assert pins.artifacts.fixture_catalog_digest == catalog.digest
        self.actor = actor
        self.cases = {}
        self.attempts = {}
        self.accepted = {}
        self.owner_products = {}
        for case in catalog.cases:
            principal = Principal(id=case.principal_id,subject=case.principal_id,kind='demo',service_client_id=None)
            review = SimpleNamespace(invocation_id=test.id,fixture_id=case.fixture.id,kind=case.kind,
                fixture_version=case.fixture.version,case_digest=canonical_digest(case),principal=principal,
                case=case.model_dump(mode='python'),catalog_digest=catalog.digest)
            self.cases[case.fixture.id] = review
            if case.kind == 'known_answer':
                check = next(check for check in test.evidence['checks'] if check['fixture_id'] == case.fixture.id)
                assert check['attempt_id'] and check['decision_digest'] == canonical_digest(decisions[principal.id])
                entry = evaluate_runtime_source_access(projection,principal,decisions[principal.id],action='execute',
                    context='fixture_test',pins=pins,fixture_case_id=case.fixture.id)
                accepted = self._accepted(pins,projection,principal,entry)
                snapshot = SimpleNamespace(attempt_id=check['attempt_id'],invocation_id=test.id,
                    fixture_id=case.fixture.id,product=product,principal=principal,actor=actor,
                    admitted_decision=decisions[principal.id],accepted=accepted.model_dump(mode='python'),
                    effective_deadline_at=accepted.effective_deadline_at)
                self.attempts[snapshot.attempt_id] = snapshot
                self.accepted[('fixture_attempt',snapshot.attempt_id)] = accepted
        entry = evaluate_runtime_source_access(projection,actor,decisions[actor.id],action='author',
            context='fixture_test',pins=pins)
        accepted = self._accepted(pins,projection,actor,entry)
        self.accepted[('fixture_invocation',test.id)] = accepted
        self.invocation = SimpleNamespace(invocation_id=test.id,product=product,draft_id=test.draft_id,
            draft_digest=test.draft_digest,catalog_digest=catalog.digest,
            execution_contract_digest=catalog.execution_contract_digest,
            consumer_binding_digest=product.binding_digest,fixture_binding_digest=pins.bindings.fixture.content_digest,
            actor=actor,accepted=accepted.model_dump(mode='python'),effective_deadline_at=accepted.effective_deadline_at,
            cases=tuple(self.cases.values()))
        self.test = test
        self.evidence_digest = canonical_digest(test.evidence)
        self.owner_batches = []

    @staticmethod
    def _accepted(pins,projection,principal,entry):
        return AcceptedApprovalRef(approval_id=projection.approval_id,approval_revision=projection.approval_revision,
            approval_digest=projection.approval_digest,audience_entry_id=entry,principal=principal,
            package_pins_digest=pins.content_digest,accepted_approval_expires_at=datetime(2030,1,1,tzinfo=timezone.utc),
            effective_deadline_at=datetime(2027,1,1,tzinfo=timezone.utc))

    def _check_roster(self):
        identities=[case.fixture.id for case in self.catalog.cases]
        invocation_ids=[case.fixture_id for case in self.invocation.cases]
        if (self.catalog.digest!=self.catalog_digest or self.invocation.catalog_digest!=self.catalog_digest
            or len(identities)!=len(set(identities)) or len(invocation_ids)!=len(set(invocation_ids))
            or set(identities)!=set(self.case_digests) or set(invocation_ids)!=set(self.case_digests)
            or set(self.cases)!=set(self.case_digests)):
            raise not_found()
        for case in self.invocation.cases:
            if canonical_digest(case.case)!=self.case_digests[case.fixture_id]:
                raise not_found()
        if any(getattr(self.test,name) != expected for name,expected in self.test_pins.items()):
            raise not_found()
        if self.invocation.product != self.product or self.invocation.actor != self.actor:
            raise not_found()
        for case in self.catalog.cases:
            review = self.cases.get(case.fixture.id)
            if (review is None or review.case_digest != canonical_digest(case)
                or canonical_digest(review.case) != review.case_digest or review.invocation_id != self.test.id):
                raise not_found()
        if canonical_digest(self.test.evidence) != self.evidence_digest:
            raise not_found()
        checks = self.test.evidence['checks']
        if {check['fixture_id'] for check in checks} != set(self.cases):
            raise not_found()
        expected_attempts = {check['attempt_id'] for check in checks if check['kind'] == 'known_answer'}
        if expected_attempts != set(self.attempts):
            raise not_found()

    async def load_fixture_test_access(self,uow,test_run_id):
        assert not uow.session.aborted
        self._check_roster()
        if test_run_id != self.test.id:
            raise not_found()
        return SimpleNamespace(invocation=deepcopy(self.invocation),attempts=tuple(deepcopy(tuple(self.attempts.values()))))

    async def load_fixture_case_review(self,uow,*,invocation_id,fixture_id,request_id):
        assert not uow.session.aborted
        self._check_roster()
        if invocation_id != self.test.id or fixture_id not in self.cases:
            raise not_found()
        return deepcopy(self.cases[fixture_id])

    async def resolve_owner(self,uow,owner,principal,decision,*,action,request_id):
        assert not uow.session.aborted
        owner = validate_owner_operation(owner,action)
        self._check_roster()
        key = (owner.kind,owner.id)
        accepted = self.accepted.get(key)
        record = self.approvals.get(self.product.manifest.content_digest)
        if accepted is None or record is None or accepted.principal != principal:
            await self._deny(uow,principal,action,request_id,'AUDIENCE_DENIED')
        pins,projection = record
        try:
            fresh = self.current_decision(principal.id,action)
        except GatewayError:
            await self._deny(uow,principal,action,request_id,'AUDIENCE_DENIED')
        if fresh != decision:
            await self._deny(uow,principal,action,request_id,'AUDIENCE_DENIED')
        case_id = None
        admitted = None
        if owner.kind == 'fixture_attempt':
            attempt = self.attempts[owner.id]
            case_id,admitted = attempt.fixture_id,attempt.admitted_decision
            if (attempt.product != self.product or attempt.actor != self.actor or attempt.principal != principal
                or decision != admitted or attempt.invocation_id != self.test.id):
                await self._deny(uow,principal,action,request_id,'AUDIENCE_DENIED')
        elif owner.kind != 'fixture_invocation' or owner.id != self.test.id or principal != self.actor:
            raise not_found()
        try:
            entry = evaluate_runtime_source_access(projection,principal,decision,action=action,
                context='fixture_test',pins=pins,fixture_case_id=case_id)
            actor_entry = evaluate_runtime_source_access(projection,self.actor,self.current_author_decision(),
                action='author',context='fixture_test',pins=pins)
            values = dict.fromkeys(AccessRequest.model_fields)
            values.update(request_kind='owner',owner=owner,product=self.product,principal=principal,decision=decision,
                action=action,context='fixture_test',request_id=request_id,pins=pins,projection=projection,
                audience_entry_id=entry,accepted=accepted,admitted_decision=admitted,
                admitted_decision_digest=canonical_digest(admitted) if admitted is not None else None,
                fixture_case_id=case_id,invoking_actor=self.actor if case_id else None,
                invoking_actor_decision=self.current_author_decision() if case_id else None,
                invoking_actor_entry_id=actor_entry if case_id else None)
            return AccessRequest.model_validate(values)
        except ValueError:
            await self._deny(uow,principal,action,request_id,'AUDIENCE_DENIED')

    async def finalize(self,uow,requests):
        assert not uow.session.aborted and type(requests) is tuple and 1 <= len(requests) <= 200
        assert len({request.request_id for request in requests}) == len(requests)
        checked = []
        owners = set()
        for request in requests:
            if request.request_kind == 'owner':
                current = await self.resolve_owner(uow,request.owner,request.principal,request.decision,
                    action=request.action,request_id=request.request_id)
                owners.add((request.owner.kind,request.owner.id,request.action))
            else:
                assert request.request_kind == 'transient'
                current = await self.match_transient(uow,request.product,request.principal,request.decision,
                    action=request.action,request_id=request.request_id)
            if current != request:
                await self._deny(uow,request.principal,request.action,request.request_id,'AUDIENCE_DENIED')
            checked.append(current)
        if owners:
            for case in self.catalog.cases:
                if case.kind == 'permission_negative':
                    try:
                        self.current_decision(case.principal_id,'execute')
                    except GatewayError as error:
                        if error.code != 'NOT_FOUND_OR_FORBIDDEN':
                            raise
                    else:
                        await self._deny(uow,self.actor,'author',requests[0].request_id,'AUDIENCE_DENIED')
            expected = {('fixture_invocation',self.test.id,'author')} | {
                ('fixture_attempt',identity,'result') for identity in self.attempts}
            if owners != expected:
                await self._deny(uow,self.actor,'author',requests[0].request_id,'AUDIENCE_DENIED')
            self.owner_batches.append(tuple(checked))
        self.batches.append(tuple(checked))
        for request in checked:
            await self.audit.append(uow,self.audit.event(request.principal,request.action,product=request.product,
                policy_revision=request.decision.policy_revision,decision='allow',correlation_id=request.request_id,
                source_approval_id=request.projection.approval_id,source_approval_revision=request.projection.approval_revision,
                source_approval_digest=request.projection.approval_digest,source_approval_state_revision=1,
                source_access_reason_code='APPROVED'))
        return ()
