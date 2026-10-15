"""Fixed provider dispatch over operator-owned bindings, never client SQL/config."""
from gateway.contracts.errors import ErrorCode,GatewayError
from gateway.contracts.execution import PreparedQuery
from .base import check_binding
from .domo_runtime import PreparedDomoQuery

SOURCE_REVISIONS=frozenset({'domo-named-v2','cube-bigquery-v1'})


class SourceDispatchAdapter:
    def __init__(self, *, bindings, cube_adapter=None, domo_adapter=None, bigquery_adapter=None):
        if all(adapter is None for adapter in (cube_adapter,domo_adapter,bigquery_adapter)):
            raise ValueError('At least one explicit provider adapter is required')
        self.bindings=bindings
        self._adapters={'cube-rest-v1':cube_adapter,'cube-rest-v2':cube_adapter,
            'domo-named-v2':domo_adapter,'cube-bigquery-v1':bigquery_adapter}

    def _select(self, revision):
        adapter=self._adapters.get(revision)
        if adapter is None:
            raise GatewayError(ErrorCode.BINDING_DRIFT,'source-dispatch','The configured provider is unavailable.')
        return adapter

    async def prepare(self, product, request, decision, **kwargs):
        b=await self.bindings.resolve(product.manifest.binding_id,product.manifest.binding_revision)
        check_binding(b,product.manifest,digest=product.binding_digest)
        return await self._select(b.adapter_revision).prepare(product,request,decision,**kwargs)

    async def source_plan(self, spec, prepared, executed_decision, *, catalog_digest=None):
        revision=spec.pinned_product.manifest.adapter_revision
        if revision not in SOURCE_REVISIONS:
            return None
        return await self._select(revision).source_plan(spec,prepared,executed_decision,catalog_digest=catalog_digest)

    async def poll(self, prepared, decision, *, spec=None, executed_decision=None, source_context=None, **kwargs):
        if spec is None:
            if type(prepared) is not PreparedQuery or source_context is not None:
                raise GatewayError(ErrorCode.BINDING_DRIFT,'source-dispatch','Trusted source context is required.')
            return await self._select('cube-rest-v1').poll(prepared,decision,**kwargs)
        revision=spec.pinned_product.manifest.adapter_revision
        adapter=self._select(revision)
        if revision in SOURCE_REVISIONS:
            if source_context is None:
                raise GatewayError(ErrorCode.BINDING_DRIFT,spec.request_id,'Trusted source context is required.')
            return await adapter.poll(prepared,decision,spec=spec,executed_decision=executed_decision,
                source_context=source_context,**kwargs)
        if source_context is not None or isinstance(prepared,PreparedDomoQuery):
            raise GatewayError(ErrorCode.BINDING_DRIFT,spec.request_id,'The source preparation is incompatible.')
        authority={'spec':spec,'executed_decision':executed_decision}
        return await adapter.poll(prepared,decision,**authority,**kwargs)

    async def validate_binding_definition(self, manifest):
        return await self._select(manifest.adapter_revision).validate_binding_definition(manifest)

    async def validate_binding(self, manifest, **kwargs):
        if manifest.adapter_revision in SOURCE_REVISIONS:
            # New profile readiness is explicitly local; no metadata/query probe.
            raise GatewayError(ErrorCode.BINDING_DRIFT,'source-readiness','Provider probes are not readiness checks.')
        return await self._select(manifest.adapter_revision).validate_binding(manifest,**kwargs)

    async def close(self):
        closed=set()
        for adapter in self._adapters.values():
            if adapter is not None and id(adapter) not in closed:
                closed.add(id(adapter))
                await adapter.close()
