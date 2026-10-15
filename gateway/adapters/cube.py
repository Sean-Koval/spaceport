"""Constrained private Cube client with independent transport and execution budgets."""
import asyncio
from datetime import datetime, timedelta
import json
import re
from typing import Callable
import zlib

import httpx
import jwt
from pydantic import ValidationError

from gateway.contracts.canonical import canonical_bytes, canonical_digest, timestamp
from gateway.contracts.clock import Clock, SystemClock
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.execution import AdapterPoll, Completed, Pending, PinnedProduct, PreparedQuery, ExecutionSpec
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import Manifest, iso_date
from gateway.contracts.query import QueryRequest, validate_query
from .base import BindingResolver, SourceCapabilities, check_binding, validate_data_decision, validate_query_visibility
from .bundles import BundleCatalog, CubeEndpoint
from .reviewed import ReviewedCubeBinding, compile_query, security_contract, validate_binding_decision

_OPERATORS = {"eq":"equals", "ne":"notEquals", "in":"equals", "not_in":"notEquals",
    "lt":"lt", "lte":"lte", "gt":"gt", "gte":"gte", "is_null":"notSet"}
_META_TYPES = {"Orders.revenue":"number", "Orders.segment":"string", "Orders.ordered_at":"time",
    "Orders.tenant_id":"string", "Orders.access_group":"string", "Watermark.loaded_at":"time"}


def _error(code, request_id, message):
    return GatewayError(code,request_id,message)


def _validate_response_structure(text: str) -> None:
    """Bound parser nesting/structural work before allocating the JSON tree.

    This adapter admits at most 32 levels and 50,000 outside-string delimiters.
    Quoted/escaped characters cannot increment either limit. JSON syntax itself
    is checked by the maintained parser on the same strictly decoded UTF-8 text.
    Raw control characters cannot represent valid JSON or conceal other encodings.
    """
    depth=tokens=0
    quoted=escaped=False
    for value in text:
        if ord(value)<32 and (quoted or value not in "\t\r\n"):
            raise ValueError("Invalid response control character")
        if quoted:
            if escaped: escaped=False
            elif value=="\\": escaped=True
            elif value=='"': quoted=False
            continue
        if value=='"': quoted=True
        elif value in "{[":
            depth+=1;tokens+=1
            if depth>32: raise ValueError("Response nesting exceeds its structural bound")
        elif value in "}]":
            depth-=1;tokens+=1
            if depth<0: raise ValueError("Invalid response nesting")
        elif value in ",:": tokens+=1
        if tokens>50_000: raise ValueError("Response structure exceeds its token bound")
    if quoted or depth: raise ValueError("Incomplete response structure")


class CubeAdapter:
    def __init__(self, *, catalog: BundleCatalog, bindings: BindingResolver,
                 secret_resolver: Callable[[str], str], clock: Clock | None = None,
                 capabilities: SourceCapabilities | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 max_response_bytes: int = 2_097_152, transport_deadline_seconds: float = 25.0):
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 8_388_608:
            raise ValueError("Invalid upstream response bound")
        if not 0 < transport_deadline_seconds <= 30: raise ValueError("Invalid transport deadline")
        self.catalog,self.bindings,self._secrets=catalog,bindings,secret_resolver
        self.clock=clock or SystemClock()
        self.capabilities=capabilities or SourceCapabilities()
        self.max_response_bytes=max_response_bytes
        self.transport_deadline_seconds=transport_deadline_seconds
        self._client=httpx.AsyncClient(transport=transport,trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(connect=5,read=20,write=5,pool=5),
            limits=httpx.Limits(max_connections=10,max_keepalive_connections=10),
            headers={"Accept-Encoding":"identity"})

    async def __aenter__(self): return self
    async def __aexit__(self,*args): await self.close()
    async def close(self): await self._client.aclose()

    def _token(self, endpoint: CubeEndpoint, decision: Decision, request_id: str, *, binding=None):
        decision=validate_binding_decision(binding,decision,request_id) if binding is not None else validate_data_decision(decision,request_id)
        if endpoint.adapter_revision=="cube-rest-v2" and not isinstance(binding,ReviewedCubeBinding):
            raise _error(ErrorCode.BINDING_DRIFT,request_id,"The reviewed source context is unavailable.")
        now=int(timestamp(self.clock.now()).timestamp())
        payload={"iss":"dpg-gateway","aud":"dpg-cube","iat":now,"exp":now+60,
            "principal_id":decision.principal_id,"tenant_id":decision.tenant_id,
            "policy_revision":decision.policy_revision,"model_bundle_digest":endpoint.bundle_digest,
            "context":endpoint.context,"row_constraints":decision.model_dump(mode="json")["row_constraints"]}
        if isinstance(binding,ReviewedCubeBinding):
            payload.update(adapter_revision=binding.adapter_revision,contract_digest=canonical_digest(security_contract(binding)))
        try:
            key=self._secrets(endpoint.secret_ref)
            if type(key) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{43}",key): raise ValueError
            return jwt.encode(payload,key,algorithm="HS256")
        except (ValueError,OSError,TypeError,jwt.PyJWTError):
            raise _error(ErrorCode.BINDING_DRIFT,request_id,"The operator endpoint signing configuration is unavailable.") from None

    async def _request(self, endpoint, decision, path, *, deadline, request_id, body=None, binding=None):
        deadline=timestamp(deadline)
        remaining=(deadline-timestamp(self.clock.now())).total_seconds()
        if remaining<=0:
            raise _error(ErrorCode.EXECUTION_TIMEOUT,request_id,"The execution deadline has elapsed.")
        duration=min(remaining,self.transport_deadline_seconds)
        try:
            async with asyncio.timeout(duration):
                async with self._client.stream("POST" if body is not None else "GET",endpoint.url+path,
                    headers={"Authorization":"Bearer "+self._token(endpoint,decision,request_id,binding=binding),"X-Request-Id":request_id,
                        **({"Content-Type":"application/json"} if body is not None else {})},
                    content=canonical_bytes(body) if body is not None else None) as response:
                    if response.status_code!=200:
                        code=ErrorCode.BINDING_DRIFT if response.status_code in {401,403,404} else ErrorCode.UPSTREAM_UNAVAILABLE
                        raise _error(code,request_id,"The private semantic source is unavailable.")
                    encoding=response.headers.get("content-encoding","identity").lower()
                    if encoding not in {"identity","gzip","deflate"}:
                        raise _error(ErrorCode.UPSTREAM_UNAVAILABLE,request_id,"The private source response is incompatible.")
                    decoder=zlib.decompressobj(31 if encoding=="gzip" else zlib.MAX_WBITS) if encoding!="identity" else None
                    data=bytearray(); raw_count=0
                    async for chunk in response.aiter_raw():
                        raw_count+=len(chunk)
                        if raw_count>self.max_response_bytes:
                            raise _error(ErrorCode.RESOURCE_LIMIT,request_id,"The private source response exceeds its byte limit.")
                        decoded=decoder.decompress(chunk,self.max_response_bytes-len(data)+1) if decoder else chunk
                        data.extend(decoded)
                        if len(data)>self.max_response_bytes or decoder and decoder.unconsumed_tail:
                            raise _error(ErrorCode.RESOURCE_LIMIT,request_id,"The private source response exceeds its byte limit.")
                        if timestamp(self.clock.now())>=deadline:
                            raise _error(ErrorCode.EXECUTION_TIMEOUT,request_id,"The execution deadline has elapsed.")
                    if decoder:
                        if not decoder.eof or decoder.unused_data: raise ValueError
                    # Only private UTF-8 JSON is supported. Passing the same str
                    # to both stages prevents json.loads from autodetecting UTF-16/32.
                    text=data.decode("utf-8",errors="strict")
                    _validate_response_structure(text)
                    value=json.loads(text,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if not isinstance(value,dict): raise ValueError
                    return value
        except (TimeoutError,httpx.TimeoutException):
            raise _error(ErrorCode.EXECUTION_TIMEOUT,request_id,"The private source exceeded the execution time budget.") from None
        except (httpx.HTTPError,OSError,ValueError,UnicodeError,zlib.error,RecursionError):
            raise _error(ErrorCode.UPSTREAM_UNAVAILABLE,request_id,"The private source returned an invalid response.") from None

    async def _binding(self, manifest, *, digest=None):
        binding=await self.bindings.resolve(manifest.binding_id,manifest.binding_revision)
        check_binding(binding,manifest,digest=digest)
        endpoint=self.catalog.resolve(manifest.model_bundle_digest)
        if endpoint.context!=binding.context or endpoint.adapter_revision!=binding.adapter_revision:
            raise _error(ErrorCode.BINDING_DRIFT,"binding-validation","The binding context is incompatible with its retained endpoint.")
        return binding

    async def validate_binding_definition(self, manifest: Manifest) -> None:
        """Static draft check only. This never establishes live metadata compatibility."""
        await self._binding(manifest)

    async def _metadata(self, endpoint, decision, members, *, deadline, request_id, binding=None):
        response=await self._request(endpoint,decision,"/cubejs-api/v1/meta",deadline=deadline,request_id=request_id,binding=binding)
        try:
            actual={}
            kinds={}
            cubes=response.get("cubes")
            if type(cubes) is not list: raise ValueError
            for cube in cubes:
                if type(cube) is not dict or type(cube.get("name")) is not str or not cube["name"]: raise ValueError
                for kind in ("measures","dimensions"):
                    items=cube.get(kind)
                    if type(items) is not list: raise ValueError
                    for item in items:
                        if (type(item) is not dict or type(item.get("name")) is not str
                            or type(item.get("type")) is not str
                            or not item["name"].startswith(cube["name"]+".")
                            or item["name"] in actual): raise ValueError
                        actual[item["name"]]=item["type"]
                        kinds[item["name"]]="measure" if kind=="measures" else "dimension"
            if any(actual.get(member)!=expected for member,expected in members.items()): raise ValueError
            if isinstance(binding,ReviewedCubeBinding):
                expected=security_contract(binding)["members"]
                if any(kinds.get(member)!=("measure" if item["kind"]=="measure" else "dimension") for member,item in expected.items()): raise ValueError
        except (ValueError,TypeError,KeyError):
            raise _error(ErrorCode.BINDING_DRIFT,request_id,"The retained source metadata is incompatible.") from None

    async def validate_binding(self, manifest: Manifest, *, decision: Decision,
                               deadline: datetime | None = None, request_id: str = "binding-validation") -> None:
        try:
            binding=await self._binding(manifest)
        except GatewayError as caught:
            raise _error(caught.code,request_id,caught.safe_message) from None
        decision=validate_binding_decision(binding,decision,request_id)
        endpoint=self.catalog.resolve(manifest.model_bundle_digest)
        members={item.member:item.cube_type for item in binding.fields.values()}
        members.update({member:"string" for member in binding.constraint_members})
        members[binding.checkpoint_member if isinstance(binding,ReviewedCubeBinding) else binding.watermark_member]="string" if isinstance(binding,ReviewedCubeBinding) else "time"
        await self._metadata(endpoint,decision,members,deadline=deadline if deadline is not None else self.clock.now()+timedelta(seconds=self.transport_deadline_seconds),request_id=request_id,binding=binding)

    @staticmethod
    def _date_filter(member,operator,value):
        def bound(day,op): return {"member":member,"operator":op,"values":[day.isoformat()+"T00:00:00"]}
        day=iso_date(value)
        tomorrow=day+timedelta(days=1)
        if operator=="eq": return {"and":[bound(day,"afterOrOnDate"),bound(tomorrow,"beforeDate")]}
        if operator=="ne": return {"or":[bound(day,"beforeDate"),bound(tomorrow,"afterOrOnDate")]}
        if operator=="gte": return bound(day,"afterOrOnDate")
        if operator=="gt": return bound(tomorrow,"afterOrOnDate")
        if operator=="lt": return bound(day,"beforeDate")
        if operator=="lte": return bound(tomorrow,"beforeDate")
        raise ValueError

    async def prepare(self, product: PinnedProduct, request: QueryRequest, decision: Decision, *,
                      context: str = "consumer", deadline: datetime | None = None,
                      request_id: str = "query-preparation") -> PreparedQuery:
        try:
            return await self._prepare(product,request,decision,context=context,deadline=deadline,request_id=request_id)
        except GatewayError as caught:
            raise _error(caught.code,request_id,caught.safe_message) from None
        except (ValueError,TypeError,KeyError,OverflowError):
            raise _error(ErrorCode.INVALID_INPUT,request_id,"The query exceeds the supported source contract.") from None

    async def _prepare(self, product, request, decision, *, context, deadline, request_id):
        product=PinnedProduct.model_validate(product.model_dump(mode="python"))
        binding=await self._binding(product.manifest,digest=product.binding_digest)
        endpoint=self.catalog.resolve(product.manifest.model_bundle_digest)
        if context!=endpoint.context or context!=binding.context:
            raise _error(ErrorCode.BINDING_DRIFT,request_id,"The execution context does not match the retained endpoint.")
        request=validate_query(product.manifest,request)
        decision=validate_binding_decision(binding,decision,request_id)
        m=product.manifest
        grain_fields={name for name,item in binding.fields.items() if item.kind!="measure"}
        if not grain_fields<=set(decision.visible_fields):
            raise _error(ErrorCode.NOT_FOUND_OR_FORBIDDEN,request_id,"The current scope cannot represent the reviewed product grain.")
        if set(decision.visible_fields)-set(m.outputs):
            raise _error(ErrorCode.NOT_FOUND_OR_FORBIDDEN,request_id,"The current field permissions are incompatible.")
        validate_query_visibility(binding,request,decision,request_id)
        bounds=m.date_boundaries
        if (iso_date(request.inputs[bounds.end_input])-iso_date(request.inputs[bounds.start_input])).days>decision.limits.max_date_range_days:
            raise _error(ErrorCode.INVALID_INPUT,request_id,"The requested date range exceeds the effective limit.")
        if isinstance(binding,ReviewedCubeBinding):
            value=compile_query(binding,m,request,decision,max_rows=self.capabilities.max_rows,request_id=request_id)
            await self.validate_binding(m,decision=decision,deadline=deadline,request_id=request_id)
            return PreparedQuery(endpoint_id=endpoint.endpoint_id,model_bundle_digest=m.model_bundle_digest,cube_query=value)
        await self.validate_binding(m,decision=decision,deadline=deadline,request_id=request_id)
        time_member=next(item.member for item in binding.fields.values() if item.kind=="time")
        filters=[{"member":time_member,"operator":"afterOrOnDate","values":[request.inputs[bounds.start_input]+"T00:00:00"]},
            {"member":time_member,"operator":"beforeDate","values":[request.inputs[bounds.end_input]+"T00:00:00"]}]
        for name,field in binding.input_fields.items():
            if name in request.inputs and request.inputs[name] is not None:
                filters.append({"member":binding.fields[field].member,"operator":"equals","values":[request.inputs[name]]})
        for item in request.filters:
            member=binding.fields[item.field]
            if member.kind=="time":
                if item.operator in {"in","not_in"}:
                    filters.append({"or" if item.operator=="in" else "and":[self._date_filter(member.member,"eq" if item.operator=="in" else "ne",value) for value in item.value]})
                else: filters.append(self._date_filter(member.member,item.operator,item.value))
            else:
                value={"member":member.member,"operator":_OPERATORS[item.operator]}
                if item.operator!="is_null":
                    if item.value is None: raise _error(ErrorCode.INVALID_INPUT,request_id,"Use the approved null filter for null values.")
                    values=list(item.value) if isinstance(item.value,tuple) else [item.value]
                    if any(type(v) is not str or len(v)>128 for v in values):
                        raise _error(ErrorCode.INVALID_INPUT,request_id,"The filter value exceeds the supported source bounds.")
                    value["values"]=values
                filters.append(value)
        filters.extend({"member":item.member,"operator":"equals","values":list(item.values)} for item in decision.row_constraints)
        query={"measures":[item.member for name,item in binding.fields.items() if item.kind=="measure" and name in decision.visible_fields],
            # Keep the complete reviewed grouping independently of measure projection.
            "dimensions":[item.member for item in binding.fields.values() if item.kind=="dimension"],
            "timeDimensions":[{"dimension":time_member,"granularity":"day"}],"filters":[{"and":filters}],
            "order":[[binding.fields[item.field].member,item.direction] for item in request.sort],
            "timezone":m.timezone,"limit":min(m.limits.max_rows,decision.limits.max_rows,self.capabilities.max_rows)+1}
        endpoint=self.catalog.resolve(m.model_bundle_digest)
        return PreparedQuery(endpoint_id=endpoint.endpoint_id,model_bundle_digest=m.model_bundle_digest,cube_query=query)

    async def poll(self, query: PreparedQuery, decision: Decision, *, deadline: datetime, request_id: str, context: str = "consumer", spec: ExecutionSpec | None = None, executed_decision: Decision | None = None) -> AdapterPoll:
        try:
            query=PreparedQuery.model_validate(query.model_dump(mode="python"))
            endpoint=self.catalog.resolve_query(query.endpoint_id,query.model_bundle_digest)
            if spec is not None:
                spec=ExecutionSpec.model_validate(spec.model_dump(mode="python"))
                if (spec.pinned_product.manifest.adapter_revision!=endpoint.adapter_revision
                    or spec.pinned_product.manifest.model_bundle_digest!=query.model_bundle_digest):
                    raise ValueError
            if endpoint.adapter_revision=="cube-rest-v2":
                return await self._poll_reviewed(query,decision,spec=spec,executed_decision=executed_decision,deadline=deadline,request_id=request_id,context=context)
            query=PreparedQuery.model_validate(query.model_dump(mode="python"))
            endpoint=self.catalog.resolve_query(query.endpoint_id,query.model_bundle_digest)
            if context!=endpoint.context: raise ValueError
            decision=validate_data_decision(decision,request_id)
            value=query.model_dump(mode="json")["cube_query"]
            members={name:_META_TYPES[name] for name in value.get("measures",[])+value.get("dimensions",[])}
            for item in value.get("timeDimensions",[]): members[item["dimension"]]=_META_TYPES[item["dimension"]]
            if any(not name.startswith("Orders.") for name in members) or not members or not 1<=value["limit"]<=1001:
                raise ValueError
        except GatewayError as caught:
            raise _error(caught.code,request_id,caught.safe_message) from None
        except (ValueError,KeyError,TypeError,AttributeError):
            raise _error(ErrorCode.BINDING_DRIFT,request_id,"The prepared source query is incompatible.") from None
        members["Watermark.loaded_at"]="time"
        await self._metadata(endpoint,decision,members,deadline=deadline,request_id=request_id)
        response=await self._request(endpoint,decision,"/cubejs-api/v1/load",deadline=deadline,request_id=request_id,body={"query":value,"cache":"must-revalidate"})
        if response.get("error")=="Continue wait": return Pending(retry_after_seconds=0.25)
        if "error" in response:
            raise _error(ErrorCode.UPSTREAM_UNAVAILABLE,request_id,"The private source could not complete this query.")
        try:
            rows=response["data"]
            if not isinstance(rows,list): raise ValueError
            if len(rows)>=value["limit"]:
                raise _error(ErrorCode.RESOURCE_LIMIT,request_id,"The complete result exceeds its row limit.")
            watermark_query={"dimensions":["Watermark.loaded_at"],"timezone":"UTC","limit":2}
            watermark=await self._request(endpoint,decision,"/cubejs-api/v1/load",deadline=deadline,request_id=request_id,body={"query":watermark_query,"cache":"must-revalidate"})
            if watermark.get("error")=="Continue wait": return Pending(retry_after_seconds=0.25)
            if "error" in watermark: raise ValueError
            recorded=watermark["data"]
            if not isinstance(recorded,list) or len(recorded)>1: raise ValueError
            source=None
            if recorded:
                text=recorded[0]["Watermark.loaded_at"]
                if type(text) is not str: raise ValueError
                # Reviewed Cube time dimensions serialize UTC without an offset.
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?",text): text+="Z"
                source=timestamp(text)
            return Completed(raw_rows=rows,source_watermark=source,cache_status="unknown")
        except (ValueError,KeyError,TypeError,ValidationError):
            raise _error(ErrorCode.BINDING_DRIFT,request_id,"The private source result is incompatible with its reviewed binding.") from None

    async def _poll_reviewed(self, query, decision, *, spec, executed_decision, deadline, request_id, context):
        """Rebuild the accepted request; a valid-looking edited query is not authority."""
        try:
            query=PreparedQuery.model_validate(query.model_dump(mode='python'))
            if spec is None or executed_decision is None: raise ValueError
            spec=ExecutionSpec.model_validate(spec.model_dump(mode='python'))
            product=spec.pinned_product
            binding=await self._binding(product.manifest,digest=product.binding_digest)
            if not isinstance(binding,ReviewedCubeBinding): raise ValueError
            endpoint=self.catalog.resolve_query(query.endpoint_id,query.model_bundle_digest)
            if (context!=spec.context or context!=endpoint.context or context!=binding.context
                or request_id!=spec.request_id or query.model_bundle_digest!=binding.model_bundle_digest): raise ValueError
            executed=validate_binding_decision(binding,executed_decision,request_id)
            decision=validate_binding_decision(binding,decision,request_id)
            if (executed.principal_id!=spec.principal_id or executed.policy_revision!=spec.policy_revision): raise ValueError
            from gateway.execution.service import check_current_scope
            request=QueryRequest.model_validate(spec.canonical_query)
            check_current_scope(product,request,binding,decision,executed,request_id)
            expected=compile_query(binding,product.manifest,request,executed,max_rows=self.capabilities.max_rows,request_id=request_id)
            if canonical_bytes(query.cube_query)!=canonical_bytes(expected): raise ValueError
            # A current tighter result cap must stop a saved wider source query.
            if (expected['limit']>min(product.manifest.limits.max_rows,decision.limits.max_rows,self.capabilities.max_rows)+1
                or not set(executed.visible_fields)<=set(decision.visible_fields)):
                raise _error(ErrorCode.NOT_FOUND_OR_FORBIDDEN,request_id,'The current scope cannot execute this product.')
            await self.validate_binding(product.manifest,decision=decision,deadline=deadline,request_id=request_id)
            response=await self._request(endpoint,decision,'/cubejs-api/v1/load',deadline=deadline,
                request_id=request_id,binding=binding,body={'query':expected,'cache':'must-revalidate'})
            if response.get('error')=='Continue wait': return Pending(retry_after_seconds=0.25)
            if 'error' in response:
                raise _error(ErrorCode.UPSTREAM_UNAVAILABLE,request_id,'The private source could not complete this query.')
            rows=response['data']
            if not isinstance(rows,list): raise ValueError
            if len(rows)>=expected['limit']:
                raise _error(ErrorCode.RESOURCE_LIMIT,request_id,'The complete result exceeds its row limit.')
            output=[]; checkpoints=set()
            for row in rows:
                if not isinstance(row,dict): raise ValueError
                row=dict(row)
                value=row.pop(binding.checkpoint_member)
                if type(value) is not str: raise ValueError
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z',value): raise ValueError
                checkpoint=timestamp(value)
                if checkpoint>timestamp(self.clock.now()): raise ValueError
                checkpoints.add(checkpoint)
                output.append(row)
            if len(checkpoints)>1: raise ValueError
            return Completed(raw_rows=output,source_watermark=next(iter(checkpoints),None),cache_status='unknown')
        except GatewayError: raise
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
            raise _error(ErrorCode.BINDING_DRIFT,request_id,'The reviewed source query or checkpoint is incompatible.') from None
