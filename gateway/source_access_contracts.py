"""Inert, bounded exact-package approvals. No I/O and no authority creation.

The canonical document is operator review content, never a provider credential
or a policy grant. The pure evaluator requires the protected record's digest in
runtime pins; active state, fresh DB expiry and transaction locking belong to
SourceAccessService. Local wall time is deliberately not an authority source.
"""
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import math
import re
from typing import Annotated, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from gateway.adapters import bigquery_runtime, domo_runtime
from gateway.contracts import identity as identity_contracts
from gateway.contracts import manifest as manifest_contracts
from gateway.contracts import query as query_contracts
from gateway.contracts.canonical import Digest, FrozenDict, Identifier, Version, canonical_bytes, canonical_digest, manifest_digest, timestamp
from gateway.contracts.errors import GatewayError
from gateway.contracts.identity import Decision, Principal, RowConstraint
from gateway.contracts.manifest import FieldSchema, Manifest, reject_secret, validate_value
from gateway.contracts.query import QueryRequest, validate_query

MAX_APPROVAL_BYTES = 65_536
APPROVAL_DIGEST_PREFIX = b"spaceport-source-approval-v1\0"
PACKAGE_DIGEST_PREFIX = b"spaceport-reviewed-package-v1\0"
PROJECTION_DIGEST_PREFIX = b"spaceport-source-projection-v1\0"
MAX_REVISION = 2_147_483_647
Action = Literal["discover", "describe", "execute", "result", "author", "publish", "policy_admin", "operate", "audit_view"]
Context = Literal["consumer", "fixture_test"]
T = TypeVar("T")


def _is_type(value, *allowed):
    actual = type(value)
    return any(actual is known for known in allowed)


def _array(value):
    if not _is_type(value, tuple, list):
        raise ValueError("Invalid approval array")
    return tuple(value)


Array = Annotated[tuple[T, ...], BeforeValidator(_array)]
Revision = Annotated[int, Field(ge=1, le=MAX_REVISION)]


def _scalar(value):
    if value is None or type(value) is bool:
        return value
    if type(value) is int and abs(value) <= 2**53 - 1:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is str and len(value) <= 128 and re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.:/+\-]*", value):
        reject_secret(value)
        return value
    raise ValueError("Invalid reviewed scalar")


ReviewScalar = Annotated[str | int | bool | None, BeforeValidator(_scalar)]
Bound = Annotated[str | int | None, BeforeValidator(_scalar)]


def _unique(values):
    # Equality collisions are rejected as well as typed duplicates. In Python,
    # bool/int/float equality otherwise silently collapses true, 1 and 1.0.
    for index, value in enumerate(values):
        if any(value == previous for previous in values[:index]):
            raise ValueError("Duplicate reviewed value")


def _rows(rows):
    if len({row.member for row in rows}) != len(rows):
        raise ValueError("Duplicate row member")
    for row in rows:
        _unique(row.values)
        for value in row.values:
            _scalar(value)


class ApprovalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True,
        validate_default=True, revalidate_instances="always", allow_inf_nan=False,
        hide_input_in_errors=True)


class ProductApprovalPin(ApprovalModel):
    product_id: Identifier
    version: Version
    manifest_digest: Digest
    policy_id: Identifier


class BindingApprovalPin(ApprovalModel):
    binding_id: Identifier
    revision: Revision
    content_digest: Digest


class ApprovalBindings(ApprovalModel):
    consumer: BindingApprovalPin
    fixture: BindingApprovalPin

    @model_validator(mode="after")
    def isolated_bindings(self):
        if self.consumer.binding_id == self.fixture.binding_id:
            raise ValueError("Separate fixture binding required")
        return self


class SourceApprovalPin(ApprovalModel):
    source_id: Identifier
    revision: Revision
    provider: Literal["bigquery", "domo"]
    context: Context
    descriptor_digest: Digest
    physical_identity_digest: Digest
    credential_binding_digest: Digest


class ApprovalSources(ApprovalModel):
    consumer: SourceApprovalPin
    fixture: SourceApprovalPin

    @model_validator(mode="after")
    def coherent_contexts(self):
        if self.consumer.context != "consumer" or self.fixture.context != "fixture_test":
            raise ValueError("Invalid source contexts")
        if self.consumer.source_id == self.fixture.source_id:
            raise ValueError("Separate fixture source required")
        if self.consumer.provider != self.fixture.provider:
            raise ValueError("Mismatched source providers")
        return self


class ApprovalArtifacts(ApprovalModel):
    model_bundle_digest: Digest
    fixture_model_bundle_digest: Digest
    statement_catalog_digest: Digest | None
    fixture_statement_catalog_digest: Digest | None
    fixture_catalog_digest: Digest
    schema_profile: Identifier
    normalization_profile: Identifier


class SourceLineageEntry(ApprovalModel):
    id: Identifier
    source_context: Context
    source_member: Identifier
    purpose: Literal["exposed", "hidden_scope", "checkpoint", "grouping", "source_input"]
    classification: Literal["non-personal"]


class ReviewedValueDomain(ApprovalModel):
    name: Identifier
    kind: Literal["enum", "range"]
    type: Literal["string", "integer", "number", "boolean", "date", "timestamp", "decimal"]
    values: Array[ReviewScalar] = Field(max_length=100)
    minimum: Bound
    maximum: Bound
    nullable: bool
    classification: Literal["non-personal"]

    @model_validator(mode="after")
    def closed_domain(self):
        _unique(self.values)
        if self.kind == "enum":
            if not self.values or self.minimum is not None or self.maximum is not None:
                raise ValueError("Invalid enum domain")
            for value in self.values:
                if (value is None or not _domain_type(self.type, value)
                        or self.type == "decimal" and not _decimal(value)):
                    raise ValueError("Invalid enum scalar")
        else:
            if self.values or self.type in ("string", "boolean") or self.minimum is None or self.maximum is None:
                raise ValueError("Invalid range domain")
            if not _domain_type(self.type, self.minimum, bound=True) or not _domain_type(self.type, self.maximum, bound=True):
                raise ValueError("Invalid range boundary")
            if _order(self.type, self.minimum) > _order(self.type, self.maximum):
                raise ValueError("Invalid range ordering")
        return self


def _decimal(value):
    if type(value) is not str or len(value) > 128 or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        return False
    number = Decimal(value)
    # One spelling for zero and no redundant fractional trailing zeroes.
    canonical = "0" if number.is_zero() else value.rstrip("0").rstrip(".") if "." in value else value
    return number.is_finite() and value == canonical


def _utc(value):
    if type(value) is not str or len(value) > 32 or not value.endswith("Z"):
        raise ValueError("Invalid UTC timestamp")
    instant = timestamp(value)
    if instant.isoformat().replace("+00:00", "Z") != value:
        raise ValueError("Noncanonical UTC timestamp")
    return value


def _domain_type(type_, value, *, bound=False):
    if type_ == "string":
        return type(value) is str
    if type_ == "integer":
        return type(value) is int and abs(value) <= 2**53 - 1
    if type_ == "number":
        return _decimal(value) if bound else _is_type(value, int, float) and math.isfinite(value)
    if type_ == "boolean":
        return type(value) is bool
    if type_ == "decimal":
        return _decimal(value) if bound else (type(value) is str and len(value) <= 128
            and re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value) is not None)
    if type_ == "date":
        return type(value) is str and date.fromisoformat(value).isoformat() == value
    if type_ == "timestamp":
        return _utc(value) == value
    return False


def _order(type_, value):
    if type_ in ("number", "decimal"):
        return Decimal(str(value))
    if type_ == "date":
        return date.fromisoformat(value)
    if type_ == "timestamp":
        return timestamp(value)
    return value


def _domain_contains(domain, value):
    if value is None:
        return domain.nullable
    if not _domain_type(domain.type, value):
        return False
    if domain.kind == "enum":
        return any(type(value) is type(choice) and value == choice for choice in domain.values)
    return _order(domain.type, domain.minimum) <= _order(domain.type, value) <= _order(domain.type, domain.maximum)


class SourceAudienceEntry(ApprovalModel):
    id: Identifier
    context: Context
    principal_id: Identifier
    principal_kind: Literal["user", "service", "demo"]
    client_ids: Array[Identifier | None] = Field(min_length=1, max_length=100)
    actions: Array[Action] = Field(min_length=1, max_length=9)
    tenant_id: Identifier
    row_constraints: Array[RowConstraint] = Field(min_length=1, max_length=20)
    visible_fields: Array[Identifier] = Field(min_length=1, max_length=50)
    fixture_case_ids: Array[Identifier] = Field(max_length=8)

    @model_validator(mode="after")
    def complete_tuple(self):
        for values in (self.client_ids, self.actions, self.visible_fields, self.fixture_case_ids):
            _unique(values)
        _rows(self.row_constraints)
        if self.context == "consumer" and self.fixture_case_ids:
            raise ValueError("Consumer entry cannot name fixture cases")
        if self.fixture_case_ids and set(self.actions) & {"author", "publish"}:
            raise ValueError("Fixed cases cannot be invoking actors")
        if self.principal_kind == "demo":
            if self.context != "fixture_test" or self.client_ids != (None,) or not self.fixture_case_ids:
                raise ValueError("Fixed fixture identity required")
        elif None in self.client_ids:
            raise ValueError("Authenticated client required")
        return self


class SourceReviewAttestation(ApprovalModel):
    review_id: Identifier
    operator_id: Identifier
    evidence_ref: Identifier
    evidence_digest: Digest
    purpose: Identifier
    lineage_reviewed: bool
    source_publication_controlled: bool
    shared_identity_approved: bool
    inference_risk_reviewed: bool

    @model_validator(mode="after")
    def affirmative_review(self):
        if not all((self.lineage_reviewed, self.source_publication_controlled,
                    self.shared_identity_approved, self.inference_risk_reviewed)):
            raise ValueError("Explicit review attestations required")
        return self


class PackageApprovalFields(ApprovalModel):
    product: ProductApprovalPin
    bindings: ApprovalBindings
    sources: ApprovalSources
    artifacts: ApprovalArtifacts
    lineage: Array[SourceLineageEntry] = Field(min_length=1, max_length=200)
    input_domains: Array[ReviewedValueDomain] = Field(max_length=50)
    filter_domains: Array[ReviewedValueDomain] = Field(max_length=50)

    @model_validator(mode="after")
    def complete_package(self):
        _approval_tree(self._package_content())
        identities = [(entry.source_context, entry.purpose, entry.id) for entry in self.lineage]
        _unique(identities)
        _unique(tuple(item.name for item in self.input_domains))
        _unique(tuple(item.name for item in self.filter_domains))
        catalogs = (self.artifacts.statement_catalog_digest, self.artifacts.fixture_statement_catalog_digest)
        if self.sources.consumer.provider == "domo":
            if None in catalogs:
                raise ValueError("Domo statement catalogs required")
        elif any(value is not None for value in catalogs):
            raise ValueError("Unexpected statement catalogs")
        return self

    def _package_content(self):
        return {name: _plain(getattr(self, name)) for name in PackageApprovalFields.model_fields}

    @property
    def package_pins_digest(self) -> str:
        return hashlib.sha256(PACKAGE_DIGEST_PREFIX + canonical_bytes(self._package_content())).hexdigest()


class SourceApprovalDocument(PackageApprovalFields):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    mode: Literal["shared_non_pii"]
    revision: Revision
    supersedes_id: str | None
    expires_at: str
    audiences: Array[SourceAudienceEntry] = Field(min_length=1, max_length=100)
    review: SourceReviewAttestation

    @field_validator("supersedes_id")
    @classmethod
    def canonical_predecessor(cls, value):
        if value is not None and (len(value) != 36 or str(UUID(value)) != value):
            raise ValueError("Invalid predecessor")
        return value

    _expiry = field_validator("expires_at")(_utc)

    @model_validator(mode="after")
    def complete_review(self):
        _approval_tree(_plain(self))
        _unique(tuple(entry.id for entry in self.audiences))
        if (self.supersedes_id is None) != (self.revision == 1):
            raise ValueError("Invalid predecessor revision")
        if len(canonical_bytes(_plain(self))) > MAX_APPROVAL_BYTES:
            raise ValueError("Approval content too large")
        return self

    @property
    def canonical_content(self) -> bytes:
        return canonical_bytes(_plain(self))

    @property
    def content_digest(self) -> str:
        return hashlib.sha256(APPROVAL_DIGEST_PREFIX + self.canonical_content).hexdigest()

    def runtime_projection(self, approval_id: str):
        document = _fresh(self, SourceApprovalDocument)
        content = document._package_content() | {
            "approval_id": approval_id, "approval_revision": document.revision,
            "approval_digest": document.content_digest, "expires_at": document.expires_at,
            "audiences": _plain(document.audiences)}
        digest = hashlib.sha256(PROJECTION_DIGEST_PREFIX + canonical_bytes(content)).hexdigest()
        return RuntimeApprovalProjection.model_validate(content | {"projection_digest": digest})

    def package_pins(self, manifest: Manifest, *, approval_digest: str | None = None):
        return ReviewedPackagePins.model_validate(self._package_content() |
            {"manifest": _plain(manifest), "approval_digest": approval_digest})


class RuntimeApprovalProjection(PackageApprovalFields):
    """Narrow runtime data, not proof of registry provenance or active state."""
    approval_id: str
    approval_revision: Revision
    approval_digest: Digest
    expires_at: str
    audiences: Array[SourceAudienceEntry] = Field(min_length=1, max_length=100)
    projection_digest: Digest

    @field_validator("approval_id")
    @classmethod
    def canonical_id(cls, value):
        if len(value) != 36 or str(UUID(value)) != value:
            raise ValueError("Invalid approval identity")
        return value

    _expiry = field_validator("expires_at")(_utc)

    @model_validator(mode="after")
    def exact_projection(self):
        _unique(tuple(entry.id for entry in self.audiences))
        plain = _plain(self)
        _approval_tree(plain)
        if len(canonical_bytes(plain)) > MAX_APPROVAL_BYTES:
            raise ValueError("Projection content too large")
        expected = plain.pop("projection_digest")
        digest = hashlib.sha256(PROJECTION_DIGEST_PREFIX + canonical_bytes(plain)).hexdigest()
        if expected != digest:
            raise ValueError("Projection digest mismatch")
        return self


class ReviewedPackagePins(PackageApprovalFields):
    manifest: Manifest
    approval_digest: Digest | None

    @model_validator(mode="after")
    def exact_manifest(self):
        manifest = self.manifest
        if (manifest.product_id != self.product.product_id or manifest.version != self.product.version
                or manifest.policy_id != self.product.policy_id or manifest_digest(manifest) != self.product.manifest_digest
                or manifest.binding_id != self.bindings.consumer.binding_id
                or manifest.binding_revision != self.bindings.consumer.revision
                or manifest.model_bundle_digest != self.artifacts.model_bundle_digest):
            raise ValueError("Manifest pin mismatch")
        adapter = "cube-bigquery-v1" if self.sources.consumer.provider == "bigquery" else "domo-named-v2"
        if manifest.adapter_revision != adapter:
            raise ValueError("Unsupported reviewed adapter")
        for context in ("consumer", "fixture_test"):
            exposed = {item.id for item in self.lineage if item.source_context == context and item.purpose == "exposed"}
            if exposed != set(manifest.outputs):
                raise ValueError("Incomplete exposed inventory")
        self._validate_domains(manifest.inputs, self.input_domains)
        self._validate_domains({name: manifest.outputs[name] for name in manifest.filters}, self.filter_domains)
        domains = {domain.name: domain for domain in self.input_domains}
        for example in manifest.examples:
            for name, value in example.inputs.items():
                if not _domain_contains(domains[name], value):
                    raise ValueError("Unreviewed manifest example")
        # INVALID_INPUT negative fixtures intentionally contain invalid values;
        # they are exact-reviewed bytes, never an approved runtime input domain.
        for fixture in manifest.fixtures:
            if fixture.expected_error != "INVALID_INPUT":
                for name, value in fixture.inputs.items():
                    if not _domain_contains(domains[name], value):
                        raise ValueError("Unreviewed fixture input")
        return self

    @staticmethod
    def _validate_domains(schemas, domains):
        by_name = {domain.name: domain for domain in domains}
        if set(by_name) != set(schemas):
            raise ValueError("Incomplete reviewed domains")
        for name, schema in schemas.items():
            domain = by_name[name]
            if domain.type != schema.type or domain.nullable != schema.nullable:
                raise ValueError("Mismatched reviewed domain")
            if schema.enum is not None and any(not _domain_contains(domain, value) for value in schema.enum):
                raise ValueError("Unreviewed manifest enum")
            if schema.default is not None and not _domain_contains(domain, schema.default.value):
                raise ValueError("Unreviewed manifest default")
            for value in domain.values:
                validate_value(schema, value)

    @property
    def content_digest(self) -> str:
        return self.package_pins_digest


# Trust-boundary conversion preserves unknown fields instead of dropping them as
# model_dump() can do for model_construct()/model_copy(update=...). Containers
# and models are exact known types, so arbitrary iterators/hooks are not called.
_MODEL_TYPES = {cls for module in (identity_contracts, manifest_contracts, query_contracts, bigquery_runtime, domo_runtime)
                for cls in vars(module).values() if isinstance(cls, type) and issubclass(cls, BaseModel)}
_MODEL_TYPES.update(cls for cls in tuple(globals().values()) if isinstance(cls, type) and issubclass(cls, ApprovalModel))


def _plain(value, *, _depth=0, _budget=None):
    if _budget is None:
        _budget = [30_000]
    _budget[0] -= 1
    if _depth > 16 or _budget[0] < 0:
        raise ValueError("Invalid bounded contract")
    recurse = lambda item: _plain(item, _depth=_depth + 1, _budget=_budget)
    if any(type(value) is known for known in _MODEL_TYPES):
        data, extra = value.__dict__, value.__pydantic_extra__
        if (type(data) is not dict or any(type(key) is not str for key in data)
                or set(data) != set(type(value).model_fields)
                or (extra is not None and (type(extra) is not dict or len(extra) != 0))):
            raise ValueError("Invalid model fields")
        return {key: recurse(item) for key, item in data.items()}
    if _is_type(value, dict, FrozenDict):
        if len(value) > 4096 or any(type(key) is not str for key in value):
            raise ValueError("Invalid contract object")
        return {key: recurse(item) for key, item in value.items()}
    if _is_type(value, tuple, list):
        if len(value) > 4096:
            raise ValueError("Invalid contract array")
        return [recurse(item) for item in value]
    if value is None or _is_type(value, str, bool, int, float):
        if type(value) is str and len(value) > 65_536:
            raise ValueError("Invalid contract string")
        return value
    raise ValueError("Invalid contract value")


def _fresh(value, model):
    if type(value) is not model:
        raise ValueError("Invalid contract type")
    return model.model_validate(_plain(value))


def _approval_tree(value):
    """Approval JSON has exact integers only and no recognized credentials."""
    if type(value) is float:
        raise ValueError("Floating-point approval values are unsupported")
    if type(value) is str:
        reject_secret(value)
    elif type(value) is dict:
        for child in value.values():
            _approval_tree(child)
    elif _is_type(value, list, tuple):
        for child in value:
            _approval_tree(child)


def _duplicate_free_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON name")
        result[key] = value
    return result


def validate_source_approval(raw: bytes) -> SourceApprovalDocument:
    """Accept only bounded canonical UTF-8 JSON; never reflect supplied data."""
    try:
        if type(raw) is not bytes or not 1 <= len(raw) <= MAX_APPROVAL_BYTES:
            raise ValueError("Invalid document size")
        plain = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_free_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid constant")))
        _approval_tree(plain)
        if type(plain) is not dict or canonical_bytes(plain) != raw:
            raise ValueError("Noncanonical document")
        document = SourceApprovalDocument.model_validate(plain)
        if document.canonical_content != raw:
            raise ValueError("Noncanonical typed document")
        return document
    except (ValueError, TypeError, UnicodeError, RecursionError, GatewayError, OverflowError):
        raise ValueError("Invalid source approval") from None


def _validated_tuple(entry, principal, decision=None):
    entry = _fresh(entry, SourceAudienceEntry)
    principal = _fresh(principal, Principal)
    if decision is not None:
        decision = _fresh(decision, Decision)
        _rows(decision.row_constraints)
    return entry, principal, decision


def _identity_matches(entry, principal):
    return (entry.principal_id == principal.id and entry.principal_kind == principal.kind
            and principal.service_client_id in entry.client_ids)


def audience_contains(entry: SourceAudienceEntry, principal: Principal, decision: Decision,
        *, action: str, context: str, fixture_case_id: str | None = None) -> bool:
    """One complete tuple only. A true value is never standalone authority."""
    try:
        entry, principal, decision = _validated_tuple(entry, principal, decision)
        if (type(action) is not str or type(context) is not str
                or (fixture_case_id is not None and type(fixture_case_id) is not str)):
            return False
        if (not _identity_matches(entry, principal) or decision.principal_id != principal.id
                or action not in entry.actions or context != entry.context or entry.tenant_id != decision.tenant_id
                or not set(decision.visible_fields) <= set(entry.visible_fields)):
            return False
        if entry.fixture_case_ids:
            if fixture_case_id not in entry.fixture_case_ids:
                return False
        elif fixture_case_id is not None:
            return False
        approved = {(row.member, row.operator): row.values for row in entry.row_constraints}
        requested = {(row.member, row.operator): row.values for row in decision.row_constraints}
        if set(approved) != set(requested):
            return False
        return all(all(any(type(value) is type(choice) and value == choice for choice in approved[key])
                           for value in values) for key, values in requested.items())
    except (ValueError, TypeError, AttributeError, RecursionError, GatewayError, OverflowError):
        return False


def reviewed_fixture_identity(entry: SourceAudienceEntry, principal: Principal, case_id: str) -> bool:
    """Structural fixed-case review for policy-negative fixtures; no data grant."""
    try:
        entry, principal, _ = _validated_tuple(entry, principal)
        return (type(case_id) is str and entry.context == "fixture_test" and not set(entry.actions) & {"author", "publish"}
            and _identity_matches(entry, principal) and case_id in entry.fixture_case_ids)
    except (ValueError, TypeError, AttributeError, RecursionError, GatewayError, OverflowError):
        return False


def validate_reviewed_values(pins: ReviewedPackagePins, manifest: Manifest, request: QueryRequest) -> None:
    """Validate actual/defaulted values before audit processing or persistence."""
    try:
        pins = _fresh(pins, ReviewedPackagePins)
        manifest = _fresh(manifest, Manifest)
        request = _fresh(request, QueryRequest)
        if manifest_digest(manifest) != pins.product.manifest_digest:
            raise ValueError("Manifest mismatch")
        normalized = validate_query(manifest, request)
        inputs = {domain.name: domain for domain in pins.input_domains}
        filters = {domain.name: domain for domain in pins.filter_domains}
        for name, value in normalized.inputs.items():
            if not _domain_contains(inputs[name], value):
                raise ValueError("Unreviewed input")
        for clause in normalized.filters:
            if clause.operator == "is_null":
                if not filters[clause.field].nullable:
                    raise ValueError("Unreviewed null")
                continue
            values = clause.value if type(clause.value) is tuple else (clause.value,)
            _unique(values)
            if any(not _domain_contains(filters[clause.field], value) for value in values):
                raise ValueError("Unreviewed filter")
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, GatewayError, OverflowError):
        raise ValueError("Invalid reviewed values") from None


def _evaluate_complete_tuple(package, audiences, principal, decision, *, action, context, pins, fixture_case_id):
    if (type(action) is not str or type(context) is not str
            or (fixture_case_id is not None and type(fixture_case_id) is not str)):
        raise ValueError("Invalid action context")
    pins = _fresh(pins, ReviewedPackagePins)
    principal = _fresh(principal, Principal)
    decision = _fresh(decision, Decision)
    _rows(decision.row_constraints)
    if package.package_pins_digest != pins.package_pins_digest:
        raise ValueError("Package pin mismatch")
    if not set(decision.visible_fields) <= set(pins.manifest.outputs):
        raise ValueError("Unreviewed output")
    if any(getattr(decision.limits, name) > getattr(pins.manifest.limits, name)
           for name in type(pins.manifest.limits).model_fields):
        raise ValueError("Manifest limit exceeded")
    members = {item.source_member for item in pins.lineage if item.source_context == context}
    if not {row.member for row in decision.row_constraints} <= members:
        raise ValueError("Unreviewed row inventory")
    matches = [entry.id for entry in audiences
        if audience_contains(entry, principal, decision, action=action, context=context, fixture_case_id=fixture_case_id)]
    if len(matches) != 1:
        raise ValueError("No unique complete tuple")
    return matches[0]


def evaluate_source_access(document: SourceApprovalDocument, principal: Principal, decision: Decision,
        *, action: str, context: str, pins: ReviewedPackagePins, fixture_case_id: str | None = None) -> str:
    """Package and tuple precheck only. Registry state/expiry need final DB gate."""
    try:
        document = _fresh(document, SourceApprovalDocument)
        pins = _fresh(pins, ReviewedPackagePins)
        if pins.approval_digest is None or document.content_digest != pins.approval_digest:
            raise ValueError("Approval pin mismatch")
        return _evaluate_complete_tuple(document, document.audiences, principal, decision,
            action=action, context=context, pins=pins, fixture_case_id=fixture_case_id)
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, GatewayError, OverflowError):
        raise ValueError("Source access denied") from None


def evaluate_runtime_source_access(projection: RuntimeApprovalProjection, principal: Principal, decision: Decision,
        *, action: str, context: str, pins: ReviewedPackagePins, fixture_case_id: str | None = None) -> str:
    """Pure runtime precheck. Final SQL must compare the protected projection.

    Rehashing coherently modified plain data cannot establish its provenance.
    This predicate does not replace protected-record/state/DB-expiry checks.
    """
    try:
        projection = _fresh(projection, RuntimeApprovalProjection)
        pins = _fresh(pins, ReviewedPackagePins)
        if pins.approval_digest is None or projection.approval_digest != pins.approval_digest:
            raise ValueError("Approval pin mismatch")
        return _evaluate_complete_tuple(projection, projection.audiences, principal, decision,
            action=action, context=context, pins=pins, fixture_case_id=fixture_case_id)
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, GatewayError, OverflowError):
        raise ValueError("Source access denied") from None


def physical_source_identity(source: bigquery_runtime.BigQuerySource | domo_runtime.DomoSource) -> dict:
    """Full provider target and actor identity, never a capacity-pool alias.

    Descriptor digests separately bind credential references. The Domo mount
    references are mutable aliases and cannot establish immutable secret
    versions or credential permission drift; activation must qualify rotation.
    """
    try:
        if type(source) is bigquery_runtime.BigQuerySource:
            source = _fresh(source, bigquery_runtime.BigQuerySource)
            return {"provider": "bigquery", "billing_project": source.billing_project,
                "data_project": source.data_project, "dataset": source.dataset,
                "tables": sorted(source.tables), "location": source.location,
                "dataset_location": source.dataset_location, "cube_endpoint": source.cube_endpoint,
                "auth": {"mode": source.auth.mode, "expected_principal": source.auth.expected_principal}}
        if type(source) is domo_runtime.DomoSource:
            source = _fresh(source, domo_runtime.DomoSource)
            return {"provider": "domo", "api_origin": source.api_origin,
                "dataset_ids": sorted(source.dataset_ids), "auth": {
                    "mode": source.auth.mode, "client_id_sha256": source.auth.client_id_sha256,
                    "expected_customer": source.auth.expected_customer,
                    "expected_user_id": source.auth.expected_user_id, "expected_role": source.auth.expected_role}}
        raise ValueError("Unknown source type")
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, GatewayError, OverflowError):
        raise ValueError("Invalid physical source identity") from None


def source_credential_binding_digest(source: bigquery_runtime.BigQuerySource | domo_runtime.DomoSource) -> str:
    """Pin configured upstream identity/version references without reading secrets.

    A configured version is not proof of mounted bytes or live permissions.
    Historical descriptors without versions remain usable for observation only.
    """
    try:
        if type(source) is bigquery_runtime.BigQuerySource:
            source = _fresh(source, bigquery_runtime.BigQuerySource)
            value = {"provider": "bigquery", "mode": source.auth.mode,
                "expected_principal": source.auth.expected_principal}
        elif type(source) is domo_runtime.DomoSource:
            source = _fresh(source, domo_runtime.DomoSource)
            if source.auth.client_id_version is None or source.auth.client_secret_version is None:
                raise ValueError("Missing immutable credential mapping")
            value = {"provider": "domo", "mode": source.auth.mode,
                "client_id_sha256": source.auth.client_id_sha256,
                "expected_customer": source.auth.expected_customer,
                "expected_user_id": source.auth.expected_user_id, "expected_role": source.auth.expected_role,
                "client_id_version": source.auth.client_id_version,
                "client_secret_version": source.auth.client_secret_version}
        else:
            raise ValueError("Unknown source type")
        return hashlib.sha256(b"spaceport-source-credential-binding-v1\0" + canonical_bytes(value)).hexdigest()
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, GatewayError, OverflowError):
        raise ValueError("Invalid source credential binding") from None


def physical_source_identity_digest(source: bigquery_runtime.BigQuerySource | domo_runtime.DomoSource) -> str:
    return canonical_digest(physical_source_identity(source))


def expected_binding_inventory(binding: bigquery_runtime.BigQueryBinding | domo_runtime.DomoRuntimeBinding,
        input_names: tuple[str, ...]) -> tuple[dict, ...]:
    """Derive structural review-request inventory, never classify or approve it.

    Returned entries deliberately omit classification. A data owner must review
    every entry and explicitly supply classifications in the approval document.
    The two inputs not mapped by a binding are the manifest's date boundaries;
    exact manifest/binding validation remains a separate mandatory prerequisite.
    """
    try:
        if type(input_names) is not tuple or not 2 <= len(input_names) <= 50:
            raise ValueError("Invalid input inventory")
        if any(type(name) is not str or len(name) > 128
               or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) for name in input_names):
            raise ValueError("Invalid input identity")
        _unique(input_names)
        if type(binding) is bigquery_runtime.BigQueryBinding:
            binding = _fresh(binding, bigquery_runtime.BigQueryBinding)
            outputs = {name: item.member for name, item in binding.fields.items()}
            grouping = {name for name, item in binding.fields.items() if item.kind != "measure"}
            time_field = next(name for name, item in binding.fields.items() if item.kind == "time")
            checkpoint = binding.checkpoint_member
        elif type(binding) is domo_runtime.DomoRuntimeBinding:
            binding = _fresh(binding, domo_runtime.DomoRuntimeBinding)
            outputs = {item.field: item.source_name for item in binding.columns}
            grouping, time_field = set(binding.grain_keys), binding.time_field
            checkpoint = binding.checkpoint.source_name if binding.checkpoint is not None else None
        else:
            raise ValueError("Unknown binding type")
        if not set(binding.input_fields) <= set(input_names) or len(set(input_names) - set(binding.input_fields)) != 2:
            raise ValueError("Binding input inventory mismatch")
        result = []
        def add(id_, member, purpose):
            result.append({"id": id_, "source_context": binding.context,
                "source_member": member, "purpose": purpose})
        for name, member in outputs.items():
            add(name, member, "exposed")
            if name in grouping:
                add(name, member, "grouping")
        for member in binding.constraint_members:
            add(member, member, "hidden_scope")
        if checkpoint is not None:
            add(checkpoint, checkpoint, "checkpoint")
        for name in input_names:
            add(name, outputs[binding.input_fields.get(name, time_field)], "source_input")
        return tuple(sorted(result, key=lambda item: (item["source_context"], item["purpose"], item["id"], item["source_member"])))
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, GatewayError, OverflowError, StopIteration):
        raise ValueError("Invalid binding inventory") from None
