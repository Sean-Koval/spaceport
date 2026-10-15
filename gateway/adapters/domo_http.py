"""Bounded documented Domo positional wire profile; never a public SQL API.

A parsed response is a protocol candidate, not current authorization or durable
source settlement. The shared lifecycle must record exact-intent terminal proof.
"""
import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import math
import re
from time import monotonic
from types import MappingProxyType

import httpx

from gateway.contracts.canonical import MAX_RESULT_BYTES, timestamp, canonical_digest, json_value
from gateway.contracts.clock import SystemClock
from .domo_runtime import DomoSource
from gateway.contracts.errors import ErrorCode, GatewayError

_DATASET = re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}')
_TYPES = {'STRING', 'LONG', 'DOUBLE', 'DATETIME'}


def source_error(code=ErrorCode.BINDING_DRIFT, request_id='domo-source'):
    messages = {
        ErrorCode.BINDING_DRIFT: 'The reviewed Domo response or source identity is incompatible.',
        ErrorCode.RESOURCE_LIMIT: 'The complete source response exceeds its approved bound.',
        ErrorCode.UPSTREAM_UNAVAILABLE: 'The configured source is unavailable.',
        ErrorCode.EXECUTION_TIMEOUT: 'The bounded source deadline expired.',
        ErrorCode.NOT_FOUND_OR_FORBIDDEN: 'The current scope cannot access this product.',
        ErrorCode.INVALID_INPUT: 'The reviewed named query is unavailable for these inputs.',
        ErrorCode.FRESHNESS_UNKNOWN: 'Required source freshness could not be established.',
        ErrorCode.STALE_DATA: 'The recorded source checkpoint is too old.',
    }
    return GatewayError(code, request_id, messages[code])


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _json(body):
    # Decode explicitly: json.loads(bytes) otherwise accepts UTF-16/UTF-32.
    return json.loads(body.decode('utf-8'), object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


@dataclass(frozen=True, init=False)
class DomoProtocolResult:
    dataset_id: str
    columns: tuple[str, ...] = field(repr=False)
    source_types: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...] = field(repr=False)
    response_sha256: str
    cache_status: str
    profile: str = 'domo-query-positional-v1'
    _raw_body: bytes = field(repr=False, default=b'')

    def __init__(self, *args, **kwargs):
        raise TypeError('Protocol evidence must come from the bounded response parser')


def parse_domo_success(body: bytes, *, dataset_id: str, max_rows: int,
                       max_bytes: int, request_id='domo-source') -> DomoProtocolResult:
    """Recognize one complete documented-example success, including one sentinel.

    JSON LONG tokens retain Python integer precision without JCS, JS, float or
    broad bounded_json conversion. Product normalization then emits exact text.
    """
    try:
        if (type(dataset_id) is not str or not _DATASET.fullmatch(dataset_id)
            or type(max_rows) is not int or not 1 <= max_rows <= 1000
            or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_RESULT_BYTES):
            raise ValueError
        if type(body) is not bytes:
            raise ValueError
        if len(body) > max_bytes:
            raise source_error(ErrorCode.RESOURCE_LIMIT, request_id)
        value = _json(body)
        if (type(value) is not dict or set(value) != {
            'datasource', 'columns', 'metadata', 'rows', 'numRows', 'numColumns', 'fromcache'}
            or value['datasource'] != dataset_id or type(value['fromcache']) is not bool):
            raise ValueError
        columns, metadata, rows = value['columns'], value['metadata'], value['rows']
        if (type(columns) is not list or not 1 <= len(columns) <= 50
            or any(type(c) is not str or not 1 <= len(c) <= 256 for c in columns)
            or len(set(columns)) != len(columns) or type(metadata) is not list
            or len(metadata) != len(columns) or type(rows) is not list
            or len(rows) > max_rows + 1 or type(value['numRows']) is not int
            or value['numRows'] != len(rows) or type(value['numColumns']) is not int
            or value['numColumns'] != len(columns)):
            raise ValueError
        for column in columns:
            column.encode('utf-8')
        source_types = []
        for meta in metadata:
            if (type(meta) is not dict or not {'type', 'dataSourceId'} <= set(meta)
                or set(meta) - {'type', 'dataSourceId', 'maxLength', 'minLength', 'periodIndex'}
                or type(meta['type']) is not str or meta['type'] not in _TYPES
                or meta['dataSourceId'] != dataset_id):
                raise ValueError
            for key, minimum in [('maxLength', -1), ('minLength', -1), ('periodIndex', 0)]:
                if key in meta and (type(meta[key]) is not int or not minimum <= meta[key] <= 2**31-1):
                    raise ValueError
            source_types.append(meta['type'])
        for row in rows:
            if type(row) is not list or len(row) != len(columns):
                raise ValueError
            for kind, cell in zip(source_types, row, strict=True):
                if cell is None:
                    continue
                if kind in {'STRING', 'DATETIME'}:
                    if type(cell) is not str or len(cell) > 4096:
                        raise ValueError
                    cell.encode('utf-8')
                elif kind == 'LONG':
                    if type(cell) is not int or not -(2**63) <= cell < 2**63:
                        raise ValueError
                elif type(cell) not in {int, float} or not math.isfinite(cell):
                    raise ValueError
        result = object.__new__(DomoProtocolResult)
        for key, item in dict(dataset_id=dataset_id, columns=tuple(columns), source_types=tuple(source_types),
            rows=tuple(tuple(row) for row in rows), response_sha256=hashlib.sha256(body).hexdigest(),
            cache_status='hit' if value['fromcache'] else 'miss', profile='domo-query-positional-v1',
            _raw_body=body).items():
            object.__setattr__(result, key, item)
        return result
    except GatewayError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
        raise source_error(ErrorCode.BINDING_DRIFT, request_id) from None


def validate_domo_evidence(value):
    """Rebind all retained fields to the complete immutable raw response bytes.

    This defensive internal contract prevents stale/mutated projections from
    carrying the original response proof. It is not a Python process sandbox.
    """
    try:
        if type(value) is not DomoProtocolResult:
            raise ValueError
        checked = parse_domo_success(value._raw_body, dataset_id=value.dataset_id,
            max_rows=1000, max_bytes=MAX_RESULT_BYTES)
        if value != checked:
            raise ValueError
        return checked
    except (ValueError, TypeError, AttributeError):
        raise source_error() from None


@dataclass(frozen=True)
class DomoToken:
    value: str = field(repr=False)
    source_digest: str
    expires_at: datetime


def _printable(value, maximum):
    return type(value) is str and 1 <= len(value) <= maximum and all(33 <= ord(c) <= 126 for c in value)


class DomoHttpClient:
    """Server-private bounded client. Construction never reads secrets or sends.

    No ambient proxies, HTTP redirects, provider retry or OAuth token persistence.
    The first revision intentionally does not cache tokens between executions.
    """
    def __init__(self, *, source: DomoSource, secret_resolver: Callable[[str], str],
                 transport=None, clock=None):
        self._source = DomoSource.model_validate(source.model_dump(mode='python'))
        self._auth = DomoSource.model_validate(source.model_dump(mode='python')).auth
        self._monotonic_deadline = None
        self._absolute_deadline = None
        self._query_started = False
        self._success = None
        if not callable(secret_resolver):
            raise ValueError('A private secret resolver is required')
        self._secrets = secret_resolver
        self.clock = clock or SystemClock()
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False,
            trust_env=False, timeout=httpx.Timeout(10.0), headers={'Accept-Encoding': 'identity'})

    @property
    def source(self):
        # Callers receive a detached validated value, never our transport identity.
        return DomoSource.model_validate(self._source.model_dump(mode='python'))

    def _budget(self, deadline):
        deadline = timestamp(deadline)
        self._absolute_deadline = min(deadline, self._absolute_deadline) if self._absolute_deadline else deadline
        now = monotonic()
        candidate = now + min(120.0, (self._absolute_deadline-timestamp(self.clock.now())).total_seconds())
        self._monotonic_deadline = min(candidate, self._monotonic_deadline) if self._monotonic_deadline is not None else candidate
        seconds = self._monotonic_deadline-now
        if seconds <= 0:
            raise source_error(ErrorCode.EXECUTION_TIMEOUT)
        return seconds

    async def _body(self, method, path, *, deadline, max_bytes, **kwargs):
        budget = self._budget(deadline)
        cleanup_deadline = asyncio.get_running_loop().time() + budget
        request = self._client.build_request(method, 'https://api.domo.com'+path, timeout=budget,
                                             **{k:v for k,v in kwargs.items() if k != 'auth'})
        response = await self._client.send(request, stream=True, **({'auth': kwargs['auth']} if 'auth' in kwargs else {}))
        try:
            if (response.status_code != 200 or response.history
                or response.headers.get('content-encoding', 'identity').lower() != 'identity'
                or not re.fullmatch(r'application/json(?:;\s*charset="?utf-8"?)?',
                    response.headers.get('content-type', '').lower().strip())):
                raise source_error(ErrorCode.UPSTREAM_UNAVAILABLE)
            length = response.headers.get('content-length')
            if length is not None:
                if not length.isdecimal() or len(length) > 12:
                    raise source_error(ErrorCode.UPSTREAM_UNAVAILABLE)
                if int(length) > max_bytes:
                    raise source_error(ErrorCode.RESOURCE_LIMIT)
            output = bytearray()
            async for chunk in response.aiter_raw():
                if len(output)+len(chunk) > max_bytes:
                    raise source_error(ErrorCode.RESOURCE_LIMIT)
                output.extend(chunk)
                self._budget(deadline)
            self._budget(deadline)
            return bytes(output)
        finally:
            # A response stream may itself stall in aclose after read cancellation.
            # Cleanup gets only the original remaining deadline, never a fresh wait.
            remaining = max(0.0, min(.05, cleanup_deadline-asyncio.get_running_loop().time()))
            try:
                async with asyncio.timeout(remaining):
                    await response.aclose()
            except (TimeoutError, httpx.HTTPError):
                pass

    async def acquire_token(self, *, deadline) -> DomoToken:
        try:
            budget = self._budget(deadline)
            async with asyncio.timeout(budget):
                auth = self._auth
                client_id = self._secrets(auth.client_id_ref)
                secret = self._secrets(auth.client_secret_ref)
                if (not _printable(client_id, 256) or ':' in client_id
                    or hashlib.sha256(client_id.encode()).hexdigest() != auth.client_id_sha256
                    or not _printable(secret, 8192)):
                    raise source_error(ErrorCode.UPSTREAM_UNAVAILABLE)
                body = await self._body('POST', '/oauth/token', deadline=deadline,
                    max_bytes=16_384, auth=httpx.BasicAuth(client_id, secret),
                    data={'grant_type': 'client_credentials', 'scope': 'data'},
                    headers={'Accept': 'application/json'})
                value = _json(body)
                required = {'access_token', 'customer', 'userId', 'expires_in', 'token_type', 'scope'}
                if (type(value) is not dict or not required <= set(value)
                    or set(value)-required-{'role', 'jti'}
                    or not _printable(value['access_token'], 8192)
                    or value['customer'] != auth.expected_customer
                    or type(value['userId']) is not int or value['userId'] != auth.expected_user_id
                    or type(value['expires_in']) is not int or not 31 <= value['expires_in'] <= 86_400
                    or type(value['token_type']) is not str or value['token_type'].lower() != 'bearer'
                    or type(value['scope']) is not str or value['scope'].split() != ['data']):
                    raise ValueError
                for key in ('role', 'jti'):
                    if key in value and not _printable(value[key], 256):
                        raise ValueError
                if auth.expected_role is not None and value.get('role') != auth.expected_role:
                    raise ValueError
                self._budget(deadline)
                expires = timestamp(self.clock.now()) + timedelta(seconds=min(value['expires_in']-30, 300))
                if expires < timestamp(deadline):
                    raise ValueError
                return DomoToken(value['access_token'], self._source.digest, expires)
        except TimeoutError:
            raise source_error(ErrorCode.EXECUTION_TIMEOUT) from None
        except GatewayError:
            raise
        except Exception:
            raise source_error(ErrorCode.UPSTREAM_UNAVAILABLE) from None

    async def query(self, *, plan, statement, token, permit) -> DomoProtocolResult:
        """One exact synchronous POST. A return is evidence, not DB settlement.

        The trusted caller checks current registry authority immediately before
        this method. No failure here permits a retry, refresh or replayed POST.
        """
        from gateway.source_lifecycle import DomoSendPermit
        from .domo_runtime import PreparedDomoQuery, DomoStatement
        try:
            plan = PreparedDomoQuery.model_validate(plan.model_dump(mode='python'))
            statement = DomoStatement.model_validate(statement.model_dump(mode='python'))
            if type(permit) is not DomoSendPermit or type(token) is not DomoToken:
                raise ValueError
            context = permit.context
            context.wire_context()  # Revalidate retained exact context immediately before use.
            spec, source = context.spec, self._source
            if ((plan.source_id, plan.source_revision, plan.source_digest, plan.context, plan.response_profile)
                != (source.source_id, source.revision, source.digest, source.context, source.response_profile)
                or plan.dataset_id not in source.dataset_ids
                or (statement.statement_id, statement.revision, statement.sha256)
                   != (plan.statement_id, plan.statement_revision, plan.statement_digest)
                or (spec.provider, spec.source_id, spec.source_revision, spec.context, spec.definition_digest,
                    spec.capacity_key, spec.prepared_digest, spec.request_digest, spec.scope_digest,
                    spec.decision_digest, spec.binding_digest, spec.model_bundle_digest)
                   != ('domo', source.source_id, source.revision, source.context, source.digest,
                       source.capacity_pool_id, plan.digest, plan.query_digest, plan.scope_digest,
                       plan.decision_digest, plan.binding_digest, plan.model_bundle_digest)
                or token.source_digest != source.digest or not _printable(token.value, 8192)
                or timestamp(token.expires_at) < context.deadline):
                raise ValueError
            budget = self._budget(context.deadline)
            async with asyncio.timeout(budget):
                if self._query_started:
                    raise ValueError
                permit.start_io(plan.digest)
                self._query_started = True
                body = await self._body('POST', '/v1/datasets/query/execute/'+plan.dataset_id,
                    deadline=context.deadline, max_bytes=plan.max_bytes,
                    headers={'Authorization': 'Bearer '+token.value, 'Accept': 'application/json'},
                    json={'sql': statement.text})
                completed_at = json_value(timestamp(self.clock.now()))
                result = parse_domo_success(body, dataset_id=plan.dataset_id, max_rows=plan.max_rows,
                    max_bytes=plan.max_bytes, request_id=context.request_id)
                self._budget(context.deadline)
                evidence = MappingProxyType(dict(plan_fingerprint=plan.digest,
                    response_profile_digest=canonical_digest(source.response_profile),
                    response_sha256=result.response_sha256, status_code=200, media_type='application/json',
                    completed_at=completed_at, row_count=len(result.rows), response_bytes=len(body)))
                self._success = (result, permit, evidence)
                return result
        except TimeoutError:
            raise source_error(ErrorCode.EXECUTION_TIMEOUT) from None
        except GatewayError:
            raise
        except (ValueError, TypeError, AttributeError):
            raise source_error() from None
        except Exception:
            raise source_error(ErrorCode.UPSTREAM_UNAVAILABLE) from None

    @property
    def is_closed(self):
        return self._client.is_closed

    def success_evidence(self, response, permit):
        """Retained exact-response metadata for the narrow late observer only."""
        if self._success is None or self._success[0] is not response or self._success[1] is not permit:
            raise source_error()
        checked=validate_domo_evidence(response)
        evidence=self._success[2]
        if (checked.response_sha256!=evidence['response_sha256']
            or len(checked.rows)!=evidence['row_count'] or len(checked._raw_body)!=evidence['response_bytes']
            or checked.profile!=self._source.response_profile):
            raise source_error()
        return dict(evidence)

    def check_deadline(self, deadline):
        """Use after close/normalization; this cannot extend the original cutoff."""
        self._budget(deadline)

    async def close(self):
        remaining = .05 if self._monotonic_deadline is None else max(0.0, min(.05, self._monotonic_deadline-monotonic()))
        try:
            async with asyncio.timeout(remaining):
                await self._client.aclose()
        except (TimeoutError, httpx.HTTPError):
            pass
