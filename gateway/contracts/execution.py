"""Internal adapter/execution types and lossless normalized result envelopes."""
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_serializer, field_validator, model_validator

from .canonical import Array, AwareTimestamp, Digest, Identifier, MAX_RESULT_BYTES, Scalar, StrictModel, Version, bounded_json, canonical_bytes, canonical_digest, json_value
from .errors import ErrorCode, GatewayError
from .manifest import FieldSchema, Link, Manifest, Unit, iso_date, validate_row, validate_timezone
from .query import QueryRequest, validate_query

CacheStatus = Literal["hit", "miss", "unknown"]
ExecutionState = Literal["accepted", "running", "succeeded", "failed", "timed_out", "expired"]


class PinnedProduct(StrictModel):
    manifest: Manifest
    binding_digest: Digest
    version_id: Identifier


class ExecutionSpec(StrictModel):
    """Created by the service after resolution/authorization, never by clients."""
    principal_id: Identifier
    pinned_product: PinnedProduct
    canonical_query: dict[str, JsonValue]
    canonical_hash: Digest
    policy_revision: int = Field(ge=1)
    context: Literal["consumer", "fixture_test"]
    request_id: Identifier

    @field_validator("canonical_query", mode="before")
    @classmethod
    def bounded_query(cls, value): return bounded_json(value)

    @field_serializer("canonical_query")
    def serialize_query(self, value):
        return json_value(value)

    @model_validator(mode="after")
    def pinned_query_and_hash(self):
        try:
            m = self.pinned_product.manifest
            q = validate_query(m, QueryRequest.model_validate(self.canonical_query))
            if q.version != m.version or q.idempotency_key is not None:
                raise ValueError("An internal execution requires a pinned version and no idempotency key")
            normalized = q.model_dump(mode="json", exclude={"idempotency_key"})
            if canonical_digest(self.canonical_query) != canonical_digest(normalized):
                raise ValueError("Internal execution inputs must already be normalized")
            if self.canonical_hash != canonical_digest(self.canonical_query):
                raise ValueError("Canonical execution hash is inconsistent")
        except GatewayError:
            raise ValueError("The internal execution query is invalid") from None
        return self


class PreparedQuery(StrictModel):
    endpoint_id: Identifier
    model_bundle_digest: Digest
    cube_query: dict[str, JsonValue]

    @field_validator("cube_query", mode="before")
    @classmethod
    def bounded_query(cls, value):
        bounded_json(value)
        if not isinstance(value, dict) or set(value) - {"measures", "dimensions", "timeDimensions", "filters", "order", "limit", "timezone"}:
            raise ValueError("Unsupported Cube query shape")
        return value


    @field_serializer("cube_query")
    def serialize_query(self, value):
        return json_value(value)


class Pending(StrictModel):
    retry_after_seconds: float = Field(gt=0, le=30)


class Completed(StrictModel):
    raw_rows: Array[dict[str, JsonValue]] = Field(max_length=10000)
    source_watermark: AwareTimestamp | None
    cache_status: CacheStatus = "unknown"


    @field_serializer("raw_rows")
    def serialize_rows(self, value):
        return json_value(value)


AdapterPoll = Pending | Completed


class ErrorView(StrictModel):
    code: Annotated[str, Field(pattern="^(" + "|".join(code.value for code in ErrorCode) + ")$")]
    request_id: Identifier
    safe_message: Annotated[str, Field(min_length=1, max_length=1024)]


class ExecutionView(StrictModel):
    execution_id: Identifier
    state: ExecutionState
    accepted_at: AwareTimestamp
    started_at: AwareTimestamp | None = None
    completed_at: AwareTimestamp | None = None
    error: ErrorView | None = None

    @model_validator(mode="after")
    def coherent_state(self):
        if self.started_at is not None and self.started_at < self.accepted_at:
            raise ValueError("Execution cannot start before acceptance")
        if self.completed_at is not None and self.completed_at < (self.started_at or self.accepted_at):
            raise ValueError("Execution cannot complete before it starts")
        if self.state in ("accepted", "running") and self.completed_at is not None:
            raise ValueError("A pending execution cannot be completed")
        if self.state == "running" and self.started_at is None:
            raise ValueError("A running execution requires a start time")
        if self.state in ("succeeded", "failed", "timed_out") and self.completed_at is None:
            raise ValueError("A terminal execution requires a completion time")
        if self.state in ("failed", "timed_out") and self.error is None:
            raise ValueError("An unsuccessful terminal execution requires a safe error")
        if self.state in ("accepted", "running", "succeeded") and self.error is not None:
            raise ValueError("Only an unsuccessful execution carries an error")
        return self


class EffectiveRange(StrictModel):
    start: str
    end: str
    interval: Literal["[start,end)"]

    @model_validator(mode="after")
    def valid_range(self):
        if not 0 < (iso_date(self.end) - iso_date(self.start)).days <= 366:
            raise ValueError("Invalid effective date range")
        return self


class Provenance(StrictModel):
    binding_id: Identifier
    binding_revision: int = Field(ge=1)
    adapter_revision: Identifier
    model_bundle_digest: Digest
    links: Array[Link] = Field(default=(), max_length=20)


class FreshnessAssessment(StrictModel):
    status: Literal["fresh", "unknown", "stale"]
    assessed_at: AwareTimestamp
    age_seconds: float | None = Field(default=None, ge=0)
    max_age_seconds: int = Field(ge=1, le=31_536_000)

    @model_validator(mode="after")
    def consistent_status(self):
        if self.status == "unknown":
            if self.age_seconds is not None: raise ValueError("Unknown freshness cannot claim a proven age")
        elif self.age_seconds is None:
            raise ValueError("Proven freshness requires a source age")
        elif (self.age_seconds <= self.max_age_seconds) != (self.status == "fresh"):
            raise ValueError("Freshness status contradicts its source age")
        return self


class ResultEnvelope(ExecutionView):
    product_id: Identifier
    version: Version
    product_digest: Digest
    typed_schema: Annotated[dict[Identifier, FieldSchema], Field(min_length=1, max_length=50)]
    rows: Array[dict[Identifier, Scalar]] = Field(default=(), max_length=1000)
    units: Annotated[dict[Identifier, Unit], Field(max_length=50)]
    grain: Annotated[str, Field(min_length=1, max_length=256)]
    effective_range: EffectiveRange | None
    timezone: Annotated[str, Field(min_length=1, max_length=128)]
    provenance: Provenance
    row_count: int = Field(ge=0, le=1000)
    truncated: Literal[False] = False
    source_watermark: AwareTimestamp | None
    freshness: FreshnessAssessment | None
    cache_status: CacheStatus = "unknown"
    warnings: Array[Annotated[str, Field(min_length=1, max_length=1024)]] = Field(default=(), max_length=20)
    next_cursor: Annotated[str, Field(min_length=1, max_length=2048)] | None = None

    _timezone = field_validator("timezone")(validate_timezone)

    @field_validator("truncated", mode="before")
    @classmethod
    def exact_false(cls, value):
        if value is not False:
            raise ValueError("Truncation must be the boolean false")
        return value

    @model_validator(mode="after")
    def lossless_complete_result(self):
        if set(self.units) - set(self.typed_schema):
            raise ValueError("Units must name declared result fields")
        for row in self.rows: validate_row(self.typed_schema, row)
        if self.row_count < len(self.rows):
            raise ValueError("Total row count cannot be smaller than the returned page")
        if self.state != "succeeded" and (self.rows or self.next_cursor is not None or self.row_count):
            raise ValueError("Unsuccessful or expired results contain no rows or cursor")
        if self.state == "succeeded" and self.freshness is None:
            raise ValueError("Successful results require a freshness assessment")
        if self.freshness is not None and self.freshness.status != "unknown":
            if self.source_watermark is None:
                raise ValueError("Source freshness requires a recorded watermark")
            age = (self.freshness.assessed_at - self.source_watermark).total_seconds()
            if age < 0 or age != self.freshness.age_seconds:
                raise ValueError("Freshness age must agree with its recorded evidence")
        if len(canonical_bytes(self.model_dump(mode="python"))) > MAX_RESULT_BYTES:
            raise ValueError("Complete normalized result exceeds 1,048,576 UTF-8 bytes")
        return self
