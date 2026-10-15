"""Configured Domo adapter through the shared durable source lifecycle.

The registry issues current authority and the sole send permit. This adapter
owns only exact HTTP/protocol observation and source-specific row semantics.
"""
from contextlib import suppress
from types import MappingProxyType

from gateway.contracts.canonical import canonical_digest, timestamp
from gateway.contracts.clock import SystemClock
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.execution import ExecutionSpec, PinnedProduct
from gateway.contracts.query import QueryRequest
from gateway.source_lifecycle import SourceAttemptContext, SourceSessionSpec
from .domo_completed import DomoCompleted
from .domo_http import DomoHttpClient, source_error
from .domo_normalize import normalize_domo_result
from .domo_runtime import (DomoSource, DomoStatementCatalog, check_domo_runtime_binding,
    compile_domo_query, validate_domo_decision, validate_domo_prepared)


class DomoRuntimeAdapter:
    def __init__(self, *, bindings, sources, statements, lifecycle, secret_resolver,
                 context, clock=None, http_transport=None):
        if context not in {'consumer','fixture_test'} or not callable(secret_resolver):
            raise ValueError('An exact server-owned Domo context is required')
        if (type(statements) not in {dict,MappingProxyType} or not statements
            or any(type(value) is not DomoStatementCatalog or key!=value.digest for key,value in statements.items())):
            raise ValueError('Exact retained Domo statement catalogs are required')
        self.bindings,self.sources,self.lifecycle=bindings,sources,lifecycle
        self.statements=MappingProxyType(dict(statements))
        self.context,self.clock=context,clock or SystemClock()
        self._secrets,self._transport=secret_resolver,http_transport
        self._seen=set()
        self._clients=[]

    async def _resolve(self, product, context):
        if context!=self.context:
            raise source_error()
        b=await self.bindings.resolve(product.manifest.binding_id,product.manifest.binding_revision)
        ref=dict(provider='domo',source_id=b.source_id,revision=b.source_revision,
            descriptor_digest=b.source_digest,context=b.context)
        source=self.sources.resolve(ref)
        source=DomoSource.model_validate(source.model_dump(mode='python'))
        b=check_domo_runtime_binding(b,product.manifest,digest=product.binding_digest,source=source)
        if b.context!=context:
            raise source_error()
        try:
            catalog=self.statements[b.model_bundle_digest]
        except KeyError:
            raise source_error() from None
        return b,source,catalog

    async def validate_binding_definition(self, manifest):
        binding=await self.bindings.resolve(manifest.binding_id,manifest.binding_revision)
        await self._resolve(PinnedProduct(manifest=manifest,binding_digest=binding.digest,
            version_id='definition-validation'),self.context)

    async def prepare(self, product, request, decision, *, context, deadline, request_id):
        b,source,catalog=await self._resolve(product,context)
        if timestamp(self.clock.now())>=timestamp(deadline):
            raise source_error(ErrorCode.EXECUTION_TIMEOUT,request_id)
        return compile_domo_query(b,product,request,decision,catalog,source=source,request_id=request_id)

    async def _reconstruct(self, spec, prepared, executed_decision):
        spec=ExecutionSpec.model_validate(spec.model_dump(mode='python'))
        b,source,catalog=await self._resolve(spec.pinned_product,spec.context)
        prepared=validate_domo_prepared(prepared,spec,executed_decision,b)
        expected=compile_domo_query(b,spec.pinned_product,QueryRequest.model_validate(spec.canonical_query),
            executed_decision,catalog,source=source,request_id=spec.request_id)
        if expected!=prepared or expected.accepted_spec_digest!=canonical_digest(spec):
            raise source_error()
        return spec,prepared,b,source,catalog

    async def source_plan(self, spec, prepared, executed_decision, *, catalog_digest=None):
        spec,prepared,b,source,catalog=await self._reconstruct(spec,prepared,executed_decision)
        if (spec.context=='consumer' and catalog_digest is not None
            or spec.context=='fixture_test' and catalog_digest is None):
            raise source_error()
        return SourceSessionSpec(provider='domo',source_id=source.source_id,source_revision=source.revision,
            context=source.context,capacity_key=source.capacity_pool_id,definition_digest=source.digest,
            request_digest=spec.canonical_hash,prepared_digest=prepared.digest,scope_digest=prepared.scope_digest,
            decision_digest=prepared.decision_digest,binding_digest=prepared.binding_digest,
            model_bundle_digest=prepared.model_bundle_digest,catalog_digest=catalog_digest,
            max_submissions=1,max_bytes_per_submission=None,max_total_bytes=None)

    async def poll(self, prepared, current, *, spec, executed_decision, source_context,
                   context, deadline, request_id):
        # Everything through session-spec comparison is pure and before secrets.
        spec,prepared,b,source,catalog=await self._reconstruct(spec,prepared,executed_decision)
        current=validate_domo_decision(b,spec.pinned_product.manifest,current)
        if (canonical_digest(current)!=canonical_digest(executed_decision)
            or context!=self.context or spec.context!=context or request_id!=spec.request_id):
            raise source_error(ErrorCode.NOT_FOUND_OR_FORBIDDEN,request_id)
        if type(source_context) is not SourceAttemptContext:
            raise source_error(request_id=request_id)
        source_context.wire_context()
        expected=await self.source_plan(spec,prepared,executed_decision,catalog_digest=source_context.spec.catalog_digest)
        if source_context.spec!=expected or source_context.session_id in self._seen:
            raise source_error(request_id=request_id)
        self._seen.add(source_context.session_id)
        deadline=min(timestamp(deadline),source_context.deadline)
        client=DomoHttpClient(source=source,secret_resolver=self._secrets,transport=self._transport,clock=self.clock)
        self._clients.append(client)
        permit=None
        sealed=False
        reason='runtime_error'
        try:
            client.check_deadline(deadline)
            await self.lifecycle.check_attempt(source_context)
            token=await client.acquire_token(deadline=deadline)
            await self.lifecycle.check_attempt(source_context)
            client.check_deadline(deadline)
            intent=dict(statement_id=prepared.statement_id,statement_digest=prepared.statement_digest,
                dataset_id=prepared.dataset_id,api_origin_digest=canonical_digest('https://api.domo.com'),
                response_profile_digest=canonical_digest(source.response_profile))
            permit=await self.lifecycle.domo_arm_send(source_context,prepared.digest,intent)
            await self.lifecycle.check_attempt(source_context)
            client.check_deadline(deadline)
            statement=catalog.resolve(prepared.statement_id,prepared.statement_revision,prepared.statement_digest)
            response=await client.query(plan=prepared,statement=statement,token=token,permit=permit)
            # Exact response observation has no current row-release authority.
            evidence=client.success_evidence(response,permit)
            await self.lifecycle.domo_observe_success(permit,evidence)
            await client.close()
            client.check_deadline(deadline)
            await self.lifecycle.check_attempt(source_context)
            candidate=normalize_domo_result(b,spec.pinned_product,QueryRequest.model_validate(spec.canonical_query),
                current,response,now=self.clock.now())
            client.check_deadline(deadline)
            status=await self.lifecycle.seal_source(source_context,'completed')
            sealed=True
            if not status.can_complete_session:
                raise source_error(ErrorCode.UPSTREAM_UNAVAILABLE,request_id)
            client.check_deadline(deadline)
            return DomoCompleted(prepared=prepared,response=response,client=client,permit=permit,
                source_watermark=candidate.source_watermark)
        except GatewayError as error:
            reason={ErrorCode.NOT_FOUND_OR_FORBIDDEN:'authorization',ErrorCode.EXECUTION_TIMEOUT:'deadline'}.get(error.code,'runtime_error')
            raise GatewayError(error.code,request_id,error.safe_message) from None
        finally:
            await client.close()
            if permit is not None and not sealed:
                # Only an unused original in-memory permit can attest no I/O.
                # Started/observed/restarted work cannot acquire this proof.
                with suppress(Exception):
                    await self.lifecycle.domo_observe_not_sent(permit)
            if not sealed:
                with suppress(Exception):
                    await self.lifecycle.seal_source(source_context,reason)

    async def close(self):
        for client in self._clients:
            await client.close()
