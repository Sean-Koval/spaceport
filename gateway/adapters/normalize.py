"""Lossless result conversion using explicit trusted execution context, never fetch time."""
from dataclasses import dataclass
from decimal import Decimal, localcontext
import re

from pydantic import ValidationError

from gateway.adapters.reviewed import Binding, validate_binding_decision
from gateway.contracts.canonical import canonical_bytes, canonical_digest, json_value, timestamp
from gateway.contracts.clock import Clock
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.execution import Completed, EffectiveRange, ExecutionSpec, ExecutionView, FreshnessAssessment, PinnedProduct, Provenance, ResultEnvelope
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import FieldSchema, validate_value
from gateway.contracts.query import QueryRequest, validate_query
from .base import authorization_scope_digest, check_binding, validate_query_visibility


@dataclass(frozen=True)
class NormalizationContext:
    spec: ExecutionSpec
    decision: Decision
    executed_decision: Decision
    binding: Binding
    clock: Clock


def normalize_value(schema: FieldSchema, value):
    """No numeric coercion or decimal rounding; return only declared strict scalars."""
    validate_value(schema,value)
    if value is None: return None
    if schema.type=="decimal":
        with localcontext() as context:
            context.prec=schema.precision
            return format(Decimal(value).quantize(Decimal(1).scaleb(-schema.scale)),"f")
    if schema.type=="timestamp": return json_value(timestamp(value))
    return value


def normalize(product: PinnedProduct, completed: Completed, execution: ExecutionView, *, context: NormalizationContext) -> ResultEnvelope:
    request_id=context.spec.request_id
    def error(code,message): return GatewayError(code,request_id,message)
    try:
        product=PinnedProduct.model_validate(product.model_dump(mode="python"))
        spec=ExecutionSpec.model_validate(context.spec.model_dump(mode="python"))
        decision=Decision.model_validate(context.decision.model_dump(mode="python"))
        executed=Decision.model_validate(context.executed_decision.model_dump(mode="python"))
        decision=validate_binding_decision(context.binding,decision,request_id)
        executed=validate_binding_decision(context.binding,executed,request_id)
        completed=Completed.model_validate(completed.model_dump(mode="python"))
        execution=ExecutionView.model_validate(execution.model_dump(mode="python"))
        if canonical_digest(spec.pinned_product)!=canonical_digest(product): raise ValueError
        if (decision.principal_id!=spec.principal_id or decision.policy_revision!=spec.policy_revision
            or executed.principal_id!=spec.principal_id or executed.policy_revision!=spec.policy_revision
            or authorization_scope_digest(executed)!=authorization_scope_digest(decision)
            or not set(decision.visible_fields)<=set(executed.visible_fields)):
            raise error(ErrorCode.NOT_FOUND_OR_FORBIDDEN,"The current scope no longer matches this execution.")
        m=product.manifest
        check_binding(context.binding,m,digest=product.binding_digest)
        if context.binding.context!=spec.context: raise ValueError
        if not set(executed.visible_fields)<=set(m.outputs): raise ValueError
        grain_fields={name for name,item in context.binding.fields.items() if item.kind!="measure"}
        if not grain_fields<=set(decision.visible_fields) or not grain_fields<=set(executed.visible_fields):
            raise error(ErrorCode.NOT_FOUND_OR_FORBIDDEN,"The current scope cannot represent the reviewed product grain.")
        if execution.state!="succeeded": raise ValueError
        now=timestamp(context.clock.now())
        if execution.completed_at>now: raise ValueError
        request=validate_query(m,QueryRequest.model_validate(spec.canonical_query))
        validate_query_visibility(context.binding,request,decision,request_id)
        bounds=m.date_boundaries
        effective_range=EffectiveRange(start=request.inputs[bounds.start_input],end=request.inputs[bounds.end_input],interval=bounds.interval)
        from gateway.contracts.manifest import iso_date
        if (iso_date(effective_range.end)-iso_date(effective_range.start)).days>decision.limits.max_date_range_days: raise ValueError
    except GatewayError as caught:
        if caught.code==ErrorCode.NOT_FOUND_OR_FORBIDDEN: raise
        raise error(ErrorCode.BINDING_DRIFT,"The trusted execution context is inconsistent.") from None
    except (ValueError,TypeError,KeyError,AttributeError):
        raise error(ErrorCode.BINDING_DRIFT,"The trusted execution context is inconsistent.") from None
    limit=min(m.limits.max_rows,decision.limits.max_rows)
    if len(completed.raw_rows)>limit:
        raise error(ErrorCode.RESOURCE_LIMIT,"The complete result exceeds its row limit.")
    watermark=completed.source_watermark
    age=(now-watermark).total_seconds() if watermark is not None else None
    known=age is not None and age>=0
    status="unknown" if not known else "fresh" if age<=m.freshness.max_age_seconds else "stale"
    if m.freshness.required and status!="fresh":
        raise error(ErrorCode.FRESHNESS_UNKNOWN if status=="unknown" else ErrorCode.STALE_DATA,
            "The required source-load freshness could not be established." if status=="unknown" else "The recorded source-load watermark is too old.")
    freshness=FreshnessAssessment(status=status,assessed_at=now,age_seconds=age if known else None,max_age_seconds=m.freshness.max_age_seconds)
    rows=[]
    grains=set()
    try:
        keys={item.response_key for item in context.binding.fields.values()}
        required={item.response_key for name,item in context.binding.fields.items() if item.kind!="measure" or name in decision.visible_fields}
        for raw in completed.raw_rows:
            raw=dict(raw)
            # Cube v1.8.5 clones the daily value into this deprecated base alias.
            # Accept only that exact reviewed alias; never select a new grouping axis.
            for item in context.binding.fields.values():
                if item.kind=="time" and item.member in raw:
                    if (type(raw[item.member]) is not str or type(raw.get(item.response_key)) is not str
                        or raw[item.member]!=raw[item.response_key]): raise ValueError
                    del raw[item.member]
            if set(raw)-keys or not required<=set(raw): raise ValueError
            converted={}
            grain=[]
            for name,item in context.binding.fields.items():
                if item.response_key not in raw: continue
                value=raw[item.response_key]
                if item.kind=="time":
                    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T00:00:00(?:\.0{1,6})?)?",value):
                        raise ValueError
                    value=value[:10]
                    if not effective_range.start<=value<effective_range.end: raise ValueError
                value=normalize_value(m.outputs[name],value)
                if item.kind!="measure": grain.append(value)
                if name in decision.visible_fields: converted[name]=value
            grain=tuple(grain)
            if grain in grains: raise ValueError
            grains.add(grain)
            rows.append(converted)
        schema={name:m.outputs[name] for name in decision.visible_fields}
        result={**execution.model_dump(mode="python"),"product_id":m.product_id,"version":m.version,
            "product_digest":m.content_digest,"typed_schema":schema,"rows":rows,
            "units":{name:unit for name,unit in m.units.items() if name in schema},"grain":m.grain,
            "effective_range":effective_range,"timezone":m.timezone,
            "provenance":Provenance(binding_id=m.binding_id,binding_revision=m.binding_revision,
                adapter_revision=m.adapter_revision,model_bundle_digest=m.model_bundle_digest,links=[]),
            "row_count":len(rows),"truncated":False,"source_watermark":watermark,"freshness":freshness,
            "cache_status":completed.cache_status,"warnings":[] if status=="fresh" else ["Source-load freshness is "+status+"."],
            "next_cursor":None}
        if len(canonical_bytes(result))>min(m.limits.max_bytes,decision.limits.max_bytes):
            raise error(ErrorCode.RESOURCE_LIMIT,"The complete normalized envelope exceeds its byte limit.")
        return ResultEnvelope.model_validate(result)
    except GatewayError as caught:
        if caught.code==ErrorCode.RESOURCE_LIMIT: raise
        raise error(ErrorCode.BINDING_DRIFT,"The source result violates its reviewed output schema.") from None
    except (ValueError,TypeError,KeyError,ValidationError):
        raise error(ErrorCode.BINDING_DRIFT,"The source result violates its reviewed output schema.") from None
