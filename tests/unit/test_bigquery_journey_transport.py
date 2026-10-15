"""The synthetic journey routing seam is exact, revocable and test-only."""
import asyncio
import httpx
import pytest

pytestmark=pytest.mark.unit


def receipt():
    return {'schema_version':1,'listeners':[
        {'context':'consumer','origin':'https://consumer.example.com','port':12341},
        {'context':'fixture_test','origin':'https://fixture.example.com','port':12342}]}


def origins():return {'consumer':'https://consumer.example.com','fixture_test':'https://fixture.example.com'}


def test_exact_route_preserves_raw_signed_body_and_headers():
    from tests.integration.source_pilot_bigquery_child import JourneyRoutes,JourneyTransport
    routes=JourneyRoutes(receipt(),origins());calls=[]
    async def handle(request):
        calls.append(request)
        return httpx.Response(200,content=b'{"data":[]}',headers={'content-type':'application/json'})
    async def run():
        transport=JourneyTransport(routes,httpx.MockTransport(handle))
        original=httpx.Request('POST','https://consumer.example.com/cubejs-api/v1/load',
            headers={'authorization':'Bearer synthetic','x-request-id':'fixed','host':'consumer.example.com'},
            content=b'{ "query" : {"signed":"exact"} }')
        response=await transport.handle_async_request(original)
        assert response.status_code==200
        assert str(calls[0].url)=='http://127.0.0.1:12341/cubejs-api/v1/load'
        assert calls[0].headers.raw==original.headers.raw and calls[0].content==original.content
        await transport.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('method,url',[
    ('GET','https://consumer.example.com/cubejs-api/v1/load'),
    ('POST','https://other.example.com/cubejs-api/v1/load'),
    ('POST','http://consumer.example.com/cubejs-api/v1/load'),
    ('POST','https://consumer.example.com/cubejs-api/v1/meta'),
    ('POST','https://consumer.example.com/cubejs-api/v1/load?next=elsewhere'),
    ('POST','https://consumer.example.com/cubejs-api/v1/%6coad'),
    ('POST','https://consumer.example.com:444/cubejs-api/v1/load'),
    ('POST','https://user:pass@consumer.example.com/cubejs-api/v1/load'),
])
def test_other_network_routes_are_rejected_before_io(method,url):
    from tests.integration.source_pilot_bigquery_child import JourneyRoutes,JourneyTransport
    calls=[]
    async def handle(request):calls.append(request);return httpx.Response(200)
    async def run():
        transport=JourneyTransport(JourneyRoutes(receipt(),origins()),httpx.MockTransport(handle))
        with pytest.raises(ValueError):await transport.handle_async_request(httpx.Request(method,url,content=b'{}'))
        assert calls==[]
        await transport.aclose()
    asyncio.run(run())


def test_child_exit_revokes_existing_routes_and_redirect_is_never_followed():
    from tests.integration.source_pilot_bigquery_child import JourneyRoutes,JourneyTransport
    routes=JourneyRoutes(receipt(),origins());calls=[]
    async def handle(request):calls.append(request);return httpx.Response(302,headers={'location':'https://elsewhere.example.com'})
    async def run():
        transport=JourneyTransport(routes,httpx.MockTransport(handle))
        request=httpx.Request('POST','https://consumer.example.com/cubejs-api/v1/load',content=b'{}')
        with pytest.raises(ValueError):await transport.handle_async_request(request)
        routes.revoke()
        with pytest.raises(ValueError):await transport.handle_async_request(request)
        assert len(calls)==1
        await transport.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('mutation',[
    lambda r:r.update(extra=True),
    lambda r:r['listeners'].reverse(),
    lambda r:r['listeners'][0].update(port=True),
    lambda r:r['listeners'][0].update(origin='https://other.example.com'),
    lambda r:r['listeners'][1].update(port=12341),
])
def test_ready_receipt_has_closed_exact_origin_and_port_schema(mutation):
    from tests.integration.source_pilot_bigquery_child import JourneyRoutes
    value=receipt();mutation(value)
    with pytest.raises(ValueError):JourneyRoutes(value,origins())


def _script(tmp_path,body):
    import json
    path=tmp_path/'child.py'
    path.write_text('import json,os,signal,sys,time\n'
        +'READY='+repr('GATEWAY_BQ_JOURNEY_READY '+json.dumps(receipt()))+'\n'
        +'RESULT='+repr('GATEWAY_BQ_JOURNEY_RESULT '+json.dumps({'schema_version':1,'status':'passed','counts':{'inserts':0,'metadata':0,'results':0,'provider_errors':0}}))+'\n'+body)
    return path


@pytest.mark.parametrize('body',[
    "print('GATEWAY_BQ_JOURNEY_READY'+json.dumps({'schema_version':1}),flush=True)",
    "print('GATEWAY_BQ_JOURNEY_READY {\"schema_version\":1,\"schema_version\":1,\"listeners\":[]}',flush=True)",
    "print('SYNTHETIC_SECRET_CANARY',file=sys.stderr,flush=True);print('GATEWAY_BQ_JOURNEY_READY {broken',flush=True)",
    "print('x'*70000,flush=True)",
    "[print('x'*16000,file=sys.stderr,flush=True) for _ in range(20)]",
    "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)",
])
def test_real_child_invalid_output_and_stall_are_bounded_and_redacted(tmp_path,body):
    import sys,time
    from tests.integration.source_pilot_bigquery_child import _managed_child
    script=_script(tmp_path,body);start=time.monotonic()
    async def run():
        with pytest.raises(AssertionError) as caught:
            async with _managed_child([sys.executable,str(script)],tmp_path,{},origins(),startup_seconds=.4,stop_seconds=.2):
                raise AssertionError('Invalid child should never yield authority')
        assert 'SYNTHETIC_SECRET_CANARY' not in str(caught.value)
        assert 'broken' not in str(caught.value)
    asyncio.run(run());assert time.monotonic()-start<4


def test_real_child_completion_boolean_version_is_rejected(tmp_path):
    import sys
    from tests.integration.source_pilot_bigquery_child import _managed_child
    script=_script(tmp_path,"def close(*args):\n print(RESULT.replace('\"schema_version\": 1','\"schema_version\": true'),flush=True);sys.exit(0)\nsignal.signal(signal.SIGTERM,close)\nprint(READY,flush=True)\nwhile True:time.sleep(.1)\n")
    async def run():
        with pytest.raises(AssertionError,match='successful cleanup'):
            async with _managed_child([sys.executable,str(script)],tmp_path,{},origins(),startup_seconds=1,stop_seconds=.5):pass
    asyncio.run(run())


def test_real_child_group_cleanup_stops_descendant_and_revokes_route(tmp_path):
    import sys,os
    from tests.integration.source_pilot_bigquery_child import _managed_child
    pid_file=tmp_path/'descendant.pid'
    script=_script(tmp_path,"child=os.fork()\nif child==0:\n signal.signal(signal.SIGTERM,lambda *a:sys.exit(0))\n while True:time.sleep(.1)\nopen("+repr(str(pid_file))+",'w').write(str(child))\ndef close(*args):\n os.waitpid(child,0);print(RESULT,flush=True);sys.exit(0)\nsignal.signal(signal.SIGTERM,close)\nprint(READY,flush=True)\nwhile True:time.sleep(.1)\n")
    async def run():
        async with _managed_child([sys.executable,str(script)],tmp_path,{},origins(),startup_seconds=1,stop_seconds=1) as child:
            route=child['routes'];assert route.target(httpx.Request('POST','https://consumer.example.com/cubejs-api/v1/load')).port==12341
        assert child['result']['status']=='passed'
        with pytest.raises(ValueError):route.target(httpx.Request('POST','https://consumer.example.com/cubejs-api/v1/load'))
    asyncio.run(run())
    with pytest.raises(ProcessLookupError):os.kill(int(pid_file.read_text()),0)


def test_real_exited_parent_with_pipe_holding_descendant_has_bounded_cleanup(tmp_path):
    import sys,time,os
    from pathlib import Path
    from tests.integration.source_pilot_bigquery_child import _managed_child
    pid_file=tmp_path/'orphan.pid'
    script=_script(tmp_path,"child=os.fork()\nif child==0:\n signal.signal(signal.SIGTERM,lambda *a:sys.exit(0))\n while True:time.sleep(.1)\nopen("+repr(str(pid_file))+",'w').write(str(child))\nprint(READY,flush=True)\nsys.exit(0)\n")
    start=time.monotonic()
    async def run():
        with pytest.raises(AssertionError,match='bounded execution or cleanup'):
            async with _managed_child([sys.executable,str(script)],tmp_path,{},origins(),startup_seconds=1,stop_seconds=.3):
                await asyncio.sleep(.1)
    asyncio.run(run());assert time.monotonic()-start<4
    status=Path('/proc')/pid_file.read_text()/'stat'
    # An orphan may briefly be a zombie waiting for PID1 to reap, but it cannot
    # run or retain the inherited pipe. No living child survives our group stop.
    assert not status.exists() or status.read_text().split()[2]=='Z'


def test_real_child_output_failure_after_ready_revokes_routes_immediately(tmp_path):
    import sys
    from tests.integration.source_pilot_bigquery_child import _managed_child
    script=_script(tmp_path,"print(READY,flush=True)\ntime.sleep(.1)\nprint('GATEWAY_BQ_JOURNEY_RESULT {broken',flush=True)\ntime.sleep(60)\n")
    async def run():
        with pytest.raises(AssertionError,match='bounded execution or cleanup'):
            async with _managed_child([sys.executable,str(script)],tmp_path,{},origins(),startup_seconds=1,stop_seconds=.3) as child:
                await asyncio.sleep(.3)
                with pytest.raises(ValueError):child['routes'].target(httpx.Request('POST','https://consumer.example.com/cubejs-api/v1/load'))
    asyncio.run(run())
