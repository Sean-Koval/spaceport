"""Offline transport parity with real ResultService and synthetic DB seams."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy.sql.elements import TextClause

from gateway.execution.results import ResultService
from tests.contract.result_parity import assert_result_page_parity
from tests.contract.test_mcp import body, post, running, services
from tests.unit.test_stored_results import TransactionProbe

pytestmark = pytest.mark.contract


def test_result_parity_reassesses_advancing_database_time_on_all_pages(clock, north_principal):
    async def scenario():
        probe = TransactionProbe(clock, count=451)
        scalar = probe.scalar
        observations = 0

        async def advancing_db_time(statement):
            nonlocal observations
            if isinstance(statement, TextClause):
                assert str(statement) == 'SELECT clock_timestamp()'
                observations += 1
                return clock.now() + timedelta(microseconds=observations)
            return await scalar(statement)

        probe.scalar = advancing_db_time
        svc = services(north_principal, clock)
        svc.results = ResultService(probe, probe.policy, probe.bindings, clock)
        expected = probe.envelope.model_dump(mode='json')
        original = deepcopy(expected)
        sizes = []
        rows = []
        cursor = None
        previous_assessment = None
        async with running(svc) as (base, _), httpx.AsyncClient(
            base_url=base, trust_env=False, headers={'Authorization': 'Bearer synthetic'}
        ) as client:
            while True:
                response = await post(base, body(name='get_result', arguments={
                    'execution_id': 'execution-1', 'cursor': cursor}))
                assert response.status_code == 200
                result = response.json()['result']
                assert result['isError'] is False
                mcp_page = result['structuredContent']
                response = await client.get('/v1/executions/execution-1',
                    params={} if cursor is None else {'cursor': cursor})
                assert response.status_code == 200
                rest_page = response.json()
                before = deepcopy((mcp_page, rest_page))
                # The old comparator rejects legitimate per-fetch observations.
                assert {k: v for k, v in mcp_page.items() if k != 'next_cursor'} != {
                    k: v for k, v in rest_page.items() if k != 'next_cursor'}
                assert_result_page_parity(mcp_page, rest_page, expected)
                assert (mcp_page, rest_page) == before
                assessed = datetime.fromisoformat(mcp_page['freshness']['assessed_at'])
                if previous_assessment is not None:
                    assert assessed > previous_assessment
                previous_assessment = datetime.fromisoformat(rest_page['freshness']['assessed_at'])
                assert previous_assessment > assessed
                sizes.append(len(mcp_page['rows']))
                rows.extend(mcp_page['rows'])
                cursor = mcp_page['next_cursor']
                if cursor is None:
                    break
        assert sizes == [200, 200, 51]
        assert rows == expected['rows']
        # Six page fetches each assess freshness and then recheck the final
        # retention/cursor deadline after authorization and audit work.
        assert observations == 12
        assert expected == original == probe.envelope.model_dump(mode='json')
        assert clock.now() == probe.clock.now() == probe.envelope.freshness.assessed_at
    asyncio.run(scenario())


@pytest.mark.parametrize('field', [
    'source_watermark', 'status', 'max_age_seconds', 'product_id', 'rows',
    'warnings', 'product_digest', 'typed_schema', 'completed_at',
])
def test_result_parity_keeps_immutable_fields_strict(clock, field):
    expected = TransactionProbe(clock).envelope.model_dump(mode='json')
    left = deepcopy(expected)
    right = deepcopy(expected)
    if field == 'status':
        right['freshness']['status'] = 'stale'
    elif field == 'max_age_seconds':
        right['freshness']['max_age_seconds'] += 1
    elif field == 'rows':
        right['rows'][0]['revenue'] = '99.99'
    elif field == 'warnings':
        right['warnings'] = ['Unexpected metadata']
    elif field == 'typed_schema':
        right['typed_schema']['segment']['nullable'] = False
    elif field in {'source_watermark', 'completed_at'}:
        right[field] = (datetime.fromisoformat(right[field]) + timedelta(seconds=1)).isoformat()
    elif field == 'product_digest':
        right[field] = '0' * 64
    else:
        right[field] = 'unrequested-product'
    with pytest.raises(AssertionError):
        assert_result_page_parity(left, right, expected)


@pytest.mark.parametrize('field', [
    'state', 'execution_id', 'product_id', 'version', 'product_digest', 'source_watermark',
])
def test_result_parity_checks_both_pages_against_original_pin(clock, field):
    expected = TransactionProbe(clock).envelope.model_dump(mode='json')
    page = deepcopy(expected)
    replacements = {'state': 'running', 'execution_id': 'wrong-execution',
        'product_id': 'wrong-product', 'version': '2.0.0', 'product_digest': '0' * 64,
        'source_watermark': '2024-03-11T23:59:59Z'}
    page[field] = replacements[field]
    with pytest.raises(AssertionError):
        assert_result_page_parity(page, deepcopy(page), expected)


@pytest.mark.parametrize('change', ['age', 'status', 'threshold', 'out_of_bounds', 'backwards'])
def test_result_parity_validates_freshness_before_normalization(clock, change):
    expected = TransactionProbe(clock).envelope.model_dump(mode='json')
    left = deepcopy(expected)
    right = deepcopy(expected)
    if change == 'backwards':
        right['freshness']['assessed_at'] = (clock.now() - timedelta(seconds=1)).isoformat()
        right['freshness']['age_seconds'] -= 1
    else:
        for page in (left, right):
            if change == 'age':
                page['freshness']['age_seconds'] += 1
            elif change == 'status':
                page['freshness']['status'] = 'unknown'
            elif change == 'threshold':
                page['freshness']['max_age_seconds'] += 1
            else:
                page['freshness']['assessed_at'] = (clock.now() + timedelta(days=2)).isoformat()
                page['freshness']['age_seconds'] += 2 * 86400
    with pytest.raises(AssertionError):
        assert_result_page_parity(left, right, expected)
