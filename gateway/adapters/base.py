"""Operator-owned binding definitions and narrow internal resolution seams."""
from typing import Annotated, Literal, Protocol
from types import MappingProxyType
import re

from pydantic import Field, model_validator

from gateway.contracts.canonical import Array, Digest, Identifier, StrictModel, canonical_bytes, canonical_digest
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.manifest import Manifest, Operator
from gateway.contracts.identity import Decision
from gateway.contracts.query import QueryRequest

CubeMember = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*$", max_length=128)]
ResponseKey = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*(?:\.day)?$", max_length=132)]


class MemberBinding(StrictModel):
    member: CubeMember
    kind: Literal["measure", "dimension", "time"]
    cube_type: Literal["number", "string", "time", "boolean"]
    response_key: ResponseKey
    granularity: Literal["day"] | None = None

    @model_validator(mode="after")
    def exact_response_key(self):
        expected = self.member + (".day" if self.granularity == "day" else "")
        if self.response_key != expected or (self.kind == "time") != (self.granularity == "day"):
            raise ValueError("Binding response key and grain must match its reviewed member")
        return self


class CubeBinding(StrictModel):
    context: Literal["consumer","fixture_test"] = "consumer"
    binding_id: Identifier
    revision: int = Field(ge=1)
    adapter_revision: Literal["cube-rest-v1"]
    model_bundle_digest: Digest
    fields: Annotated[dict[Identifier, MemberBinding], Field(min_length=1, max_length=50)]
    input_fields: Annotated[dict[Identifier, Identifier], Field(max_length=50)] = Field(default_factory=dict)
    constraint_members: Array[CubeMember] = Field(min_length=2, max_length=3)
    watermark_member: Literal["Watermark.loaded_at"]

    @model_validator(mode="after")
    def reviewed_synthetic_models(self):
        # This revision implements the frozen reviewed runtime, not a compiler.
        allowed = {"Orders.revenue":("measure","number"), "Orders.segment":("dimension","string"),
            "Orders.ordered_at":("time","time")}
        for item in self.fields.values():
            if allowed.get(item.member) != (item.kind, item.cube_type):
                raise ValueError("Unsupported reviewed member or source type")
        if len({item.response_key for item in self.fields.values()}) != len(self.fields):
            raise ValueError("Binding fields cannot alias a response column")
        if len([item for item in self.fields.values() if item.kind == "time"]) != 1:
            raise ValueError("This adapter requires one reviewed daily time dimension")
        required = {"Orders.tenant_id", "Orders.access_group"}
        if (not required <= set(self.constraint_members) or len(set(self.constraint_members)) != len(self.constraint_members)
            or set(self.constraint_members) - required - {"Orders.segment"}):
            raise ValueError("Unsupported mandatory constraint members")
        if any(field not in self.fields or self.fields[field].kind != "dimension" for field in self.input_fields.values()):
            raise ValueError("Inputs require an approved dimension mapping")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self)


def check_binding(binding: CubeBinding, manifest: Manifest, *, digest: str | None = None) -> None:
    try:
        binding = CubeBinding.model_validate(binding.model_dump(mode="python"))
        manifest = Manifest.model_validate(manifest.model_dump(mode="python"))
        if ((binding.binding_id, binding.revision, binding.adapter_revision, binding.model_bundle_digest)
            != (manifest.binding_id, manifest.binding_revision, manifest.adapter_revision, manifest.model_bundle_digest)
            or (digest is not None and digest != binding.digest) or set(binding.fields) != set(manifest.outputs)
            or manifest.grain != "daily" or manifest.date_boundaries is None
            or manifest.timezone not in {"UTC", "America/New_York"}):
            raise ValueError
        for name, item in binding.fields.items():
            field = manifest.outputs[name]
            permitted = {"measure":{"decimal"}, "dimension":{"string"}, "time":{"date"}}
            if field.type not in permitted[item.kind]: raise ValueError
            if name in manifest.filters and item.kind == "measure": raise ValueError
            if item.kind == "time" and name in manifest.filters and set(manifest.filters[name].operators) - {"eq","ne","in","not_in","lt","lte","gt","gte"}:
                raise ValueError
        bounds = manifest.date_boundaries
        if set(manifest.inputs) != {bounds.start_input,bounds.end_input} | set(binding.input_fields):
            raise ValueError
        for name, field in binding.input_fields.items():
            if manifest.inputs[name].type != manifest.outputs[field].type: raise ValueError
    except (ValueError, TypeError, KeyError):
        raise GatewayError(ErrorCode.BINDING_DRIFT, "binding-validation", "The reviewed product binding is incompatible.") from None


class BindingResolver(Protocol):
    async def resolve(self, binding_id: str, revision: int) -> CubeBinding: ...


class StaticBindingResolver:
    """Immutable local operator fixture catalog; registry-backed resolvers can implement the protocol."""
    def __init__(self, bindings: list[CubeBinding]):
        entries = [CubeBinding.model_validate(item.model_dump(mode="python")) for item in bindings]
        self._bindings = MappingProxyType({(item.binding_id,item.revision):item for item in entries})
        if len(self._bindings) != len(entries): raise ValueError("Duplicate binding revision")

    async def resolve(self, binding_id: str, revision: int) -> CubeBinding:
        try:
            return self._bindings[(binding_id,revision)]
        except KeyError:
            raise GatewayError(ErrorCode.BINDING_DRIFT, "binding-resolution", "The pinned product binding is unavailable.") from None


class SourceCapabilities(StrictModel):
    operators: Array[Operator] = ("eq","ne","lt","lte","gt","gte","in","not_in","is_null")
    field_types: Array[Literal["string","date","decimal"]] = ("string","date","decimal")
    date_semantics: Literal["local-calendar-half-open"] = "local-calendar-half-open"
    max_rows: int = Field(default=1000,ge=1,le=1000)
    # A statement cap applies only while a PostgreSQL statement is active.
    # It cannot prove that queued/orphaned Cube work has finished.
    proven_statement_timeout_seconds: float | None = Field(default=None,gt=0,le=120)
    statement_timeout_evidence: str | None = Field(default=None,min_length=1,max_length=2048)
    # Only a separately proven admission/queue/orphan bound can populate this.
    proven_upstream_lifetime_seconds: float | None = Field(default=None,gt=0,le=86_400)
    upstream_lifetime_evidence: str | None = Field(default=None,min_length=1,max_length=2048)
    source_cost_bound: Literal["unproven", "not-applicable-synthetic-postgres"] = "unproven"

    @model_validator(mode="after")
    def paired_evidence(self):
        for seconds,evidence in ((self.proven_statement_timeout_seconds,self.statement_timeout_evidence),
            (self.proven_upstream_lifetime_seconds,self.upstream_lifetime_evidence)):
            if (seconds is None)!=(evidence is None):
                raise ValueError("Each proven source bound requires its independent actual evidence")
        return self


def authorization_scope_digest(decision: Decision) -> str:
    """Deterministic typed scope identity; fields and limits are checked separately.

    Cube equality/in membership have the same semantics. Values and constraint
    ordering do not change scope; canonical encodings preserve bool/int types.
    """
    decision=Decision.model_validate(decision.model_dump(mode="python"))
    constraints=[]
    for item in decision.row_constraints:
        encoded={canonical_bytes(value):value for value in item.values}
        constraints.append({"member":item.member,"values":[encoded[key] for key in sorted(encoded)]})
    constraints.sort(key=lambda item:item["member"])
    return canonical_digest({"principal_id":decision.principal_id,"policy_revision":decision.policy_revision,
        "tenant_id":decision.tenant_id,"row_constraints":constraints})


def validate_data_decision(decision: Decision, request_id: str, allowed=None) -> Decision:
    try:
        decision=Decision.model_validate(decision.model_dump(mode="python"))
        constraints=decision.row_constraints
        members={item.member for item in constraints}
        if (len(constraints) not in {2,3} or len(members)!=len(constraints)
            or not {"Orders.tenant_id","Orders.access_group"} <= members
            or members - {"Orders.tenant_id","Orders.access_group","Orders.segment"}
            or allowed is not None and members - set(allowed)):
            raise ValueError
        for item in constraints:
            if any(type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",value) for value in item.values): raise ValueError
            if len(set(item.values))!=len(item.values): raise ValueError
        tenant=next(item for item in constraints if item.member=="Orders.tenant_id")
        if tenant.operator!="eq" or tenant.values!=(decision.tenant_id,): raise ValueError
        return decision
    except (ValueError,TypeError,AttributeError):
        raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN,request_id,"The current scope cannot execute this product.") from None


def validate_query_visibility(binding: CubeBinding, request: QueryRequest,
                              decision: Decision, request_id: str) -> None:
    """Selection/order dependencies remain authorized before any result projection."""
    dependencies={item.field for item in (*request.filters,*request.sort)}
    dependencies.update(field for name,field in binding.input_fields.items()
        if name in request.inputs and request.inputs[name] is not None)
    if not dependencies<=set(decision.visible_fields):
        raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN,request_id,
            "The current scope cannot use the saved query's fields.")
