"""REST boundary tests. Stubs isolate dispatch, not database authorization evidence."""
import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import ExecutionView

pytestmark = pytest.mark.contract


def api():
    try:
        module=importlib.import_module('gateway.api.app')
        from gateway.api.transport import TransportSettings
        original=module.create_app
        return SimpleNamespace(create_app=lambda services: original(services,settings=TransportSettings(allowed_hosts=('testserver',))))
    except ModuleNotFoundError as error:
        pytest.fail(f'REST API implementation is missing: {error.name}')


def services(principal, clock):
    accepted = ExecutionView(execution_id='execution-1', state='accepted', accepted_at=clock.now())
    return SimpleNamespace(authenticator=SimpleNamespace(authenticate=AsyncMock(return_value=principal)),
        registry=None, policy=SimpleNamespace(change_policy=AsyncMock(return_value=2)), bindings=None, bundles=None,
        execution=SimpleNamespace(submit_and_wait=AsyncMock(return_value=accepted)),
        results=SimpleNamespace(get=AsyncMock(return_value=accepted)), publishing=SimpleNamespace(), clock=clock,
        catalog=SimpleNamespace(search=AsyncMock(return_value={'items': [], 'next_cursor': None}), describe=AsyncMock()),
        choices=SimpleNamespace(bindings=AsyncMock(return_value={'items': [], 'next_cursor': None}),
                                policies=AsyncMock(return_value={'items': [], 'next_cursor': None})),
        audit_reader=SimpleNamespace(list=AsyncMock(return_value={'items': [], 'next_cursor': None})),
        readiness=SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(ready=True))))


async def request(svc, method, path, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api().create_app(svc)), base_url='http://testserver') as client:
        return await client.request(method, path, **kwargs)


def test_catalog_dispatch_and_fresh_authentication(north_principal, clock):
    svc = services(north_principal, clock)
    response = asyncio.run(request(svc, 'GET', '/v1/products?q=revenue&limit=3', headers={'Authorization':'Bearer synthetic'}))
    assert response.status_code == 200
    assert response.json() == {'items': [], 'next_cursor': None}
    svc.authenticator.authenticate.assert_awaited_once_with('Bearer synthetic')
    svc.catalog.search.assert_awaited_once_with(north_principal, query='revenue', cursor=None, limit=3)
    assert response.headers['cache-control'] == 'no-store'


def test_execution_submission_and_read_use_separate_safe_services(north_principal, clock):
    svc = services(north_principal, clock)
    response = asyncio.run(request(svc, 'POST', '/v1/executions', json={'product_id':'revenue','inputs':{}}))
    assert response.status_code == 200 and response.json()['execution_id'] == 'execution-1'
    args, kwargs = svc.execution.submit_and_wait.await_args
    assert args[0] == north_principal and args[1].product_id == 'revenue' and kwargs == {'seconds':5}
    response = asyncio.run(request(svc, 'GET', '/v1/executions/execution-1?cursor=opaque'))
    assert response.status_code == 200
    svc.results.get.assert_awaited_once_with(north_principal, 'execution-1', 'opaque')


@pytest.mark.parametrize('code,status', [('UNAUTHENTICATED',401),('NOT_FOUND_OR_FORBIDDEN',404),
    ('INVALID_INPUT',422),('CONFLICT',409),('RESOURCE_LIMIT',429),('UPSTREAM_UNAVAILABLE',503),
    ('BINDING_DRIFT',503),('STALE_DATA',503),('FRESHNESS_UNKNOWN',503),('EXECUTION_TIMEOUT',503)])
def test_errors_have_fixed_safe_text_and_server_correlation(north_principal, clock, code, status):
    svc = services(north_principal, clock)
    svc.catalog.search.side_effect = GatewayError(code,'upstream-secret','SECRET SQL PASSWORD')
    response = asyncio.run(request(svc,'GET','/v1/products',headers={'X-Request-ID':'attacker'}))
    assert response.status_code == status
    body = response.json()
    assert body['code'] == code and body['request_id'] == response.headers['x-request-id']
    assert body['request_id'] not in {'attacker','upstream-secret'}
    assert 'SECRET' not in response.text and 'SQL' not in response.text


def test_hidden_and_unknown_have_identical_public_errors(north_principal, clock):
    svc=services(north_principal,clock)
    outputs=[]
    for message in ('hidden product','missing product'):
        svc.catalog.describe.side_effect=GatewayError('NOT_FOUND_OR_FORBIDDEN','service-request',message)
        response=asyncio.run(request(svc,'GET','/v1/products/restricted-revenue'))
        body=response.json(); body.pop('request_id'); outputs.append(body)
    assert outputs[0] == outputs[1]


def test_health_readiness_and_no_hosted_docs(north_principal,clock):
    svc=services(north_principal,clock)
    assert asyncio.run(request(svc,'GET','/healthz')).json() == {'alive':True}
    assert asyncio.run(request(svc,'GET','/readyz')).status_code == 200
    svc.readiness.check.return_value=SimpleNamespace(ready=False)
    assert asyncio.run(request(svc,'GET','/readyz')).status_code == 503
    svc.authenticator.authenticate.assert_not_awaited()
    assert asyncio.run(request(svc,'GET','/docs')).status_code == 404


def test_explicit_route_inventory_and_openapi_snapshot(north_principal,clock):
    schema=api().create_app(services(north_principal,clock)).openapi()
    expected={'/v1/products','/v1/products/{product_id}','/v1/executions','/v1/executions/{execution_id}',
        '/v1/drafts','/v1/drafts/{draft_id}','/v1/drafts/{draft_id}/tests','/v1/drafts/{draft_id}/tests/{run_id}',
        '/v1/drafts/{draft_id}/diff','/v1/drafts/{draft_id}/publish','/v1/products/{product_id}/lifecycle',
        '/v1/bindings','/v1/policies','/v1/policies/{policy_id}','/v1/audit','/healthz','/readyz'}
    assert set(schema['paths']) == expected
    assert not {'Decision','Principal','ExecutionSpec','FixtureCapability','HTTPValidationError'} & set(schema['components']['schemas'])
    from pathlib import Path
    assert schema == json.loads(Path('docs/compatibility/openapi.json').read_text())


def test_fixture_wire_projection_excludes_internal_proof(north_principal,clock):
    svc=services(north_principal,clock)
    row=SimpleNamespace(id='run-1',draft_id='draft-1',draft_digest='d'*64,binding_digest='b'*64,
        policy_id='revenue-access',policy_revision=1,fixture_version='1',status='passed',created_at=clock.now(),
        evidence={'schema_version':'1','catalog_digest':'c'*64,'execution_contract_digest':'e'*64,
            'fixture_binding_digest':'f'*64,'fixture_model_bundle_digest':'a'*64,
            'checks':[{'fixture_id':'north-total','kind':'known_answer','passed':True,'expected':[],'actual':[],
                'decision_digest':'secret-scope-digest','attempt_id':'private-attempt','absolute_tolerance':'0','relative_tolerance':'0',
                'expected_error':None,'actual_error':None,'reference_watermark':None,
                'observed_source_load_watermark':None,'freshness_status':None}]})
    svc.publishing.get_test=AsyncMock(return_value=row)
    response=asyncio.run(request(svc,'GET','/v1/drafts/draft-1/tests/run-1'))
    assert response.status_code==200
    body=response.json()
    assert body['created_at'] and body['policy_id']=='revenue-access'
    assert 'decision_digest' not in response.text and 'fixture_binding_digest' not in response.text and 'attempt_id' not in response.text
    assert body['checks'][0]['passed'] is True


def test_openapi_documents_all_page_inputs(north_principal,clock):
    schema=api().create_app(services(north_principal,clock)).openapi()
    for route,expected in [('/v1/products',{'q','cursor','limit'}),('/v1/bindings',{'draft_id','cursor','limit'}),
        ('/v1/policies',{'draft_id','cursor','limit'}),('/v1/audit',{'cursor','limit'})]:
        assert {item['name'] for item in schema['paths'][route]['get'].get('parameters',[])}==expected


def test_explicit_origin_preflight_and_response_headers(north_principal,clock):
    from gateway.api.app import create_app
    from gateway.api.transport import TransportSettings
    svc=services(north_principal,clock)
    async def scenario():
        app=create_app(svc,settings=TransportSettings(allowed_hosts=('testserver',),allowed_origins=('https://editor.example',)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver') as client:
            response=await client.options('/v1/products',headers={'Origin':'https://editor.example','Access-Control-Request-Method':'GET','Access-Control-Request-Headers':'authorization'})
            assert response.status_code==200 and response.headers['access-control-allow-origin']=='https://editor.example'
            svc.authenticator.authenticate.assert_not_awaited()
            response=await client.get('/v1/products',headers={'Origin':'https://editor.example'})
            assert response.headers['access-control-allow-origin']=='https://editor.example'
    asyncio.run(scenario())


def test_all_versioned_routes_document_bearer_security(north_principal,clock):
    schema=api().create_app(services(north_principal,clock)).openapi()
    assert schema['components']['securitySchemes']['GatewayBearer']['scheme']=='bearer'
    for path,operations in schema['paths'].items():
        for operation in operations.values():
            if path.startswith('/v1/'):assert operation['security']==[{'GatewayBearer':[]}]


def test_completed_failure_remains_execution_view(north_principal,clock):
    svc=services(north_principal,clock)
    svc.results.get.return_value=ExecutionView(execution_id='execution-1',state='failed',accepted_at=clock.now(),
        started_at=clock.now(),completed_at=clock.now(),error={'code':'BINDING_DRIFT','request_id':'recorded-request','safe_message':'The reviewed product binding is unavailable.'})
    response=asyncio.run(request(svc,'GET','/v1/executions/execution-1'))
    assert response.status_code==200 and response.json()['error']['code']=='BINDING_DRIFT'
    assert response.json()['error']['request_id']=='recorded-request'


def test_draft_and_admin_routes_dispatch_to_authoritative_services(north_principal,clock,valid_manifest):
    from gateway.contracts.manifest import validate_manifest
    from gateway.contracts.execution import PinnedProduct
    from gateway.publishing.service import DraftView,VersionDiff
    svc=services(north_principal,clock);manifest=validate_manifest(valid_manifest)
    row=SimpleNamespace(id='new-draft',product_id='revenue',revision=2,content_digest=manifest.content_digest)
    svc.publishing.save_draft=AsyncMock(return_value=row);svc.publishing.update_draft=AsyncMock(return_value=row)
    svc.publishing.get_draft=AsyncMock(return_value=DraftView(id='new-draft',product_id='revenue',revision=2,digest=manifest.content_digest,
        definition={'outputs':{'revenue':{'type':'decimal','precision':38,'scale':2}}},editable_full_definition=False,
        current_draft_id='new-draft',is_current_revision=True,can_test=True,can_publish=False))
    svc.publishing.diff=AsyncMock(return_value=VersionDiff(draft_id='new-draft',product_id='revenue',revision=2,latest_test_run_id=None,
        before_digest=None,after_digest=manifest.content_digest,changes=[],stale_evidence=['missing']))
    svc.publishing.publish=AsyncMock(return_value=PinnedProduct(manifest=manifest,binding_digest='b'*64,version_id='version-1'))
    svc.publishing.transition=AsyncMock()
    for method,path in [('POST','/v1/drafts'),('PUT','/v1/drafts/old-draft')]:
        response=asyncio.run(request(svc,method,path,json={'manifest':valid_manifest,'expected_revision':1}))
        assert response.status_code==200 and response.json()['id']=='new-draft' and 'manifest' not in response.json()
    args=svc.publishing.update_draft.await_args.args
    assert args[:2]==(north_principal,'old-draft') and args[3]==1
    assert asyncio.run(request(svc,'GET','/v1/drafts/new-draft')).json()['editable_full_definition'] is False
    assert asyncio.run(request(svc,'GET','/v1/drafts/new-draft/diff')).json()['latest_test_run_id'] is None
    response=asyncio.run(request(svc,'POST','/v1/drafts/new-draft/publish',json={'tested_digest':manifest.content_digest,'expected_revision':2,'reason':'Reviewed'}))
    assert response.status_code==200 and response.json()['version_id']=='version-1'
    response=asyncio.run(request(svc,'POST','/v1/products/revenue/lifecycle',json={'version':'1.0.0','action':'deprecate','reason':'Reviewed'}))
    assert response.status_code==200 and response.json()=={'changed':True}
    svc.publishing.transition.assert_awaited_once_with(north_principal,'revenue','1.0.0','deprecate','Reviewed')
    response=asyncio.run(request(svc,'GET','/v1/audit?limit=2'))
    assert response.status_code==200
    svc.audit_reader.list.assert_awaited_once_with(north_principal,cursor=None,limit=2)


def test_no_network_fetch_for_inert_metadata_or_app_creation(north_principal,clock,valid_manifest,monkeypatch):
    import socket
    from tests.unit.test_api_services import binding,decision
    from gateway.contracts.manifest import validate_manifest
    from gateway.contracts.execution import PinnedProduct
    from gateway.api.catalog import project_product
    calls=[]
    def forbidden(*args,**kwargs):calls.append(args);raise AssertionError('Unexpected network call')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    manifest=validate_manifest(valid_manifest);b=binding(manifest)
    project_product(PinnedProduct(manifest=manifest,binding_digest=b.digest,version_id='version-1'),decision(north_principal,['day','segment','revenue']),b,'published')
    response=asyncio.run(request(services(north_principal,clock),'GET','/v1/products'))
    assert response.status_code==200 and calls==[]


@pytest.mark.parametrize('kind',['value','framework'])
def test_unexpected_service_errors_are_generic_availability(north_principal,clock,kind):
    from starlette.exceptions import HTTPException
    svc=services(north_principal,clock)
    svc.catalog.search.side_effect=ValueError('private upstream query') if kind=='value' else HTTPException(503,'private upstream query')
    response=asyncio.run(request(svc,'GET','/v1/products'))
    assert response.status_code==503 and response.json()['code']=='UPSTREAM_UNAVAILABLE'
    assert 'private' not in response.text


@pytest.mark.parametrize('failure,status,code',[
    ('authentication',401,'UNAUTHENTICATED'),('duplicate_auth',401,'UNAUTHENTICATED'),
    ('duplicate_json',422,'INVALID_INPUT'),('oversized',413,'RESOURCE_LIMIT'),
    ('unexpected_authentication',503,'UPSTREAM_UNAVAILABLE')])
@pytest.mark.parametrize('origin',['https://editor.example','https://unapproved.example'])
def test_boundary_errors_use_only_exact_approved_cors(north_principal,clock,failure,status,code,origin):
    from gateway.api.app import create_app
    from gateway.api.transport import TransportSettings
    svc=services(north_principal,clock)
    if failure=='authentication':svc.authenticator.authenticate.side_effect=GatewayError('UNAUTHENTICATED','service-private','private token detail')
    if failure=='unexpected_authentication':svc.authenticator.authenticate.side_effect=RuntimeError('private dependency detail')
    headers=[('Origin',origin),('Content-Type','application/json')]
    body=b'{"product_id":"revenue","inputs":{}}'
    if failure=='duplicate_auth':headers.extend([('Authorization','Bearer one'),('Authorization','Bearer two')])
    if failure=='duplicate_json':body=b'{"product_id":"first","product_id":"second","inputs":{}}'
    if failure=='oversized':body=b'x'*65537
    async def scenario():
        app=create_app(svc,settings=TransportSettings(allowed_hosts=('testserver',),allowed_origins=('https://editor.example',)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver') as client:
            return await client.post('/v1/executions',content=body,headers=headers)
    response=asyncio.run(scenario())
    assert response.headers['cache-control']=='no-store'
    assert response.json()['request_id']==response.headers['x-request-id']
    assert 'private' not in response.text
    if origin=='https://editor.example':
        assert (response.status_code,response.json()['code'])==(status,code)
        assert response.headers.get('access-control-allow-origin')==origin
        assert 'x-request-id' in response.headers.get('access-control-expose-headers','').lower()
        assert 'origin' in response.headers.get('vary','').lower()
    else:
        assert response.headers.get('access-control-allow-origin') is None
        assert response.headers.get('access-control-expose-headers') is None
    svc.execution.submit_and_wait.assert_not_awaited()


@pytest.mark.parametrize('extra_headers',[
    [('Host','unapproved.example')],
    [('Origin','https://editor.example'),('Origin','https://unapproved.example')],
    [('Host','testserver'),('Host','unapproved.example')],
])
def test_rejected_host_or_ambiguous_origin_never_gets_cors(north_principal,clock,extra_headers):
    from gateway.api.app import create_app
    from gateway.api.transport import TransportSettings
    svc=services(north_principal,clock)
    headers=extra_headers if any(name=='Origin' for name,_ in extra_headers) else [('Origin','https://editor.example'),*extra_headers]
    async def scenario():
        app=create_app(svc,settings=TransportSettings(allowed_hosts=('testserver',),allowed_origins=('https://editor.example',)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver') as client:
            return await client.get('/v1/products',headers=headers)
    response=asyncio.run(scenario())
    assert response.status_code==422
    assert response.headers.get('access-control-allow-origin') is None
    svc.authenticator.authenticate.assert_not_awaited()
