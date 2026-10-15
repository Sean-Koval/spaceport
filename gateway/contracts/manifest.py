"""Strict, inert product definitions and pure manifest validation."""
from datetime import date
from decimal import Decimal
import math
import re
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, field_validator, model_validator

from .canonical import Array, Digest, Identifier, MAX_RESULT_BYTES, Scalar, StrictModel, Version, bounded_json, manifest_digest, timestamp
from .errors import ErrorCode, GatewayError, invalid_input

FieldType = Literal["string", "integer", "number", "boolean", "date", "timestamp", "decimal"]
Operator = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in", "is_null"]
# Signed access URLs are bearer capabilities, not harmless provenance links.
# Query keys are decoded once by parse_qsl and compared case-insensitively.
SIGNED_ACCESS_QUERY_KEYS = frozenset({
    "x-goog-algorithm", "x-goog-credential", "x-goog-date", "x-goog-expires",
    "x-goog-signedheaders", "x-goog-signature", "googleaccessid", "signature",
    "awsaccesskeyid", "x-amz-algorithm", "x-amz-credential", "x-amz-date",
    "x-amz-expires", "x-amz-signedheaders", "x-amz-signature", "x-amz-security-token",
    "sig",
})
SENSITIVE_ARGUMENTS = {"claimed_user_id", "user_id", "principal_id", "subject", "tenant_id", "roles", "entitlements", "sql", "script", "source_url", "credentials", "password", "api_key", "access_token", "policy_override"}


class Limits(StrictModel):
    max_rows: int = Field(default=1000, ge=1, le=1000)
    max_bytes: int = Field(default=MAX_RESULT_BYTES, ge=1, le=MAX_RESULT_BYTES)
    page_size: int = Field(default=200, ge=1, le=200)
    per_principal_concurrency: int = Field(default=2, ge=1, le=2)
    deployment_concurrency: int = Field(default=10, ge=1, le=10)
    max_date_range_days: int = Field(default=366, ge=1, le=366)
    sync_wait_seconds: int = Field(default=5, ge=0, le=5)
    execution_timeout_seconds: int = Field(default=120, ge=1, le=120)

    @model_validator(mode="after")
    def coherent_limits(self):
        if self.page_size > self.max_rows:
            raise ValueError("Page size cannot exceed execution row limit")
        if self.per_principal_concurrency > self.deployment_concurrency:
            raise ValueError("Principal capacity cannot exceed deployment capacity")
        if self.sync_wait_seconds > self.execution_timeout_seconds:
            raise ValueError("Synchronous wait cannot exceed execution timeout")
        return self


def reject_secret(value):
    if not isinstance(value, str):
        return
    if (re.search(r"-----BEGIN .*PRIVATE KEY-----", value)
            or value.startswith(("sk-", "ghp_", "github_pat_", "AKIA", "secret://", "env:", "postgres://", "postgresql://"))
            or re.fullmatch(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value)):
        raise ValueError("Credentials and secret references do not belong in product values")


def iso_date(value) -> date:
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected YYYY-MM-DD")
    return date.fromisoformat(value)


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Expected a known IANA timezone") from None
    return value


class DefaultValue(StrictModel):
    value: Scalar

    @field_validator("value", mode="before")
    @classmethod
    def bounded_default(cls, value): return bounded_json(value)


class FieldSchema(StrictModel):
    type: FieldType
    nullable: bool = False
    required: bool = True
    enum: Array[Scalar] | None = Field(default=None, min_length=1, max_length=100)
    default: DefaultValue | None = None
    precision: int | None = Field(default=None, ge=1, le=38)
    scale: int | None = Field(default=None, ge=0, le=38)

    @field_validator("enum", mode="before")
    @classmethod
    def bounded_enum(cls, value):
        return bounded_json(value, max_nodes=101)

    @model_validator(mode="after")
    def valid_definition(self):
        if self.type == "decimal":
            if self.precision is None or self.scale is None or self.scale > self.precision:
                raise ValueError("Decimal fields require valid precision and scale")
        elif self.precision is not None or self.scale is not None:
            raise ValueError("Precision and scale apply only to decimal fields")
        if self.enum is not None:
            encoded = []
            for value in self.enum:
                validate_value(self, value, check_enum=False)
                token = (type(value).__name__, str(value))
                if token in encoded: raise ValueError("Enum values must be distinct")
                encoded.append(token)
        if self.default is not None:
            validate_value(self, self.default.value)
        return self


def validate_value(schema: FieldSchema, value: Scalar, *, check_enum: bool = True) -> Scalar:
    """Validate a scalar without turning decimal strings into binary floats."""
    reject_secret(value)
    if value is None:
        if not schema.nullable: raise ValueError("This field is not nullable")
        return value
    if schema.type in ("string", "date", "timestamp", "decimal"):
        if type(value) is not str: raise ValueError("Expected a string")
        if schema.type == "date": iso_date(value)
        elif schema.type == "timestamp": timestamp(value)
        elif schema.type == "decimal":
            if not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
                raise ValueError("Expected a lossless decimal string")
            amount = Decimal(value)
            fractional = max(-amount.as_tuple().exponent, 0)
            whole = 0 if amount.is_zero() else max(amount.adjusted() + 1, 0)
            if fractional > schema.scale or whole > schema.precision - schema.scale:
                raise ValueError("Decimal exceeds its declared precision or scale")
    elif schema.type == "integer":
        if type(value) is not int or abs(value) > 2**53 - 1: raise ValueError("Expected a canonical JSON integer")
    elif schema.type == "number":
        if type(value) not in (int, float) or (type(value) is int and abs(value) > 2**53 - 1) or not math.isfinite(value):
            raise ValueError("Expected a finite JSON number")
    elif schema.type == "boolean":
        if type(value) is not bool: raise ValueError("Expected a boolean")
    if check_enum and schema.enum is not None and not any(type(value) is type(choice) and value == choice for choice in schema.enum):
        raise ValueError("Value is outside the declared enum")
    return value


class Link(StrictModel):
    label: Annotated[str, Field(min_length=1, max_length=256)]
    url: Annotated[str, Field(min_length=1, max_length=2048)]

    @field_validator("url")
    @classmethod
    def safe_link(cls, value):
        parsed = urlsplit(value)
        # These remain inert metadata. This does not make them safe to fetch.
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or any(ch.isspace() or ord(ch) < 32 for ch in value):
            raise ValueError("Metadata links must be credential-free HTTPS URLs")
        try: parsed.port
        except ValueError: raise ValueError("Invalid metadata URL") from None
        if any(key.lower() in SIGNED_ACCESS_QUERY_KEYS | {"password", "token", "access_token", "api_key", "secret", "authorization"} for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            raise ValueError("Metadata URLs cannot contain credentials")
        return value


class Example(StrictModel):
    description: Annotated[str, Field(min_length=1, max_length=4096)]
    inputs: Annotated[dict[Identifier, Scalar], Field(max_length=50)]

    @field_validator("inputs", mode="before")
    @classmethod
    def bounded_inputs(cls, value): return bounded_json(value)


class FilterDefinition(StrictModel):
    operators: Array[Operator] = Field(min_length=1, max_length=9)
    max_values: int = Field(default=100, ge=1, le=100)

    @field_validator("operators")
    @classmethod
    def distinct(cls, value):
        if len(set(value)) != len(value): raise ValueError("Filter operators must be distinct")
        return value


class SortDefinition(StrictModel):
    fields: Array[Identifier] = Field(default=(), max_length=50)
    max_fields: int = Field(default=5, ge=0, le=5)

    @field_validator("fields")
    @classmethod
    def distinct(cls, value):
        if len(set(value)) != len(value): raise ValueError("Sort fields must be distinct")
        return value


class Unit(StrictModel):
    unit: Annotated[str, Field(min_length=1, max_length=128)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None


class DateBoundaries(StrictModel):
    start_input: Identifier
    end_input: Identifier
    interval: Literal["[start,end)"]


class FreshnessRequirement(StrictModel):
    required: bool
    max_age_seconds: int = Field(ge=1, le=31_536_000)
    meaning: Literal["source_load"]


class Fixture(StrictModel):
    id: Identifier
    version: Identifier
    inputs: Annotated[dict[Identifier, Scalar], Field(max_length=50)]
    expected_rows: Array[dict[Identifier, Scalar]] = Field(default=(), max_length=1000)
    expected_error: Annotated[str, Field(pattern="^(" + "|".join(code.value for code in ErrorCode) + ")$")] | None = None
    tolerance: Annotated[str, Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")] = "0"
    source_watermark: Annotated[str, Field(max_length=40)] | None = None

    @field_validator("inputs", "expected_rows", mode="before")
    @classmethod
    def bounded_values(cls, value): return bounded_json(value, max_nodes=10000)

    @field_validator("inputs")
    @classmethod
    def no_credential_inputs(cls, value):
        # Negative fixtures waive query validity, never credential prohibition.
        for item in value.values():
            reject_secret(item)
        return value

    @field_validator("source_watermark")
    @classmethod
    def valid_watermark(cls, value):
        if value is not None: timestamp(value)
        return value

    @model_validator(mode="after")
    def consistent_expectation(self):
        if self.expected_error is not None and self.expected_rows:
            raise ValueError("An error fixture cannot also expect rows")
        return self


class Manifest(StrictModel):
    schema_version: Literal["1.0"]
    product_id: Identifier
    version: Version
    name: Annotated[str, Field(min_length=1, max_length=256)]
    description: Annotated[str, Field(min_length=1, max_length=16384)]
    examples: Array[Example] = Field(max_length=20)
    owner: Identifier
    domain: Identifier
    links: Array[Link] = Field(max_length=20)
    inputs: Annotated[dict[Identifier, FieldSchema], Field(min_length=1, max_length=50)]
    outputs: Annotated[dict[Identifier, FieldSchema], Field(min_length=1, max_length=50)]
    filters: Annotated[dict[Identifier, FilterDefinition], Field(max_length=50)]
    sort: SortDefinition
    grain: Annotated[str, Field(min_length=1, max_length=256)]
    units: Annotated[dict[Identifier, Unit], Field(max_length=50)]
    timezone: Annotated[str, Field(min_length=1, max_length=128)]
    date_boundaries: DateBoundaries | None
    aggregation: Annotated[str, Field(min_length=1, max_length=4096)]
    freshness: FreshnessRequirement
    binding_id: Identifier
    binding_revision: int = Field(ge=1)
    adapter_revision: Identifier
    policy_id: Identifier
    classification: Literal["public", "internal", "confidential", "restricted"]
    redaction: Annotated[dict[Identifier, Literal["omit", "digest"]], Field(max_length=50)]
    fixtures: Array[Fixture] = Field(min_length=1, max_length=100)
    content_digest: Digest | None = None
    model_bundle_digest: Digest
    limits: Limits = Field(default_factory=Limits)

    _timezone = field_validator("timezone")(validate_timezone)

    @field_validator("binding_id", "adapter_revision", "policy_id", "owner", "domain")
    @classmethod
    def no_secret_identifiers(cls, value):
        reject_secret(value)
        return value

    @model_validator(mode="after")
    def coherent_contract(self):
        if set(self.inputs) & SENSITIVE_ARGUMENTS:
            raise ValueError("Identity, credentials and source controls are never product inputs")
        if set(self.filters) - set(self.outputs) or set(self.sort.fields) - set(self.outputs) or set(self.units) - set(self.outputs):
            raise ValueError("Filters, sorting and units must name declared output fields")
        if set(self.redaction) != set(self.inputs):
            raise ValueError("Every input requires exactly one redaction rule")
        for name, definition in self.filters.items():
            field = self.outputs[name]
            allowed = {"eq", "ne", "in", "not_in"}
            if field.nullable: allowed.add("is_null")
            if field.type in ("integer", "number", "decimal", "date", "timestamp"):
                allowed |= {"lt", "lte", "gt", "gte"}
            if set(definition.operators) - allowed:
                raise ValueError("Operator is unsupported for this field type")
        if self.date_boundaries is not None:
            bounds = self.date_boundaries
            if bounds.start_input == bounds.end_input:
                raise ValueError("Date boundary inputs must be distinct")
            for name in (bounds.start_input, bounds.end_input):
                if name not in self.inputs or self.inputs[name].type != "date" or self.inputs[name].nullable or not self.inputs[name].required:
                    raise ValueError("Date boundaries require two nonnullable required date inputs")
        elif any(field.type == "date" for field in self.inputs.values()):
            raise ValueError("Date-based products must declare half-open date boundaries")
        from .query import QueryRequest, validate_query
        for example in self.examples:
            validate_query(self, QueryRequest(product_id=self.product_id, version=self.version, inputs=example.inputs))
        seen = set()
        for fixture in self.fixtures:
            if fixture.id in seen: raise ValueError("Fixture IDs must be unique")
            seen.add(fixture.id)
            # Negative fixtures intentionally exercise invalid input handling.
            if fixture.expected_error != "INVALID_INPUT":
                validate_query(self, QueryRequest(product_id=self.product_id, version=self.version, inputs=fixture.inputs))
            for row in fixture.expected_rows:
                validate_row(self.outputs, row)
        digest = manifest_digest(self)
        if self.content_digest is not None and self.content_digest != digest:
            raise ValueError("Content digest does not match the canonical manifest")
        object.__setattr__(self, "content_digest", digest)
        return self


def validate_row(schema: dict[str, FieldSchema], row: dict) -> None:
    if set(row) != set(schema): raise ValueError("Rows must contain exactly the declared columns")
    for name, field in schema.items(): validate_value(field, row[name])


def validate_manifest(raw: dict) -> Manifest:
    try:
        if not isinstance(raw, dict): raise ValueError("Expected a manifest object")
        return Manifest.model_validate(raw)
    except (ValidationError, ValueError, TypeError, GatewayError, RecursionError):
        raise invalid_input("The product manifest is invalid.") from None
