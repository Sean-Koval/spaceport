"""Four bounded MCP tools. HTTP request scope, never a session, supplies identity.

The official SDK owns protocol negotiation. The gateway owns strict argument and
output validation, authority, redaction and transport limits. No HTTP-to-REST proxy.
"""
from dataclasses import dataclass
import json
from typing import Annotated
from urllib.parse import urlsplit

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.tools.base import Tool
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import classify_inbound_request, InboundLadderRejection, ERROR_CODE_HTTP_STATUS
from mcp_types import CallToolResult, ServerCapabilities, TextContent, ToolAnnotations, ToolsCapability
from pydantic import ConfigDict, Field, TypeAdapter, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gateway.api.models import CatalogPage, ProductDescription
from gateway.api.services import Services
from gateway.api.transport import MAX_BODY, SAFE_MESSAGES, TransportBoundary, error_response, parse_json
from gateway.auth.settings import trusted_https_url, validate_mode
from gateway.contracts.canonical import Identifier, StrictModel, Version
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.execution import ExecutionView, ResultEnvelope
from gateway.contracts.identity import Principal
from gateway.contracts.query import QueryRequest

PROTOCOL_VERSION = '2026-07-28'
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_META_KEYS = frozenset({'io.modelcontextprotocol/protocolVersion',
    'io.modelcontextprotocol/clientCapabilities','io.modelcontextprotocol/clientInfo'})


@dataclass(frozen=True)
class MCPSettings:
    resource_url: str = 'http://127.0.0.1:8080/mcp'
    allowed_hosts: tuple[str, ...] = ('127.0.0.1:8080', 'localhost:8080')
    allowed_origins: tuple[str, ...] = ()

    def __post_init__(self):
        url = urlsplit(self.resource_url)
        if (url.scheme not in {'http','https'} or not url.hostname or url.path != '/mcp'
            or url.username or url.password or url.query or url.fragment or url.netloc not in self.allowed_hosts):
            raise ValueError('An explicit canonical /mcp resource URL and matching host are required')
        if not self.allowed_hosts or any(not host or '*' in host or '/' in host for host in self.allowed_hosts):
            raise ValueError('Exact MCP hosts are required')
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in {'http','https'} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ValueError('Exact MCP origins are required')


class ToolArguments(ArgModelBase, StrictModel):
    model_config = ConfigDict(extra='forbid',strict=True,frozen=True,validate_default=True,
                              revalidate_instances='always',allow_inf_nan=False)


class SearchArguments(ToolArguments):
    query: Annotated[str, Field(max_length=256)] = ''
    cursor: Annotated[str, Field(min_length=1,max_length=4096)] | None = None
    limit: Annotated[int, Field(ge=1,le=50)] = 50


class DescribeArguments(ToolArguments):
    product_id: Identifier
    version: Version | None = None


class QueryArguments(ToolArguments):
    request: QueryRequest


class ResultArguments(ToolArguments):
    execution_id: Identifier
    cursor: Annotated[str, Field(min_length=1,max_length=4096)] | None = None


ARGUMENTS = {'search_products': SearchArguments, 'describe_product': DescribeArguments,
             'query_product': QueryArguments, 'get_result': ResultArguments}
OUTPUTS = {'search_products': CatalogPage, 'describe_product': ProductDescription,
           'query_product': ExecutionView, 'get_result': ExecutionView | ResultEnvelope}


class StrictMetadata(FuncMetadata):
    """Use the SDK's public metadata seam without its JSON-string pre-parser."""
    def validate_arguments(self, arguments_to_validate):
        return self.arg_model.model_validate(arguments_to_validate).model_dump_one_level()


def _authority(context):
    request = context.request
    if not isinstance(request, Request) or not isinstance(getattr(request.state,'principal',None),Principal):
        raise GatewayError('UNAUTHENTICATED','unavailable','A valid identity is required.')
    return request.state.principal, request.state.request_id


def _failure(code, request_id):
    code = ErrorCode(code)
    value = {'code':code.value,'request_id':request_id,'safe_message':SAFE_MESSAGES[code]}
    return CallToolResult(is_error=True,structured_content=value,
                          content=[TextContent(type='text',text=json.dumps(value,separators=(',',':')))])


class AuthenticatorTokenVerifier:
    """Adapter only: the existing authenticator validates the actual audience."""
    def __init__(self, authenticator): self.authenticator = authenticator

    async def verify_token(self, token: str):
        try:
            principal = await self.authenticator.authenticate('Bearer '+token)
            return AccessToken(token=token,client_id=principal.service_client_id or principal.id,
                subject=principal.subject,scopes=[],claims={'iss':self.authenticator.settings.oidc_issuer})
        except Exception:
            return None


def create_mcp_server(services: Services, *, settings: MCPSettings | None = None) -> MCPServer:
    settings = settings or MCPSettings()
    auth = verifier = None
    identity_settings = services.authenticator.settings
    if identity_settings.mode == 'production':
        validate_mode(identity_settings)
        if not trusted_https_url(settings.resource_url):
            raise ValueError('Production MCP requires the configured canonical HTTPS resource')
        auth = AuthSettings(issuer_url=identity_settings.oidc_issuer,resource_server_url=settings.resource_url,
                            required_scopes=[],validate_token_resource=False)
        verifier = AuthenticatorTokenVerifier(services.authenticator)
    elif identity_settings.mode != 'demo' or identity_settings.oidc_issuer is not None:
        raise ValueError('Demo MCP cannot advertise an authorization server')

    async def dispatch(name, context, values):
        request_id = 'unavailable'
        try:
            principal,request_id = _authority(context.request_context)
            if name == 'search_products':
                result = await services.catalog.search(principal,**values)
            elif name == 'describe_product':
                result = await services.catalog.describe(principal,**values)
            elif name == 'query_product':
                result = await services.execution.submit_and_wait(principal,values['request'],seconds=5)
            else:
                result = await services.results.get(principal,values['execution_id'],values['cursor'])
            # All output conversion occurs inside this safe boundary. In particular,
            # the SDK must never log an output-validation traceback with source rows.
            adapter = TypeAdapter(OUTPUTS[name])
            validated = adapter.validate_python(result)
            value = adapter.dump_python(validated,mode='json')
            encoded = json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
            if len(encoded) > MAX_RESPONSE_BYTES - 65536:
                return _failure('RESOURCE_LIMIT',request_id)
            return CallToolResult(structured_content=value,
                content=[TextContent(type='text',text='Gateway response is in structuredContent.')])
        except GatewayError as error:
            return _failure(error.code,request_id)
        except Exception:
            return _failure('UPSTREAM_UNAVAILABLE',request_id)

    def make_handler(name):
        async def handler(context, **values): return await dispatch(name,context,values)
        return handler

    tools = [Tool(fn=make_handler(name),name=name,description={
        'search_products':'Search authorized published products with bounded pagination.',
        'describe_product':'Describe the authorized projection of one product version.',
        'query_product':'Submit one bounded product query; wait at most five seconds.',
        'get_result':'Read one authorized execution or its next bounded result page.'}[name],
        parameters=model.model_json_schema(),fn_metadata=StrictMetadata(arg_model=model,
            output_schema=TypeAdapter(OUTPUTS[name]).json_schema()),is_async=True,context_kwarg='context',
        annotations=ToolAnnotations(read_only_hint=name!='query_product',destructive_hint=False,
                                    idempotent_hint=name!='query_product',open_world_hint=False))
        for name,model in ARGUMENTS.items()]

    async def boundary(context, call_next):
        try:
            _, request_id = _authority(context)
        except GatewayError:
            raise MCPError(code=-32600,message='A valid HTTP identity is required.') from None
        if context.method not in {'server/discover','tools/list','tools/call','ping'}:
            raise MCPError(code=-32601,message='Method not found.')
        params = dict(context.params or {})
        meta = params.pop('_meta',{})
        if not isinstance(meta,dict) or not set(meta).issubset(_META_KEYS):
            raise MCPError(code=-32602,message='Invalid parameters.')
        if context.method == 'tools/call':
            if context.request_id is None: return None
            if set(params)-{'name','arguments'} or type(params.get('name')) is not str:
                raise MCPError(code=-32602,message='Invalid parameters.')
            name = params['name']
            if name not in ARGUMENTS:
                raise MCPError(code=-32602,message='Unknown tool.')
            try:
                ARGUMENTS[name].model_validate(params.get('arguments',{}))
            except (ValidationError,GatewayError,ValueError,TypeError):
                return _failure('INVALID_INPUT',request_id)
        elif params:
            # The fixed four-tool inventory has no continuation pages.
            raise MCPError(code=-32602,message='Invalid parameters.')
        try:
            result = await call_next(context)
        except Exception:
            # SDK parse errors must not reflect rejected arguments or raw names.
            raise MCPError(code=-32602,message='Invalid parameters.') from None
        if context.method == 'server/discover':
            capabilities = {'tools': {'listChanged': False}}
            result = {**result, 'capabilities': capabilities} if isinstance(result, dict) else result.model_copy(
                update={'capabilities': ServerCapabilities(tools=ToolsCapability(list_changed=False))})
        return result

    server = MCPServer('Data Product Gateway',version='1.0.0',tools=tools,middleware=[boundary],
                       subscriptions=False,auth=auth,token_verifier=verifier,log_level='ERROR')
    server.gateway_settings = settings
    server.gateway_services = services
    return server


class _Challenge:
    """Preserve the appropriate challenge for early shared-boundary HTTP 401s."""
    def __init__(self,app,*,challenge): self.app,self.challenge=app,challenge
    async def __call__(self,scope,receive,send):
        if scope.get('path') == '/mcp' and scope.get('method') == 'OPTIONS':
            # MCP's direct-SDK profile has no cross-origin preflight bypass.
            # Preserve Host/Origin and make the existing shared guard authenticate
            # this as an ordinary OPTIONS request; no second identity verifier.
            scope = {**scope, 'headers': [(key, value) for key, value in scope.get('headers', [])
                if key.lower() != b'access-control-request-method']}
        async def challenged(message):
            if scope.get('path')=='/mcp' and message['type']=='http.response.start' and message['status']==401:
                headers=[(k,v) for k,v in message.get('headers',[]) if k.lower()!=b'www-authenticate']
                message={**message,'headers':headers+[(b'www-authenticate',self.challenge.encode('ascii'))]}
            await send(message)
        return await self.app(scope,receive,challenged)


class _MCPBodyBoundary:
    """Reject ambiguous envelopes before SDK parsing; bound complete JSON output."""
    def __init__(self,app,settings): self.app,self.settings=app,settings
    async def __call__(self,scope,receive,send):
        if scope['type']!='http': return await self.app(scope,receive,send)
        if scope.get('path')!='/mcp': return await self.app(scope,receive,send)
        request_id=scope.get('state',{}).get('request_id','unavailable')
        async def reject():
            return await error_response('INVALID_INPUT',request_id)(scope,receive,send)
        headers={}
        for key,value in scope.get('headers',[]):headers.setdefault(key.lower(),[]).append(value)
        if any(len(values)>1 for key,values in headers.items() if key.startswith(b'mcp-')):return await reject()
        try:
            if headers.get(b'host',[b''])[0].decode('ascii') not in self.settings.allowed_hosts:return await reject()
            if b'origin' in headers and headers[b'origin'][0].decode('ascii') not in self.settings.allowed_origins:return await reject()
            if scope.get('query_string'):return await reject()
            if scope['method']=='POST':
                body=bytearray()
                while True:
                    message=await receive()
                    if message['type']!='http.request':return await reject()
                    chunk=message.get('body',b'')
                    if len(chunk)>MAX_BODY-len(body):return await reject()
                    body.extend(chunk)
                    if not message.get('more_body',False):break
                value=parse_json(body)
                if (not isinstance(value,dict) or set(value)-{'jsonrpc','id','method','params'}
                    or value.get('jsonrpc')!='2.0' or type(value.get('method')) is not str
                    or len(value['method'])>128 or ('params' in value and not isinstance(value['params'],dict))
                    or ('id' in value and (type(value['id']) not in {str,int} or len(str(value['id']))>128))):
                    return await reject()
                if 'id' in value:
                    verdict = classify_inbound_request(value, headers={key.decode('ascii'): values[0].decode('ascii')
                        for key, values in headers.items()}, supported_modern_versions=(PROTOCOL_VERSION,))
                    if isinstance(verdict, InboundLadderRejection):
                        error = {'code': verdict.code, 'message': verdict.message}
                        if verdict.data is not None: error['data'] = verdict.data
                        return await JSONResponse({'jsonrpc': '2.0', 'id': value['id'], 'error': error},
                            status_code=ERROR_CODE_HTTP_STATUS.get(verdict.code, 400))(scope, receive, send)
                    meta = value.get('params', {}).get('_meta', {})
                    if not isinstance(meta, dict) or not set(meta).issubset(_META_KEYS): return await reject()
                else:
                    # Modern HTTP notifications are fire-and-forget. Admit their
                    # explicit profile before SDK era routing, then drop locally:
                    # no legacy notification dispatcher, method-value logging, or
                    # cross-request cancellation authority is involved.
                    if headers.get(b'mcp-protocol-version') != [PROTOCOL_VERSION.encode('ascii')]:
                        return await reject()
                    meta = value.get('params', {}).get('_meta', {})
                    if not isinstance(meta, dict) or not set(meta).issubset(_META_KEYS):
                        return await reject()
                    if ('io.modelcontextprotocol/protocolVersion' in meta
                        and meta['io.modelcontextprotocol/protocolVersion'] != PROTOCOL_VERSION):
                        return await reject()
                    if any(key in meta and not isinstance(meta[key], dict) for key in (
                        'io.modelcontextprotocol/clientCapabilities', 'io.modelcontextprotocol/clientInfo')):
                        return await reject()
                    return await Response(status_code=202)(scope, receive, send)
                delivered=False
                async def replay():
                    nonlocal delivered
                    if not delivered:
                        delivered=True
                        return {'type':'http.request','body':bytes(body),'more_body':False}
                    return await receive()
                receiver=replay
            else:receiver=receive
        except (ValueError,UnicodeError,RecursionError):return await reject()
        # JSON-only operation: buffer bounded ASGI messages so an overflow is
        # rejected before sending a 200 header or any unmarked partial result.
        messages=[]; size=0
        async def bounded_send(message):
            nonlocal size
            if message['type']=='http.response.body':
                size+=len(message.get('body',b''))
                if size>MAX_RESPONSE_BYTES:raise OverflowError
            messages.append(message)
        try:
            await self.app(scope,receiver,bounded_send)
        except OverflowError:
            return await error_response('RESOURCE_LIMIT',request_id)(scope,receive,send)
        for message in messages:await send(message)


def mount_mcp(app,server: MCPServer):
    """Mount after REST routes. Caller enters session_manager.run() in lifespan."""
    settings=server.gateway_settings
    if app.state.services is not server.gateway_services:
        raise ValueError('REST and MCP must share the same service container')
    shared=[item for item in app.user_middleware if item.cls is TransportBoundary]
    if len(shared)!=1 or '/mcp' not in shared[0].kwargs['settings'].authenticated_prefixes:
        raise ValueError('MCP requires the shared per-request authentication boundary')
    child=server.streamable_http_app(streamable_http_path='/mcp',stateless_http=True,json_response=True,
        max_request_body_size=MAX_BODY,transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=list(settings.allowed_origins)))
    child.router.redirect_slashes = False
    challenge='Bearer'
    if server.settings.auth is not None:
        url=urlsplit(settings.resource_url)
        challenge=f'Bearer resource_metadata="{url.scheme}://{url.netloc}/.well-known/oauth-protected-resource/mcp"'
    app.add_middleware(_Challenge,challenge=challenge)
    app.mount('/',_MCPBodyBoundary(child,settings))
