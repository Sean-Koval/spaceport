"""Context-bound BigQuery serving HTTP adapter; no startup/source probes."""
import re
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.execution import Completed
from .bigquery_runtime import (BigQuerySource, BigQueryBinding, check_bigquery_binding,
    compile_bigquery_query, validate_bigquery_decision, bigquery_security_contract)


# The serving adapter reuses only the bounded private HTTP transport. It never
# invokes the legacy metadata/watermark queries or their token path.
from contextvars import ContextVar
from .cube import CubeAdapter

_SERVING_REQUEST = ContextVar("spaceport_bigquery_serving_request", default=None)


class BigQueryServingAdapter(CubeAdapter):
    def __init__(self, *, sources, **kwargs):
        super().__init__(**kwargs)
        self.sources = sources

    def _source(self, binding):
        source = self.sources.resolve(dict(provider="bigquery",source_id=binding.source_id,
            revision=binding.source_revision,context=binding.context,descriptor_digest=binding.source_digest))
        source = BigQuerySource.model_validate(source.model_dump(mode="python"))
        if source.model_bundle_digest != binding.model_bundle_digest:
            raise ValueError("Source bundle differs from its retained binding")
        return source

    async def _binding(self, manifest, *, digest=None):
        binding = await self.bindings.resolve(manifest.binding_id,manifest.binding_revision)
        binding = BigQueryBinding.model_validate(binding.model_dump(mode="python"))
        check_bigquery_binding(binding,manifest,digest=digest)
        source = self._source(binding)
        endpoint = self.catalog.resolve(binding.model_bundle_digest)
        if ((endpoint.context,endpoint.adapter_revision,endpoint.url)
                != (binding.context,binding.adapter_revision,source.cube_endpoint)):
            raise ValueError("The exact source endpoint is unavailable")
        return binding

    async def validate_binding(self, *args, **kwargs):
        raise GatewayError(ErrorCode.BINDING_DRIFT,"source-readiness","Provider probes are not readiness checks.")

    async def prepare(self, product, request, decision, *, context, deadline, request_id):
        from gateway.contracts.execution import PreparedQuery
        from gateway.contracts.canonical import timestamp
        try:
            if timestamp(self.clock.now()) >= timestamp(deadline):
                raise GatewayError(ErrorCode.EXECUTION_TIMEOUT,request_id,"The execution deadline has elapsed.")
            binding = await self._binding(product.manifest,digest=product.binding_digest)
            if binding.context != context:
                raise ValueError
            endpoint = self.catalog.resolve(binding.model_bundle_digest)
            return PreparedQuery(endpoint_id=endpoint.endpoint_id,model_bundle_digest=binding.model_bundle_digest,
                cube_query=compile_bigquery_query(binding,product.manifest,request,decision,
                    max_rows=self.capabilities.max_rows,request_id=request_id))
        except GatewayError:
            raise
        except (ValueError,TypeError,AttributeError,KeyError):
            raise GatewayError(ErrorCode.BINDING_DRIFT,request_id,"The source preparation is incompatible.") from None

    async def source_plan(self, spec, prepared, executed_decision, *, catalog_digest=None):
        from gateway.adapters.base import authorization_scope_digest
        from gateway.contracts.execution import ExecutionSpec,PreparedQuery
        from gateway.contracts.query import QueryRequest
        from gateway.source_lifecycle import SourceSessionSpec
        spec = ExecutionSpec.model_validate(spec.model_dump(mode="python"))
        prepared = PreparedQuery.model_validate(prepared.model_dump(mode="python"))
        binding = await self._binding(spec.pinned_product.manifest,digest=spec.pinned_product.binding_digest)
        source = self._source(binding)
        endpoint = self.catalog.resolve_query(prepared.endpoint_id,prepared.model_bundle_digest)
        executed = validate_bigquery_decision(binding,executed_decision,spec.request_id)
        expected = compile_bigquery_query(binding,spec.pinned_product.manifest,QueryRequest.model_validate(spec.canonical_query),
            executed,max_rows=self.capabilities.max_rows,request_id=spec.request_id)
        retained_endpoint=self.catalog.resolve(binding.model_bundle_digest)
        if (prepared.model_bundle_digest != binding.model_bundle_digest
                or (endpoint.endpoint_id,endpoint.bundle_digest,endpoint.adapter_revision,endpoint.url,endpoint.context) !=
                   (retained_endpoint.endpoint_id,binding.model_bundle_digest,binding.adapter_revision,source.cube_endpoint,binding.context)
                or spec.context != source.context or spec.principal_id != executed.principal_id or spec.policy_revision != executed.policy_revision
                or endpoint.context != spec.context or canonical_digest(prepared.cube_query) != canonical_digest(expected)
                or (spec.context == "fixture_test") != (catalog_digest is not None)):
            raise GatewayError(ErrorCode.BINDING_DRIFT,spec.request_id,"The source preparation is incompatible.")
        return SourceSessionSpec(provider="bigquery",source_id=source.source_id,source_revision=source.revision,
            context=source.context,capacity_key=source.capacity_pool_id,definition_digest=source.digest,
            request_digest=spec.canonical_hash,prepared_digest=canonical_digest(prepared.cube_query),
            scope_digest=authorization_scope_digest(executed),decision_digest=canonical_digest(executed),
            binding_digest=binding.digest,model_bundle_digest=binding.model_bundle_digest,catalog_digest=catalog_digest,
            max_submissions=source.max_jobs_per_execution,max_bytes_per_submission=source.per_job_maximum_bytes_billed,
            max_total_bytes=source.total_execution_bytes)

    def _token(self, endpoint, decision, request_id, *, binding=None):
        import jwt
        from gateway.contracts.canonical import timestamp
        active = _SERVING_REQUEST.get()
        if active is None or active[0] is not self:
            raise GatewayError(ErrorCode.BINDING_DRIFT,request_id,"A committed source context is required.")
        _,context,query_digest = active
        wire = context.wire_context()
        if request_id != context.request_id or query_digest != context.spec.prepared_digest:
            raise GatewayError(ErrorCode.BINDING_DRIFT,request_id,"The source request fence is incompatible.")
        decision = validate_bigquery_decision(binding,decision,request_id)
        now = int(timestamp(self.clock.now()).timestamp())
        expiry = min(now+60,int(context.deadline.timestamp()))
        if expiry <= now:
            raise GatewayError(ErrorCode.EXECUTION_TIMEOUT,request_id,"The execution deadline has elapsed.")
        payload=dict(iss="dpg-gateway",aud="dpg-cube-source-v1",iat=now,exp=expiry,
            principal_id=decision.principal_id,tenant_id=decision.tenant_id,policy_revision=decision.policy_revision,
            model_bundle_digest=endpoint.bundle_digest,context=endpoint.context,
            row_constraints=decision.model_dump(mode="json")["row_constraints"],adapter_revision="cube-bigquery-v1",
            contract_digest=canonical_digest(bigquery_security_contract(binding)),source_attempt=wire,cube_query_digest=query_digest)
        key=self._secrets(endpoint.secret_ref)
        if type(key) is not str or re.fullmatch(r"[A-Za-z0-9_-]{43}",key) is None:
            raise ValueError("The operator signing configuration is unavailable")
        return jwt.encode(payload,key,algorithm="HS256")

    async def poll(self, query, decision, *, spec=None, executed_decision=None, source_context=None,
                   deadline, request_id, context="consumer"):
        from gateway.contracts.canonical import timestamp
        from gateway.contracts.execution import PreparedQuery,ExecutionSpec,Pending
        from gateway.source_lifecycle import SourceAttemptContext
        try:
            if type(source_context) is not SourceAttemptContext or spec is None or executed_decision is None:
                raise ValueError
            source_context.wire_context()
            query = PreparedQuery.model_validate(query.model_dump(mode="python"))
            spec = ExecutionSpec.model_validate(spec.model_dump(mode="python"))
            binding = await self._binding(spec.pinned_product.manifest,digest=spec.pinned_product.binding_digest)
            expected_plan = await self.source_plan(spec,query,executed_decision,catalog_digest=source_context.spec.catalog_digest)
            if (expected_plan != source_context.spec or context != spec.context or request_id != spec.request_id
                    or timestamp(deadline)>source_context.deadline):
                raise ValueError
            decision=validate_bigquery_decision(binding,decision,request_id)
            if canonical_digest(decision)!=source_context.spec.decision_digest:
                raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN,request_id,"The current source authority changed.")
            endpoint=self.catalog.resolve_query(query.endpoint_id,query.model_bundle_digest)
            marker=_SERVING_REQUEST.set((self,source_context,canonical_digest(query.cube_query)))
            try:
                response=await self._request(endpoint,decision,"/cubejs-api/v1/load",deadline=min(timestamp(deadline),source_context.deadline),
                    request_id=source_context.request_id,binding=binding,body={"query":query.model_dump(mode="json")["cube_query"],"cache":"must-revalidate"})
            finally:
                _SERVING_REQUEST.reset(marker)
            if response.get("error")=="Continue wait":
                return Pending(retry_after_seconds=0.25)
            if "error" in response:
                raise GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE,request_id,"The private source could not complete this query.")
            rows=response["data"]
            if type(rows) is not list:
                raise ValueError
            if len(rows)>=query.cube_query["limit"]:
                raise GatewayError(ErrorCode.RESOURCE_LIMIT,request_id,"The complete result exceeds its row limit.")
            expected_keys={item.response_key for name,item in binding.fields.items()
                if item.kind!="measure" or name in decision.visible_fields}
            time_field=next(item for item in binding.fields.values() if item.kind=="time")
            time_member=time_field.member
            allowed=expected_keys|{time_member}|({binding.checkpoint_member} if binding.checkpoint_member else set())
            output=[];checkpoints=set()
            for row in rows:
                if type(row) is not dict or not expected_keys<=set(row) or set(row)-allowed:
                    raise ValueError
                row=dict(row)
                if time_member in row and row[time_member] != row[time_field.response_key]:
                    raise ValueError
                if binding.checkpoint_member is not None:
                    value=row.pop(binding.checkpoint_member)
                    if type(value) is not str or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z",value) is None:
                        raise ValueError
                    checkpoint=timestamp(value)
                    if checkpoint>timestamp(self.clock.now()):
                        raise ValueError
                    checkpoints.add(checkpoint)
                output.append(row)
            if len(checkpoints)>1:
                raise ValueError
            return Completed(raw_rows=output,source_watermark=next(iter(checkpoints),None),cache_status="unknown")
        except GatewayError:
            raise
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
            raise GatewayError(ErrorCode.BINDING_DRIFT,request_id,"The source query, context or checkpoint is incompatible.") from None
