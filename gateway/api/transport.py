"""Bounded ASGI boundary shared by public transports, with no body/token logging."""
from dataclasses import dataclass
import json
import math
import re
from urllib.parse import urlsplit,parse_qsl
from uuid import uuid4
from starlette.responses import JSONResponse
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.source_delegation import has_unsupported_delegation_headers

MAX_BODY = 65_536
SAFE_MESSAGES = {
    ErrorCode.UNAUTHENTICATED: 'A valid identity is required.',
    ErrorCode.NOT_FOUND_OR_FORBIDDEN: 'The requested product is unavailable.',
    ErrorCode.INVALID_INPUT: 'The request is invalid.',
    ErrorCode.CONFLICT: 'The request conflicts with current state.',
    ErrorCode.RESOURCE_LIMIT: 'The request exceeds a gateway limit.',
    ErrorCode.UPSTREAM_UNAVAILABLE: 'The gateway dependency is unavailable.',
    ErrorCode.BINDING_DRIFT: 'The reviewed product binding is unavailable.',
    ErrorCode.EXECUTION_TIMEOUT: 'The execution deadline was exceeded.',
    ErrorCode.FRESHNESS_UNKNOWN: 'Required source freshness is unavailable.',
    ErrorCode.STALE_DATA: 'The recorded source data is stale.',
}
STATUS = {ErrorCode.UNAUTHENTICATED:401, ErrorCode.NOT_FOUND_OR_FORBIDDEN:404,
    ErrorCode.INVALID_INPUT:422, ErrorCode.CONFLICT:409, ErrorCode.RESOURCE_LIMIT:429}


def error_response(code, request_id, status=None):
    code=ErrorCode(code)
    return JSONResponse({'code':code.value,'request_id':request_id,'safe_message':SAFE_MESSAGES[code]},
        status_code=status or STATUS.get(code,503), headers={'Cache-Control':'no-store','X-Request-ID':request_id})


@dataclass(frozen=True)
class TransportSettings:
    allowed_hosts: tuple[str,...] = ('localhost','127.0.0.1','::1')
    allowed_origins: tuple[str,...] = ()
    authenticated_prefixes: tuple[str,...] = ('/v1',)

    def __post_init__(self):
        if not self.allowed_hosts or any(not host or '*' in host or '/' in host for host in self.allowed_hosts):
            raise ValueError('Explicit allowed hosts are required')
        if any(not prefix.startswith('/') or prefix.endswith('/') or '?' in prefix or '#' in prefix for prefix in self.authenticated_prefixes):
            raise ValueError('Explicit authentication prefixes are required')
        for origin in self.allowed_origins:
            parsed=urlsplit(origin)
            if parsed.scheme not in {'http','https'} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ValueError('Explicit origins are required')


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('Duplicate key')
        result[key]=value
    return result


def parse_json(body):
    def constant(_):raise ValueError('Nonfinite number')
    value=json.loads(body.decode('utf-8'),object_pairs_hook=unique_object,parse_constant=constant)
    def check(item,depth=0):
        if depth>32:raise ValueError('Excessive nesting')
        if type(item) is float and not math.isfinite(item):raise ValueError('Nonfinite number')
        if isinstance(item,dict):
            for child in item.values():check(child,depth+1)
        elif isinstance(item,list):
            for child in item:check(child,depth+1)
    check(value)
    return value


class TransportBoundary:
    """Cap the actual receive stream before JSON decoding; preserve streaming output.

    This performs fresh authentication for /v1 routes. Mounted MCP can use its
    protocol-aware authentication adapter and reuse this parsing/error boundary.
    """
    def __init__(self, app, *, services, settings):
        self.app,self.services,self.settings=app,services,settings

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        request_id=str(uuid4())
        scope.setdefault('state',{})['request_id']=request_id
        started=False
        request_validated=False
        cors_origin=None
        async def safe_send(message):
            nonlocal started
            if message['type']=='http.response.start':
                started=True
                headers=[(k,v) for k,v in message.get('headers',[]) if k.lower() not in {b'cache-control',b'x-request-id'}]
                if cors_origin is not None:
                    # This is the outer boundary: its early rejections never reach
                    # inner CORSMiddleware. Apply only the already verified origin.
                    vary=[part.strip() for key,value in headers if key.lower()==b'vary' for part in value.split(b',')]
                    if not any(part.lower()==b'origin' for part in vary):vary.append(b'Origin')
                    headers=[(key,value) for key,value in headers if key.lower() not in {
                        b'access-control-allow-origin',b'access-control-expose-headers',b'vary'}]
                    headers.extend([(b'access-control-allow-origin',cors_origin),
                        (b'access-control-expose-headers',b'X-Request-ID'),(b'vary',b', '.join(vary))])
                message={**message,'headers':headers+[(b'cache-control',b'no-store'),(b'x-request-id',request_id.encode())]}
            await send(message)
        async def reject(code,status=None):
            await error_response(code,request_id,status)(scope,receive,safe_send)
        try:
            # Inspect the original sequence: folding into a dictionary would
            # discard repeated assertions. Unrelated identity headers remain
            # ignored; neither their absence nor a plain token proves no agent.
            identity_settings = getattr(self.services.authenticator, 'settings', None)
            if (getattr(identity_settings, 'source', None) == 'reviewed_sources'
                and has_unsupported_delegation_headers(scope.get('headers', []))):
                return await reject('INVALID_INPUT')
            headers={}
            for name,value in scope.get('headers',[]):headers.setdefault(name.lower(),[]).append(value)
            for name in (b'host',b'origin'):
                if len(headers.get(name,[]))>1:return await reject('INVALID_INPUT')
            host=headers.get(b'host',[b''])[0].decode('ascii')
            parsed=urlsplit('//'+host)
            if parsed.hostname not in self.settings.allowed_hosts or parsed.username or parsed.path or parsed.query or parsed.fragment:
                return await reject('INVALID_INPUT')
            _=parsed.port
            origin=headers.get(b'origin',[None])[0]
            if origin is not None:
                if origin.decode('ascii') not in self.settings.allowed_origins:return await reject('INVALID_INPUT')
                cors_origin=origin
            for name in (b'authorization',b'content-type',b'content-length',b'content-encoding'):
                if len(headers.get(name,[]))>1:
                    return await reject('UNAUTHENTICATED' if name==b'authorization' else 'INVALID_INPUT')
            if headers.get(b'content-encoding',[b'identity'])[0].lower()!=b'identity':return await reject('INVALID_INPUT')
            length=headers.get(b'content-length',[None])[0]
            if length is not None:
                if not re.fullmatch(rb'[0-9]{1,10}',length):return await reject('INVALID_INPUT')
                length=int(length)
                if length>MAX_BODY:return await reject('RESOURCE_LIMIT',413)
            raw_query=scope.get('query_string',b'')
            if len(raw_query)>8192:return await reject('INVALID_INPUT')
            parse_qsl(raw_query.decode('ascii'),encoding='utf-8',errors='strict',max_num_fields=50)
            body=bytearray()
            while True:
                message=await receive()
                if message['type']=='http.disconnect':return
                if message['type']!='http.request':return await reject('INVALID_INPUT')
                chunk=message.get('body',b'')
                if len(chunk)>MAX_BODY-len(body):return await reject('RESOURCE_LIMIT',413)
                body.extend(chunk)
                if not message.get('more_body',False):break
            if length is not None and length!=len(body):return await reject('INVALID_INPUT')
            if body and scope['method'] in {'GET','HEAD','DELETE','OPTIONS'}:return await reject('INVALID_INPUT')
            if body:
                media=headers.get(b'content-type',[b''])[0].decode('ascii').lower().split(';',1)[0].strip()
                if media!='application/json':return await reject('INVALID_INPUT')
                parse_json(body)
            auth=headers.get(b'authorization',[None])[0]
            authorization=None if auth is None else auth.decode('ascii')
            request_validated=True
            path=scope.get('path','')
            preflight=scope['method']=='OPTIONS' and origin is not None and b'access-control-request-method' in headers
            if not preflight and any(path==prefix or path.startswith(prefix+'/') for prefix in self.settings.authenticated_prefixes):
                scope['state']['principal']=await self.services.authenticator.authenticate(authorization)
            delivered=False
            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered=True
                    return {'type':'http.request','body':bytes(body),'more_body':False}
                return await receive()
            await self.app(scope,bounded_receive,safe_send)
        except GatewayError as error:
            if not started:await reject(error.code)
        except (ValueError,UnicodeError,RecursionError):
            if not started:await reject('UPSTREAM_UNAVAILABLE' if request_validated else 'INVALID_INPUT')
        except Exception:
            if not started:await reject('UPSTREAM_UNAVAILABLE')
