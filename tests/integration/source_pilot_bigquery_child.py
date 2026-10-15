"""Test-only exact HTTPS-origin routing to a guarded synthetic Cube child.

This qualifies real HTTP/compiler/driver/registry semantics. TLS and live Google
identity/provider qualification are explicitly separate. Production has no URL
rewrite or synthetic provider switch.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import signal
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx


class JourneyRoutes:
    def __init__(self, receipt, origins, *, child_alive=lambda:True):
        if (type(receipt) is not dict or set(receipt)!={'schema_version','listeners'}
                or type(receipt['schema_version']) is not int or receipt['schema_version']!=1
                or type(receipt['listeners']) is not list or len(receipt['listeners'])!=2
                or set(origins)!={'consumer','fixture_test'}):
            raise ValueError('Exact synthetic child receipt required')
        routes={}
        for row,context in zip(receipt['listeners'],('consumer','fixture_test')):
            if (type(row) is not dict or set(row)!={'context','origin','port'}
                    or row['context']!=context or row['origin']!=origins[context]
                    or type(row['port']) is not int or not 1<=row['port']<=65535):
                raise ValueError('Exact synthetic child listener required')
            origin=row['origin'];parts=urlsplit(origin)
            if (parts.scheme!='https' or parts.username or parts.password or parts.port or parts.path
                    or parts.query or parts.fragment or origin!='https://'+parts.netloc.lower()):
                raise ValueError('Exact declared synthetic HTTPS origin required')
            routes[origin]=row['port']
        if len(routes)!=2 or len(set(routes.values()))!=2:
            raise ValueError('Distinct synthetic listeners required')
        self._routes=MappingProxyType(routes);self._active=True;self._child_alive=child_alive

    def revoke(self):self._active=False

    def target(self,request):
        url=request.url
        if (not self._active or not self._child_alive() or request.method!='POST'
                or url.scheme!='https' or url.username or url.password or url.query or url.fragment
                or url.raw_path!=b'/cubejs-api/v1/load'):
            raise ValueError('Synthetic route is unavailable')
        origin=str(url.copy_with(path='',query=None,fragment=None))
        if origin not in self._routes or request.headers.get('host')!=url.netloc.decode('ascii'):
            raise ValueError('Synthetic route is unavailable')
        return httpx.URL(f'http://127.0.0.1:{self._routes[origin]}/cubejs-api/v1/load')


class JourneyTransport(httpx.AsyncBaseTransport):
    def __init__(self,routes,transport=None):
        self.routes=routes
        self._transport=transport or httpx.AsyncHTTPTransport(trust_env=False,retries=0)

    async def handle_async_request(self,request):
        target=self.routes.target(request)
        # The private adapter already serialized and signed this exact body.
        # Retain every raw header (including original Host) and body byte.
        body=await request.aread()
        routed=httpx.Request(request.method,target,headers=request.headers.raw,content=body,extensions=request.extensions)
        if routed.headers.raw!=request.headers.raw or routed.content!=body:
            raise ValueError('Synthetic routing changed signed request bytes')
        response=await self._transport.handle_async_request(routed)
        if 300<=response.status_code<400 or 'location' in response.headers:
            await response.aclose()
            raise ValueError('Synthetic routing rejects redirects')
        return response

    async def aclose(self):await self._transport.aclose()


@asynccontextmanager
async def bigquery_child(config_file,origins):
    """Only the test child receives provider DB roles and synthetic signing keys."""
    root=Path(__file__).resolve().parents[2]
    script=root/'tests/runtime/bigquery-journey-child.cjs'
    node=shutil.which('node')
    if node is None or not script.is_file():raise ValueError('The pinned Cube journey child is required')
    environment={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'NODE_ENV':'production','CUBEJS_DEV_MODE':'false','PORT':'0',
        'GATEWAY_SOURCE_TEST_AUTHORIZATION':'private-synthetic-source-only',
        'GATEWAY_BQ_JOURNEY_CONFIG_FILE':str(Path(config_file).resolve()),
        'GATEWAY_BIGQUERY_RUNTIME_DIR':str(root/'cube/bigquery-runtime')}
    # Harness input only; does not select network/credential behavior. The hosted
    # image fixes NODE_PATH=/cube/node_modules to its immutable installed packages;
    # local package reproduction supplies its separately qualified package path.
    if os.environ.get('NODE_PATH'):environment['NODE_PATH']=os.environ['NODE_PATH']
    async with _managed_child([node,'--max-old-space-size=512',str(script)],root,environment,origins) as state:
        yield state


def _receipt(line,prefix):
    marker=prefix.encode()
    if not line.startswith(marker):return None
    if not line.startswith(marker+b' '):raise ValueError('Invalid synthetic child marker')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('Duplicate synthetic receipt key')
            result[key]=value
        return result
    return json.loads(line[len(marker)+1:],object_pairs_hook=pairs)


def _result(value):
    if (type(value) is not dict or set(value)!={'schema_version','status','counts'}
            or type(value['schema_version']) is not int or value['schema_version']!=1
            or value['status']!='passed' or type(value['counts']) is not dict
            or set(value['counts'])!={'inserts','metadata','results','provider_errors'}
            or any(type(v) is not int or v<0 for v in value['counts'].values())
            or value['counts']['provider_errors']!=0):
        raise ValueError('Invalid synthetic child completion')
    return value


@asynccontextmanager
async def _managed_child(argv,root,environment,origins,*,startup_seconds=25,stop_seconds=5):
    # The sole public wrapper above fixes argv/environment. Injection is internal
    # to portable lifecycle tests, never selected by CLI/environment/config.
    process=await asyncio.create_subprocess_exec(*argv,cwd=root,env=environment,start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,limit=65536)
    ready=asyncio.get_running_loop().create_future();result=asyncio.get_running_loop().create_future()
    routes=None;read_errors=[]
    def send(signum):
        try:os.killpg(process.pid,signum)
        except ProcessLookupError:pass
    async def read(stream,parse=False):
        total=0
        try:
            while line:=await stream.readline():
                total+=len(line)
                if total>262144:raise ValueError('Synthetic child output exceeded bound')
                if parse:
                    for prefix,future in (('GATEWAY_BQ_JOURNEY_READY',ready),('GATEWAY_BQ_JOURNEY_RESULT',result)):
                        if line.startswith(prefix.encode()):
                            value=_receipt(line,prefix)
                            if future.done():raise ValueError('Duplicate synthetic child receipt')
                            future.set_result(value)
            if parse and not ready.done():raise ValueError('Missing synthetic readiness')
        except (ValueError,UnicodeError,json.JSONDecodeError):
            read_errors.append(True)
            if routes:routes.revoke()
            send(signal.SIGTERM)
            if not ready.done():ready.set_exception(ValueError('Invalid synthetic child output'))
    readers=[asyncio.create_task(read(process.stdout,True)),asyncio.create_task(read(process.stderr))]
    failure=None
    try:
        try:receipt=await asyncio.wait_for(asyncio.shield(ready),startup_seconds)
        except (ValueError,TimeoutError):raise AssertionError('Synthetic Cube child did not provide valid readiness') from None
        routes=JourneyRoutes(receipt,origins,child_alive=lambda:process.returncode is None and not read_errors)
        state={'routes':routes,'result':None}
        yield state
    except BaseException as caught:
        failure=caught
        raise
    finally:
        if routes:routes.revoke()
        # Signal the whole owned group, including descendants that retained pipes
        # after their parent exited. Never wait unboundedly for process or readers.
        send(signal.SIGTERM)
        try:await asyncio.wait_for(process.wait(),stop_seconds)
        except TimeoutError:
            send(signal.SIGKILL)
            try:await asyncio.wait_for(process.wait(),2)
            except TimeoutError:read_errors.append(True)
        send(signal.SIGKILL)
        try:await asyncio.wait_for(asyncio.gather(*readers),2)
        except Exception:
            read_errors.append(True)
            for task in readers:task.cancel()
            await asyncio.gather(*readers,return_exceptions=True)
        if not ready.done():ready.cancel()
        elif not ready.cancelled():ready.exception()  # consume a late startup error
        if failure is None:
            if process.returncode!=0 or read_errors or not result.done():
                raise AssertionError('Synthetic Cube child failed bounded execution or cleanup')
            try:state['result']=_result(result.result())
            except ValueError:raise AssertionError('Synthetic Cube child did not prove successful cleanup') from None
