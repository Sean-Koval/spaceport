"""Lossless Domo v2 semantics, independent of provider terminal observation."""
from pydantic import Field

from gateway.contracts.canonical import Array, AwareTimestamp, Digest, StrictModel, canonical_bytes, timestamp
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.execution import FreshnessAssessment, PinnedProduct
from gateway.contracts.manifest import FieldSchema, iso_date
from gateway.contracts.query import validate_query
from .domo_http import DomoProtocolResult, source_error, validate_domo_evidence
from .domo_runtime import check_domo_runtime_binding, validate_domo_decision


class DomoCandidate(StrictModel):
    """Private semantic candidate; never sufficient authority to release data."""
    rows: Array[dict[str, object]] = Field(repr=False)
    typed_schema: dict[str, FieldSchema]
    source_watermark: AwareTimestamp | None
    freshness: FreshnessAssessment
    cache_status: str
    response_sha256: Digest


def normalize_domo_result(binding, product, request, decision, wire, *, now):
    from .normalize import normalize_value
    try:
        p = PinnedProduct.model_validate(product.model_dump(mode='python'))
        b = check_domo_runtime_binding(binding, p.manifest, digest=p.binding_digest)
        d = validate_domo_decision(b, p.manifest, decision)
        q = validate_query(p.manifest, request)
        if q.version not in {None, p.manifest.version} or type(wire) is not DomoProtocolResult:
            raise ValueError
        wire = validate_domo_evidence(wire)
        now = timestamp(now)
        m = p.manifest
        bounds = m.date_boundaries
        start, end = q.inputs[bounds.start_input], q.inputs[bounds.end_input]
        if (iso_date(end)-iso_date(start)).days > d.limits.max_date_range_days:
            raise ValueError
        columns = tuple(c.source_name for c in b.columns)
        kinds = tuple(c.source_type for c in b.columns)
        if b.checkpoint:
            columns += (b.checkpoint.source_name,)
            kinds += ('STRING',)
        if (wire.dataset_id != b.dataset_id or wire.profile != 'domo-query-positional-v1'
            or wire.columns != columns or wire.source_types != kinds
            or wire.cache_status not in {'hit', 'miss'}):
            raise ValueError
        if len(wire.rows) > min(m.limits.max_rows, d.limits.max_rows):
            raise source_error(ErrorCode.RESOURCE_LIMIT)
        rows, grains = [], set()
        for raw in wire.rows:
            if len(raw) != len(columns):
                raise ValueError
            converted = {}
            for column, value in zip(b.columns, raw):
                if column.conversion in {'long-integer', 'long-decimal0'} and value is not None:
                    if type(value) is not int or not -(2**63) <= value < 2**63:
                        raise ValueError
                    if column.conversion == 'long-decimal0':
                        value = str(value)
                value = normalize_value(m.outputs[column.field], value)
                if column.field == b.time_field and not start <= value < end:
                    raise ValueError
                converted[column.field] = value
            if any(q.inputs[name] is not None and converted[field] != q.inputs[name]
                   for name, field in b.input_fields.items()):
                raise ValueError
            grain = tuple(converted[name] for name in b.grain_keys)
            if grain in grains:
                raise ValueError
            grains.add(grain)
            rows.append(converted)
        watermark = None
        age = None
        status = 'unknown'
        if b.checkpoint and wire.rows:
            checkpoints = {timestamp(row[-1]) for row in wire.rows if row[-1] is not None}
            if len(checkpoints) > 1 or any(checkpoint > now for checkpoint in checkpoints):
                raise ValueError
            if checkpoints and all(row[-1] is not None for row in wire.rows):
                watermark = checkpoints.pop()
                age = (now-watermark).total_seconds()
                status = 'fresh' if age <= m.freshness.max_age_seconds else 'stale'
        if m.freshness.required and status != 'fresh':
            raise source_error(ErrorCode.FRESHNESS_UNKNOWN if status == 'unknown' else ErrorCode.STALE_DATA)
        freshness = FreshnessAssessment(status=status, assessed_at=now, age_seconds=age,
            max_age_seconds=m.freshness.max_age_seconds)
        result = DomoCandidate(rows=rows, typed_schema=dict(m.outputs), source_watermark=watermark,
            freshness=freshness, cache_status=wire.cache_status, response_sha256=wire.response_sha256)
        if len(canonical_bytes(result)) > min(m.limits.max_bytes, d.limits.max_bytes):
            raise source_error(ErrorCode.RESOURCE_LIMIT)
        return result
    except GatewayError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise source_error() from None


def normalize_domo_envelope(product, completed, execution, *, context):
    """Reapply current authority and exact-response semantics at result commit."""
    from gateway.contracts.canonical import canonical_digest
    from gateway.contracts.execution import ExecutionSpec,ExecutionView,EffectiveRange,Provenance,ResultEnvelope
    from .domo_completed import DomoCompleted
    from .domo_runtime import validate_domo_prepared
    request_id=context.spec.request_id
    try:
        if type(completed) is not DomoCompleted:
            raise ValueError
        completed.evidence()
        completed.check_deadline()
        product=PinnedProduct.model_validate(product.model_dump(mode='python'))
        spec=ExecutionSpec.model_validate(context.spec.model_dump(mode='python'))
        view=ExecutionView.model_validate(execution.model_dump(mode='python'))
        b=check_domo_runtime_binding(context.binding,product.manifest,digest=product.binding_digest)
        executed=validate_domo_decision(b,product.manifest,context.executed_decision)
        current=validate_domo_decision(b,product.manifest,context.decision)
        if canonical_digest(current)!=canonical_digest(executed):
            raise source_error(ErrorCode.NOT_FOUND_OR_FORBIDDEN)
        now=timestamp(context.clock.now())
        if (canonical_digest(product)!=canonical_digest(spec.pinned_product)
            or view.state!='succeeded' or view.completed_at>now):
            raise ValueError
        validate_domo_prepared(completed.prepared,spec,executed,b)
        from gateway.contracts.query import QueryRequest
        request=QueryRequest.model_validate(spec.canonical_query)
        candidate=normalize_domo_result(b,product,request,current,completed.response,now=now)
        if completed.source_watermark!=candidate.source_watermark:
            raise ValueError
        m=product.manifest
        bounds=m.date_boundaries
        result=ResultEnvelope(**view.model_dump(mode='python'),product_id=m.product_id,version=m.version,
            product_digest=m.content_digest,typed_schema=candidate.typed_schema,rows=candidate.rows,units=m.units,
            grain=m.grain,effective_range=EffectiveRange(start=request.inputs[bounds.start_input],
                end=request.inputs[bounds.end_input],interval=bounds.interval),timezone=m.timezone,
            provenance=Provenance(binding_id=m.binding_id,binding_revision=m.binding_revision,
                adapter_revision=m.adapter_revision,model_bundle_digest=m.model_bundle_digest,links=[]),
            row_count=len(candidate.rows),truncated=False,source_watermark=candidate.source_watermark,
            freshness=candidate.freshness,cache_status=candidate.cache_status,
            warnings=[] if candidate.freshness.status=='fresh' else ['Source-load freshness is '+candidate.freshness.status+'.'],
            next_cursor=None)
        if len(canonical_bytes(result))>min(m.limits.max_bytes,current.limits.max_bytes):
            raise source_error(ErrorCode.RESOURCE_LIMIT)
        completed.check_deadline()
        return result
    except GatewayError as error:
        raise GatewayError(error.code,request_id,error.safe_message) from None
    except (ValueError,TypeError,AttributeError,KeyError):
        raise source_error(request_id=request_id) from None
