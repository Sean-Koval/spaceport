"""Captured Task 5 pending projection replay, then a separate live Orders result.

The dual-backend case uses the actual registry/queue/worker and unmodified
CubeAdapter. Replayed Continue-wait frames originate in an independently
completed SlowOrders probe. This is not live SlowOrders worker admission or
cancellation evidence. Offline helper cases prove only the replay seam.
"""
import asyncio
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path

import httpx
import pytest

from gateway.adapters.base import StaticBindingResolver
from gateway.adapters.bundles import BundleCatalog
from gateway.adapters.cube import CubeAdapter
from gateway.contracts.canonical import canonical_bytes, canonical_digest
from gateway.contracts.clock import SystemClock
from gateway.contracts.execution import Completed, Pending, PreparedQuery
from gateway.execution.worker import Worker
from tests.contract.test_cube_adapter import FixtureStream, meta, synthetic
from tests.integration.test_audit import principal
from tests.integration.test_execution_queue import setup
from test_cube_compatibility import cube_stack, raw_query

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_ROWS = (
    {'day': '2024-03-09', 'segment': 'A', 'revenue': '10.10'},
    {'day': '2024-03-10', 'segment': None, 'revenue': '20.20'},
    {'day': '2024-03-11', 'segment': 'A', 'revenue': '9007199254740993.01'},
)


def _pending_payloads(records):
    """Freeze only the status/error projection actually stored by Task 5."""
    if type(records) is not list or not 1 <= len(records) <= 480:
        raise ValueError('Invalid captured pending evidence')
    payloads = []
    for record in records:
        if (type(record) is not dict or set(record) != {'status', 'error', 'keys'}
            or type(record['status']) is not int or record['status'] != 200
            or record['error'] != 'Continue wait' or type(record['keys']) is not list
            or not 1 <= len(record['keys']) <= 20
            or any(type(key) is not str or not 1 <= len(key) <= 128 for key in record['keys'])
            or record['keys'] != sorted(set(record['keys'])) or 'error' not in record['keys']):
            raise ValueError('Invalid captured pending evidence')
        payloads.append(canonical_bytes({'error': record['error']}))
    return tuple(payloads)


class PendingReplayTransport(httpx.AsyncBaseTransport):
    """A test-only replay seam; the real adapter still parses streamed HTTP."""
    def __init__(self, *, endpoint, records, delegate):
        if endpoint.context != 'consumer':
            raise ValueError('Unexpected pending replay request endpoint')
        self.endpoint, self.delegate = endpoint, delegate
        self.payloads = _pending_payloads(records)
        self.frame_count = len(self.payloads)
        self.replayed_count = self.live_data_calls = 0
        self.expected_body = None
        self.watermark_body = canonical_bytes({'query': {
            'dimensions': ['Watermark.loaded_at'], 'timezone': 'UTC', 'limit': 2},
            'cache': 'must-revalidate'})

    def arm(self, prepared):
        if (self.expected_body is not None or prepared.endpoint_id != self.endpoint.endpoint_id
            or prepared.model_bundle_digest != self.endpoint.bundle_digest):
            raise ValueError('Unexpected pending replay request binding')
        self.expected_body = canonical_bytes({'query': prepared.model_dump(mode='json')['cube_query'],
            'cache': 'must-revalidate'})

    async def handle_async_request(self, request):
        if request.method == 'GET' and str(request.url) == self.endpoint.url + '/cubejs-api/v1/meta':
            return await self.delegate.handle_async_request(request)
        if (request.method != 'POST' or str(request.url) != self.endpoint.url + '/cubejs-api/v1/load'
            or self.expected_body is None):
            raise ValueError('Unexpected pending replay request')
        body = canonical_bytes(json.loads(request.content))
        if body == self.expected_body:
            if self.replayed_count < self.frame_count:
                payload = self.payloads[self.replayed_count]
                self.replayed_count += 1
                return httpx.Response(200, stream=FixtureStream(payload))
            self.live_data_calls += 1
        elif body != self.watermark_body:
            raise ValueError('Unexpected pending replay request')
        return await self.delegate.handle_async_request(request)

    async def aclose(self):
        await self.delegate.aclose()


def _offline_seam(records):
    endpoint = BundleCatalog.from_file(ROOT / 'cube/bundles.json').resolve(synthetic()[0].model_bundle_digest)
    prepared = PreparedQuery(endpoint_id=endpoint.endpoint_id, model_bundle_digest=endpoint.bundle_digest,
        cube_query={'measures': ['Orders.revenue'], 'dimensions': ['Orders.segment'], 'limit': 1001})
    delegated = []
    class Delegate(httpx.AsyncBaseTransport):
        closed = False
        async def handle_async_request(self, request):
            delegated.append(request)
            return httpx.Response(200, stream=FixtureStream(b'{"data":[]}'))
        async def aclose(self): self.closed = True
    delegate = Delegate()
    replay = PendingReplayTransport(endpoint=endpoint, records=records, delegate=delegate)
    replay.arm(prepared)
    return endpoint, prepared, replay, delegated, delegate


@pytest.mark.parametrize('records', [
    [], {}, [{'status': 201, 'error': 'Continue wait', 'keys': ['error']}],
    [{'status': 200, 'error': 'other', 'keys': ['error']}],
    [{'status': 200, 'error': 'Continue wait', 'keys': []}],
    [{'status': 200, 'error': 'Continue wait', 'keys': ['error', 'error']}],
    [{'status': 200, 'error': 'Continue wait', 'keys': ['error'], 'raw_token': 'must-not-be-accepted'}],
])
@pytest.mark.contract
def test_pending_capture_rejects_missing_or_changed_evidence(records):
    with pytest.raises(ValueError, match='pending evidence'):
        _pending_payloads(records)


@pytest.mark.contract
def test_pending_capture_keeps_exact_count_and_only_observed_protocol_projection():
    records = [{'status': 200, 'error': 'Continue wait', 'keys': ['error', 'requestId']}] * 2
    assert _pending_payloads(records) == (b'{"error":"Continue wait"}',) * 2


@pytest.mark.contract
def test_pending_transport_replays_each_frame_once_then_forwards_original_requests():
    async def scenario():
        records = [{'status': 200, 'error': 'Continue wait', 'keys': ['error']}] * 2
        endpoint, prepared, replay, delegated, delegate = _offline_seam(records)
        records[0]['error'] = 'mutated-after-construction'
        async with httpx.AsyncClient(transport=replay) as client:
            await client.get(endpoint.url + '/cubejs-api/v1/meta')
            body = {'query': prepared.model_dump(mode='json')['cube_query'], 'cache': 'must-revalidate'}
            responses = [await client.post(endpoint.url + '/cubejs-api/v1/load', json=body) for _ in range(3)]
            watermark = {'query': {'dimensions': ['Watermark.loaded_at'], 'timezone': 'UTC', 'limit': 2},
                'cache': 'must-revalidate'}
            await client.post(endpoint.url + '/cubejs-api/v1/load', json=watermark)
        assert [response.json() for response in responses] == [
            {'error': 'Continue wait'}, {'error': 'Continue wait'}, {'data': []}]
        assert replay.replayed_count == replay.frame_count == 2
        assert replay.live_data_calls == 1
        assert len(delegated) == 3
        assert delegated[0].method == 'GET'
        assert json.loads(delegated[1].content) == body
        assert json.loads(delegated[2].content) == watermark
        assert all(str(request.url).startswith(endpoint.url + '/') for request in delegated)
        assert delegate.closed
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['endpoint', 'query', 'method', 'unarmed'])
@pytest.mark.contract
def test_pending_transport_never_replays_for_an_unexpected_request(change):
    async def scenario():
        records = [{'status': 200, 'error': 'Continue wait', 'keys': ['error']}]
        endpoint, prepared, replay, delegated, _ = _offline_seam(records)
        url = endpoint.url + '/cubejs-api/v1/load'
        body = {'query': prepared.model_dump(mode='json')['cube_query'], 'cache': 'must-revalidate'}
        method = 'POST'
        if change == 'endpoint': url = 'http://cube-test:4000/cubejs-api/v1/load'
        if change == 'query': body['query']['measures'] = ['SlowOrders.revenue']
        if change == 'method': method = 'PUT'
        if change == 'unarmed': replay.expected_body = None
        async with httpx.AsyncClient(transport=replay) as client:
            with pytest.raises(ValueError, match='replay request'):
                await client.request(method, url, json=body)
        assert replay.replayed_count == 0 and not delegated
    asyncio.run(scenario())


@pytest.mark.contract
def test_replayed_projection_is_decoded_by_real_adapter_before_synthetic_completion(clock):
    """Offline parser coverage only; these completion rows are not live evidence."""
    async def scenario():
        binding, product, decision, request = synthetic()
        catalog = BundleCatalog.from_file(ROOT / 'cube/bundles.json')
        endpoint = catalog.resolve(binding.model_bundle_digest)
        records = [{'status': 200, 'error': 'Continue wait', 'keys': ['error']}] * 2
        rows = [{'Orders.ordered_at.day': row['day'] + 'T00:00:00.000',
            'Orders.segment': row['segment'], 'Orders.revenue': row['revenue']} for row in EXPECTED_ROWS]
        class SyntheticResponses(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if request.method == 'GET':
                    body = meta()
                elif json.loads(request.content)['query'].get('dimensions') == ['Watermark.loaded_at']:
                    body = {'data': [{'Watermark.loaded_at': '2024-03-12T00:00:00.000'}]}
                else:
                    body = {'data': rows}
                return httpx.Response(200, stream=FixtureStream(canonical_bytes(body)))
        transport = PendingReplayTransport(endpoint=endpoint, records=records, delegate=SyntheticResponses())
        async with CubeAdapter(catalog=catalog, bindings=StaticBindingResolver([binding]), clock=clock,
            secret_resolver=lambda _: 'a' * 43, transport=transport) as client:
            deadline = clock.now() + timedelta(seconds=120)
            prepared = await client.prepare(product, request, decision, deadline=deadline)
            transport.arm(prepared)
            values = [await client.poll(prepared, decision, deadline=deadline, request_id='offline-replay')
                for _ in range(3)]
        assert all(isinstance(value, Pending) and value.retry_after_seconds == .25 for value in values[:2])
        assert isinstance(values[2], Completed) and list(values[2].raw_rows) == rows
        assert transport.replayed_count == 2 and transport.live_data_calls == 1
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.cube
def test_worker_replays_actual_continue_wait_then_live_orders_completes(registry_database, cube_stack, capsys):
    """Real pending capture replay + independent live consumer completion.

    Breaking Worker pending handling, retaining no source slot, restarting its
    deadline, or releasing capacity before Completed makes this gate fail.
    No mock completion rows or generated Pending objects enter the worker.
    """
    probe_started = SystemClock().now()
    probe_query = raw_query('SlowOrders')
    slow_rows = cube_stack.load(probe_query, endpoint='cube-test', require_pending=True,
        principal='task7-pending-probe')
    probe_completed = SystemClock().now()
    assert len(slow_rows) == 3
    assert {row['SlowOrders.id']: (row['SlowOrders.segment'], row['SlowOrders.revenue'])
        for row in slow_rows} == {
        'n1': ('A', '10.10'), 'n2': (None, '20.20'), 'n3': ('A', '9007199254740993.01')}
    assert {row['SlowOrders.tenant_id'] for row in slow_rows} == {'north'}
    # Read only this just-completed probe's capture. No cross-test ordering or
    # checked-in handwritten Pending response can stand in for actual evidence.
    records = json.loads((cube_stack.evidence / 'pending.json').read_text())
    payloads = _pending_payloads(records)
    source_proof = {
        'operation': 'independent_raw_fixture_probe', 'context': 'fixture_test',
        'model': 'SlowOrders', 'principal': 'task7-pending-probe',
        'model_bundle_digest': cube_stack.catalog['fixture_test']['bundle_digest'],
        'query_digest': canonical_digest(probe_query), 'started_at': probe_started.isoformat(),
        'completed_at': probe_completed.isoformat(), 'completed_rows_verified': 3,
        'observed_pending_count': len(payloads), 'captured_projection': records,
        'projection_digest': canonical_digest(records),
        'replay_mapping': 'HTTP 200 and observed error only; no member or row mapping',
        'byte_for_byte_wire_replay': False,
    }
    cube_stack.capture('task7-pending-source-projection.json', source_proof)

    async def scenario():
        service, request = await setup(registry_database)
        accepted = await service.submit(principal(), request)
        deadline = registry_database.scalar(
            'SELECT deadline_at FROM gateway.executions WHERE id=%s', (accepted.execution_id,))
        assert deadline == accepted.accepted_at + timedelta(seconds=120)
        catalog_records = json.loads((ROOT / 'cube/bundles.json').read_text())
        if cube_stack.host_mode:
            for record in catalog_records.values():
                name = 'cube-test' if record['context'] == 'fixture_test' else 'cube'
                record['url'] = f'http://{cube_stack.hosts[name]}:4000'
        catalog = BundleCatalog(catalog_records)
        endpoint = catalog.resolve(synthetic()[0].model_bundle_digest)
        transport = PendingReplayTransport(endpoint=endpoint, records=records,
            delegate=httpx.AsyncHTTPTransport(trust_env=False,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=10)))
        clock = SystemClock()
        client = CubeAdapter(catalog=catalog, bindings=service.bindings, clock=clock,
            secret_resolver=lambda ref: (cube_stack.secrets / Path(ref).name).read_text().strip(),
            transport=transport)
        observations, sleeps, deadlines = [], [], []

        def snapshot(stage):
            observed = registry_database.scalar("""
                SELECT jsonb_build_object(
                    'state', s.state,
                    'result_count', (SELECT count(*) FROM gateway.results r WHERE r.execution_id=e.id),
                    'preparation_count', (SELECT count(*) FROM gateway.execution_preparations p WHERE p.execution_id=e.id),
                    'own_held', (SELECT count(*) FROM gateway.held_source_capacity h WHERE h.principal_id=e.principal_id),
                    'total_held', (SELECT count(*) FROM gateway.held_source_capacity),
                    'held_attempts', (SELECT jsonb_agg(jsonb_build_array(r.id,r.generation))
                        FROM gateway.source_reservations r WHERE r.execution_id=e.id AND r.released_at IS NULL)
                ) FROM gateway.executions e JOIN gateway.execution_states s ON s.execution_id=e.id
                WHERE e.id=%s
                """, (accepted.execution_id,))
            assert observed['state'] == 'running'
            assert observed['result_count'] == 0 and observed['preparation_count'] == 1
            assert (observed['own_held'], observed['total_held']) == (1, 1)
            assert len(observed['held_attempts']) == 1
            if observations:
                assert observed['held_attempts'] == observations[0]['held_attempts']
            assert registry_database.scalar('SELECT deadline_at FROM gateway.executions WHERE id=%s',
                (accepted.execution_id,)) == deadline
            observations.append({'stage': stage, **observed})

        class ObservedAdapter:
            replay_pending = live_pending = 0
            async def prepare(self, *args, **kwargs):
                deadlines.append(kwargs['deadline'])
                assert kwargs['context'] == 'consumer' and kwargs['deadline'] == deadline
                prepared = await client.prepare(*args, **kwargs)
                transport.arm(prepared)
                return prepared
            async def poll(self, *args, **kwargs):
                deadlines.append(kwargs['deadline'])
                assert kwargs['context'] == 'consumer' and kwargs['deadline'] == deadline
                before = transport.replayed_count
                value = await client.poll(*args, **kwargs)
                if isinstance(value, Pending):
                    if transport.replayed_count > before:
                        assert transport.replayed_count == before + 1
                        self.replay_pending += 1
                        snapshot('replayed_pending')
                    else:
                        self.live_pending += 1
                        snapshot('live_orders_pending')
                else:
                    assert isinstance(value, Completed) and transport.live_data_calls >= 1
                    assert transport.replayed_count == transport.frame_count
                    snapshot('live_orders_completed_before_release')
                    # Validate exact source values before Worker can release the
                    # reservation. Queue normalization separately checks aliases,
                    # schema/grain and all other result invariants below.
                    assert len(value.raw_rows) == 3
                    assert {(row['Orders.ordered_at.day'][:10], row['Orders.segment']): row['Orders.revenue']
                        for row in value.raw_rows} == {
                        (row['day'], row['segment']): row['revenue'] for row in EXPECTED_ROWS}
                return value
            async def close(self): await client.close()

        class ObservedClock:
            def now(self): return clock.now()
            async def sleep(self, seconds):
                started = clock.now()
                sleeps.append({'started_at': started.isoformat(), 'seconds': seconds})
                nominal = (.25, .5, 1, 2)[min(len(sleeps) - 1, 3)]
                assert 0 < seconds <= nominal
                if deadline - started > timedelta(seconds=nominal):
                    assert seconds == nominal
                assert started + timedelta(seconds=seconds) <= deadline
                await clock.sleep(seconds)

        adapter = ObservedAdapter()
        try:
            assert await Worker(service.queue, adapter_factory=lambda: adapter,
                worker_id='captured-pending-live-orders-worker', clock=ObservedClock()).run_once()
        finally:
            await client.close()
        view = await service.queue.view(accepted.execution_id)
        assert view.state == 'succeeded', view.error
        assert accepted.accepted_at <= view.completed_at < deadline
        assert deadlines and all(item == deadline for item in deadlines)
        assert adapter.replay_pending == transport.replayed_count == len(payloads)
        assert len(sleeps) == adapter.replay_pending + adapter.live_pending
        assert sum(item['stage'] == 'replayed_pending' for item in observations) == len(payloads)
        envelope = registry_database.scalar('SELECT envelope FROM gateway.results WHERE execution_id=%s',
            (accepted.execution_id,))
        assert envelope['row_count'] == len(envelope['rows']) == 3 and envelope['truncated'] is False
        assert {(row['day'], row['segment']): row for row in envelope['rows']} == {
            (row['day'], row['segment']): row for row in EXPECTED_ROWS}
        assert sum((Decimal(row['revenue']) for row in envelope['rows']), Decimal(0)) == Decimal('9007199254741023.31')
        assert envelope['source_watermark'] and envelope['freshness']['status'] == 'fresh'
        assert envelope['cache_status'] == 'unknown'
        assert registry_database.scalar('SELECT count(*) FROM gateway.held_source_capacity') == 0
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE execution_id=%s',
            (accepted.execution_id,)) == 1
        assert registry_database.scalar("""SELECT count(*) FROM gateway.source_reservations
            WHERE execution_id=%s AND released_at IS NOT NULL AND release_reason='completed'""",
            (accepted.execution_id,)) == 1
        worker_proof = {
            'evidence_mode': 'recorded_pending_projection_then_separate_live_orders_completion',
            'source_probe': source_proof, 'execution_id': accepted.execution_id,
            'worker_context': 'consumer', 'worker_model': 'Orders',
            'worker_bundle_digest': endpoint.bundle_digest, 'replayed_pending_count': adapter.replay_pending,
            'live_orders_pending_count': adapter.live_pending, 'live_orders_data_calls': transport.live_data_calls,
            'same_source_operation': False, 'live_slow_orders_worker_acceptance': False,
            'cancellation_proven': False, 'accepted_at': accepted.accepted_at.isoformat(),
            'deadline_at': deadline.isoformat(), 'completed_at': view.completed_at.isoformat(),
            'state': view.state, 'observations': observations, 'worker_sleeps': sleeps,
            'rows': envelope['rows'], 'row_count': envelope['row_count'],
            'source_watermark': envelope['source_watermark'], 'truncated': envelope['truncated'],
            'final_held_capacity': 0, 'release_reason': 'completed',
        }
        cube_stack.capture('task7-worker-pending-replay.json', worker_proof)
        # The run-private directory is removed by the harness. Emit only this
        # selected synthetic proof so successful CI retains its provenance.
        with capsys.disabled():
            print('Task 7 recorded-pending worker evidence: ' + json.dumps(worker_proof, sort_keys=True))
    asyncio.run(scenario())
