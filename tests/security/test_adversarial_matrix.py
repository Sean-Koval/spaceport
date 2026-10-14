"""Missing Task15 interactions, classified at their actual trust boundary.

E2E cases use the composed stack, real identities and real REST/MCP sockets.
The operator/service case uses real migrated PostgreSQL and actual authentication
and sockets, but deliberately seeded immutable results, without a Cube worker.
Existing revocation/cursor/cache/bounds cases are mapped in docs/evidence/security.md.
"""
import asyncio
import json
import secrets
from types import SimpleNamespace

import httpx
import pytest
from psycopg.types.json import Jsonb

from tests.e2e.test_acceptance import mcp_request, mcp_value, PROTOCOL
from tests.integration.conftest import registry_database
from tests.integration.test_operations import actual_demo, demo_request

QUERY = {'product_id': 'revenue', 'inputs': {'start': '2024-03-09', 'end': '2024-03-12'}}
MATRIX_CURSOR_KEY = b'm' * 32


def safe_error(response, code, *, mcp=False):
    assert response.status_code == (200 if mcp else {'UNAUTHENTICATED': 401, 'NOT_FOUND_OR_FORBIDDEN': 404, 'INVALID_INPUT': 422}[code])
    value = response.json()['result']['structuredContent'] if mcp else response.json()
    assert value['code'] == code
    assert set(value) == {'code', 'request_id', 'safe_message'}
    assert response.headers['cache-control'] == 'no-store'
    return {key: value[key] for key in value if key != 'request_id'}


@pytest.mark.e2e
@pytest.mark.parametrize('account', [None, 'demo-north', 'demo-south', 'demo-author', 'demo-publisher'],
    ids=['anonymous', 'north-consumer', 'south-consumer', 'author', 'publisher'])
def test_actual_external_profile_metadata_query_result_and_audit_matrix(actual_demo, account):
    _, client, tokens = actual_demo
    token = tokens[account] if account else None
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    # Claims supplied by a caller never elevate any of these identities.
    headers.update({'X-User-ID': 'demo-north', 'X-Roles': 'consumer,operator,audit_view', 'MCP-Session-ID': 'forged-authority'})
    authenticated = account is not None
    consumer = account in {'demo-north', 'demo-south'}
    search = client.get('/v1/products', headers=headers)
    mcp_search = mcp_request(client, token, 'search_products', {}, headers=headers)
    if not authenticated:
        safe_error(search, 'UNAUTHENTICATED')
        safe_error(mcp_search, 'UNAUTHENTICATED')
    else:
        assert search.status_code == 200
        assert search.json() == mcp_value(mcp_search)
        assert bool(search.json()['items']) is consumer
    describe = client.get('/v1/products/revenue', headers=headers)
    mcp_describe = mcp_request(client, token, 'describe_product', {'product_id': 'revenue'}, headers=headers)
    if consumer:
        assert describe.status_code == 200
        assert describe.json() == mcp_value(mcp_describe)
    else:
        code = 'NOT_FOUND_OR_FORBIDDEN' if authenticated else 'UNAUTHENTICATED'
        hidden = safe_error(describe, code)
        missing = safe_error(client.get('/v1/products/does-not-exist', headers=headers), code)
        assert hidden == missing
        safe_error(mcp_describe, code, mcp=authenticated)
        safe_error(client.post('/v1/executions', headers=headers, json=QUERY), code)
        safe_error(mcp_request(client, token, 'query_product', {'request': QUERY}, headers=headers), code, mcp=authenticated)
    code = 'NOT_FOUND_OR_FORBIDDEN' if authenticated else 'UNAUTHENTICATED'
    safe_error(client.get('/v1/executions/does-not-exist', headers=headers), code)
    safe_error(mcp_request(client, token, 'get_result', {'execution_id': 'does-not-exist', 'cursor': 'forged-cursor'}, headers=headers), code, mcp=authenticated)
    # Operator capability is not present in these four external accounts; audit
    # needs its own independent audit_view grant even for an operator.
    safe_error(client.get('/v1/audit', headers=headers), code)


@pytest.mark.e2e
@pytest.mark.parametrize('injection', [
    {'claimed_user_id': 'ADVERSARIAL_SENTINEL'},
    {'source_url': 'https://ADVERSARIAL_SENTINEL.example'},
    {'sql': 'select ADVERSARIAL_SENTINEL'},
    {'filters': [{'field': 'Orders.tenant_id', 'operator': 'eq', 'value': 'ADVERSARIAL_SENTINEL'}]},
    {'inputs': {'start': '2024-01-01', 'end': '2025-01-02'}},
])
def test_actual_rest_mcp_reject_authority_members_and_date_overflow(actual_demo, injection):
    _, client, tokens = actual_demo
    payload = {**QUERY, **injection}
    rest = demo_request(client, tokens, 'demo-north', 'POST', '/v1/executions', json=payload)
    mcp = mcp_request(client, tokens['demo-north'], 'query_product', {'request': payload})
    safe_error(rest, 'INVALID_INPUT')
    safe_error(mcp, 'INVALID_INPUT', mcp=True)
    assert 'ADVERSARIAL_SENTINEL' not in rest.text + mcp.text


@pytest.mark.postgres
@pytest.mark.parametrize('profile', ['operator', 'service'])
def test_actual_postgres_operator_and_service_authority_over_same_socket(registry_database, valid_manifest, tmp_path, profile):
    """Real authority and socket; seeded result is explicitly not live Cube evidence."""
    from gateway.adapters.base import StaticBindingResolver
    from gateway.api.audit import AuditReadService
    from gateway.api.catalog import CatalogService
    from gateway.api.cursors import PageCursorCodec
    from gateway.auth.principals import PrincipalStore, credential_digest
    from gateway.auth.settings import Settings
    from gateway.auth.tokens import Authenticator
    from gateway.execution.service import ExecutionService
    from tests.contract.test_mcp import body, running
    from tests.integration.test_results import seeded_result

    async def scenario():
        ctx = await seeded_result(registry_database, valid_manifest, count=201)
        registry = registry_database.registry
        kind = 'service' if profile == 'service' else 'demo'
        actor = 'matrix-' + profile
        token = ('dpg_svc_' if kind == 'service' else 'dpg_demo_') + secrets.token_urlsafe(32)
        owner_token = 'dpg_demo_' + secrets.token_urlsafe(32)
        registry_database.execute_owner(
            'INSERT INTO gateway.principals (id,subject,kind,service_client_id,active) VALUES (%s,%s,%s,%s,true)',
            (actor, 'synthetic-' + actor, kind, 'matrix-client' if kind == 'service' else None))
        registry_database.execute_owner(
            "INSERT INTO gateway.principal_grants (principal_id,policy_id,tenant_id,roles,entitlements) VALUES (%s,'revenue-access','north',%s,'{\"groups\":[\"north\"]}')",
            (actor, Jsonb(['consumer'] if kind == 'service' else ['operator'])))
        hashes = tmp_path / 'credential-hashes.json'
        hashes.write_text(json.dumps({'version': 1, 'credentials': [
            {'digest': credential_digest(token), 'principal_id': actor, 'kind': kind, 'active': True},
            {'digest': credential_digest(owner_token), 'principal_id': 'north-user', 'kind': 'demo', 'active': True}]}))
        hashes.chmod(0o600)
        settings = {'credential_hash_file': str(hashes)}
        if profile == 'service':
            # Real opaque service-key verification requires production identity
            # mode. These reserved synthetic issuer URLs are never contacted.
            settings.update(mode='production', oidc_issuer='https://issuer.example.test',
                oidc_jwks_url='https://issuer.example.test/keys', oidc_audience='synthetic-gateway',
                oidc_access_token_profile='rfc9068', oidc_client_ids=('synthetic-client',))
        auth = Authenticator(Settings(**settings), PrincipalStore(registry.sessions), clock=ctx.clock)
        bindings = StaticBindingResolver([ctx.binding])
        cursors = PageCursorCodec(MATRIX_CURSOR_KEY, ctx.clock)
        svc = SimpleNamespace(authenticator=auth, clock=ctx.clock,
            catalog=CatalogService(registry=registry, policy=ctx.policy, bindings=bindings, cursors=cursors),
            execution=ExecutionService(registry=registry, policy=ctx.policy, bindings=bindings, clock=ctx.clock),
            results=ctx.service,
            audit_reader=AuditReadService(registry=registry, policy=ctx.policy, cursors=cursors, clock=ctx.clock))
        try:
            async with running(svc, production=profile == 'service') as (base, _), httpx.AsyncClient(base_url=base, trust_env=False,
                limits=httpx.Limits(max_connections=1), timeout=10) as client:
                async def call(name, args, bearer):
                    return await client.post('/mcp', headers={'Authorization': 'Bearer ' + bearer,
                        'MCP-Protocol-Version': PROTOCOL, 'MCP-Method': 'tools/call', 'MCP-Name': name,
                        'Accept': 'application/json, text/event-stream', 'MCP-Session-ID': 'same-connection',
                        'X-User-ID': 'north-user', 'X-Roles': 'consumer,audit_view'}, json=body(name=name, arguments=args))
                if profile == 'service':
                    # Demo owner tokens are intentionally invalid in production
                    # mode. Obtain this seeded owner's cursor through the real
                    # result service before testing the foreign service key.
                    from tests.integration.test_audit import principal
                    owned = (await ctx.service.get(principal(), 'execution-1')).model_dump(mode='json')
                    safe_error(await call('search_products', {}, owner_token), 'UNAUTHENTICATED')
                else:
                    owned = mcp_value(await call('get_result', {'execution_id': 'execution-1'}, owner_token))
                assert len(owned['rows']) == 200 and owned['next_cursor']
                for cursor in (None, owned['next_cursor']):
                    response = await call('get_result', {'execution_id': 'execution-1', 'cursor': cursor}, token)
                    safe_error(response, 'NOT_FOUND_OR_FORBIDDEN', mcp=True)
                search = mcp_value(await call('search_products', {}, token))
                assert bool(search['items']) is (profile == 'service')
                description = await call('describe_product', {'product_id': 'revenue'}, token)
                if profile == 'service':
                    assert mcp_value(description)['digest'] == ctx.product.manifest.content_digest
                    query = mcp_value(await call('query_product', {'request': QUERY}, token))
                    assert registry_database.scalar('SELECT principal_id FROM gateway.executions WHERE id=%s', (query['execution_id'],)) == actor
                    assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE actor_id=%s AND service_client_id='matrix-client'", (actor,)) > 0
                else:
                    safe_error(description, 'NOT_FOUND_OR_FORBIDDEN', mcp=True)
                    safe_error(await call('query_product', {'request': QUERY}, token), 'NOT_FOUND_OR_FORBIDDEN', mcp=True)
                safe_error(await client.get('/v1/audit', headers={'Authorization': 'Bearer ' + token}), 'NOT_FOUND_OR_FORBIDDEN')
                if profile == 'operator':
                    # The same HTTP connection returns to the real owner's authority.
                    assert mcp_value(await call('get_result', {'execution_id': 'execution-1', 'cursor': owned['next_cursor']}, owner_token))['rows'] == ctx.envelope.model_dump(mode='json')['rows'][200:]
                else:
                    # Invalid demo auth on this connection cannot replace the
                    # scoped service identity or prevent its subsequent use.
                    safe_error(await call('search_products', {}, owner_token), 'UNAUTHENTICATED')
                    assert mcp_value(await call('get_result', {'execution_id': query['execution_id']}, token))['state'] == 'accepted'
                registry_database.execute_owner('UPDATE gateway.principal_grants SET active=false WHERE principal_id=%s', (actor,))
                assert mcp_value(await call('search_products', {}, token))['items'] == []
                registry_database.execute_owner('UPDATE gateway.principals SET active=false WHERE id=%s', (actor,))
                safe_error(await call('search_products', {}, token), 'UNAUTHENTICATED')
        finally:
            await auth.close()
    asyncio.run(scenario())
