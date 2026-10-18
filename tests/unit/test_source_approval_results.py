"""Result gate sequencing with a transaction probe; not PostgreSQL authority evidence."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from gateway.adapters.base import StaticBindingResolver
from gateway.adapters.bigquery_runtime import BigQueryBinding
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.identity import Principal
from gateway.execution.results import ResultService
from gateway.registry.repository import not_found
from gateway.contracts.errors import GatewayError
from gateway.source_access_contracts import validate_source_approval
from gateway.contracts.canonical import canonical_bytes
from tests.unit.test_source_approval_contracts import package as package_fixture
from tests.unit.test_stored_results import TransactionProbe

pytestmark = pytest.mark.unit


class ReviewedResultProbe(TransactionProbe):
    """Real domain/scope models, fake database and protected-authority repository."""
    def __init__(self, clock, *, count=3, denied=False):
        super().__init__(clock, count=count)
        raw = self.context.binding.model_dump(mode='python')
        for key in ('constraint_members', 'watermark_member'):
            raw.pop(key)
        raw.update(adapter_revision='cube-bigquery-v1', source_id='consumer-source', source_revision=1,
            source_digest='d'*64, scope={'tenant_member':'Orders.tenant_id', 'access_group_member':'Orders.access_group'},
            freshness_mode='same-query-checkpoint', checkpoint_member='Orders.loaded_at')
        binding = BigQueryBinding.model_validate(raw)
        document, manifest = package_fixture.__wrapped__(self.product.manifest.model_dump(mode='json', exclude={'content_digest'}))
        document['bindings']['consumer']['content_digest'] = binding.digest
        for domain in document['input_domains'] + document['filter_domains']:
            if domain['type'] == 'date': domain['maximum'] = '2030-01-01'
        approval = validate_source_approval(canonical_bytes(document))
        self.access_request = SimpleNamespace(pins=approval.package_pins(manifest, approval_digest=approval.content_digest),
            projection=approval.runtime_projection('79902c27-e882-4d40-a68b-7f7aaf637a8c'))
        self.product = PinnedProduct(manifest=manifest, binding_digest=binding.digest, version_id=self.product.version_id)
        spec = self.context.spec.model_copy(update={'pinned_product':self.product})
        self.context = replace(self.context, binding=binding, spec=spec)
        self.bindings = StaticBindingResolver([binding])
        self.envelope = self.envelope.model_copy(update={'product_digest':manifest.content_digest,
            'provenance':self.envelope.provenance.model_copy(update={'adapter_revision':'cube-bigquery-v1'})})
        self.result.envelope = self.envelope.model_dump(mode='json')
        self.actor = Principal(id='north-user',subject='synthetic-north',kind='user',service_client_id='test-client')
        self.authorization.principal_snapshot = self.actor.model_dump(mode='json')
        self.trace = []
        self.denied = denied
        self.policy.requires_source_approval = lambda product: True
        self.policy.source_access = self
        self.policy.evaluate = self.evaluate

    async def prelock(self, uow, products, *, version_ids=(), draft_ids=()):
        assert products == (self.product,) and version_ids == (self.product.version_id,)
        self.trace.append('prelock')

    async def evaluate(self, principal, action, product, uow, **kwargs):
        self.trace.append('policy')
        return self.context.decision

    async def resolve_owner(self, uow, owner, principal, decision, *, action, request_id):
        assert owner == {'kind':'execution', 'id':self.execution.id}
        assert principal == self.actor and action == 'result'
        self.trace.append('owner')
        return self.access_request

    async def scalar(self, statement):
        if hasattr(statement, 'column_descriptions'):
            self.trace.append(statement.column_descriptions[0]['entity'].__name__)
        return await super().scalar(statement)

    def add(self, row):
        self.trace.append(type(row).__name__)
        super().add(row)

    async def finalize(self, uow, requests):
        assert requests == (self.access_request,)
        self.trace.append('finalize')
        if self.denied:
            raise not_found()
        return (SimpleNamespace(audit_id='synthetic-final-audit'),)

    async def _deny(self, uow, principal, action, request_id, reason):
        from gateway.source_access import SourceAccessService
        await SourceAccessService(self, self.audit)._deny(uow, principal, action, request_id, reason)


def test_reviewed_result_finalizes_after_all_state_cursor_and_outcome_work(clock):
    async def scenario():
        probe = ReviewedResultProbe(clock, count=201)
        response = await ResultService(probe,probe.policy,probe.bindings,clock).get(probe.actor,'execution-1')
        assert response.next_cursor is not None and probe.committed
        assert probe.trace.index('prelock') < probe.trace.index('policy') < probe.trace.index('owner')
        assert probe.trace.index('owner') < probe.trace.index('ExecutionState')
        assert probe.trace.index('ResultCursor') < probe.trace.index('finalize')
        assert probe.trace[-1] == 'finalize'
    asyncio.run(scenario())


@pytest.mark.parametrize('state', ['accepted','running','succeeded','failed','timed_out'])
def test_final_approval_denial_releases_no_status_rows_or_cursor(clock, state):
    async def scenario():
        probe = ReviewedResultProbe(clock, count=201, denied=True)
        probe.state.state = state
        if state in {'accepted','running'}: probe.state.completed_at = None
        if state == 'accepted': probe.state.started_at = None
        if state in {'failed','timed_out'}:
            probe.state.error_code = 'EXECUTION_TIMEOUT'
        with pytest.raises(GatewayError) as caught:
            await ResultService(probe,probe.policy,probe.bindings,clock).get(probe.actor,'execution-1')
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert 'finalize' in probe.trace and probe.rolled_back
        assert not probe.committed_cursors
        assert not [event for event in probe.committed_events if event.decision == 'allow']
    asyncio.run(scenario())


def test_result_requires_exact_accepted_client_before_owner_resolution(clock):
    async def scenario():
        probe = ReviewedResultProbe(clock)
        other = probe.actor.model_copy(update={'service_client_id':'other-client'})
        with pytest.raises(GatewayError) as caught:
            await ResultService(probe,probe.policy,probe.bindings,clock).get(other,'execution-1')
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert 'owner' not in probe.trace and 'finalize' not in probe.trace
    asyncio.run(scenario())


def test_reviewed_result_without_source_service_denies_before_policy(clock):
    async def scenario():
        probe = ReviewedResultProbe(clock)
        probe.policy.source_access = None
        with pytest.raises(GatewayError) as caught:
            await ResultService(probe,probe.policy,probe.bindings,clock).get(probe.actor,'execution-1')
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert 'policy' not in probe.trace and 'owner' not in probe.trace
        assert not [event for event in probe.committed_events if event.decision == 'allow']
    asyncio.run(scenario())


def test_every_cached_cursor_page_rechecks_original_owner_approval(clock):
    async def scenario():
        probe = ReviewedResultProbe(clock, count=201)
        service = ResultService(probe,probe.policy,probe.bindings,clock)
        first = await service.get(probe.actor,'execution-1')
        probe.denied = True
        with pytest.raises(GatewayError) as caught:
            await service.get(probe.actor,'execution-1', first.next_cursor)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert probe.trace.count('owner') == 2 and probe.trace.count('finalize') == 2
        assert len(probe.committed_cursors) == 1
    asyncio.run(scenario())


def test_retained_query_domain_rejection_precedes_parameter_processing(clock):
    from gateway.contracts.canonical import canonical_digest
    async def scenario():
        probe = ReviewedResultProbe(clock)
        probe.execution.canonical_query['inputs'].update(start='2031-01-01', end='2031-01-02')
        probe.execution.canonical_hash = canonical_digest(probe.execution.canonical_query)
        processed = []
        original = probe.audit.parameter_summary
        def summarize(parameters, **kwargs):
            processed.append(dict(parameters))
            return original(parameters, **kwargs)
        probe.audit.parameter_summary = summarize
        with pytest.raises(GatewayError) as caught:
            await ResultService(probe,probe.policy,probe.bindings,clock).get(probe.actor,'execution-1')
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert all('2031-01-01' not in values.values() for values in processed)
        assert 'finalize' not in probe.trace
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['owner', 'finalize'])
@pytest.mark.parametrize('source_audited', [True, False])
def test_result_preserves_single_correlated_source_denial(clock, stage, source_audited):
    """A preserves request IDs only after a safe source denial was committed."""
    from unittest.mock import AsyncMock
    async def scenario():
        probe = ReviewedResultProbe(clock)
        service = ResultService(probe, probe.policy, probe.bindings, clock)
        service._denied = AsyncMock(wraps=service._denied)
        original = probe.resolve_owner
        request_ids = []
        def denied():
            if source_audited:
                return GatewayError('NOT_FOUND_OR_FORBIDDEN', request_ids[-1], 'The requested product is unavailable.')
            return not_found()
        async def resolve(uow, owner, principal, decision, *, action, request_id):
            request_ids.append(request_id)
            if stage == 'owner':
                raise denied()
            return await original(uow, owner, principal, decision, action=action, request_id=request_id)
        async def finalize(uow, requests):
            raise denied()
        probe.resolve_owner = resolve
        probe.finalize = finalize
        with pytest.raises(GatewayError) as caught:
            await service.get(probe.actor, 'execution-1')
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert caught.value.request_id == request_ids[-1]
        assert service._denied.await_count == (0 if source_audited else 1)
        assert not [event for event in probe.committed_events if event.decision == 'allow']
    asyncio.run(scenario())


@pytest.mark.parametrize('expires', ['result', 'cursor'])
def test_result_retention_is_rechecked_after_final_approval(clock, expires):
    """The approval clock cannot extend retained rows or a shorter input cursor."""
    from datetime import timedelta
    async def scenario():
        probe = ReviewedResultProbe(clock, count=201)
        service = ResultService(probe, probe.policy, probe.bindings, clock)
        cursor = None
        if expires == 'cursor':
            first = await service.get(probe.actor, 'execution-1')
            cursor = first.next_cursor
            probe.committed_cursors[0].expires_at = clock.now() + timedelta(seconds=1)
        else:
            probe.result.expires_at = clock.now() + timedelta(seconds=1)
        previous_events = len(probe.committed_events)
        previous_cursors = len(probe.committed_cursors)
        original = probe.finalize
        async def slow_finalization(uow, requests):
            result = await original(uow, requests)
            clock.instant += timedelta(seconds=2)
            return result
        probe.finalize = slow_finalization
        with pytest.raises(GatewayError) as caught:
            await service.get(probe.actor, 'execution-1', cursor)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert len(probe.committed_cursors) == previous_cursors
        added_events = probe.committed_events[previous_events:]
        assert len(added_events) == 1 and added_events[0].decision == 'deny'
        assert added_events[0].correlation_id == caught.value.request_id
        assert added_events[0].product_id is None and added_events[0].parameter_names == []
    asyncio.run(scenario())
