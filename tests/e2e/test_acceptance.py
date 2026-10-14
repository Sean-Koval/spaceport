"""Real composed REST/MCP parity after genuine publisher fixture execution.

Run on the fresh, already-published Task14 stack before fault/rotation phases.
No synthetic TestRun insertion, seeded response or service double is used here.
Restart/restore and independent SDK reconciliation remain separate mandatory
suites; re-running their work here would not create independent evidence.
"""
import time

import pytest

from tests.integration.test_operations import actual_demo, demo_request

pytestmark = pytest.mark.e2e
PROTOCOL = '2026-07-28'


def mcp_request(client, token, name, arguments, *, headers=None):
    current = {'MCP-Protocol-Version': PROTOCOL, 'MCP-Method': 'tools/call',
        'MCP-Name': name, 'Accept': 'application/json, text/event-stream'}
    if token is not None:
        current['Authorization'] = 'Bearer ' + token
    current.update(headers or {})
    return client.post('/mcp', headers=current, json={'jsonrpc': '2.0', 'id': 1,
        'method': 'tools/call', 'params': {'name': name, 'arguments': arguments,
        '_meta': {'io.modelcontextprotocol/protocolVersion': PROTOCOL,
                  'io.modelcontextprotocol/clientCapabilities': {}}}})


def mcp_value(response):
    assert response.status_code == 200, 'Actual MCP transport did not accept this request'
    result = response.json()['result']
    assert result['isError'] is False, 'Actual MCP operation did not succeed'
    assert response.headers['cache-control'] == 'no-store'
    return result['structuredContent']


def query_described_version(client, tokens, account, description):
    """Run the exact observed current version, after all browser publications."""
    response = demo_request(client, tokens, account, 'POST', '/v1/executions', json={
        'product_id': description['product_id'], 'version': description['version'],
        'inputs': {'start': '2024-03-09', 'end': '2024-03-12'},
        'sort': [{'field': 'day', 'direction': 'asc'}, {'field': 'segment', 'direction': 'asc'}]})
    assert response.status_code == 200, 'Actual current-version query admission failed'
    identifier = response.json()['execution_id']
    deadline = time.monotonic() + 125
    while time.monotonic() < deadline:
        response = demo_request(client, tokens, account, 'GET', '/v1/executions/' + identifier)
        assert response.status_code == 200, 'Actual current-version result access failed'
        value = response.json()
        if value['state'] == 'succeeded':
            assert value['version'] == description['version'] and value['product_digest'] == description['digest']
            return value
        assert value['state'] in {'accepted', 'running'}, 'Actual current-version query failed'
        time.sleep(.25)
    pytest.fail('Actual current-version query exceeded its completion deadline')


@pytest.mark.parametrize('account', ['demo-north', 'demo-south'])
def test_actual_catalog_examples_provenance_and_result_match_across_transports(actual_demo, account):
    _, client, tokens = actual_demo
    description = demo_request(client, tokens, account, 'GET', '/v1/products/revenue')
    assert description.status_code == 200
    rest = description.json()
    assert rest == mcp_value(mcp_request(client, tokens[account], 'describe_product', {'product_id': 'revenue'}))
    assert rest['product_id'] == 'revenue' and rest['version']
    assert rest['examples'] and rest['provenance']['adapter_revision'] == 'cube-rest-v1'
    assert rest['partial'] is False and set(rest['outputs']) == {'day', 'segment', 'revenue'}
    search = demo_request(client, tokens, account, 'GET', '/v1/products', params={'q': 'revenue'})
    assert search.status_code == 200
    assert search.json() == mcp_value(mcp_request(client, tokens[account], 'search_products', {'query': 'revenue'}))
    assert [item['digest'] for item in search.json()['items']] == [rest['digest']]
    result = query_described_version(client, tokens, account, rest)
    from_mcp = mcp_value(mcp_request(client, tokens[account], 'get_result', {'execution_id': result['execution_id']}))
    # Each fetch deliberately reassesses freshness at its own server time.
    assert {key: value for key, value in from_mcp.items() if key != 'freshness'} == {
        key: value for key, value in result.items() if key != 'freshness'}
    from datetime import datetime
    assert datetime.fromisoformat(from_mcp['freshness']['assessed_at']) >= datetime.fromisoformat(result['freshness']['assessed_at'])
    for value in (result, from_mcp):
        freshness = value['freshness']
        assert freshness['status'] == 'fresh'
        assert 0 <= freshness['age_seconds'] <= freshness['max_age_seconds']
    assert result['product_digest'] == rest['digest'] and result['provenance'] == rest['provenance']
    assert result['row_count'] == len(result['rows']) and result['truncated'] is False
    assert result['source_watermark'] and result['next_cursor'] is None


def test_actual_query_replay_preserves_pinned_execution_across_rest_and_mcp(actual_demo):
    """One canonical request and principal share idempotency across both transports."""
    import uuid
    _, client, tokens = actual_demo
    request = {'product_id': 'revenue', 'inputs': {'start': '2024-03-09', 'end': '2024-03-12'},
        'idempotency_key': 'acceptance-' + uuid.uuid4().hex}
    first = demo_request(client, tokens, 'demo-north', 'POST', '/v1/executions', json=request)
    assert first.status_code == 200
    second = mcp_value(mcp_request(client, tokens['demo-north'], 'query_product', {'request': request}))
    assert second['execution_id'] == first.json()['execution_id']
    changed = {**request, 'inputs': {'start': '2024-02-01', 'end': '2024-02-02'}}
    conflict = mcp_request(client, tokens['demo-north'], 'query_product', {'request': changed})
    assert conflict.status_code == 200
    assert conflict.json()['result']['structuredContent']['code'] == 'CONFLICT'
