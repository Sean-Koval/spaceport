"""Independent official Python SDK harness. Secrets are read only from a local file."""
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2
from mcp import Client
from mcp_types import DiscoverResult
from mcp.client.streamable_http import streamable_http_client

PROTOCOL = '2026-07-28'
MAX_WIRE_BYTES = 8 * 1024 * 1024


class BoundedStream(httpx2.AsyncByteStream):
    def __init__(self,stream): self.stream=stream
    async def __aiter__(self):
        remaining=MAX_WIRE_BYTES
        async for chunk in self.stream:
            if len(chunk)>remaining:raise ValueError('Response exceeds transport limit')
            remaining-=len(chunk)
            yield chunk
    async def aclose(self): await self.stream.aclose()


class BoundedTransport(httpx2.AsyncBaseTransport):
    def __init__(self): self.transport=httpx2.AsyncHTTPTransport(retries=0)
    async def handle_async_request(self,request):
        response=await self.transport.handle_async_request(request)
        if response.headers.get('content-encoding','identity').lower()!='identity':
            await response.aclose()
            raise ValueError('Compressed response is unsupported')
        response.stream=BoundedStream(response.stream)
        return response
    async def aclose(self): await self.transport.aclose()


def setting(name,default=None):
    """Read SPACEPORT_<name>; the legacy DPG_<name> is a deprecated fallback."""
    for prefix in ('SPACEPORT_','DPG_'):
        value=os.environ.get(prefix+name)
        if value:return value
    if default is None:raise KeyError('SPACEPORT_'+name)
    return default


def normalize(value):
    value=json.loads(json.dumps(value,allow_nan=False))
    for name in ('execution_id','accepted_at','started_at','completed_at','next_cursor'): value.pop(name,None)
    if 'freshness' in value:
        value['freshness'].pop('assessed_at',None)
        value['freshness'].pop('age_seconds',None)
    return value


async def run():
    url=setting('MCP_URL')
    target=urlsplit(url)
    if target.scheme not in {'http','https'} or not target.hostname or target.username or target.password or target.path!='/mcp' or target.query or target.fragment:
        raise ValueError('Explicit /mcp URL required')
    with Path(setting('TOKEN_FILE')).open('rb') as file:token=file.read(16385)
    if not 1<=len(token)<=16384:raise ValueError('Invalid credential file')
    token=token.decode('ascii').strip()
    if not token or any(char.isspace() for char in token):raise ValueError('Invalid credential file')
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+token,'Accept-Encoding':'identity'},
        transport=BoundedTransport(),trust_env=False,timeout=10,follow_redirects=False) as http:
        transport=streamable_http_client(url,http_client=http,max_sse_event_size=MAX_WIRE_BYTES)
        async with Client(transport,mode=PROTOCOL,cache=None,read_timeout_seconds=10) as client:
            if client.protocol_version!=PROTOCOL:raise ValueError('Protocol pin mismatch')
            discovery=DiscoverResult.model_validate(await client.session.send_discover(PROTOCOL))
            if discovery.supported_versions != [PROTOCOL]:raise ValueError('Server protocol discovery mismatch')
            if discovery.capabilities.tools is None or discovery.capabilities.resources is not None or discovery.capabilities.prompts is not None:
                raise ValueError('Unexpected server capabilities')
            listing=await client.list_tools()
            if {tool.name for tool in listing.tools}!={'search_products','describe_product','query_product','get_result'}:
                raise ValueError('Unexpected tool contract')
            async def call(name,arguments):
                result=await client.call_tool(name,arguments)
                if result.is_error or not isinstance(result.structured_content,dict):raise ValueError('Gateway tool failed')
                return result.structured_content
            cursor=None;seen=set();selected=None
            for _ in range(20):
                page=await call('search_products',{'query':'revenue','limit':50,'cursor':cursor})
                if not isinstance(page.get('items'),list) or len(page['items'])>50:raise ValueError('Invalid catalog page')
                selected=next((item for item in page['items'] if item['product_id']=='revenue'),None)
                if selected:break
                cursor=page.get('next_cursor')
                if not cursor:break
                if cursor in seen:raise ValueError('Repeated catalog cursor')
                seen.add(cursor)
            if selected is None:raise ValueError('Authorized product not found')
            description=await call('describe_product',{'product_id':'revenue','version':selected['version']})
            request={'product_id':'revenue','version':description['version'],
                'inputs':{'start':setting('QUERY_START','2024-03-09'),
                          'end':setting('QUERY_END','2024-03-12')},
                'sort':[{'field':'day','direction':'asc'},{'field':'segment','direction':'asc'}],
                'idempotency_key':str(uuid4())}
            execution=await call('query_product',{'request':request})
            async def get_result(cursor=None):
                arguments={'execution_id':execution['execution_id']}
                if cursor is not None:arguments['cursor']=cursor
                result=await call('get_result',arguments)
                if result['execution_id']!=execution['execution_id']:
                    raise ValueError('Result execution identity mismatch')
                if result['state']=='succeeded' and (result['product_id']!=description['product_id']
                    or result['product_digest']!=description['digest'] or result['version']!=description['version']):
                    raise ValueError('Result product identity mismatch')
                return result
            deadline=time.monotonic()+125
            while True:
                result=await get_result()
                if result['state']=='succeeded':break
                if result['state'] not in {'accepted','running'}:raise ValueError('Execution terminated without results')
                if time.monotonic()>=deadline:raise ValueError('Execution deadline exceeded')
                await asyncio.sleep(.25)
            rows=[];seen=set();first=result
            while True:
                page_rows=result.get('rows')
                if not isinstance(page_rows,list) or len(page_rows)>200 or len(rows)+len(page_rows)>1000 or result.get('truncated') is not False:
                    raise ValueError('Invalid or partial result page')
                if any(not isinstance(row.get('revenue'),str) for row in page_rows):raise ValueError('Lossy decimal')
                rows.extend(page_rows)
                cursor=result.get('next_cursor')
                if not cursor:break
                if cursor in seen or len(seen)>=1000:raise ValueError('Repeated result cursor')
                seen.add(cursor)
                result=await get_result(cursor)
                left=normalize(first);right=normalize(result)
                left.pop('rows',None);right.pop('rows',None)
                if left!=right:raise ValueError('Result page metadata changed')
            if len(rows)!=first['row_count']:raise ValueError('Result count mismatch')
            answer=normalize(first);answer['rows']=rows
            return {'compatibility':{'sdk':'mcp==2.3.0','protocol':client.protocol_version,
                'transport':'streamable-http-json','max_wire_bytes':MAX_WIRE_BYTES},'answer':answer}


def main():
    logging.disable(logging.CRITICAL)
    try:
        result=asyncio.run(asyncio.wait_for(run(),140))
        encoded=json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        if len(encoded.encode())>MAX_WIRE_BYTES:raise ValueError('Output exceeds limit')
        print(encoded)
        return 0
    except Exception:
        print('{"error":"CLIENT_ACCEPTANCE_FAILED"}',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
