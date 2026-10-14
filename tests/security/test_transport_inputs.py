"""Untrusted HTTP parsing must fail before service dispatch without echoing inputs."""
import asyncio
import json
import pytest
from tests.contract.test_rest import api, services, request

pytestmark=pytest.mark.contract


@pytest.mark.parametrize('payload', [
    {'product_id':'revenue','inputs':{},'sql':'select secret'},
    {'product_id':'revenue','inputs':{},'source_url':'https://attacker.example'},
    {'product_id':'revenue','inputs':{},'claimed_user_id':'admin'},
    {'product_id':'revenue','inputs':{},'decision':{'principal_id':'admin'}},
    {'product_id':'revenue','inputs':{},'filters':[{'field':'day','operator':'eq','value':'x','sql':'secret'}]},
])
def test_authority_and_unknown_fields_are_rejected(north_principal,clock,payload):
    svc=services(north_principal,clock)
    response=asyncio.run(request(svc,'POST','/v1/executions',json=payload))
    assert (response.status_code,response.json()['code']) == (422,'INVALID_INPUT')
    svc.execution.submit_and_wait.assert_not_awaited()
    assert 'attacker' not in response.text and 'secret' not in response.text


@pytest.mark.parametrize('content', [b'{"product_id":"first","product_id":"second","inputs":{}}',
    b'{"product_id":"revenue","inputs":{"x":1,"x":2}}', b'{"inputs":NaN}',b'\xff',b'{',
    b'{"inputs":'+b'['*70+b'0'+b']'*70+b'}'])
def test_invalid_or_ambiguous_json_rejected(north_principal,clock,content):
    svc=services(north_principal,clock)
    response=asyncio.run(request(svc,'POST','/v1/executions',content=content,headers={'Content-Type':'application/json'}))
    assert (response.status_code,response.json()['code']) == (422,'INVALID_INPUT')
    svc.execution.submit_and_wait.assert_not_awaited()


@pytest.mark.parametrize('path', ['/v1/products?claimed_user_id=admin','/v1/products?limit=2&limit=3',
    '/v1/products?limit=0','/v1/products?limit=51','/v1/products?limit=true','/v1/products?q='+('x'*257),
    '/v1/executions/e?context=fixture_test'])
def test_query_arguments_are_bounded_strict_and_unique(north_principal,clock,path):
    response=asyncio.run(request(services(north_principal,clock),'GET',path))
    assert (response.status_code,response.json()['code']) == (422,'INVALID_INPUT')


@pytest.mark.parametrize('headers', [[('Authorization','Bearer one'),('Authorization','Bearer two')],
    [('Content-Encoding','gzip')]])
def test_ambiguous_auth_or_compressed_body_rejected(north_principal,clock,headers):
    svc=services(north_principal,clock)
    response=asyncio.run(request(svc,'POST','/v1/executions',content=b'{}',headers=headers))
    assert response.status_code in {401,422}
    svc.execution.submit_and_wait.assert_not_awaited()


def test_bodyless_route_rejects_content(north_principal,clock):
    response=asyncio.run(request(services(north_principal,clock),'GET','/v1/products',content=b'{}'))
    assert response.status_code == 422


@pytest.mark.parametrize('declared', [None,'1','65537'])
def test_streaming_body_cap_before_json_parsing(north_principal,clock,declared):
    svc=services(north_principal,clock)
    chunks=[b'x'*32768,b'x'*32768,b'x',b'never read']
    count=0
    async def run():
        nonlocal count
        messages=[]
        async def receive():
            nonlocal count
            data=chunks[count];count+=1
            return {'type':'http.request','body':data,'more_body':True}
        async def send(message):messages.append(message)
        scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST','scheme':'http',
            'path':'/v1/executions','raw_path':b'/v1/executions','query_string':b'',
            'headers':[(b'host',b'testserver'),(b'content-type',b'application/json')],
            'client':('127.0.0.1',1),'server':('testserver',80)}
        if declared is not None:scope['headers'].append((b'content-length',declared.encode()))
        await api().create_app(svc)(scope,receive,send)
        return messages
    messages=asyncio.run(run())
    assert messages[0]['status'] == 413
    assert json.loads(messages[1]['body'])['code'] == 'RESOURCE_LIMIT'
    assert count <= 3
    svc.execution.submit_and_wait.assert_not_awaited()


@pytest.mark.parametrize('number',['1e309','-1e999'])
def test_overflowing_json_number_rejected_before_dispatch(north_principal,clock,number):
    svc=services(north_principal,clock)
    response=asyncio.run(request(svc,'POST','/v1/executions',content=('{"product_id":"revenue","inputs":{"x":'+number+'}}').encode(),headers={'Content-Type':'application/json'}))
    assert response.status_code==422
    svc.execution.submit_and_wait.assert_not_awaited()


def test_parser_itself_rejects_nonfinite_exponent():
    from gateway.api.transport import parse_json
    with pytest.raises(ValueError):parse_json(b'{"x":1e999}')


@pytest.mark.parametrize('headers',[{'Host':'attacker.example'},{'Origin':'https://attacker.example'},
    {'Content-Length':'-1'},{'Content-Length':'001x'}])
def test_host_origin_and_length_are_explicit(north_principal,clock,headers):
    response=asyncio.run(request(services(north_principal,clock),'GET','/v1/products',headers=headers))
    assert response.status_code==422


def test_body_exactly_64k_is_accepted_with_whitespace(north_principal,clock):
    payload=b'{"product_id":"revenue","inputs":{}}'
    payload+=b' '*(65536-len(payload))
    response=asyncio.run(request(services(north_principal,clock),'POST','/v1/executions',content=payload,headers={'Content-Type':'application/json'}))
    assert response.status_code==200


@pytest.mark.parametrize('path',['/healthz?secret=x','/readyz?principal_id=admin','/v1/products?q=%FF'])
def test_health_and_malformed_query_arguments_are_rejected(north_principal,clock,path):
    response=asyncio.run(request(services(north_principal,clock),'GET',path))
    assert response.status_code==422
