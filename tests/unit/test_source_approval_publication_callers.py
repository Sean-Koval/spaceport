"""Publication caller ordering only; protected authority requires PostgreSQL tests."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from gateway.contracts.errors import GatewayError
from gateway.contracts.identity import Principal
from gateway.publishing.service import PublishingService, review_target
from gateway.registry.models import ProductVersion
from gateway.registry.repository import not_found
from tests.unit.test_publication_definitions import make_catalog_evidence

pytestmark = pytest.mark.unit


class EvidenceReviewProbe:
    def __init__(self, *, previous=False, missing=False, denied=False):
        binding, product, _, stored, _ = make_catalog_evidence()
        stored.id = 'retained-run'
        stored.product_id = product.manifest.product_id
        stored.draft_digest = product.manifest.content_digest
        stored.policy_id = product.manifest.policy_id
        stored.actor_id = 'original-author'
        self.actor = Principal(id='current-reviewer',subject='current-reviewer',kind='user',service_client_id='review-client')
        self.stored = stored
        self.before = deepcopy(stored.evidence)
        self.target = review_target(product.manifest,stored.draft_id,binding.digest)
        self.draft = SimpleNamespace(id=stored.draft_id,revision=1,content_digest=stored.draft_digest)
        self.old = SimpleNamespace(id='previous-version',manifest=product.manifest.model_dump(mode='json'),
            binding_digest=binding.digest,content_digest=product.manifest.content_digest) if previous else None
        self.head = SimpleNamespace(current_version_id=self.old.id if previous else None)
        self.trace, self.batches = [], []
        self.missing, self.denied = missing, denied
        self.original_author = object()
        self.fixed_result = object()
        self.uow = SimpleNamespace(session=self)
        probe = self
        class Service(PublishingService):
            async def _draft(self,uow,draft_id):
                assert uow is probe.uow and draft_id == probe.draft.id
                probe.trace.append('draft-prefix')
                return probe.head,probe.draft,probe.target
            async def _prepare_review(self,actor,target,uow):
                assert actor == probe.actor
                probe.trace.append('review:'+target.version_id)
                return SimpleNamespace(visible_fields=('day','segment')),('author',),(target.version_id,)
            async def _latest_test(self,uow,draft):
                probe.trace.append('latest-test')
                return None if probe.missing else probe.stored
            async def _stale(self,*args):
                probe.trace.append('stale-policy-source-work')
                return ('missing',) if probe.missing else ()
        self.service = Service(registry=self,policy=SimpleNamespace(
            requires_source_approval=lambda product:True,source_access=self),bindings=None,bundles=None,fixture_runner=self)

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self.uow
        except Exception:
            self.trace.append('rollback')
            raise
        else:
            self.trace.append('commit')

    async def scalar(self,statement):
        self.trace.append('test-read')
        return self.stored

    async def get(self,model,identity):
        assert model is ProductVersion and self.old is not None and identity == self.old.id
        self.trace.append('previous-version')
        return self.old

    async def prelock(self,uow,products,**kwargs):
        assert uow is self.uow
        self.trace.append('all-earlier-locks')

    async def prepare_evidence_access(self,uow,test):
        assert uow is self.uow and test is self.stored and test.actor_id != self.actor.id
        self.trace.append('original-evidence-owners')
        return self.original_author,self.fixed_result

    async def finalize(self,uow,requests):
        assert uow is self.uow
        self.trace.append('finalize')
        self.batches.append(requests)
        if self.denied:
            raise not_found()


@pytest.mark.parametrize('operation', ['get_test','diff'])
def test_evidence_review_batches_current_reviewer_and_original_owners_once(operation):
    async def scenario():
        probe = EvidenceReviewProbe(previous=operation=='diff')
        if operation == 'get_test':
            result = await probe.service.get_test(probe.actor,probe.draft.id,probe.stored.id)
            assert result.id == probe.stored.id and result.actor_id == 'original-author'
            assert all('revenue' not in row for check in result.evidence['checks'] for key in ('expected','actual') for row in check[key])
        else:
            result = await probe.service.diff(probe.actor,probe.draft.id)
            assert result.latest_test_run_id == probe.stored.id
            assert probe.trace.index('stale-policy-source-work') < probe.trace.index('original-evidence-owners')
        assert probe.stored.evidence == probe.before
        assert len(probe.batches) == 1
        expected = ((probe.target.version_id,'previous-version') if operation == 'diff' else (probe.target.version_id,))
        assert probe.batches[0] == expected + (probe.original_author,probe.fixed_result)
        assert probe.trace[-2:] == ['finalize','commit']
        assert probe.trace.index('all-earlier-locks') < probe.trace.index('original-evidence-owners')
        assert max(i for i,item in enumerate(probe.trace) if item.startswith('review:')) < probe.trace.index('original-evidence-owners')
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['get_test','diff'])
def test_late_evidence_denial_releases_no_projection(operation):
    async def scenario():
        probe = EvidenceReviewProbe(denied=True)
        with pytest.raises(GatewayError) as caught:
            if operation == 'get_test':
                await probe.service.get_test(probe.actor,probe.draft.id,probe.stored.id)
            else:
                await probe.service.diff(probe.actor,probe.draft.id)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert probe.trace[-2:] == ['finalize','rollback']
        assert probe.stored.evidence == probe.before
    asyncio.run(scenario())


def test_diff_without_a_test_has_no_invented_evidence_authority():
    async def scenario():
        probe = EvidenceReviewProbe(missing=True)
        result = await probe.service.diff(probe.actor,probe.draft.id)
        assert result.latest_test_run_id is None and result.stale_evidence == ('missing',)
        assert probe.batches == [(probe.target.version_id,)]
        assert 'original-evidence-owners' not in probe.trace
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['get_test','diff'])
def test_reviewed_evidence_without_runner_cannot_fall_back_to_reviewer_approval(operation):
    async def scenario():
        probe = EvidenceReviewProbe()
        probe.service.fixture_runner = None
        with pytest.raises(GatewayError) as caught:
            if operation == 'get_test':
                await probe.service.get_test(probe.actor,probe.draft.id,probe.stored.id)
            else:
                await probe.service.diff(probe.actor,probe.draft.id)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert not probe.batches
    asyncio.run(scenario())


class PublicationOperationProbe:
    """Checks call order and receipt plumbing, never protected SQL acceptance."""
    def __init__(self, *, denied=False, audit_denied=False):
        from gateway.policy.audit import AuditWriter
        binding, product, _, stored, _ = make_catalog_evidence()
        self.actor = Principal(id='current-publisher',subject='current-publisher',kind='user',service_client_id='publisher-client')
        self.product = product
        self.draft = SimpleNamespace(id='draft-1',revision=1,content_digest=product.manifest.content_digest)
        self.target = review_target(product.manifest,self.draft.id,binding.digest)
        self.previous = SimpleNamespace(id='previous-version',draft_id='previous-draft',manifest=product.manifest.model_dump(mode='json'),
            binding_digest=binding.digest,product_id=product.manifest.product_id,version='1.0.0')
        self.versions = {self.previous.id: self.previous}
        self.prelocks, self.prepared_targets = [], []
        self.head = SimpleNamespace(current_version_id=self.previous.id,publication_revision=7,source_operation_id='previous-operation')
        self.state = SimpleNamespace(status='published',actor_id='old-publisher',reason='Original',source_operation_id='previous-operation')
        stored.id = 'retained-run'
        self.stored = stored
        self.trace, self.events, self.calls = [], [], []
        self.denied, self.audit_denied = denied, audit_denied
        self.receipt = 'b965a6c7-f728-4d75-bf4e-f9dcbf31ce11'
        self.audit = AuditWriter(b'publication-operation-ordering-key-32')
        self.audit.append = self.append_audit
        self.uow = SimpleNamespace(session=self,insert_version=self.insert_version,lock_product=self.lock_product)
        probe = self
        class Service(PublishingService):
            async def _draft(self,uow,draft_id):
                assert uow is probe.uow and draft_id == probe.draft.id
                probe.trace.append('draft-prefix')
                return probe.head,probe.draft,probe.target
            async def _prepare_scopes(self,actor,targets,action,uow):
                assert actor == probe.actor and action == 'publish'
                probe.trace.append('prepare-publish')
                probe.prepared_targets.extend(targets)
                from uuid import uuid4
                decisions = {item.version_id:SimpleNamespace(policy_revision=1) for item in targets}
                requests = tuple(SimpleNamespace(product=item,principal=actor,action=action,
                    decision=decisions[item.version_id],request_id=str(uuid4()),
                    projection=SimpleNamespace(approval_id='36ce8fbc-efdd-4a6b-9d6d-1115e9d4f3c8',
                        approval_revision=1,approval_digest='a'*64)) for item in targets)
                return decisions,requests
            async def _scopes(self,*args):
                raise AssertionError('Reviewed publication cannot finalize through ordinary authorize')
            async def _latest_test(self,uow,draft):
                probe.trace.append('latest-test')
                return probe.stored
            async def _stale(self,*args):
                probe.trace.append('stale-policy-source-work')
                return ()
            async def _binding(self,*args):
                probe.trace.append('binding')
        self.service = Service(registry=self,policy=SimpleNamespace(
            requires_source_approval=lambda target:True,source_access=self,audit=self.audit),bindings=None,
            bundles=SimpleNamespace(resolve=self.bundle))

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self.uow
        except Exception:
            self.trace.append('rollback')
            raise
        else:
            self.trace.append('commit')

    async def prelock(self,*args,**kwargs):
        self.trace.append('prelock')
        self.prelocks.append(kwargs)
    async def finalize(self,*args): raise AssertionError('Recorder owns the final approval lock suffix')
    async def lock_product(self,product_id):
        assert product_id == self.product.manifest.product_id
        self.trace.append('product')
        return self.head
    async def scalar(self,statement):
        from gateway.registry.models import VersionState
        entity = statement.column_descriptions[0]['entity']
        if entity is ProductVersion:
            self.trace.append('version')
            return self.previous
        assert entity is VersionState
        self.trace.append('state-lock')
        return self.state
    async def get(self,model,identity):
        from gateway.registry.models import VersionState
        if model is ProductVersion:
            return self.versions[identity]
        assert model is VersionState
        self.trace.append('state-read')
        return self.state
    def bundle(self,*args):
        self.trace.append('bundle')
        return SimpleNamespace(context='consumer')
    async def append_audit(self,uow,event):
        from gateway.policy.audit import audit_unavailable
        assert uow is self.uow
        self.trace.append('provisional-audit')
        if self.audit_denied: raise audit_unavailable()
        self.events.append(event)
        return 'provisional-audit-id'
    async def record_publication_operation(self,uow,**kwargs):
        assert uow is self.uow
        self.trace.append('recorder')
        assert self.state.actor_id == 'old-publisher' and self.head.source_operation_id == 'previous-operation'
        self.calls.append(kwargs)
        if self.denied: raise not_found()
        return self.receipt
    async def insert_version(self,**kwargs):
        assert self.trace[-1] == 'recorder'
        assert kwargs['source_operation_id'] == self.receipt
        assert kwargs['version_id'] == self.calls[-1]['version_id']
        self.trace.append('insert-version')
        return SimpleNamespace(id=kwargs['version_id'],product_id=self.product.manifest.product_id,binding_digest=self.target.binding_digest)
    def add(self,event):
        self.trace.append('event')
        self.events.append(event)
    async def flush(self): self.trace.append('flush')


@pytest.mark.parametrize('action', ['publish','deprecate','withdraw','rollback'])
def test_publication_recorder_is_the_only_final_gate_and_binds_exact_mutation(action,monkeypatch):
    from gateway.publishing import service as module
    async def scenario():
        probe = PublicationOperationProbe()
        async def capacity(uow):
            assert uow is probe.uow
            probe.trace.append('capacity')
        monkeypatch.setattr(module,'lock_execution_capacity',capacity)
        if action == 'publish':
            await probe.service.publish(probe.actor,probe.draft.id,probe.draft.content_digest,1,'Reviewed release')
        else:
            await probe.service.transition(probe.actor,probe.product.manifest.product_id,'1.0.0',action,'Reviewed change')
        assert len(probe.calls) == 1 and len(probe.events) == 2
        audit,event = probe.events
        operation = probe.calls[0]
        assert operation['actor'] == probe.actor and operation['action'] == action
        assert operation['previous_version_id'] == probe.previous.id
        assert operation['expected_product_revision'] == 7 and probe.head.publication_revision == 7
        assert operation['allow_audit_id'] == 'provisional-audit-id'
        assert operation['request_id'] == audit.correlation_id == event.request_id
        assert audit.action == 'publish' and audit.source_access_reason_code == 'APPROVED'
        assert audit.version_id == (probe.draft.id if action == 'publish' else probe.previous.id)
        assert event.source_operation_id == probe.receipt
        assert operation['draft_id'] == (probe.draft.id if action == 'publish' else None)
        assert operation['test_run_id'] == (probe.stored.id if action == 'publish' else None)
        assert operation['expected_draft_revision'] == (1 if action == 'publish' else None)
        assert probe.trace.index('prelock') < probe.trace.index('prepare-publish') < probe.trace.index('provisional-audit')
        earlier = ('stale-policy-source-work','bundle') if action == 'publish' else ('state-lock',)
        assert all(probe.trace.index(item) < probe.trace.index('recorder') for item in earlier)
        expected_drafts = ('draft-1','previous-draft') if action == 'publish' else ('previous-draft',)
        assert probe.prelocks[0]['draft_ids'] == expected_drafts
        assert probe.prelocks[0]['version_ids'] == (probe.previous.id,)
        assert probe.trace[-1] == 'commit'
    asyncio.run(scenario())


@pytest.mark.parametrize('action', ['publish','withdraw'])
@pytest.mark.parametrize('failure', ['recorder','audit'])
def test_publication_gate_failure_never_mutates_or_appends_a_publication_event(action,failure,monkeypatch):
    from gateway.publishing import service as module
    async def scenario():
        probe = PublicationOperationProbe(denied=failure=='recorder',audit_denied=failure=='audit')
        async def capacity(uow): pass
        monkeypatch.setattr(module,'lock_execution_capacity',capacity)
        with pytest.raises(GatewayError):
            if action == 'publish':
                await probe.service.publish(probe.actor,probe.draft.id,probe.draft.content_digest,1,'Reviewed release')
            else:
                await probe.service.transition(probe.actor,probe.product.manifest.product_id,'1.0.0',action,'Reviewed change')
        assert probe.trace[-1] == 'rollback'
        assert not {'insert-version','event'} & set(probe.trace)
        assert probe.state.actor_id == 'old-publisher' and probe.head.source_operation_id == 'previous-operation'
        assert len(probe.calls) == (1 if failure == 'recorder' else 0)
    asyncio.run(scenario())


@pytest.mark.parametrize('action', ['deprecate','withdraw','rollback'])
def test_reviewed_transition_prelocks_and_authorizes_distinct_current_head_for_every_action(action,monkeypatch):
    from gateway.publishing import service as module
    async def scenario():
        probe = PublicationOperationProbe()
        current = SimpleNamespace(**{**vars(probe.previous),'id':'current-version','draft_id':'current-draft'})
        probe.versions[current.id] = current
        probe.head.current_version_id = current.id
        async def capacity(uow): pass
        monkeypatch.setattr(module,'lock_execution_capacity',capacity)
        await probe.service.transition(probe.actor,probe.product.manifest.product_id,'1.0.0',action,'Reviewed change')
        assert {item.version_id for item in probe.prepared_targets} == {current.id,probe.previous.id}
        assert probe.prelocks[0]['draft_ids'] == ('current-draft','previous-draft')
        assert probe.prelocks[0]['version_ids'] == ('current-version','previous-version')
        assert probe.calls[0]['previous_version_id'] == current.id
        assert probe.state.source_operation_id == probe.receipt
        assert probe.head.current_version_id == (probe.previous.id if action == 'rollback' else current.id)
    asyncio.run(scenario())


def test_fixture_run_preflight_finalizes_author_only_after_binding_then_rechecks_retained_evidence():
    async def scenario():
        probe = EvidenceReviewProbe()
        probe.head.latest_draft_id = probe.draft.id
        async def prepare(actor,targets,action,uow):
            assert action == 'author' and targets == (probe.target,)
            probe.trace.append('author-preflight')
            return {},('preflight-author',)
        async def binding(*args): probe.trace.append('binding')
        async def run(actor,draft_id):
            assert probe.trace[-2:] == ['finalize','commit']
            probe.trace.append('fixture-run')
            return probe.stored
        probe.service._prepare_scopes = prepare
        probe.service._binding = binding
        probe.run = run
        result = await probe.service.test(probe.actor,probe.draft.id)
        assert result.id == probe.stored.id
        assert probe.batches == [('preflight-author',),(probe.target.version_id,probe.original_author,probe.fixed_result)]
        assert probe.trace.index('author-preflight') < probe.trace.index('binding') < probe.trace.index('finalize') < probe.trace.index('fixture-run')
    asyncio.run(scenario())
