"""Offline-only reviewed Domo named queries; no HTTP/authentication factory.

The finite selector copies immutable operator-reviewed statements, never formats
SQL. Injected transports and authorization gates make the local boundary testable;
none is installed by a runtime. These contracts prove neither Domo PDP equivalence
nor enforceable billing, cancellation, source lifetime, or actual source freshness.
"""
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from types import MappingProxyType
from typing import Annotated, Literal, Protocol

from pydantic import Field, TypeAdapter, model_validator

from gateway.contracts.canonical import (Array, AwareTimestamp, Digest, Identifier,
    MAX_RESULT_BYTES, Scalar, StrictModel, bounded_json, canonical_bytes, canonical_digest, timestamp)
from gateway.contracts.clock import SystemClock
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.execution import FreshnessAssessment, PinnedProduct
from gateway.contracts.identity import Decision, Principal
from gateway.contracts.manifest import FieldSchema, validate_row
from gateway.contracts.query import QueryRequest, validate_query
from .base import authorization_scope_digest
from .normalize import normalize_value

DatasetId = Annotated[str, Field(pattern=r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')]


def _error(code, request_id='domo-contract'):
    messages = {
        ErrorCode.UNAUTHENTICATED: 'A valid identity is required.',
        ErrorCode.CONFLICT: 'The current operation conflicts with retained state.',
        ErrorCode.INVALID_INPUT: 'The reviewed named query is unavailable for these inputs.',
        ErrorCode.BINDING_DRIFT: 'The reviewed Domo definition or response is incompatible.',
        ErrorCode.NOT_FOUND_OR_FORBIDDEN: 'The current scope cannot access this product.',
        ErrorCode.UPSTREAM_UNAVAILABLE: 'The source or current authorization is unavailable.',
        ErrorCode.RESOURCE_LIMIT: 'The complete result exceeds its approved limits.',
        ErrorCode.EXECUTION_TIMEOUT: 'The bounded read deadline expired.',
        ErrorCode.FRESHNESS_UNKNOWN: 'The required source-load freshness could not be established.',
        ErrorCode.STALE_DATA: 'The recorded source-load watermark is too old.',
    }
    return GatewayError(code, request_id, messages[ErrorCode(code)])


class ReviewedStatement(StrictModel):
    statement_id: Identifier
    revision: int = Field(ge=1, le=2**31-1)
    text: str = Field(min_length=1, max_length=65_536, repr=False)
    sha256: Digest

    @model_validator(mode='after')
    def immutable_bytes(self):
        if len(self.text.encode('utf-8')) > 65_536 or hashlib.sha256(self.text.encode('utf-8')).hexdigest() != self.sha256:
            raise ValueError('The retained statement bytes do not match the reviewed digest')
        return self


class StatementCatalog:
    """Bounded operator assets, not an agent statement-upload interface."""
    def __init__(self, statements: list[ReviewedStatement]):
        if not isinstance(statements, (list, tuple)) or not 1 <= len(statements) <= 1000:
            raise _error(ErrorCode.BINDING_DRIFT)
        try:
            entries = [ReviewedStatement.model_validate(item.model_dump(mode='python')) for item in statements]
            self._entries = MappingProxyType({(item.statement_id, item.revision): item for item in entries})
            if len(self._entries) != len(entries): raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise _error(ErrorCode.BINDING_DRIFT) from None

    def resolve(self, statement_id: str, revision: int, digest: str) -> ReviewedStatement:
        try:
            item = self._entries[(statement_id, revision)]
            item = ReviewedStatement.model_validate(item.model_dump(mode='python'))
            if item.sha256 != digest: raise ValueError
            return item
        except (KeyError, ValueError, TypeError):
            raise _error(ErrorCode.BINDING_DRIFT) from None


class DomoQueryVariant(StrictModel):
    query_digest: Digest
    scope_digest: Digest
    statement_id: Identifier
    statement_revision: int = Field(ge=1, le=2**31-1)
    statement_digest: Digest


class DomoColumn(StrictModel):
    field: Identifier
    source_name: str = Field(min_length=1, max_length=256)
    source_type: Literal['STRING', 'LONG', 'DOUBLE']


class DomoNamedQueryBinding(StrictModel):
    binding_id: Identifier
    revision: int = Field(ge=1, le=2**31-1)
    adapter_revision: Literal['domo-named-v1'] = 'domo-named-v1'
    model_bundle_digest: Digest
    owner: Identifier
    policy_id: Identifier
    tenant_id: Identifier
    dataset_id: DatasetId
    authoritative_definition_digest: Digest
    tenant_member: Identifier
    entitlement_member: Identifier
    columns: Array[DomoColumn] = Field(min_length=1, max_length=50)
    variants: Array[DomoQueryVariant] = Field(min_length=1, max_length=1000)

    @model_validator(mode='after')
    def distinct_contract(self):
        if (self.tenant_member == self.entitlement_member
            or len({c.field for c in self.columns}) != len(self.columns)
            or len({c.source_name for c in self.columns}) != len(self.columns)
            or len({(v.query_digest, v.scope_digest) for v in self.variants}) != len(self.variants)):
            raise ValueError('The named query requires unambiguous independent mappings')
        return self

    @property
    def digest(self):
        return canonical_digest(self)


class DomoReadPlan(StrictModel):
    dataset_id: DatasetId
    binding_digest: Digest
    definition_digest: Digest
    query_digest: Digest
    scope_digest: Digest
    statement: ReviewedStatement = Field(repr=False)
    max_rows: int = Field(ge=1, le=1000)
    max_bytes: int = Field(ge=1, le=MAX_RESULT_BYTES)


def request_digest(request: QueryRequest) -> str:
    request = QueryRequest.model_validate(request.model_dump(mode='python'))
    return canonical_digest(request.model_dump(mode='python', exclude={'idempotency_key'}))


def check_domo_binding(binding: DomoNamedQueryBinding, product: PinnedProduct) -> None:
    try:
        binding = DomoNamedQueryBinding.model_validate(binding.model_dump(mode='python'))
        product = PinnedProduct.model_validate(product.model_dump(mode='python'))
        m = product.manifest
        if ((binding.binding_id, binding.revision, binding.adapter_revision, binding.model_bundle_digest,
             binding.owner, binding.policy_id) != (m.binding_id, m.binding_revision, m.adapter_revision,
             m.model_bundle_digest, m.owner, m.policy_id) or product.binding_digest != binding.digest
             or {c.field for c in binding.columns} != set(m.outputs)):
            raise ValueError
        source_types = {'STRING': {'string', 'date', 'timestamp', 'decimal'}, 'LONG': {'integer'}, 'DOUBLE': {'number'}}
        if any(m.outputs[c.field].type not in source_types[c.source_type] for c in binding.columns):
            raise ValueError
    except (ValueError, TypeError, AttributeError, GatewayError):
        raise _error(ErrorCode.BINDING_DRIFT) from None


def _scope(binding, manifest, decision):
    try:
        decision = Decision.model_validate(decision.model_dump(mode='python'))
        by_member = {item.member: item for item in decision.row_constraints}
        if (decision.tenant_id != binding.tenant_id or len(decision.row_constraints) != 2
            or set(by_member) != {binding.tenant_member, binding.entitlement_member}
            or by_member[binding.tenant_member].operator != 'eq'
            or by_member[binding.tenant_member].values != (decision.tenant_id,)
            or by_member[binding.entitlement_member].operator != 'in'
            or set(decision.visible_fields) != set(manifest.outputs)):
            raise ValueError
        values = by_member[binding.entitlement_member].values
        if len({canonical_bytes(value) for value in values}) != len(values): raise ValueError
        return decision
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _error(ErrorCode.NOT_FOUND_OR_FORBIDDEN) from None


def compile_named_query(binding, product, request, decision, statements) -> DomoReadPlan:
    """Select exact reviewed bytes. No dynamic dates, parameters or SQL rendering."""
    check_domo_binding(binding, product)
    m = product.manifest
    request = validate_query(m, request)
    request = request.model_copy(update={'version': m.version})
    decision = _scope(binding, m, decision)
    if m.date_boundaries:
        from gateway.contracts.manifest import iso_date
        bounds = m.date_boundaries
        if (iso_date(request.inputs[bounds.end_input]) - iso_date(request.inputs[bounds.start_input])).days > decision.limits.max_date_range_days:
            raise _error(ErrorCode.INVALID_INPUT)
    qdigest, sdigest = request_digest(request), authorization_scope_digest(decision)
    variants = [v for v in binding.variants if (v.query_digest, v.scope_digest) == (qdigest, sdigest)]
    if len(variants) != 1: raise _error(ErrorCode.INVALID_INPUT)
    v = variants[0]
    try:
        statement = statements.resolve(v.statement_id, v.statement_revision, v.statement_digest)
        statement = ReviewedStatement.model_validate(statement.model_dump(mode='python'))
        if ((statement.statement_id, statement.revision, statement.sha256)
            != (v.statement_id, v.statement_revision, v.statement_digest)):
            raise ValueError
    except Exception:
        raise _error(ErrorCode.BINDING_DRIFT) from None
    return DomoReadPlan(dataset_id=binding.dataset_id, binding_digest=binding.digest,
        definition_digest=binding.authoritative_definition_digest, query_digest=qdigest, scope_digest=sdigest,
        statement=statement, max_rows=min(m.limits.max_rows, decision.limits.max_rows),
        max_bytes=min(m.limits.max_bytes, decision.limits.max_bytes))


class DomoFreshnessEvidence(StrictModel):
    """Trusted source-load checkpoint tied to this exact response, never fromcache.

    Supplying this type is not evidence that a live provider can establish it.
    The future transport must independently prove checkpoint/row coherence.
    """
    binding_digest: Digest
    definition_digest: Digest
    query_digest: Digest
    scope_digest: Digest
    response_sha256: Digest
    source_watermark: AwareTimestamp


class DomoValidatedResult(StrictModel):
    binding_digest: Digest
    query_digest: Digest
    scope_digest: Digest
    typed_schema: Annotated[dict[Identifier, FieldSchema], Field(min_length=1, max_length=50)]
    rows: Array[dict[Identifier, Scalar]] = Field(max_length=1000, repr=False)
    source_watermark: AwareTimestamp | None
    freshness: FreshnessAssessment
    cache_status: Literal['hit', 'miss', 'unknown']

    @model_validator(mode='after')
    def bounded_rows(self):
        bounded_json(self.rows, max_nodes=100_000)
        for row in self.rows: validate_row(self.typed_schema, row)
        return self


class _Metadata(StrictModel):
    type: Literal['STRING', 'LONG', 'DOUBLE']
    dataSourceId: DatasetId
    maxLength: int | None = Field(default=None, ge=-1, le=2**31-1)
    minLength: int | None = Field(default=None, ge=-1, le=2**31-1)
    periodIndex: int | None = Field(default=None, ge=0, le=2**31-1)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('Duplicate response key')
        result[key] = value
    return result


def validate_domo_response(binding, product, plan, body: bytes, evidence, *, now) -> DomoValidatedResult:
    check_domo_binding(binding, product)
    m = product.manifest
    try:
        plan = DomoReadPlan.model_validate(plan.model_dump(mode='python'))
        if ((plan.dataset_id, plan.binding_digest, plan.definition_digest)
            != (binding.dataset_id, binding.digest, binding.authoritative_definition_digest)
            or plan.max_rows > m.limits.max_rows or plan.max_bytes > m.limits.max_bytes
            or not any((v.query_digest, v.scope_digest, v.statement_id, v.statement_revision, v.statement_digest)
                == (plan.query_digest, plan.scope_digest, plan.statement.statement_id,
                    plan.statement.revision, plan.statement.sha256) for v in binding.variants)):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise _error(ErrorCode.BINDING_DRIFT) from None
    if type(body) is not bytes or len(body) > plan.max_bytes: raise _error(ErrorCode.RESOURCE_LIMIT)
    try:
        data = json.loads(body, object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        bounded_json(data, max_nodes=100_000)
        if (not isinstance(data, dict) or set(data) != {'datasource','columns','metadata','rows','numRows','numColumns','fromcache'}
            or data['datasource'] != binding.dataset_id or data['columns'] != [c.source_name for c in binding.columns]
            or type(data['numRows']) is not int or type(data['numColumns']) is not int
            or type(data['fromcache']) is not bool or not isinstance(data['rows'], list)
            or not isinstance(data['metadata'], list) or data['numRows'] != len(data['rows'])
            or data['numColumns'] != len(binding.columns) or len(data['metadata']) != len(binding.columns)):
            raise ValueError
        if len(data['rows']) > plan.max_rows: raise _error(ErrorCode.RESOURCE_LIMIT)
        for col, meta in zip(binding.columns, data['metadata'], strict=True):
            meta = _Metadata.model_validate(meta)
            if meta.type != col.source_type or meta.dataSourceId != binding.dataset_id: raise ValueError
        rows = []
        for raw in data['rows']:
            if not isinstance(raw, list) or len(raw) != len(binding.columns): raise ValueError
            rows.append({col.field: normalize_value(m.outputs[col.field], value)
                for col, value in zip(binding.columns, raw, strict=True)})
    except GatewayError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise _error(ErrorCode.BINDING_DRIFT) from None
    watermark = None
    try:
        if evidence is not None:
            proof = DomoFreshnessEvidence.model_validate(evidence.model_dump(mode='python'))
            if ((proof.binding_digest, proof.definition_digest, proof.query_digest, proof.scope_digest, proof.response_sha256)
                != (plan.binding_digest, plan.definition_digest, plan.query_digest, plan.scope_digest, hashlib.sha256(body).hexdigest())):
                raise ValueError
            watermark = proof.source_watermark
    except (ValueError, TypeError, AttributeError):
        raise _error(ErrorCode.FRESHNESS_UNKNOWN) from None
    now = timestamp(now)
    age = (now-watermark).total_seconds() if watermark is not None else None
    known = age is not None and age >= 0
    status = 'unknown' if not known else 'fresh' if age <= m.freshness.max_age_seconds else 'stale'
    if m.freshness.required and status != 'fresh':
        raise _error(ErrorCode.STALE_DATA if status == 'stale' else ErrorCode.FRESHNESS_UNKNOWN)
    result = DomoValidatedResult(binding_digest=plan.binding_digest, query_digest=plan.query_digest,
        scope_digest=plan.scope_digest, typed_schema=m.outputs, rows=rows, source_watermark=watermark,
        freshness=FreshnessAssessment(status=status, assessed_at=now, age_seconds=age if known else None,
            max_age_seconds=m.freshness.max_age_seconds), cache_status='hit' if data['fromcache'] else 'miss')
    if len(canonical_bytes(result)) > plan.max_bytes: raise _error(ErrorCode.RESOURCE_LIMIT)
    return result


class DomoResponseHead(StrictModel):
    status_code: int = Field(ge=100, le=599)
    content_encoding: Literal['identity', 'gzip', 'br'] = 'identity'
    media_type: Literal['application/json', 'application/octet-stream'] = 'application/json'
    redirected: bool = False
    transcoded: bool = False


@dataclass(frozen=True)
class DomoResponse:
    head: DomoResponseHead
    body: AsyncIterator[bytes]
    freshness: DomoFreshnessEvidence | None = None


class DomoTransport(Protocol):
    """A trusted cooperative transport performing exactly one source request.

    It must bound raw bytes, refuse redirects, transformations and internal retries,
    honor the supplied deadline, and provide an aclose-capable response stream.
    A production implementation and source-completion evidence are absent here.
    """
    async def query(self, plan: DomoReadPlan, *, deadline: datetime, max_bytes: int) -> DomoResponse: ...


AuthorizationGate = Callable[[Principal, PinnedProduct, Literal['execute', 'result'], str], Awaitable[Decision]]


class DomoReader:
    """One bounded injected request; no retries, registration, credentials or cache."""
    def __init__(self, *, binding, statements, transport, authorize: AuthorizationGate, clock=None):
        if (type(statements) is not StatementCatalog or transport is None
            or not callable(getattr(transport, 'query', None)) or not callable(authorize)):
            raise ValueError('A retained statement catalog, explicit transport and current authorization gate are required')
        self.binding = DomoNamedQueryBinding.model_validate(binding.model_dump(mode='python'))
        self.statements, self.transport, self.authorize = statements, transport, authorize
        self.clock = clock or SystemClock()

    async def read(self, product, request, principal, *, deadline, request_id):
        response = None
        close_attempted = False
        safe_request_id = 'domo-read'
        try:
            # Bounds and identifiers are checked before authority or source calls.
            principal = Principal.model_validate(principal.model_dump(mode='python'))
            product = PinnedProduct.model_validate(product.model_dump(mode='python'))
            request_id = IdentifierAdapter.validate_python(request_id)
            safe_request_id = request_id
            now = timestamp(self.clock.now())
            budget = min(120, product.manifest.limits.execution_timeout_seconds, (timestamp(deadline)-now).total_seconds())
            if budget <= 0: raise _error(ErrorCode.EXECUTION_TIMEOUT, request_id)
            start = asyncio.get_running_loop().time()
            async with asyncio.timeout_at(start+budget) as timeout:
                async def gate(action):
                    # Give injected code snapshots, never mutable aliases to the
                    # pinned authority used after that code returns.
                    return await self.authorize(
                        Principal.model_validate(principal.model_dump(mode='python')),
                        PinnedProduct.model_validate(product.model_dump(mode='python')),
                        action, request_id)
                before = await gate('execute')
                before = Decision.model_validate(before.model_dump(mode='python'))
                if before.principal_id != principal.id: raise _error(ErrorCode.NOT_FOUND_OR_FORBIDDEN)
                budget = min(budget, before.limits.execution_timeout_seconds)
                timeout.reschedule(start+budget)
                def check_deadline():
                    if (asyncio.get_running_loop().time() >= start+budget
                        or timestamp(self.clock.now()) >= now+timedelta(seconds=budget)):
                        raise _error(ErrorCode.EXECUTION_TIMEOUT)
                check_deadline()
                plan = compile_named_query(self.binding, product, request, before, self.statements)
                response = await self.transport.query(DomoReadPlan.model_validate(plan.model_dump(mode='python')), deadline=now+timedelta(seconds=budget), max_bytes=plan.max_bytes)
                if (not isinstance(response, DomoResponse)
                    or not callable(getattr(response.body, 'aclose', None))):
                    raise _error(ErrorCode.UPSTREAM_UNAVAILABLE)
                head = DomoResponseHead.model_validate(response.head.model_dump(mode='python'))
                if (head.status_code != 200 or head.content_encoding != 'identity'
                    or head.media_type != 'application/json' or head.redirected or head.transcoded):
                    raise _error(ErrorCode.UPSTREAM_UNAVAILABLE)
                body = bytearray()
                async for chunk in response.body:
                    if type(chunk) is not bytes: raise _error(ErrorCode.UPSTREAM_UNAVAILABLE)
                    if len(body)+len(chunk) > plan.max_bytes: raise _error(ErrorCode.RESOURCE_LIMIT)
                    body.extend(chunk)
                    check_deadline()
                check_deadline()
                close_attempted = True
                await response.body.aclose()
                check_deadline()
                after = await gate('result')
                after = Decision.model_validate(after.model_dump(mode='python'))
                check_deadline()
                if canonical_digest(before) != canonical_digest(after): raise _error(ErrorCode.NOT_FOUND_OR_FORBIDDEN)
                result = validate_domo_response(self.binding, product, plan, bytes(body), response.freshness, now=self.clock.now())
                check_deadline()
                return result
        except TimeoutError:
            raise _error(ErrorCode.EXECUTION_TIMEOUT, safe_request_id) from None
        except GatewayError as error:
            raise _error(error.code, safe_request_id) from None
        except Exception:
            raise _error(ErrorCode.UPSTREAM_UNAVAILABLE, safe_request_id) from None
        finally:
            if isinstance(response, DomoResponse) and not close_attempted:
                close = getattr(response.body, 'aclose', None)
                if close:
                    try:
                        async with asyncio.timeout(1): await close()
                    except Exception:
                        pass


# A bounded public-style identifier is used only as safe internal correlation.
IdentifierAdapter = TypeAdapter(Identifier)
