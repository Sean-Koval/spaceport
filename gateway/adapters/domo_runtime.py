"""Operator-owned Domo v2 source contracts. Parsing has no I/O or authority."""
import hashlib
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, model_validator

from gateway.contracts.canonical import Array, Digest, Identifier, StrictModel, canonical_digest, MAX_RESULT_BYTES
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.execution import ExecutionSpec, PinnedProduct
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import Manifest, iso_date
from gateway.contracts.query import QueryRequest, validate_query

DatasetId = Annotated[str, Field(pattern=r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')]

SecretRef = Annotated[str, Field(pattern=r'^/run/secrets/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')]
SecretVersion = Annotated[str, Field(max_length=512, pattern=r'^projects/(?:[a-z][a-z0-9-]{4,28}[a-z0-9]|[1-9][0-9]{0,19})/secrets/[A-Za-z0-9_-]{1,255}/versions/[1-9][0-9]{0,18}$')]


class DomoOAuthConfig(StrictModel):
    mode: Literal['oauth-client-credentials']
    client_id_ref: SecretRef
    client_secret_ref: SecretRef
    client_id_sha256: Digest
    expected_customer: Annotated[str, Field(pattern=r'^[a-z0-9][a-z0-9-]{0,127}$')]
    expected_user_id: int = Field(ge=1, le=2**53-1)
    expected_role: str | None = Field(default=None, min_length=1, max_length=256)
    # Old observation descriptors remain byte-identical when versions are absent.
    client_id_version: SecretVersion | None = Field(default=None, exclude_if=lambda value: value is None)
    client_secret_version: SecretVersion | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode='after')
    def complete_version_mapping(self):
        if (self.client_id_version is None) != (self.client_secret_version is None):
            raise ValueError('Both immutable credential versions are required')
        return self


class DomoSource(StrictModel):
    provider: Literal['domo'] = 'domo'
    source_id: Identifier
    revision: int = Field(ge=1, le=2**31-1)
    context: Literal['consumer', 'fixture_test']
    api_origin: Literal['https://api.domo.com'] = 'https://api.domo.com'
    response_profile: Literal['domo-query-positional-v1'] = 'domo-query-positional-v1'
    auth: DomoOAuthConfig
    dataset_ids: Array[DatasetId] = Field(min_length=1, max_length=50)
    source_policy_digest: Digest
    auth_policy: Literal['shared-service-identity'] = 'shared-service-identity'
    max_dispatches: int = Field(ge=1, le=1_000_000)
    max_active_sends: int = Field(ge=1, le=100)

    @model_validator(mode='after')
    def distinct_datasets(self):
        if len(set(self.dataset_ids)) != len(self.dataset_ids):
            raise ValueError('Duplicate reviewed dataset identity')
        if self.auth.client_id_ref == self.auth.client_secret_ref:
            raise ValueError('Independent credential references are required')
        return self

    @property
    def digest(self):
        return canonical_digest(self)

    @property
    def capacity_pool_id(self):
        return 'domo-' + canonical_digest({'provider': 'domo', 'customer': self.auth.expected_customer})


def _error(code=ErrorCode.BINDING_DRIFT):
    # Keep module loading independent of the HTTP client's source-config import.
    from .domo_http import source_error
    return source_error(code)


class DomoStatement(StrictModel):
    statement_id: Identifier
    revision: int = Field(ge=1, le=2**31-1)
    text: str = Field(min_length=1, max_length=65_536, repr=False)
    sha256: Digest

    @model_validator(mode='after')
    def checked_bytes(self):
        if (len(self.text.encode('utf-8')) > 65_536
            or hashlib.sha256(self.text.encode('utf-8')).hexdigest() != self.sha256):
            raise ValueError('Statement bytes differ from the reviewed identity')
        return self


class DomoStatementCatalog:
    def __init__(self, statements):
        if type(statements) not in {list, tuple} or not 1 <= len(statements) <= 1000:
            raise _error()
        try:
            entries = [DomoStatement.model_validate(s.model_dump(mode='python')) for s in statements]
            self._entries = MappingProxyType({(s.statement_id,s.revision): s for s in entries})
            if len(self._entries) != len(entries): raise ValueError
            self._digest = canonical_digest(sorted(entries, key=lambda s:(s.statement_id,s.revision)))
        except (ValueError, TypeError, AttributeError):
            raise _error() from None

    @property
    def digest(self): return self._digest

    def resolve(self, statement_id, revision, digest):
        try:
            item = DomoStatement.model_validate(self._entries[(statement_id,revision)].model_dump(mode='python'))
            if item.sha256 != digest: raise ValueError
            return item
        except (ValueError, TypeError, KeyError, AttributeError):
            raise _error() from None


class DomoRuntimeColumn(StrictModel):
    field: Identifier
    source_name: str = Field(min_length=1, max_length=256)
    source_type: Literal['STRING','LONG','DOUBLE']
    conversion: Literal['string','long-integer','long-decimal0','double-number']

    @model_validator(mode='after')
    def compatible_conversion(self):
        if self.conversion not in {'STRING':{'string'}, 'LONG':{'long-integer','long-decimal0'},
                                  'DOUBLE':{'double-number'}}[self.source_type]:
            raise ValueError('Incompatible explicit source conversion')
        return self


class DomoRuntimeVariant(StrictModel):
    query_digest: Digest
    scope_digest: Digest
    decision_digest: Digest
    statement_id: Identifier
    statement_revision: int = Field(ge=1, le=2**31-1)
    statement_digest: Digest


class DomoCheckpoint(StrictModel):
    source_name: str = Field(min_length=1,max_length=256)
    producer_contract_digest: Digest


class DomoRuntimeBinding(StrictModel):
    binding_id: Identifier
    revision: int = Field(ge=1,le=2**31-1)
    adapter_revision: Literal['domo-named-v2'] = 'domo-named-v2'
    context: Literal['consumer','fixture_test']
    model_bundle_digest: Digest
    source_id: Identifier
    source_revision: int = Field(ge=1,le=2**31-1)
    source_digest: Digest
    owner: Identifier
    policy_id: Identifier
    tenant_id: Identifier
    dataset_id: DatasetId
    authoritative_definition_digest: Digest
    tenant_member: Identifier
    entitlement_member: Identifier
    columns: Array[DomoRuntimeColumn] = Field(min_length=2,max_length=50)
    grain_keys: Array[Identifier] = Field(min_length=1,max_length=49)
    time_field: Identifier
    input_fields: Annotated[dict[Identifier,Identifier],Field(max_length=48)] = Field(default_factory=dict)
    checkpoint: DomoCheckpoint | None = None
    variants: Array[DomoRuntimeVariant] = Field(min_length=1,max_length=1000)

    @model_validator(mode='after')
    def exact_mapping(self):
        names = {c.field for c in self.columns}
        source_names = {c.source_name for c in self.columns}
        if (len(names) != len(self.columns) or len(source_names) != len(self.columns)
            or self.tenant_member == self.entitlement_member
            or len(set(self.grain_keys)) != len(self.grain_keys)
            or not set(self.grain_keys) <= names or self.time_field not in self.grain_keys
            or any(value not in self.grain_keys for value in self.input_fields.values())
            or len({(v.query_digest,v.scope_digest,v.decision_digest) for v in self.variants}) != len(self.variants)):
            raise ValueError('Ambiguous reviewed Domo mapping')
        if self.checkpoint and (len(self.columns) >= 50 or self.checkpoint.source_name in source_names):
            raise ValueError('Checkpoint must fit the total wire-column bound')
        return self

    @property
    def digest(self): return canonical_digest(self)

    @property
    def constraint_members(self): return (self.tenant_member,self.entitlement_member)


def parse_domo_binding(value):
    if isinstance(value,DomoRuntimeBinding): value=value.model_dump(mode='python')
    return DomoRuntimeBinding.model_validate(value)


def check_domo_runtime_binding(binding, manifest, *, digest=None, source=None):
    try:
        b = parse_domo_binding(binding)
        m = Manifest.model_validate(manifest.model_dump(mode='python'))
        if ((b.binding_id,b.revision,b.adapter_revision,b.model_bundle_digest,b.owner,b.policy_id)
            != (m.binding_id,m.binding_revision,m.adapter_revision,m.model_bundle_digest,m.owner,m.policy_id)
            or digest is not None and b.digest != digest or {c.field for c in b.columns} != set(m.outputs)
            or m.grain != 'daily' or m.timezone != 'UTC' or m.date_boundaries is None
            or m.outputs[b.time_field].type != 'date'):
            raise ValueError
        expected={'string':{'string','date','timestamp','decimal'}, 'long-integer':{'integer'},
                  'long-decimal0':{'decimal'}, 'double-number':{'number'}}
        for c in b.columns:
            field=m.outputs[c.field]
            if (field.type not in expected[c.conversion]
                or c.conversion=='long-decimal0' and field.scale!=0
                or c.field in b.grain_keys and field.type not in {'string','date'}):
                raise ValueError
        bounds=m.date_boundaries
        if set(m.inputs)!={bounds.start_input,bounds.end_input}|set(b.input_fields): raise ValueError
        if any(m.inputs[name].type!=m.outputs[field].type for name,field in b.input_fields.items()): raise ValueError
        if source is not None:
            s=DomoSource.model_validate(source.model_dump(mode='python'))
            if ((s.source_id,s.revision,s.digest,s.context)!=(b.source_id,b.source_revision,b.source_digest,b.context)
                or b.dataset_id not in s.dataset_ids): raise ValueError
        return b
    except (ValueError,TypeError,KeyError,AttributeError,GatewayError):
        raise _error() from None


def validate_domo_scope(binding, decision):
    try:
        b=parse_domo_binding(binding)
        d=Decision.model_validate(decision.model_dump(mode='python'))
        constraints={c.member:c for c in d.row_constraints}
        if (len(d.row_constraints)!=2 or set(constraints)!=set(b.constraint_members)
            or d.tenant_id!=b.tenant_id or set(d.visible_fields)!={c.field for c in b.columns}
            or constraints[b.tenant_member].operator!='eq'
            or constraints[b.tenant_member].values!=(d.tenant_id,)
            or constraints[b.entitlement_member].operator!='in'):
            raise ValueError
        for constraint in constraints.values():
            if any(type(v) is not str for v in constraint.values) or len(set(constraint.values))!=len(constraint.values):
                raise ValueError
        return d
    except (ValueError,TypeError,KeyError,AttributeError):
        raise _error(ErrorCode.NOT_FOUND_OR_FORBIDDEN) from None


def validate_domo_decision(binding, manifest, decision):
    d = validate_domo_scope(binding, decision)
    if set(d.visible_fields) != set(manifest.outputs):
        raise _error(ErrorCode.NOT_FOUND_OR_FORBIDDEN)
    return d


def domo_request_digest(request):
    request=QueryRequest.model_validate(request.model_dump(mode='python'))
    return canonical_digest(request.model_dump(mode='python',exclude={'idempotency_key'}))


class PreparedDomoQuery(StrictModel):
    kind: Literal['domo-named-v2'] = 'domo-named-v2'
    context: Literal['consumer','fixture_test']
    accepted_spec_digest: Digest
    binding_id: Identifier
    binding_revision: int = Field(ge=1,le=2**31-1)
    binding_digest: Digest
    model_bundle_digest: Digest
    source_id: Identifier
    source_revision: int = Field(ge=1,le=2**31-1)
    source_digest: Digest
    dataset_id: DatasetId
    definition_digest: Digest
    query_digest: Digest
    scope_digest: Digest
    decision_digest: Digest
    statement_id: Identifier
    statement_revision: int = Field(ge=1,le=2**31-1)
    statement_digest: Digest
    response_profile: Literal['domo-query-positional-v1'] = 'domo-query-positional-v1'
    max_rows: int = Field(ge=1,le=1000)
    max_bytes: int = Field(ge=1,le=MAX_RESULT_BYTES)

    @property
    def digest(self): return canonical_digest(self)


def compile_domo_query(binding, product, request, decision, statements, *, source, request_id='domo-prepare'):
    from .base import authorization_scope_digest
    try:
        p=PinnedProduct.model_validate(product.model_dump(mode='python'))
        b=check_domo_runtime_binding(binding,p.manifest,digest=p.binding_digest,source=source)
        d=validate_domo_decision(b,p.manifest,decision)
        q=validate_query(p.manifest,request)
        if q.version not in {None,p.manifest.version}: raise _error(ErrorCode.INVALID_INPUT)
        q=q.model_copy(update={'version':p.manifest.version,'idempotency_key':None})
        bounds=p.manifest.date_boundaries
        if (iso_date(q.inputs[bounds.end_input])-iso_date(q.inputs[bounds.start_input])).days>d.limits.max_date_range_days:
            raise _error(ErrorCode.INVALID_INPUT)
        qdigest,sdigest=domo_request_digest(q),authorization_scope_digest(d)
        variants=[v for v in b.variants
            if (v.query_digest,v.scope_digest,v.decision_digest)==(qdigest,sdigest,canonical_digest(d))]
        if len(variants)!=1: raise _error(ErrorCode.INVALID_INPUT)
        v=variants[0]
        if type(statements) is not DomoStatementCatalog or statements.digest!=b.model_bundle_digest: raise _error()
        statements.resolve(v.statement_id,v.statement_revision,v.statement_digest)
        query=q.model_dump(mode='json',exclude={'idempotency_key'})
        spec=ExecutionSpec(principal_id=d.principal_id,pinned_product=p,canonical_query=query,
            canonical_hash=canonical_digest(query),policy_revision=d.policy_revision,context=b.context,request_id=request_id)
        return PreparedDomoQuery(context=b.context,accepted_spec_digest=canonical_digest(spec),
            binding_id=b.binding_id,binding_revision=b.revision,binding_digest=b.digest,
            model_bundle_digest=b.model_bundle_digest,source_id=b.source_id,source_revision=b.source_revision,
            source_digest=b.source_digest,dataset_id=b.dataset_id,definition_digest=b.authoritative_definition_digest,
            query_digest=qdigest,scope_digest=sdigest,decision_digest=canonical_digest(d),
            statement_id=v.statement_id,statement_revision=v.statement_revision,statement_digest=v.statement_digest,
            max_rows=min(p.manifest.limits.max_rows,d.limits.max_rows),
            max_bytes=min(p.manifest.limits.max_bytes,d.limits.max_bytes))
    except GatewayError:
        raise
    except (ValueError,TypeError,KeyError,AttributeError):
        raise _error() from None


def validate_domo_source_isolation(consumer, fixture):
    """Validate configured isolation, never claim actual shares/PDP/equivalence."""
    try:
        c = DomoSource.model_validate(consumer.model_dump(mode='python'))
        f = DomoSource.model_validate(fixture.model_dump(mode='python'))
        if (c.context != 'consumer' or f.context != 'fixture_test'
            or c.source_id == f.source_id or set(c.dataset_ids) & set(f.dataset_ids)
            or c.auth.client_id_sha256 == f.auth.client_id_sha256
            or (c.auth.expected_customer, c.auth.expected_user_id)
               == (f.auth.expected_customer, f.auth.expected_user_id)):
            raise ValueError
        return c, f
    except (ValueError, TypeError, AttributeError):
        raise _error() from None


def validate_domo_prepared(prepared, spec, executed_decision, binding):
    """Reconstruct all non-SQL references from the accepted trusted context.

    Before I/O the adapter additionally recompiles with the retained exact SQL
    catalog. This helper binds normalization to that accepted preparation.
    """
    from .base import authorization_scope_digest
    try:
        spec=ExecutionSpec.model_validate(spec.model_dump(mode='python'))
        b=check_domo_runtime_binding(binding,spec.pinned_product.manifest,digest=spec.pinned_product.binding_digest)
        d=validate_domo_decision(b,spec.pinned_product.manifest,executed_decision)
        p=PreparedDomoQuery.model_validate(prepared.model_dump(mode='python'))
        if (spec.context!=b.context or spec.principal_id!=d.principal_id or spec.policy_revision!=d.policy_revision):
            raise ValueError
        q=QueryRequest.model_validate(spec.canonical_query)
        qdigest,sdigest,ddigest=domo_request_digest(q),authorization_scope_digest(d),canonical_digest(d)
        variants=[v for v in b.variants if (v.query_digest,v.scope_digest,v.decision_digest)==(qdigest,sdigest,ddigest)]
        if len(variants)!=1:
            raise ValueError
        v=variants[0]
        expected=PreparedDomoQuery(context=b.context,accepted_spec_digest=canonical_digest(spec),
            binding_id=b.binding_id,binding_revision=b.revision,binding_digest=b.digest,
            model_bundle_digest=b.model_bundle_digest,source_id=b.source_id,source_revision=b.source_revision,
            source_digest=b.source_digest,dataset_id=b.dataset_id,definition_digest=b.authoritative_definition_digest,
            query_digest=qdigest,scope_digest=sdigest,decision_digest=ddigest,
            statement_id=v.statement_id,statement_revision=v.statement_revision,statement_digest=v.statement_digest,
            max_rows=min(spec.pinned_product.manifest.limits.max_rows,d.limits.max_rows),
            max_bytes=min(spec.pinned_product.manifest.limits.max_bytes,d.limits.max_bytes))
        if p!=expected:
            raise ValueError
        return p
    except GatewayError:
        raise
    except (ValueError,TypeError,AttributeError,KeyError):
        raise _error() from None
