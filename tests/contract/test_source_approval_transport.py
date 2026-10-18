"""Real REST/MCP transport rejection, with service doubles only for dispatch."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from tests.contract.test_rest import request, services as rest_services
from tests.contract.test_mcp import body, post, running_contract as running, services as mcp_services, PROTOCOL

pytestmark = pytest.mark.contract
HEADERS = [
    [('On-Behalf-Of', 'synthetic-actor')],
    [('x-on-behalf-of', '')],
    [('oN-bEhAlF-oF', 'one'), ('ON-BEHALF-OF', 'two')],
    [('X-On-Behalf-Of', 'one'), ('x-on-behalf-of', 'two')],
]
TOOLS = [
    ('search_products', {}),
    ('describe_product', {'product_id': 'revenue'}),
    ('query_product', {'request': {'product_id': 'revenue', 'inputs': {}}}),
    ('get_result', {'execution_id': 'execution-1'}),
]


@pytest.mark.parametrize('headers', HEADERS)
def test_reviewed_rest_rejects_designated_raw_headers(north_principal, clock, headers):
    svc = rest_services(north_principal, clock)
    svc.authenticator.settings = SimpleNamespace(source='reviewed_sources')
    response = asyncio.run(request(svc, 'GET', '/v1/products', headers=headers))
    assert response.status_code == 422
    assert response.json()['code'] == 'INVALID_INPUT'
    assert 'synthetic-actor' not in response.text
    svc.authenticator.authenticate.assert_not_awaited()
    svc.catalog.search.assert_not_awaited()


@pytest.mark.parametrize('headers', HEADERS)
@pytest.mark.parametrize('name,arguments', TOOLS)
def test_all_mcp_tools_reject_designated_raw_headers(north_principal, clock, headers, name, arguments):
    async def scenario():
        svc = mcp_services(north_principal, clock)
        svc.authenticator.settings.source = 'reviewed_sources'
        async with running(svc) as (base, _):
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(base + '/mcp', json=body(name=name, arguments=arguments),
                    headers=[('Authorization', 'Bearer synthetic'), ('MCP-Protocol-Version', PROTOCOL),
                             ('MCP-Method', 'tools/call'), ('MCP-Name', name), *headers])
            assert response.status_code == 422
            assert response.json()['code'] == 'INVALID_INPUT'
            svc.authenticator.authenticate.assert_not_awaited()
            for method in (svc.catalog.search, svc.catalog.describe,
                           svc.execution.submit_and_wait, svc.results.get):
                method.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('source', ['synthetic_postgres', 'bigquery', 'postgres'])
def test_legacy_header_behavior_is_unchanged(north_principal, clock, source):
    svc = rest_services(north_principal, clock)
    svc.authenticator.settings = SimpleNamespace(source=source)
    response = asyncio.run(request(svc, 'GET', '/v1/products', headers=HEADERS[0]))
    assert response.status_code == 200
    svc.catalog.search.assert_awaited_once()


def test_reviewed_ignores_unrelated_spoof_headers(north_principal, clock):
    svc = rest_services(north_principal, clock)
    svc.authenticator.settings = SimpleNamespace(source='reviewed_sources')
    response = asyncio.run(request(svc, 'GET', '/v1/products', headers={
        'X-User-ID': 'forged', 'X-Tenant-ID': 'forged', 'X-Roles': 'admin'}))
    assert response.status_code == 200
    assert svc.catalog.search.await_args.args[0] == north_principal


@pytest.mark.parametrize('name,arguments', TOOLS)
@pytest.mark.parametrize('field', ['on_behalf_of', 'actor_chain', 'delegated_source'])
def test_all_mcp_tools_reject_delegation_arguments(north_principal, clock, name, arguments, field):
    async def scenario():
        svc = mcp_services(north_principal, clock)
        svc.authenticator.settings.source = 'reviewed_sources'
        async with running(svc) as (base, _):
            response = await post(base, body(name=name, arguments={**arguments, field: 'synthetic-actor'}))
            assert response.status_code == 200
            assert response.json()['result']['structuredContent']['code'] == 'INVALID_INPUT'
            assert 'synthetic-actor' not in response.text
            for method in (svc.catalog.search, svc.catalog.describe,
                           svc.execution.submit_and_wait, svc.results.get):
                method.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('claim', ['act', 'may_act'])
@pytest.mark.parametrize('name,arguments', TOOLS)
def test_signed_delegation_rejected_before_all_mcp_dispatch(north_principal, clock, claim, name, arguments):
    from tests.unit.test_identity import authenticator, production_settings, signed_token
    async def scenario():
        svc = mcp_services(north_principal, clock)
        settings = production_settings().model_copy(update={'source': 'reviewed_sources'})
        svc.authenticator, _, client = authenticator(clock, settings=settings)
        try:
            async with running(svc, production=True) as (base, _):
                response = await post(base, body(name=name, arguments=arguments), headers={
                    'Authorization': 'Bearer ' + signed_token(clock, **{claim: None})})
                assert response.status_code == 401
                assert response.json()['code'] == 'UNAUTHENTICATED'
                for method in (svc.catalog.search, svc.catalog.describe,
                               svc.execution.submit_and_wait, svc.results.get):
                    method.assert_not_awaited()
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('claim', ['act', 'may_act'])
@pytest.mark.parametrize('method,path,payload', [
    ('GET', '/v1/products', None), ('GET', '/v1/products/revenue', None),
    ('POST', '/v1/executions', {'product_id': 'revenue', 'inputs': {}}),
    ('GET', '/v1/executions/execution-1', None),
])
def test_signed_delegation_rejected_before_rest_dispatch(north_principal, clock, claim, method, path, payload):
    from tests.unit.test_identity import authenticator, production_settings, signed_token
    async def scenario():
        svc = rest_services(north_principal, clock)
        settings = production_settings().model_copy(update={'source': 'reviewed_sources'})
        svc.authenticator, _, client = authenticator(clock, settings=settings)
        try:
            response = await request(svc, method, path, **({'json': payload} if payload is not None else {}),
                headers={'Authorization': 'Bearer ' + signed_token(clock, **{claim: {}})})
            assert response.status_code == 401
            assert response.json()['code'] == 'UNAUTHENTICATED'
            for target in (svc.catalog.search, svc.catalog.describe,
                           svc.execution.submit_and_wait, svc.results.get):
                target.assert_not_awaited()
        finally:
            await client.aclose()
    asyncio.run(scenario())
