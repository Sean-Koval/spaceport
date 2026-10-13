"""Operator-only eligibility profile for the existing Cube REST adapter.

Nothing here compiles SQL, resolves Google credentials, or activates a source.
The deployed synthetic PostgreSQL bundle is not a BigQuery deployment. Source
cost and lifetime evidence for pinned Cube 1.8.5 is absent, so live execution and
production activation are fail-closed. These are internal configuration types,
not a new public connector registry or API schema.
"""
from decimal import Decimal
import re
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from gateway.contracts.canonical import AwareTimestamp, MAX_RESULT_BYTES, StrictModel, json_value, timestamp
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.manifest import FieldSchema, iso_date, validate_value
from .base import SourceCapabilities
from .cube import CubeAdapter

ProjectId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")]
DatasetId = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")]
Location = Annotated[str, Field(pattern=r"^(?:US|EU|[a-z]+(?:-[a-z]+)+[1-9][0-9]?)$", max_length=64)]
SecretRef = Annotated[str, Field(pattern=r"^/run/secrets/[A-Za-z][A-Za-z0-9_.-]{0,127}$")]


def _invalid(message="The operator BigQuery profile is invalid."):
    return GatewayError(ErrorCode.INVALID_INPUT, "bigquery-profile", message)


class SourceSettings(StrictModel):
    """Reviewed operator settings; only a mounted secret reference, never a key.

    dataset_region is an operator-owned expected location, not live discovery.
    A requested maximum_bytes_billed records intent only: this driver does not
    propagate it on its normal query path. No setting can assert it is proven.
    """
    project_id: ProjectId
    dataset_id: DatasetId
    region: Location
    dataset_region: Location
    secret_ref: SecretRef
    cube_version: Literal["1.8.5"] = "1.8.5"
    read_only: Literal[True] = True
    pre_aggregations: Literal[False] = False
    export_bucket: None = None
    max_rows: int = Field(default=1000, ge=1, le=1000)
    max_bytes: int = Field(default=MAX_RESULT_BYTES, ge=1, le=MAX_RESULT_BYTES)
    maximum_bytes_billed: int | None = Field(default=None, gt=0, le=2**63-1)

    @field_validator("read_only", "pre_aggregations", mode="before")
    @classmethod
    def exact_bool(cls, value):
        if type(value) is not bool:
            raise ValueError("Expected an explicit boolean")
        return value

    @field_validator("secret_ref")
    @classmethod
    def mounted_reference(cls, value):
        if ".." in value:
            raise ValueError("Invalid mounted reference")
        return value

    @model_validator(mode="after")
    def matching_region(self):
        if self.region != self.dataset_region:
            raise ValueError("The query and reviewed dataset locations must match")
        return self


class BigQuerySourceCapabilities(SourceCapabilities):
    """Profile facts only; a caller cannot turn configured intent into proof."""
    production_enabled: Literal[False] = False
    max_bytes: int = Field(default=MAX_RESULT_BYTES, ge=1, le=MAX_RESULT_BYTES)
    source_cost_bound: Literal["unproven"] = "unproven"
    proven_statement_timeout_seconds: None = None
    statement_timeout_evidence: None = None
    proven_upstream_lifetime_seconds: None = None
    upstream_lifetime_evidence: None = None


class BigQueryCubeProfile:
    """Eligibility validation behind Cube, with no executable serving methods."""
    @staticmethod
    def validate(settings: SourceSettings) -> SourceCapabilities:
        try:
            settings = SourceSettings.model_validate(settings)
        except (ValidationError, ValueError, TypeError):
            raise _invalid() from None
        return BigQuerySourceCapabilities(max_rows=settings.max_rows, max_bytes=settings.max_bytes)

    @staticmethod
    def validate_source_type(source_type: str) -> FieldSchema:
        """Only already-declared gateway scalar types; no generic compiler.

        Full BIGNUMERIC/BIGDECIMAL(76,38), civil DATETIME, arrays/structs, FLOAT64
        and INT64 bindings are unavailable in this reviewed adapter revision.
        A narrower projection needs its own reviewed binding, not a flag here.
        """
        if type(source_type) is not str:
            raise _invalid("The BigQuery source type is unsupported by the reviewed binding.")
        if source_type in {"NUMERIC", "DECIMAL"}:
            return FieldSchema(type="decimal", precision=38, scale=9)
        if source_type == "DATE":
            return FieldSchema(type="date")
        if source_type == "STRING":
            return FieldSchema(type="string")
        raise _invalid("The BigQuery source type is unsupported by the reviewed binding.")


def select_adapter(source: str, *, settings: SourceSettings | None = None,
                   production: bool = False) -> type[CubeAdapter] | type[BigQueryCubeProfile]:
    """Internal eligibility selection, never a declaration of live compatibility.

    PostgreSQL retains its existing adapter. Explicit BigQuery settings select
    only this non-executable profile. Domo and unreviewed sources are unavailable.
    A later deployment factory must independently validate retained BigQuery
    driver/model/binding assets and production gates before making an adapter.
    """
    if type(production) is not bool:
        raise _invalid("The requested source profile is unavailable.")
    if source == "postgresql" and settings is None:
        return CubeAdapter
    if source == "bigquery" and settings is not None:
        BigQueryCubeProfile.validate(settings)
        if not production:
            return BigQueryCubeProfile
    raise _invalid("The requested source profile is unavailable.")


def convert_numeric(value, *, precision: int = 38, scale: int = 9,
                    nullable: bool = False) -> str | None:
    """Validate exact plain decimal text without padding, rounding or float casts."""
    try:
        schema = FieldSchema(type="decimal", precision=precision, scale=scale, nullable=nullable)
        validate_value(schema, value)
        return value
    except (ValidationError, ValueError, TypeError):
        raise _invalid("The BigQuery decimal exceeds its declared exact contract.") from None


def _validate_timestamp_offset(value):
    """Reject overflow offset components before the shared parser normalizes them."""
    if isinstance(value, str):
        offset = re.search(r"[+-]([0-9]{2}):([0-9]{2})\Z", value)
        if offset and (int(offset[1]) > 23 or int(offset[2]) > 59):
            raise ValueError("Invalid RFC3339 numeric offset")
    return value


def convert_timestamp(value, *, nullable: bool = False) -> str | None:
    """Absolute RFC3339 instant, microsecond exact; civil DATETIME is rejected.

    This scalar contract helper does not broaden CubeBinding's daily DATE output.
    """
    try:
        _validate_timestamp_offset(value)
        schema = FieldSchema(type="timestamp", nullable=nullable)
        validate_value(schema, value)
        return None if value is None else json_value(timestamp(value))
    except (ValidationError, ValueError, TypeError, OverflowError):
        raise _invalid("The BigQuery timestamp requires a valid explicit instant.") from None


def convert_date(value, *, nullable: bool = False) -> str | None:
    try:
        schema = FieldSchema(type="date", nullable=nullable)
        validate_value(schema, value)
        if value is not None:
            iso_date(value)
        return value
    except (ValidationError, ValueError, TypeError, OverflowError):
        raise _invalid("The BigQuery date requires a valid calendar date.") from None


class LiveSmokeAuthorization(StrictModel):
    """A separately approved one-shot scope, never inferred from settings/env opt-in."""
    authorization_id: str = Field(min_length=1, max_length=128)
    operation: Literal["one-read-only-cube-smoke"]
    project_id: ProjectId
    dataset_id: DatasetId
    region: Location
    secret_ref: SecretRef
    scoped_credentials: Literal[True]
    scope_evidence: str = Field(min_length=1, max_length=2048)
    spend_ceiling: str = Field(min_length=1, max_length=64)
    currency: Literal["USD"]
    expires_at: AwareTimestamp

    @field_validator("expires_at", mode="before")
    @classmethod
    def valid_expiry_offset(cls, value):
        return _validate_timestamp_offset(value)

    @field_validator("authorization_id", "scope_evidence")
    @classmethod
    def nonblank_evidence(cls, value):
        if not value.strip():
            raise ValueError("Explicit authorization evidence is required")
        return value

    @field_validator("scoped_credentials", mode="before")
    @classmethod
    def exact_scope(cls, value):
        if value is not True:
            raise ValueError("Scoped credential review is required")
        return value

    @field_validator("spend_ceiling")
    @classmethod
    def positive_exact_ceiling(cls, value):
        validate_value(FieldSchema(type="decimal", precision=38, scale=9), value)
        if Decimal(value) <= 0:
            raise ValueError("An explicit positive spend ceiling is required")
        return value


def validate_live_authorization(settings: SourceSettings, authorization: LiveSmokeAuthorization,
                                *, now) -> LiveSmokeAuthorization:
    """Pure authorization/configuration checks; no credential access or I/O."""
    try:
        settings = SourceSettings.model_validate(settings)
        authorization = LiveSmokeAuthorization.model_validate(authorization)
        if ((settings.project_id, settings.dataset_id, settings.region, settings.secret_ref) !=
            (authorization.project_id, authorization.dataset_id, authorization.region, authorization.secret_ref)
            or authorization.expires_at <= timestamp(now)):
            raise ValueError
        return authorization
    except (ValidationError, ValueError, TypeError, OverflowError):
        raise _invalid("Separate scoped, current BigQuery smoke authorization is required.") from None


def require_live_source_bounds(settings: SourceSettings) -> None:
    """Fail before secret resolution or network I/O, including with full approval.

    The selected driver/profile has no proven enforcement of approved spend/time bounds.
    Do not bypass Cube, infer a cap from SDK support, or treat cancellation intent
    as a proven upstream lifetime. Replacing this gate requires reviewed evidence.
    """
    BigQueryCubeProfile.validate(settings)
    raise GatewayError(ErrorCode.RESOURCE_LIMIT, "bigquery-live-gate",
        "Live BigQuery is blocked until source cost and lifetime enforcement are proven.")
