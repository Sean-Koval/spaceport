"""MCP tests use actual loopback HTTP; doubles only establish transport dispatch."""
import importlib
import pytest

pytestmark = pytest.mark.contract


def mcp_module():
    try:
        return importlib.import_module('gateway.mcp.server')
    except ModuleNotFoundError as error:
        pytest.fail(f'MCP implementation is missing: {error.name}')


def test_mcp_public_factory_exists():
    assert callable(mcp_module().create_mcp_server)

import asyncio
from contextlib import asynccontextmanager
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from gateway.api.app import create_app
from gateway.api.transport import TransportSettings
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import ExecutionView
from gateway.contracts.identity import Principal

PROTOCOL = '2026-07-28'


def services(principal, clock):
    async def authenticate(value):
        if value != 'Bearer synthetic':
            raise GatewayError('UNAUTHENTICATED','private','secret auth details')
        return principal
    accepted = ExecutionView(execution_id='execution-1', state='accepted', accepted_at=clock.now())
    return SimpleNamespace(authenticator=SimpleNamespace(authenticate=AsyncMock(side_effect=authenticate),
        settings=SimpleNamespace(mode='demo',oidc_issuer=None)), clock=clock,
        catalog=SimpleNamespace(search=AsyncMock(return_value={'items': [], 'next_cursor': None}),describe=AsyncMock()),
        execution=SimpleNamespace(submit_and_wait=AsyncMock(return_value=accepted)),
        results=SimpleNamespace(get=AsyncMock(return_value=accepted)),
        readiness=SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(ready=True))))


@asynccontextmanager
async def running(svc, *, production=False):
    module = mcp_module()
    sock=socket.socket(); sock.bind(('127.0.0.1',0)); sock.listen(128)
    port=sock.getsockname()[1]
    server=module.create_mcp_server(svc,settings=module.MCPSettings(
        resource_url='https://gateway.example/mcp' if production else f'http://127.0.0.1:{port}/mcp',
        allowed_hosts=(f'127.0.0.1:{port}','gateway.example'),
        allowed_origins=('http://approved.example',)))
    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run(): yield
    app=create_app(svc,settings=TransportSettings(allowed_hosts=('127.0.0.1',),
        allowed_origins=('http://approved.example',),authenticated_prefixes=('/v1','/mcp')),lifespan=lifespan)
    module.mount_mcp(app,server)
    runner=uvicorn.Server(uvicorn.Config(app,log_level='critical',access_log=False,host='127.0.0.1',lifespan='on'))
    task=asyncio.create_task(runner.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if runner.started:break
            if task.done():await task
            await asyncio.sleep(.01)
        assert runner.started
        yield f'http://127.0.0.1:{port}',server
    finally:
        runner.should_exit=True
        await asyncio.wait_for(task,10)
        sock.close()


@asynccontextmanager
async def sdk(base):
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer synthetic'},trust_env=False,timeout=10) as http:
        async with Client(streamable_http_client(base+'/mcp',http_client=http,max_sse_event_size=8*1024*1024),
                          mode=PROTOCOL,cache=None,read_timeout_seconds=10) as client:
            yield client


def body(method='tools/call',arguments=None,name='search_products',request_id=1):
    params={'_meta':{'io.modelcontextprotocol/protocolVersion':PROTOCOL,
                     'io.modelcontextprotocol/clientCapabilities':{}}}
    if method=='tools/call': params.update(name=name,arguments=arguments if arguments is not None else {})
    return {'jsonrpc':'2.0','id':request_id,'method':method,'params':params}


async def post(base, payload, **kwargs):
    headers={'Authorization':'Bearer synthetic','MCP-Protocol-Version':PROTOCOL,
             'MCP-Method':payload.get('method','tools/call'),'Accept':'application/json, text/event-stream'}
    if payload.get('method')=='tools/call':headers['MCP-Name']=payload['params']['name']
    headers.update(kwargs.pop('headers',{}))
    async with httpx.AsyncClient(trust_env=False) as client:
        return await client.post(base+'/mcp',json=payload,headers=headers,**kwargs)


def test_official_python_http_discovery_strict_schema_and_dispatch(north_principal,clock):
    async def check():
        svc=services(north_principal,clock)
        async with running(svc) as (base,server),sdk(base) as client:
            assert client.protocol_version == PROTOCOL
            listing=await client.list_tools()
            assert {tool.name for tool in listing.tools}=={'search_products','describe_product','query_product','get_result'}
            for tool in listing.tools: assert tool.input_schema['additionalProperties'] is False
            result=await client.call_tool('search_products',{'query':'"literal"','limit':3})
            assert result.structured_content == {'items':[],'next_cursor':None}
            assert len(result.content[0].text)<100
            svc.catalog.search.assert_awaited_once_with(north_principal,query='"literal"',cursor=None,limit=3)
            result=await client.call_tool('query_product',{'request':{'product_id':'revenue','inputs':{}}})
            assert result.structured_content['execution_id']=='execution-1'
            args,kwargs=svc.execution.submit_and_wait.await_args
            assert args[0]==north_principal and args[1].product_id=='revenue' and kwargs=={'seconds':5}
            result=await client.call_tool('get_result',{'execution_id':'execution-1'})
            assert result.structured_content['state']=='accepted'
            async with httpx.AsyncClient(trust_env=False) as http:
                assert (await http.get(base+'/healthz')).status_code==200
                assert (await http.get(base+'/.well-known/oauth-protected-resource/mcp')).status_code==404
            assert svc.authenticator.authenticate.await_count>=4
    asyncio.run(check())


@pytest.mark.parametrize('name,arguments',[
    ('search_products',{'limit':True}),('search_products',{'limit':'50'}),('search_products',{'limit':51}),
    ('search_products',{'query':'x'*257}),('search_products',{'tenant':'SECRET_SENTINEL'}),
    ('search_products',{'principal':'SECRET_SENTINEL'}),('search_products',{'cursor':'x'*4097}),
    ('describe_product',{'product_id':'revenue','version':True}),
    ('query_product',{'request':'{"product_id":"revenue"}'}),
    ('query_product',{'request':{'product_id':'revenue','cube_query':'SECRET_SENTINEL'}}),
    ('query_product',{'request':{'product_id':'revenue','filters':[{'field':'x','operator':'eq','member':'SECRET_SENTINEL'}]}}),
    ('get_result',{'execution_id':'execution-1','limit':1}),
])
def test_forged_or_coerced_arguments_fail_before_services(north_principal,clock,name,arguments,caplog):
    async def check():
        svc=services(north_principal,clock)
        async with running(svc) as (base,_):
            response=await post(base,body(name=name,arguments=arguments))
            assert response.status_code==200,response.text
            result=response.json()['result']
            assert result['isError'] is True
            assert result['structuredContent']['code']=='INVALID_INPUT'
            assert 'SECRET_SENTINEL' not in response.text
            assert response.headers['cache-control']=='no-store'
            svc.catalog.search.assert_not_awaited();svc.catalog.describe.assert_not_awaited()
            svc.execution.submit_and_wait.assert_not_awaited();svc.results.get.assert_not_awaited()
    asyncio.run(check())
    assert 'SECRET_SENTINEL' not in caplog.text


def test_fresh_auth_missing_origin_session_and_notification_guards(north_principal,clock):
    async def check():
        svc=services(north_principal,clock)
        async with running(svc) as (base,_):
            async with httpx.AsyncClient(trust_env=False) as http:
                for method in ('GET','POST','DELETE'):
                    r=await http.request(method,base+'/mcp',headers={'Mcp-Session-Id':'forged'})
                    assert r.status_code==401 and r.headers['www-authenticate'].startswith('Bearer')
            for origin in ('null','http://evil.example'):
                r=await post(base,body(),headers={'Origin':origin})
                assert r.status_code==422
            notification=body();notification.pop('id')
            r=await post(base,notification)
            assert r.status_code in (202,400)
            svc.catalog.search.assert_not_awaited()
            r=await post(base,body(),headers={'Origin':'http://approved.example'})
            assert r.status_code==200
            svc.authenticator.authenticate.side_effect=GatewayError('UNAUTHENTICATED','private','revoked')
            r=await post(base,body())
            assert r.status_code==401
    asyncio.run(check())


def test_safe_domain_and_unexpected_errors(north_principal,clock,caplog):
    async def check():
        svc=services(north_principal,clock)
        async with running(svc) as (base,_):
            for error,code in [(GatewayError('NOT_FOUND_OR_FORBIDDEN','secret','SQL SECRET_SENTINEL'),'NOT_FOUND_OR_FORBIDDEN'),
                               (RuntimeError('SQL SECRET_SENTINEL'),'UPSTREAM_UNAVAILABLE')]:
                svc.catalog.search.side_effect=error
                response=await post(base,body())
                result=response.json()['result']['structuredContent']
                assert result['code']==code and result['request_id']==response.headers['x-request-id']
                assert 'SECRET_SENTINEL' not in response.text
    asyncio.run(check())
    assert 'SECRET_SENTINEL' not in caplog.text


@pytest.mark.parametrize('source_order',['original','reversed'])
def test_independent_sdk_client_harnesses_over_real_http(north_principal,clock,tmp_path,source_order):
    """SDK/process compatibility only; services here are explicitly synthetic doubles."""
    from pathlib import Path
    import os
    from tests.unit.test_normalization import setup,completed
    from gateway.api.catalog import project_product
    from gateway.contracts.query import validate_query
    async def check():
        svc=services(north_principal,clock)
        normalizer,product,context,view=setup(clock)
        description=project_product(product,context.decision,context.binding,'published')
        envelope=normalizer.normalize(product,completed(),view,context=context)
        if source_order=='reversed':
            values=envelope.model_dump(mode='json');values['rows'].reverse()
            envelope=type(envelope).model_validate(values)
        svc.catalog.search.return_value={'items':[description.model_dump(mode='json')],'next_cursor':None}
        svc.catalog.describe.return_value=description
        svc.execution.submit_and_wait.return_value=view
        svc.results.get.return_value=envelope
        token=tmp_path/'token';token.write_text('synthetic');token.chmod(0o600)
        answers=[]
        async with running(svc) as (base,_):
            for language,command in [('python',['.venv/bin/python','clients/python_agent.py']),
                                     ('typescript',['node','clients/typescript_agent.ts'])]:
                assert Path(command[-1]).is_file(),f'{language} client harness missing'
                process=await asyncio.create_subprocess_exec(*command,
                    env={**os.environ,'DPG_MCP_URL':base+'/mcp','DPG_TOKEN_FILE':str(token)},
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                stdout,stderr=await asyncio.wait_for(process.communicate(),20)
                assert process.returncode==0,(language,stderr.decode())
                result=json.loads(stdout)
                assert result['compatibility']['protocol']==PROTOCOL
                assert result['compatibility']['transport']=='streamable-http-json'
                answers.append(result['answer'])
            expected_sort=[{'field':'day','direction':'asc'},{'field':'segment','direction':'asc'}]
            emitted=[call.args[1] for call in svc.execution.submit_and_wait.await_args_list]
            assert [request.model_dump(mode='json')['sort'] for request in emitted]==[expected_sort,expected_sort]
            for request in emitted:
                assert validate_query(product.manifest,request).model_dump(mode='json')['sort']==expected_sort
            assert answers[0]==answers[1]
            # Preserve all raw returned rows, even when the transport double
            # intentionally reverses them; clients must not hide discrepancies.
            assert answers[0]['rows']==envelope.model_dump(mode='json')['rows']
            assert any(row['revenue']=='9007199254740993.01' for row in answers[0]['rows'])
            assert any(row['segment'] is None for row in answers[0]['rows'])
            assert svc.catalog.search.await_count==2 and svc.catalog.describe.await_count==2
            assert svc.execution.submit_and_wait.await_count==2 and svc.results.get.await_count==2
    asyncio.run(check())


def test_protocol_negotiation_and_fixed_surface(north_principal,clock):
    async def check():
        svc=services(north_principal,clock)
        async with running(svc) as (base,server):
            discover=await post(base,body(method='server/discover'))
            assert discover.status_code==200,discover.text
            assert set(discover.json()['result']['capabilities'])=={'tools'}
            for method in ('resources/list','prompts/list','subscriptions/listen','tasks/list','raw_sql'):
                response=await post(base,body(method=method))
                assert response.json()['error']['code']==-32601
            unknown=await post(base,body(name='SECRET_TOOL'))
            assert unknown.json()['error']['code']==-32602 and 'SECRET_TOOL' not in unknown.text
            payload=body();payload['params']['_meta']['io.modelcontextprotocol/protocolVersion']='2099-01-01'
            unsupported=await post(base,payload,headers={'MCP-Protocol-Version':'2099-01-01'})
            assert unsupported.status_code==400 and unsupported.json()['error']['code']==-32022
            mismatch=await post(base,body(),headers={'MCP-Protocol-Version':'2025-11-25'})
            assert mismatch.status_code==400
            pagination=body(method='tools/list');pagination['params']['cursor']='SECRET_CURSOR'
            response=await post(base,pagination)
            assert response.json()['error']['code']==-32602 and 'SECRET_CURSOR' not in response.text
            svc.catalog.search.assert_not_awaited()
            # Even the official in-memory client gets no HTTP request authority.
            with pytest.raises(Exception):
                async with Client(server,mode=PROTOCOL,cache=None) as direct:
                    await direct.list_tools()
    asyncio.run(check())


def test_same_connection_and_concurrent_requests_never_reuse_identity(north_principal,clock):
    async def check():
        svc=services(north_principal,clock)
        south=Principal(id='south-user',subject='synthetic-south',kind='demo',service_client_id=None)
        async def auth(value):
            if value=='Bearer synthetic':return north_principal
            if value=='Bearer south':return south
            raise GatewayError('UNAUTHENTICATED','id','secret')
        svc.authenticator.authenticate.side_effect=auth
        async def get(principal,execution_id,cursor=None):
            await asyncio.sleep(.01)
            if principal.id!='north-user':raise GatewayError('NOT_FOUND_OR_FORBIDDEN','id','secret')
            return ExecutionView(execution_id=execution_id,state='accepted',accepted_at=clock.now())
        svc.results.get.side_effect=get
        async with running(svc) as (base,_):
            payload=body(name='get_result',arguments={'execution_id':'execution-1','cursor':'north-cursor'})
            headers={'Authorization':'Bearer synthetic','MCP-Protocol-Version':PROTOCOL,'MCP-Method':'tools/call',
                     'MCP-Name':'get_result','Accept':'application/json, text/event-stream'}
            async with httpx.AsyncClient(trust_env=False,limits=httpx.Limits(max_connections=1)) as http:
                assert not (await http.post(base+'/mcp',json=payload,headers=headers)).json()['result']['isError']
                headers['Authorization']='Bearer south'
                denied=await http.post(base+'/mcp',json=payload,headers=headers)
                assert denied.json()['result']['structuredContent']['code']=='NOT_FOUND_OR_FORBIDDEN'
                del headers['Authorization'];headers['MCP-Session-ID']='north-session'
                assert (await http.post(base+'/mcp',json=payload,headers=headers)).status_code==401
            responses=await asyncio.gather(*(post(base,payload,headers={'Authorization':'Bearer '+token})
                for token in ['synthetic','south']*8))
            assert [r.json()['result']['isError'] for r in responses]==[False,True]*8
    asyncio.run(check())


@pytest.mark.parametrize('raw',[
    b'[]',b'{"jsonrpc":"2.0","jsonrpc":"2.0","id":1,"method":"tools/list"}',
    b'{"jsonrpc":"2.0","id":true,"method":"tools/list"}',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/list","secret":"SECRET_SENTINEL"}',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"x":1e309}}',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"x":"\xff"}}',
    b'{"x":'+b'['*35+b'0'+b']'*35+b'}',
])
def test_malformed_envelopes_are_bounded_and_safe(north_principal,clock,raw,caplog):
    async def check():
        svc=services(north_principal,clock)
        async with running(svc) as (base,_),httpx.AsyncClient(trust_env=False) as http:
            response=await http.post(base+'/mcp',content=raw,headers={'Authorization':'Bearer synthetic','Content-Type':'application/json'})
            assert response.status_code in (400,422)
            assert 'SECRET_SENTINEL' not in response.text
            svc.catalog.search.assert_not_awaited()
    asyncio.run(check())
    assert 'SECRET_SENTINEL' not in caplog.text


def test_body_chunking_full_limit_and_duplicate_headers(north_principal,clock):
    async def check():
        svc=services(north_principal,clock)
        async with running(svc) as (base,_),httpx.AsyncClient(trust_env=False) as http:
            content=json.dumps(body()).encode()
            headers={'Authorization':'Bearer synthetic','MCP-Protocol-Version':PROTOCOL,'MCP-Method':'tools/call',
                     'MCP-Name':'search_products','Accept':'application/json, text/event-stream','Content-Type':'application/json'}
            # Whitespace is legal JSON and included in the full transport bound.
            exact=content+b' '*(65536-len(content))
            assert (await http.post(base+'/mcp',content=exact,headers=headers)).status_code==200
            assert (await http.post(base+'/mcp',content=exact+b' ',headers=headers)).status_code==413
            async def chunks():
                yield exact[:40000];yield exact[40000:];yield b' '
            assert (await http.post(base+'/mcp',content=chunks(),headers=headers)).status_code==413
            for name in ('MCP-Protocol-Version','MCP-Method','Origin','Authorization','Content-Type'):
                pairs=list(headers.items())
                if name=='Host':pairs += [('Host',httpx.URL(base).netloc.decode())]*2
                elif name=='Origin':pairs += [('Origin','http://approved.example')]*2
                else:pairs += [(name,headers[name])]
                response=await http.post(base+'/mcp',content=content,headers=pairs)
                assert response.status_code in (401,422)
            endpoint=httpx.URL(base)
            reader,writer=await asyncio.open_connection(endpoint.host,endpoint.port)
            wire=(f'POST /mcp HTTP/1.1\r\nHost: {endpoint.netloc.decode()}\r\nHost: {endpoint.netloc.decode()}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n').encode()
            writer.write(wire);await writer.drain()
            raw=await asyncio.wait_for(reader.read(4096),5)
            assert raw.startswith(b'HTTP/1.1 400')
            writer.close();await writer.wait_closed()
            compressed=await http.post(base+'/mcp',content=content,headers={**headers,'Content-Encoding':'gzip'})
            assert compressed.status_code==422
    asyncio.run(check())


def test_output_validation_redacts_input_and_terminal_views_remain_results(north_principal,clock,caplog):
    async def check():
        svc=services(north_principal,clock)
        async with running(svc) as (base,_):
            svc.catalog.search.return_value={'items':'SECRET_SENTINEL'}
            response=await post(base,body())
            assert response.json()['result']['structuredContent']['code']=='UPSTREAM_UNAVAILABLE'
            assert 'SECRET_SENTINEL' not in response.text
            for state in ('failed','timed_out','expired'):
                values={'execution_id':'execution-1','state':state,'accepted_at':clock.now()}
                if state!='expired':values.update(completed_at=clock.now(),error={'code':'EXECUTION_TIMEOUT',
                    'request_id':'request-1','safe_message':'The execution deadline was exceeded.'})
                svc.results.get.return_value=ExecutionView(**values)
                result=(await post(base,body(name='get_result',arguments={'execution_id':'execution-1'}))).json()['result']
                assert result['isError'] is False and result['structuredContent']['state']==state
                assert 'rows' not in result['structuredContent']
    asyncio.run(check())
    assert 'SECRET_SENTINEL' not in caplog.text


def test_no_implicit_redirect_or_unauthenticated_mcp_preflight(north_principal,clock):
    async def check():
        svc=services(north_principal,clock)
        async with running(svc) as (base,_),httpx.AsyncClient(trust_env=False,follow_redirects=False) as http:
            headers={'Origin':'http://approved.example','Access-Control-Request-Method':'POST'}
            response=await http.options(base+'/mcp',headers=headers)
            assert response.status_code==401
            response=await http.options(base+'/mcp',headers={**headers,'Authorization':'Bearer synthetic'})
            assert response.status_code==405
            for path in ('/mcp/','/mcp/mcp'):
                response=await http.get(base+path,headers={'Authorization':'Bearer synthetic'})
                assert response.status_code==404
    asyncio.run(check())


@pytest.mark.parametrize('profile',['near_limit','paged'])
def test_both_clients_receive_large_lossless_and_paged_results(north_principal,clock,tmp_path,profile):
    import os
    from gateway.contracts.canonical import canonical_bytes,MAX_RESULT_BYTES
    from gateway.contracts.execution import ResultEnvelope
    from gateway.api.catalog import project_product
    from tests.unit.test_normalization import setup,completed
    async def check():
        svc=services(north_principal,clock)
        normalizer,product,context,view=setup(clock)
        description=project_product(product,context.decision,context.binding,'published')
        sample=normalizer.normalize(product,completed(),view,context=context).model_dump(mode='json')
        if profile=='near_limit':
            count=200;low=0;high=2000
            while low<high:
                middle=(low+high+1)//2
                sample['rows']=[{'day':'2024-03-09','segment':'"\\\n\x00é🐈'*middle,'revenue':'10.10'}]*count
                sample['row_count']=count
                if len(canonical_bytes(sample))<=MAX_RESULT_BYTES:low=middle
                else:high=middle-1
            sample['rows']=[{'day':'2024-03-09','segment':'"\\\n\x00é🐈'*low,'revenue':'10.10'}]*count
            assert MAX_RESULT_BYTES-4000<len(canonical_bytes(sample))<=MAX_RESULT_BYTES
        else:
            sample['rows']=[{'day':'2024-03-09','segment':str(index),'revenue':'10.10'} for index in range(451)]
            sample['row_count']=451
        complete=ResultEnvelope.model_validate(sample)
        async def get(principal,execution_id,cursor=None):
            offset=0 if cursor is None else int(cursor.removeprefix('page-'))
            page=complete.model_dump(mode='json');page['rows']=page['rows'][offset:offset+200]
            page['next_cursor']='page-'+str(offset+200) if offset+200<len(complete.rows) else None
            return ResultEnvelope.model_validate(page)
        svc.results.get.side_effect=get
        svc.catalog.search.return_value={'items':[description.model_dump(mode='json')],'next_cursor':None}
        svc.catalog.describe.return_value=description;svc.execution.submit_and_wait.return_value=view
        token=tmp_path/'token';token.write_text('synthetic');token.chmod(0o600)
        answers=[]
        async with running(svc) as (base,_):
            raw=await post(base,body(name='get_result',arguments={'execution_id':'execution-1'}))
            assert raw.status_code==200 and len(raw.content)<8*1024*1024
            assert len(raw.json()['result']['content'][0]['text'])<100
            for command in (['.venv/bin/python','clients/python_agent.py'],['node','clients/typescript_agent.ts']):
                process=await asyncio.create_subprocess_exec(*command,env={**os.environ,
                    'DPG_MCP_URL':base+'/mcp','DPG_TOKEN_FILE':str(token)},stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                stdout,stderr=await asyncio.wait_for(process.communicate(),25)
                assert process.returncode==0,stderr.decode()
                answers.append(json.loads(stdout)['answer'])
        assert answers[0]==answers[1] and answers[0]['rows']==sample['rows']
        if profile=='paged':assert svc.results.get.await_count==7
    asyncio.run(check())


def test_production_uses_actual_issuer_resource_and_verifier_without_fabricated_audience(clock,north_principal):
    from tests.unit.test_identity import authenticator,signed_token
    from gateway.mcp.server import AuthenticatorTokenVerifier
    async def check():
        auth,requests,http=authenticator(clock)
        svc=services(north_principal,clock);svc.authenticator=auth
        try:
            verified=await AuthenticatorTokenVerifier(auth).verify_token(signed_token(clock))
            assert verified.subject=='verified-user' and verified.client_id=='studio-client'
            assert verified.claims=={'iss':'https://issuer.example.test'} and verified.resource is None
            async with running(svc,production=True) as (base,server),httpx.AsyncClient(trust_env=False) as client:
                metadata=await client.get(base+'/.well-known/oauth-protected-resource/mcp')
                assert metadata.status_code==200
                assert metadata.json()['resource']=='https://gateway.example/mcp'
                assert metadata.json()['authorization_servers']==['https://issuer.example.test']
                unauthorized=await client.get(base+'/mcp')
                assert unauthorized.status_code==401
                assert unauthorized.headers['www-authenticate']=='Bearer resource_metadata="https://gateway.example/.well-known/oauth-protected-resource/mcp"'
                assert server.settings.auth.validate_token_resource is False
                success=await post(base,body(),headers={'Authorization':'Bearer '+signed_token(clock)})
                assert success.status_code==200 and not success.json()['result']['isError']
                called=svc.catalog.search.await_args.args[0]
                assert called.id=='db-user' and called.subject=='verified-user' and called.service_client_id=='studio-client'
        finally:await http.aclose()
    asyncio.run(check())


@pytest.mark.parametrize('changes',[{'exp':0},{'aud':'wrong'},{'iss':'https://wrong.example'},
    {'sub':'wrong'},{'client_id':'unapproved'},{'token_use':'id'}])
def test_production_verifier_rejects_profile_changes_without_service_call(north_principal,clock,changes):
    from tests.unit.test_identity import authenticator,signed_token
    async def check():
        auth,requests,http=authenticator(clock)
        svc=services(north_principal,clock);svc.authenticator=auth
        try:
            async with running(svc,production=True) as (base,_):
                response=await post(base,body(),headers={'Authorization':'Bearer '+signed_token(clock,**changes)})
                assert response.status_code==401
                svc.catalog.search.assert_not_awaited()
        finally:await http.aclose()
    asyncio.run(check())


def test_frozen_tool_schema_snapshot(north_principal,clock):
    from pathlib import Path
    async def check():
        svc=services(north_principal,clock)
        async with running(svc) as (base,_):
            response=await post(base,body(method='tools/list'))
            actual=response.json()['result']['tools']
            assert actual==json.loads(Path('docs/compatibility/mcp-tools.json').read_text())
    asyncio.run(check())


def test_python_harness_verifies_live_discovery(north_principal,clock,tmp_path):
    import os
    from mcp.shared.exceptions import MCPError
    async def check():
        svc=services(north_principal,clock)
        token=tmp_path/'token';token.write_text('synthetic');token.chmod(0o600)
        async with running(svc) as (base,server):
            async def reject_discovery(context,call_next):
                if context.method=='server/discover':raise MCPError(code=-32601,message='Unavailable')
                return await call_next(context)
            server.middleware.append(reject_discovery)
            process=await asyncio.create_subprocess_exec('.venv/bin/python','clients/python_agent.py',
                env={**os.environ,'DPG_MCP_URL':base+'/mcp','DPG_TOKEN_FILE':str(token)},
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(process.communicate(),20)
            assert process.returncode==1
            svc.catalog.search.assert_not_awaited()
    asyncio.run(check())


def test_result_wire_ceiling_rejects_before_partial_release(north_principal,clock):
    async def check():
        from gateway.mcp.server import _MCPBodyBoundary,MCPSettings,MAX_RESPONSE_BYTES
        async def oversized(scope,receive,send):
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'x'*(MAX_RESPONSE_BYTES+1)})
        app=_MCPBodyBoundary(oversized,MCPSettings())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1:8080') as client:
            response=await client.get('/mcp')
            assert response.status_code==429 and response.json()['code']=='RESOURCE_LIMIT'
            assert len(response.content)<1024 and not response.content.startswith(b'x')
    asyncio.run(check())


@pytest.mark.parametrize('method',['SECRET_METHOD_SENTINEL','tools/call'])
@pytest.mark.parametrize('profile',['headerless','legacy','unsupported','modern'])
def test_notification_admission_precedes_sdk_routing_without_request_value_logs(
    north_principal,clock,caplog,method,profile,
):
    import logging
    async def check():
        svc=services(north_principal,clock)
        payload={'jsonrpc':'2.0','method':method,'params':{'name':'SECRET_TOOL_SENTINEL',
            'arguments':{'secret':'SECRET_ARG_SENTINEL'}}}
        headers={'Authorization':'Bearer synthetic','Content-Type':'application/json',
            'Accept':'application/json, text/event-stream','X-Debug':'SECRET_HEADER_SENTINEL'}
        if profile!='headerless':
            headers['MCP-Protocol-Version']={'legacy':'2025-11-25',
                'unsupported':'SECRET_PROTOCOL_SENTINEL','modern':PROTOCOL}[profile]
        async with running(svc) as (base,_),httpx.AsyncClient(trust_env=False) as client:
            response=await client.post(base+'/mcp',json=payload,headers=headers)
            # Logs are checked first so the original legacy error/traceback leak
            # is the observed red, rather than only a newly expected status code.
            for sentinel in ('SECRET_METHOD_SENTINEL','SECRET_TOOL_SENTINEL','SECRET_ARG_SENTINEL',
                             'SECRET_HEADER_SENTINEL','SECRET_PROTOCOL_SENTINEL','Bearer synthetic'):
                assert sentinel not in caplog.text
                assert sentinel not in response.text
            assert response.status_code==(202 if profile=='modern' else 422)
            assert response.headers['cache-control']=='no-store'
            if profile=='modern':assert response.content==b''
            else:assert response.json()['code']=='INVALID_INPUT'
            svc.authenticator.authenticate.assert_awaited_once_with('Bearer synthetic')
            svc.catalog.search.assert_not_awaited();svc.catalog.describe.assert_not_awaited()
            svc.execution.submit_and_wait.assert_not_awaited();svc.results.get.assert_not_awaited()
    with caplog.at_level(logging.DEBUG):asyncio.run(check())


@pytest.mark.parametrize('meta',[
    'SECRET_META_SENTINEL',[],None,{'principal':'SECRET_META_SENTINEL'},
    {'io.modelcontextprotocol/protocolVersion':'2025-11-25'},
    {'io.modelcontextprotocol/clientCapabilities':'SECRET_META_SENTINEL'},
    {'io.modelcontextprotocol/clientInfo':'SECRET_META_SENTINEL'},
])
def test_malformed_modern_notification_metadata_is_rejected_before_dispatch(
    north_principal,clock,caplog,meta,
):
    async def check():
        svc=services(north_principal,clock)
        async with running(svc) as (base,_):
            payload={'jsonrpc':'2.0','method':'SECRET_METHOD_SENTINEL','params':{'_meta':meta}}
            response=await post(base,payload)
            assert response.status_code==422
            assert response.json()['code']=='INVALID_INPUT'
            assert 'SECRET_' not in response.text and 'SECRET_' not in caplog.text
            svc.catalog.search.assert_not_awaited();svc.execution.submit_and_wait.assert_not_awaited()
    asyncio.run(check())


@pytest.mark.parametrize('authorization',[None,'Bearer SECRET_INVALID_AUTH_SENTINEL'])
def test_notification_still_requires_fresh_authentication(north_principal,clock,caplog,authorization):
    async def check():
        svc=services(north_principal,clock)
        payload={'jsonrpc':'2.0','method':'SECRET_METHOD_SENTINEL','params':{'secret':'SECRET_ARG_SENTINEL'}}
        async with running(svc) as (base,_),httpx.AsyncClient(trust_env=False) as client:
            headers={'MCP-Protocol-Version':PROTOCOL}
            if authorization is not None:headers['Authorization']=authorization
            response=await client.post(base+'/mcp',json=payload,headers=headers)
            assert response.status_code==401 and response.headers['www-authenticate']=='Bearer'
            assert 'SECRET_' not in response.text and 'SECRET_' not in caplog.text
            svc.authenticator.authenticate.assert_awaited_once_with(authorization)
            svc.catalog.search.assert_not_awaited();svc.execution.submit_and_wait.assert_not_awaited()
    asyncio.run(check())


def test_modern_notification_drops_cancellation_without_touching_another_request(north_principal,clock,caplog):
    async def check():
        svc=services(north_principal,clock);started=asyncio.Event();release=asyncio.Event();cancelled=[]
        async def search(*args,**kwargs):
            started.set()
            try:await release.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return {'items':[],'next_cursor':None}
        svc.catalog.search.side_effect=search
        async with running(svc) as (base,_):
            active=asyncio.create_task(post(base,body(request_id=73)))
            try:
                await asyncio.wait_for(started.wait(),5)
                notification={'jsonrpc':'2.0','method':'notifications/cancelled',
                    'params':{'requestId':73,'reason':'SECRET_CANCEL_SENTINEL','_meta':{}}}
                response=await post(base,notification)
                assert response.status_code==202 and response.content==b''
                await asyncio.sleep(.02)
                assert not active.done() and not cancelled
                release.set()
                result=await asyncio.wait_for(active,5)
                assert result.status_code==200 and not result.json()['result']['isError']
                assert 'SECRET_CANCEL_SENTINEL' not in caplog.text
                assert svc.authenticator.authenticate.await_count==2
            finally:
                release.set()
                if not active.done():await active
    asyncio.run(check())
