"""Mandatory actual-stack MCP acceptance. Missing setup is a failure, never a skip.

Run after publishing the synthetic revenue product on the isolated Task14 stack.
The independent SDK subprocesses each search, describe, submit and page results.
"""
import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import httpx
import pytest

pytestmark=pytest.mark.e2e
ROOT=Path(__file__).resolve().parents[2]
PROTOCOL='2026-07-28'
MAX_OUTPUT=8*1024*1024


@pytest.fixture(scope='module')
def stack():
    if os.environ.get('GATEWAY_DEMO_TEST_AUTHORIZATION')!='private-synthetic-gateway-demo':
        pytest.fail('Actual MCP acceptance requires explicit private-synthetic-gateway-demo authorization',pytrace=False)
    url=os.environ.get('DPG_MCP_URL','')
    parsed=urlsplit(url)
    if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','localhost'} or not parsed.port or parsed.path!='/mcp' or parsed.query or parsed.fragment or parsed.username:
        pytest.fail('Actual MCP acceptance requires the explicitly selected loopback demo /mcp URL',pytrace=False)
    files={profile:Path(os.environ.get('DPG_DEMO_'+profile.upper()+'_TOKEN_FILE','/nonexistent')) for profile in ('north','south')}
    if not all(path.is_file() for path in files.values()):
        pytest.fail('Actual MCP acceptance requires the two provisioned local credential files',pytrace=False)
    return url,files


async def run_client(language,url,token_file,*,start='2024-03-09',end='2024-03-12'):
    command=[sys.executable,str(ROOT/'clients/python_agent.py')] if language=='python' else ['node',str(ROOT/'clients/typescript_agent.ts')]
    process=await asyncio.create_subprocess_exec(*command,cwd=ROOT,
        env={**os.environ,'DPG_MCP_URL':url,'DPG_TOKEN_FILE':str(token_file),'DPG_QUERY_START':start,'DPG_QUERY_END':end},
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    async def read(stream,limit):
        output=bytearray()
        while True:
            part=await stream.read(65536)
            if not part:return bytes(output)
            if len(part)>limit-len(output):raise ValueError('Client output exceeded bound')
            output.extend(part)
    try:
        stdout,stderr,_=await asyncio.wait_for(asyncio.gather(read(process.stdout,MAX_OUTPUT),read(process.stderr,65536),process.wait()),150)
        if process.returncode:pytest.fail(language+' actual MCP harness failed; raw output withheld',pytrace=False)
        result=json.loads(stdout)
        assert result['compatibility']['protocol']==PROTOCOL
        assert result['compatibility']['transport']=='streamable-http-json'
        assert result['compatibility']['max_wire_bytes']==MAX_OUTPUT
        return result['answer']
    finally:
        if process.returncode is None:process.kill();await process.wait()


def test_actual_two_clients_two_distinct_profiles(stack):
    async def scenario():
        url,files=stack;answers={}
        for profile in ('north','south'):
            pair=[await run_client(language,url,files[profile]) for language in ('python','typescript')]
            assert pair[0]==pair[1]
            answers[profile]=pair[0]
        assert answers['north']['product_digest']==answers['south']['product_digest']
        assert answers['north']['rows']!=answers['south']['rows']
        assert sum((Decimal(row['revenue']) for row in answers['north']['rows']),Decimal(0))==Decimal('9007199254741023.31')
        assert sum((Decimal(row['revenue']) for row in answers['south']['rows']),Decimal(0))==Decimal('100.01')
        assert any(row['segment'] is None for row in answers['north']['rows'])
        assert any(row['revenue']=='9007199254740993.01' for row in answers['north']['rows'])
        for value in answers.values():assert value['truncated'] is False and value['source_watermark'] is not None
    asyncio.run(scenario())


def test_actual_empty_range_both_clients_and_profiles(stack):
    async def scenario():
        url,files=stack
        for profile in ('north','south'):
            pair=[await run_client(language,url,files[profile],start='2024-02-01',end='2024-02-02')
                  for language in ('python','typescript')]
            assert pair[0]==pair[1] and pair[0]['rows']==[] and pair[0]['row_count']==0
    asyncio.run(scenario())


def test_actual_same_connection_owner_isolation_and_no_session_authority(stack):
    url,files=stack
    headers={'MCP-Protocol-Version':PROTOCOL,'MCP-Method':'tools/call','Accept':'application/json, text/event-stream'}
    def message(name,args,number):
        return {'jsonrpc':'2.0','id':number,'method':'tools/call','params':{'name':name,'arguments':args,
            '_meta':{'io.modelcontextprotocol/protocolVersion':PROTOCOL,'io.modelcontextprotocol/clientCapabilities':{}}}}
    north=files['north'].read_text().strip();south=files['south'].read_text().strip()
    with httpx.Client(trust_env=False,timeout=10,limits=httpx.Limits(max_connections=1)) as client:
        response=client.post(url,headers={**headers,'Authorization':'Bearer '+north,'MCP-Name':'query_product'},
            json=message('query_product',{'request':{'product_id':'revenue','inputs':{'start':'2024-03-09','end':'2024-03-12'}}},1))
        assert response.status_code==200
        result=response.json()['result'];assert not result['isError']
        execution=result['structuredContent']['execution_id']
        for token in (south,None):
            current={**headers,'MCP-Name':'get_result','MCP-Session-ID':'not-an-identity'}
            if token:current['Authorization']='Bearer '+token
            response=client.post(url,headers=current,json=message('get_result',{'execution_id':execution},2))
            if token:assert response.json()['result']['structuredContent']['code']=='NOT_FOUND_OR_FORBIDDEN'
            else:assert response.status_code==401
