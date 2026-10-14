"""Thin REST composition; callers own dependency lifetime and operator configuration."""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from starlette.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException
from .dependencies import query
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import ErrorView
from . import products,publishing,executions,admin
from .models import LivenessView, ReadinessView
from .services import Services
from .transport import TransportBoundary,TransportSettings,error_response


def create_app(services: Services, *, settings: TransportSettings | None=None, lifespan=None) -> FastAPI:
    app=FastAPI(title='Data Product Gateway',version='1.0.0',docs_url=None,redoc_url=None,openapi_url=None,
        lifespan=lifespan,responses={status:{'model':ErrorView} for status in (401,404,409,413,422,429,503)})
    app.state.services=services
    settings=settings or TransportSettings()
    app.add_middleware(CORSMiddleware,allow_origins=list(settings.allowed_origins),allow_credentials=False,
        allow_methods=['GET','POST','PUT','OPTIONS'],allow_headers=['Authorization','Content-Type'],
        expose_headers=['X-Request-ID'])
    app.add_middleware(TransportBoundary,services=services,settings=settings)
    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request,error: GatewayError):
        return error_response(error.code,request.state.request_id)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request,error):
        return error_response('INVALID_INPUT',request.state.request_id)
    @app.exception_handler(HTTPException)
    async def framework_error(request: Request,error: HTTPException):
        code={401:'UNAUTHENTICATED',404:'NOT_FOUND_OR_FORBIDDEN',405:'INVALID_INPUT',413:'RESOURCE_LIMIT',422:'INVALID_INPUT'}.get(error.status_code,'UPSTREAM_UNAVAILABLE')
        return error_response(code,request.state.request_id,error.status_code if error.status_code in {401,404,405,413,422} else 503)
    @app.exception_handler(Exception)
    async def internal_error(request: Request,error):
        return error_response('UPSTREAM_UNAVAILABLE',request.state.request_id)
    for router in (products.router,publishing.router,executions.router,admin.router):app.include_router(router)
    @app.get('/healthz',response_model=LivenessView)
    async def health(request: Request):
        query(request,set())
        return LivenessView()
    @app.get('/readyz',response_model=ReadinessView)
    async def ready(request: Request):
        query(request,set())
        view=await services.readiness.check()
        if not view.ready:return error_response('UPSTREAM_UNAVAILABLE',request.state.request_id)
        return ReadinessView(ready=True)
    def openapi():
        if app.openapi_schema is None:
            schema=get_openapi(title=app.title,version=app.version,routes=app.routes)
            schema.setdefault('components',{}).setdefault('securitySchemes',{})['GatewayBearer']={
                'type':'http','scheme':'bearer','description':'Fresh gateway authentication; permissions are server-managed.'}
            for path,operations in schema['paths'].items():
                if path.startswith('/v1/'):
                    for operation in operations.values():operation['security']=[{'GatewayBearer':[]}]
            app.openapi_schema=schema
        return app.openapi_schema
    app.openapi=openapi
    return app
