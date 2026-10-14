"""Strict public projections and requests, separate from execution authority."""
from typing import Annotated, Literal
from pydantic import Field, JsonValue
from gateway.contracts.canonical import Array, AwareTimestamp, Digest, Identifier, StrictModel, Version, Scalar
from gateway.contracts.execution import ErrorView, Provenance
from gateway.contracts.manifest import (Manifest, FieldSchema, FilterDefinition, SortDefinition, Unit,
    DateBoundaries, FreshnessRequirement, Example, Link, Limits)
from gateway.policy.service import PolicyRules


class ProductDescription(StrictModel):
    product_id: Identifier
    version: Version
    digest: Digest
    name: str
    status: Literal['published','deprecated']
    description: str | None = None
    owner: Identifier | None = None
    domain: Identifier | None = None
    examples: Array[Example] = ()
    links: Array[Link] = ()
    inputs: dict[Identifier, FieldSchema]
    outputs: dict[Identifier, FieldSchema]
    filters: dict[Identifier, FilterDefinition]
    sort: SortDefinition
    units: dict[Identifier, Unit]
    grain: str | None = None
    aggregation: str | None = None
    timezone: str
    date_boundaries: DateBoundaries | None
    freshness: FreshnessRequirement
    provenance: Provenance
    partial: bool
    limits: Limits


class CatalogPage(StrictModel):
    items: Array[ProductDescription] = Field(max_length=50)
    next_cursor: str | None = None


class DraftSummary(StrictModel):
    """Minimal authorized saved-head metadata; never a manifest or test verdict."""
    id: Identifier
    product_id: Identifier
    version: Version
    name: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    editable_full_definition: bool = Field(description='Current full-definition author capability hint; edits recheck authority and revision.')
    can_test: bool = Field(description='Current author capability hint only; not evidence readiness or authorization to run tests.')
    can_publish: bool = Field(description='Current base and target publisher capability hint only; not evidence readiness or publication authorization.')


class DraftPage(StrictModel):
    items: Array[DraftSummary] = Field(max_length=50)
    next_cursor: str | None = None


class BindingChoice(StrictModel):
    binding_id: Identifier
    revision: int = Field(ge=1)
    digest: Digest
    adapter_revision: Identifier
    model_bundle_digest: Digest


class PolicyChoice(StrictModel):
    policy_id: Identifier
    revision: int = Field(ge=1)


class BindingChoicePage(StrictModel):
    items: Array[BindingChoice] = Field(max_length=50)
    next_cursor: str | None = None


class PolicyChoicePage(StrictModel):
    items: Array[PolicyChoice] = Field(max_length=50)
    next_cursor: str | None = None


class AuditEntry(StrictModel):
    id: Identifier
    created_at: AwareTimestamp
    actor_id: Identifier
    service_client_id: Identifier | None
    action: str
    product_id: Identifier | None
    version_id: Identifier | None
    policy_id: Identifier
    policy_revision: int | None
    decision: str
    parameter_names: Array[str]
    parameter_digest: Digest | None
    duration_ms: int | None
    row_count: int | None
    adapter: str | None
    correlation_id: Identifier


class AuditPage(StrictModel):
    items: Array[AuditEntry] = Field(max_length=50)
    next_cursor: str | None = None


class DraftWrite(StrictModel):
    manifest: Manifest
    expected_revision: int = Field(ge=0)


class DraftReceipt(StrictModel):
    id: Identifier
    product_id: Identifier
    revision: int = Field(ge=1)
    digest: Digest


class FixtureCheckView(StrictModel):
    fixture_id: Identifier
    kind: Literal['known_answer','input_negative','permission_negative']
    passed: bool
    expected: Array[dict[Identifier, Scalar]] = ()
    actual: Array[dict[Identifier, Scalar]] = ()
    expected_error: str | None = None
    actual_error: str | None = None
    absolute_tolerance: str | None = None
    relative_tolerance: str | None = None
    reference_watermark: AwareTimestamp | None = None
    observed_source_load_watermark: AwareTimestamp | None = None
    freshness_status: Literal['fresh','stale','unknown'] | None = None


class TestRunView(StrictModel):
    id: Identifier
    draft_id: Identifier
    draft_digest: Digest
    binding_digest: Digest
    policy_id: Identifier
    policy_revision: int = Field(ge=1)
    fixture_version: Identifier
    status: Literal['passed','failed']
    created_at: AwareTimestamp
    checks: Array[FixtureCheckView] = Field(min_length=1,max_length=100)


Reason = Annotated[str, Field(min_length=1,max_length=1024)]


class PublishRequest(StrictModel):
    tested_digest: Digest
    expected_revision: int = Field(ge=1)
    reason: Reason


class PublicationReceipt(StrictModel):
    product_id: Identifier
    version: Version
    version_id: Identifier
    digest: Digest


class LifecycleRequest(StrictModel):
    version: Version
    action: Literal['deprecate','withdraw','rollback']
    reason: Reason


class PolicyWrite(StrictModel):
    rules: PolicyRules
    reason: Reason


class PolicyReceipt(StrictModel):
    policy_id: Identifier
    revision: int = Field(ge=1)


class Changed(StrictModel):
    changed: Literal[True] = True


class ReadinessView(StrictModel):
    ready: bool


class LivenessView(StrictModel):
    alive: Literal[True] = True
