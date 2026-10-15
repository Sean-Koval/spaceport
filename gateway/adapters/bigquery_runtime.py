"""Explicit serving-only BigQuery source descriptors; parsing performs no I/O."""
from typing import Annotated, Literal
from urllib.parse import urlsplit
import re

from pydantic import Field, field_validator, model_validator

from gateway.auth.settings import trusted_https_url
from gateway.contracts.canonical import Array, Digest, Identifier, StrictModel, canonical_digest
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import Manifest
from gateway.contracts.execution import Completed
from .base import CubeMember, MemberBinding, validate_query_visibility
from .bigquery_types import DatasetId, Location, ProjectId

TableId = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")]
ServiceAccountEmail = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$", max_length=254)]


class BigQueryWorkloadIdentity(StrictModel):
    mode: Literal["gke-workload-identity"]
    expected_principal: ServiceAccountEmail


class BigQuerySource(StrictModel):
    provider: Literal["bigquery"] = "bigquery"
    source_id: Identifier
    revision: int = Field(ge=1, le=2**31 - 1)
    context: Literal["consumer", "fixture_test"]
    auth: BigQueryWorkloadIdentity
    billing_project: ProjectId
    data_project: ProjectId
    dataset: DatasetId
    tables: Array[TableId] = Field(min_length=1, max_length=50)
    location: Location
    dataset_location: Location
    cube_endpoint: str = Field(max_length=2048)
    model_bundle_digest: Digest
    source_policy_digest: Digest
    max_jobs_per_execution: int = Field(ge=1, le=8)
    per_job_maximum_bytes_billed: int = Field(ge=1, le=2**53 - 1)
    total_execution_bytes: int = Field(ge=1, le=2**53 - 1)
    max_active_jobs: int = Field(ge=1, le=100)
    max_global_active_jobs: int = Field(ge=1, le=100)
    job_timeout_ms: int = Field(ge=1, le=120000)

    @field_validator("cube_endpoint")
    @classmethod
    def exact_private_endpoint(cls, value):
        if not trusted_https_url(value):
            raise ValueError("An exact operator-owned private HTTPS origin is required")
        parts = urlsplit(value)
        if parts.path or value != "https://" + parts.netloc.lower() or parts.port is not None:
            raise ValueError("A canonical private HTTPS origin is required")
        return value

    @model_validator(mode="after")
    def coherent_source(self):
        if self.location != self.dataset_location or len(set(self.tables)) != len(self.tables):
            raise ValueError("A unique reviewed same-location source is required")
        if (self.total_execution_bytes < self.per_job_maximum_bytes_billed
                or self.max_active_jobs > self.max_global_active_jobs):
            raise ValueError("Inconsistent source budget")
        return self

    @property
    def digest(self):
        return canonical_digest(self)

    @property
    def capacity_pool_id(self):
        return "bigquery-" + canonical_digest({"provider": "bigquery",
            "billing_project": self.billing_project, "location": self.location})


class BigQueryScope(StrictModel):
    tenant_member: CubeMember
    access_group_member: CubeMember


class BigQueryBinding(StrictModel):
    context: Literal["consumer", "fixture_test"]
    binding_id: Identifier
    revision: int = Field(ge=1)
    adapter_revision: Literal["cube-bigquery-v1"]
    source_id: Identifier
    source_revision: int = Field(ge=1)
    source_digest: Digest
    model_bundle_digest: Digest
    fields: Annotated[dict[Identifier, MemberBinding], Field(min_length=2, max_length=19)]
    input_fields: Annotated[dict[Identifier, Identifier], Field(max_length=19)] = Field(default_factory=dict)
    scope: BigQueryScope
    freshness_mode: Literal["unknown", "same-query-checkpoint"]
    checkpoint_member: CubeMember | None

    @model_validator(mode="after")
    def exact_semantics(self):
        items = list(self.fields.values())
        cubes = {item.member.split(".")[0] for item in items}
        expected = {"measure": "number", "dimension": "string", "time": "time"}
        if (len(cubes) != 1 or len({item.member for item in items}) != len(items)
                or any(item.cube_type != expected[item.kind] for item in items)
                or sum(item.kind == "time" for item in items) != 1
                or not any(item.kind == "measure" for item in items)):
            raise ValueError("One exact reviewed daily cube is required")
        hidden = list(self.constraint_members)
        if self.freshness_mode == "unknown":
            if self.checkpoint_member is not None:
                raise ValueError("Unknown freshness has no checkpoint")
        else:
            if self.checkpoint_member is None:
                raise ValueError("Correlated source freshness requires a checkpoint")
            hidden.append(self.checkpoint_member)
        if (len(set(hidden)) != len(hidden) or any(member.split(".")[0] not in cubes for member in hidden)
                or set(hidden) & {item.member for item in items}
                or any(name not in self.fields or self.fields[name].kind != "dimension" for name in self.input_fields.values())):
            raise ValueError("Source scope, checkpoint and output members must be independent")
        return self

    @property
    def constraint_members(self):
        return (self.scope.tenant_member, self.scope.access_group_member)

    @property
    def digest(self):
        return canonical_digest(self)


def check_bigquery_binding(binding, manifest, *, digest=None):
    try:
        binding = BigQueryBinding.model_validate(binding)
        manifest = Manifest.model_validate(manifest)
        if ((binding.binding_id, binding.revision, binding.adapter_revision, binding.model_bundle_digest)
                != (manifest.binding_id, manifest.binding_revision, manifest.adapter_revision, manifest.model_bundle_digest)
                or digest is not None and digest != binding.digest or set(binding.fields) != set(manifest.outputs)
                or manifest.grain != "daily" or manifest.date_boundaries is None
                or manifest.timezone not in {"UTC", "America/New_York"}
                or binding.freshness_mode == "unknown" and manifest.freshness.required):
            raise ValueError
        for name, item in binding.fields.items():
            if manifest.outputs[name].type != {"measure": "decimal", "dimension": "string", "time": "date"}[item.kind]:
                raise ValueError
            if name in manifest.filters and item.kind == "measure":
                raise ValueError
            if (item.kind == "time" and name in manifest.filters
                    and set(manifest.filters[name].operators) - {"eq","ne","in","not_in","lt","lte","gt","gte"}):
                raise ValueError
        bounds = manifest.date_boundaries
        if set(manifest.inputs) != {bounds.start_input, bounds.end_input} | set(binding.input_fields):
            raise ValueError
        for name, output in binding.input_fields.items():
            if manifest.inputs[name].type != manifest.outputs[output].type:
                raise ValueError
    except (ValueError, TypeError, KeyError):
        raise GatewayError(ErrorCode.BINDING_DRIFT, "bigquery-binding", "The reviewed BigQuery binding is incompatible.") from None


def validate_bigquery_decision(binding, decision, request_id):
    try:
        binding = BigQueryBinding.model_validate(binding)
        decision = Decision.model_validate(decision)
        constraints = {item.member: item for item in decision.row_constraints}
        if len(decision.row_constraints) != 2 or set(constraints) != set(binding.constraint_members):
            raise ValueError
        for item in constraints.values():
            if (any(type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None for value in item.values)
                    or len(set(item.values)) != len(item.values)):
                raise ValueError
        tenant = constraints[binding.scope.tenant_member]
        if (tenant.operator != "eq" or tenant.values != (decision.tenant_id,)
                or constraints[binding.scope.access_group_member].operator not in {"eq", "in"}):
            raise ValueError
        return decision
    except (ValueError, TypeError, KeyError, AttributeError):
        raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN, request_id, "The current scope cannot execute this product.") from None


def bigquery_security_contract(binding):
    binding = BigQueryBinding.model_validate(binding)
    members = {item.member: {"kind": item.kind, "type": item.cube_type} for item in binding.fields.values()}
    members.update({member: {"kind": "dimension", "type": "string"} for member in binding.constraint_members})
    if binding.checkpoint_member is not None:
        members[binding.checkpoint_member] = {"kind": "dimension", "type": "string"}
    return dict(schema_version=1, adapter_revision="cube-bigquery-v1",
        data_cube=binding.scope.tenant_member.split(".")[0], members=members,
        tenant_member=binding.scope.tenant_member, access_group_member=binding.scope.access_group_member,
        time_member=next(item.member for item in binding.fields.values() if item.kind == "time"),
        checkpoint_member=binding.checkpoint_member)


def validate_bigquery_completed(binding, completed):
    binding = BigQueryBinding.model_validate(binding)
    completed = Completed.model_validate(completed)
    if binding.freshness_mode == "unknown" and completed.source_watermark is not None:
        raise ValueError("Unknown source freshness cannot carry a watermark")
    return completed


def compile_bigquery_query(binding, manifest, request, decision, *, max_rows=1000, request_id="query-compilation"):
    from gateway.contracts.manifest import iso_date
    from gateway.contracts.query import validate_query
    from .reviewed import date_filter
    binding = BigQueryBinding.model_validate(binding)
    check_bigquery_binding(binding, manifest)
    decision = validate_bigquery_decision(binding, decision, request_id)
    request = validate_query(manifest, request)
    if (not {name for name,item in binding.fields.items() if item.kind != "measure"} <= set(decision.visible_fields)
            or set(decision.visible_fields) - set(manifest.outputs)):
        raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN, request_id, "The current fields cannot represent the product grain.")
    validate_query_visibility(binding, request, decision, request_id)
    bounds = manifest.date_boundaries
    if (iso_date(request.inputs[bounds.end_input])-iso_date(request.inputs[bounds.start_input])).days > decision.limits.max_date_range_days:
        raise GatewayError(ErrorCode.INVALID_INPUT, request_id, "The date range exceeds the source contract.")
    time_member = next(item.member for item in binding.fields.values() if item.kind == "time")
    filters = [{"member":time_member,"operator":"afterOrOnDate","values":[request.inputs[bounds.start_input]+"T00:00:00"]},
        {"member":time_member,"operator":"beforeDate","values":[request.inputs[bounds.end_input]+"T00:00:00"]}]
    for name, field in binding.input_fields.items():
        if name in request.inputs and request.inputs[name] is not None:
            filters.append({"member":binding.fields[field].member,"operator":"equals","values":[request.inputs[name]]})
    operators = {"eq":"equals","ne":"notEquals","in":"equals","not_in":"notEquals","lt":"lt","lte":"lte","gt":"gt","gte":"gte","is_null":"notSet"}
    for item in request.filters:
        member = binding.fields[item.field]
        if member.kind == "time":
            filters.append({"or" if item.operator == "in" else "and":[date_filter(member.member,"eq" if item.operator=="in" else "ne",v) for v in item.value]}
                if item.operator in {"in","not_in"} else date_filter(member.member,item.operator,item.value))
        else:
            expression = {"member":member.member,"operator":operators[item.operator]}
            if item.operator != "is_null":
                values = list(item.value) if isinstance(item.value, tuple) else [item.value]
                if any(type(value) is not str or len(value)>128 for value in values):
                    raise ValueError("Unsupported source filter")
                expression["values"] = values
            filters.append(expression)
    filters.extend({"member":item.member,"operator":"equals","values":list(item.values)} for item in decision.row_constraints)
    return {"measures":[item.member for name,item in binding.fields.items() if item.kind=="measure" and name in decision.visible_fields],
        "dimensions":[item.member for item in binding.fields.values() if item.kind=="dimension"]+([binding.checkpoint_member] if binding.checkpoint_member else []),
        "timeDimensions":[{"dimension":time_member,"granularity":"day"}],"filters":[{"and":filters}],
        "order":[[binding.fields[item.field].member,item.direction] for item in request.sort],"timezone":manifest.timezone,
        "limit":min(manifest.limits.max_rows,decision.limits.max_rows,max_rows)+1}


def __getattr__(name):
    if name == "BigQueryServingAdapter":
        from .bigquery_serving import BigQueryServingAdapter
        return BigQueryServingAdapter
    raise AttributeError(name)
