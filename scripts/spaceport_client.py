"""Private, origin-pinned client for the existing Spaceport REST contract.

No provider credentials, source discovery, source activation or mutation retries.
A review preflight cannot atomically pin a test-run ID: publication sends only the
existing tested_digest/expected_revision/reason contract, which the server rechecks.
"""
import asyncio
import hashlib
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import TypeAdapter, field_validator

from gateway.api.models import DraftReceipt, TestRunView, PublicationReceipt, DraftWrite, PublishRequest, ProductDescription
from gateway.contracts.canonical import Identifier, StrictModel, canonical_bytes, canonical_digest, Digest, Version
from gateway.contracts.execution import ExecutionView, ResultEnvelope
from gateway.contracts.query import QueryRequest
from gateway.pilot_config import _https_url
from gateway.publishing.service import VersionDiff
from scripts.spaceport_files import FileError, parse_json, read_bytes, read_json, write_json


class CLIError(ValueError):
    def __init__(self, code='CLIENT_UNAVAILABLE', *, uncertain=False):
        self.code = code
        self.uncertain = uncertain
        super().__init__('The operation outcome is uncertain; do not repeat the mutation automatically.'
                         if uncertain else 'The requested operation is unavailable or incompatible.')


class VersionedFile(StrictModel):
    schema_version: Literal[1] = 1

    @field_validator('schema_version',mode='before')
    @classmethod
    def exact_file_version(cls,value):
        if type(value) is not int:
            raise ValueError('Integer file version required')
        return value


class ClientConfig(VersionedFile):
    schema_version: Literal[1]
    gateway_origin: str

    @field_validator('schema_version', mode='before')
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError('Integer schema version required')
        return value

    @field_validator('gateway_origin')
    @classmethod
    def exact_origin(cls, value):
        return _https_url(value, path='origin')

    @classmethod
    def from_file(cls, path):
        try:
            return cls.model_validate(read_json(path,private=True,private_parent=True))
        except (ValueError, TypeError):
            raise CLIError() from None


class ReviewReceipt(VersionedFile):
    schema_version: Literal[1] = 1
    gateway_origin: str
    review: VersionDiff


class QueryOperation(VersionedFile):
    schema_version: Literal[1] = 1
    kind: Literal['query'] = 'query'
    gateway_origin: str
    request_digest: Digest
    credential_fingerprint: Digest
    context_digest: Digest | None = None
    request: QueryRequest


class AcceptedExecution(VersionedFile):
    kind: Literal["accepted-execution"] = "accepted-execution"
    gateway_origin: str
    operation_digest: Digest
    execution_id: Identifier


def _exists(path):
    return Path(path).exists() or Path(path).is_symlink()


def _identifier(value):
    try:
        return TypeAdapter(Identifier).validate_python(value, strict=True)
    except ValueError:
        raise CLIError() from None


class GatewayClient:
    def __init__(self, config, *, token_file, gateway=None, transport=None, deadline_seconds=180, monotonic=time.monotonic):
        try:
            if type(deadline_seconds) not in (int,float) or not 0 < deadline_seconds <= 180:
                raise ValueError
            config = ClientConfig.model_validate(config.model_dump())
            if gateway is not None and _https_url(gateway, path='origin') != config.gateway_origin:
                raise ValueError
            token = read_bytes(token_file, limit=16384, private=True).decode('ascii').rstrip('\n')
            if not token or len(token) > 16384 or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
                raise ValueError
        except (OSError, ValueError, UnicodeError, TypeError):
            raise CLIError() from None
        self.origin = config.gateway_origin
        self._credential_fingerprint = hashlib.sha256(b"spaceport-cli-session-v1\0" + token.encode("ascii")).hexdigest()
        self._clock = monotonic
        self._deadline = monotonic() + deadline_seconds
        self._runner = asyncio.Runner()
        self._client = httpx.AsyncClient(base_url=self.origin, transport=transport, follow_redirects=False,
            trust_env=False, timeout=httpx.Timeout(180,connect=5),
            headers={'Authorization':'Bearer '+token,'Accept':'application/json','Accept-Encoding':'identity'})

    def remaining(self):
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise CLIError('COMMAND_DEADLINE')
        return remaining

    def close(self):
        async def cleanup():
            async with asyncio.timeout(max(0.001,min(1,self._deadline-self._clock()))):
                await self._client.aclose()
        try:
            self._runner.run(cleanup())
        except (TimeoutError,ValueError,OSError,httpx.HTTPError):
            pass
        finally:
            self._runner.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _request(self, method, path, model, *, body=None, params=None):
        mutating = method != 'GET'
        remaining = self.remaining()  # Expired before dispatch is not ambiguous.
        async def exchange():
            # One deadline spans connect, body streaming and response cleanup.
            async with asyncio.timeout(remaining):
                kwargs = {'params':params,'timeout':httpx.Timeout(remaining,connect=min(5,remaining))}
                if body is not None:
                    kwargs['json'] = body
                async with self._client.stream(method, path, **kwargs) as response:
                    self.remaining()
                    if response.status_code != 200:
                        raise CLIError('HTTP_REJECTED', uncertain=mutating and (response.status_code >= 500 or 300 <= response.status_code < 400))
                    if (response.headers.get('content-encoding','identity').lower() != 'identity'
                        or response.headers.get('content-type','').split(';')[0].strip().lower() != 'application/json'):
                        raise ValueError
                    data = bytearray()
                    async for part in response.aiter_bytes():
                        self.remaining()
                        if len(part) > 1048576 - len(data):
                            raise ValueError
                        data.extend(part)
                    self.remaining()
                    value = TypeAdapter(model).validate_python(parse_json(data))
                    self.remaining()
                self.remaining()
                return value
        try:
            return self._runner.run(exchange())
        except CLIError as error:
            if mutating and error.code == 'COMMAND_DEADLINE':
                raise CLIError('COMMAND_DEADLINE',uncertain=True) from None
            raise
        except (TimeoutError,httpx.HTTPError,OSError,ValueError,TypeError,RecursionError):
            raise CLIError(uncertain=mutating) from None

    def describe(self, product_id, version):
        product_id = _identifier(product_id)
        try:
            TypeAdapter(Version).validate_python(version,strict=True)
        except ValueError:
            raise CLIError() from None
        result = self._request('GET','/v1/products/'+product_id,ProductDescription,params={'version':version})
        if result.product_id != product_id or result.version != version:
            raise CLIError()
        return result

    def save(self, manifest, *, expected_revision, draft_id=None):
        try:
            body = DraftWrite.model_validate(dict(manifest=manifest,expected_revision=expected_revision))
        except (ValueError,TypeError):
            raise CLIError() from None
        path = '/v1/drafts' + ('/'+_identifier(draft_id) if draft_id is not None else '')
        receipt = self._request('PUT' if draft_id is not None else 'POST',path,DraftReceipt,body=body.model_dump(mode='json'))
        if (receipt.product_id != body.manifest.product_id or receipt.digest != body.manifest.content_digest
            or receipt.revision != expected_revision + 1):
            raise CLIError(uncertain=True)
        return receipt

    def test(self, draft_id):
        draft_id = _identifier(draft_id)
        result = self._request('POST','/v1/drafts/'+draft_id+'/tests',TestRunView)
        if result.draft_id != draft_id:
            raise CLIError(uncertain=True)
        return result

    def preview(self, draft_id, run_id):
        draft_id,run_id = _identifier(draft_id),_identifier(run_id)
        result = self._request('GET',f'/v1/drafts/{draft_id}/tests/{run_id}',TestRunView)
        if result.draft_id != draft_id or result.id != run_id:
            raise CLIError()
        return result

    def review(self, draft_id):
        draft_id = _identifier(draft_id)
        result = self._request('GET',f'/v1/drafts/{draft_id}/diff',VersionDiff)
        if result.draft_id != draft_id:
            raise CLIError()
        return result

    def publish(self, draft_id, *, review, reason):
        draft_id = _identifier(draft_id)
        try:
            receipt = ReviewReceipt.model_validate(review)
            if receipt.gateway_origin != self.origin:
                raise ValueError
            reviewed = receipt.review
            if reviewed.draft_id != draft_id or reviewed.stale_evidence or not reviewed.latest_test_run_id:
                raise ValueError
        except (ValueError,TypeError):
            raise CLIError() from None
        current = self.review(draft_id)
        if canonical_bytes(current) != canonical_bytes(reviewed):
            raise CLIError('REVIEW_CHANGED')
        try:
            body = PublishRequest(tested_digest=current.after_digest,expected_revision=current.revision,reason=reason)
        except (ValueError,TypeError):
            raise CLIError() from None
        receipt = self._request('POST',f'/v1/drafts/{draft_id}/publish',PublicationReceipt,body=body.model_dump(mode='json'))
        if receipt.product_id != current.product_id or receipt.digest != current.after_digest:
            raise CLIError(uncertain=True)
        return receipt

    def query(self, request, *, operation_file, context_digest=None, retry_ambiguous=False):
        """First send once; known execution resumes by GET under current authority.

        A missing acceptance sidecar is ambiguous. Only an explicit same-token,
        same-key retry may repeat that POST; corrupt receipts never fall back.
        """
        known = None
        sidecar = Path(str(operation_file)+'.execution.json')
        try:
            if type(retry_ambiguous) is not bool:
                raise ValueError
            if context_digest is not None:
                TypeAdapter(Digest).validate_python(context_digest,strict=True)
            query = QueryRequest.model_validate(request)
            if query.version is None or query.idempotency_key is not None:
                raise ValueError
            digest = canonical_digest(query)
            existing = _exists(operation_file)
            if existing:
                operation = QueryOperation.model_validate(read_json(operation_file,private=True,durable=True))
                original = operation.request.model_copy(update={'idempotency_key':None})
                if (operation.gateway_origin != self.origin or operation.context_digest != context_digest
                    or operation.request_digest != digest or canonical_digest(original) != digest
                    or not operation.request.idempotency_key):
                    raise ValueError
                if _exists(sidecar):
                    known = AcceptedExecution.model_validate(read_json(sidecar,private=True,durable=True))
                    if known.gateway_origin != self.origin or known.operation_digest != canonical_digest(operation):
                        raise ValueError
                elif not retry_ambiguous:
                    raise CLIError('AMBIGUOUS_RESUME_REQUIRES_REVIEW')
                if known is None and operation.credential_fingerprint != self._credential_fingerprint:
                    raise CLIError('AMBIGUOUS_SESSION_CHANGED')
            else:
                if _exists(sidecar):
                    raise ValueError
                operation = QueryOperation(gateway_origin=self.origin,request_digest=digest,
                    credential_fingerprint=self._credential_fingerprint,context_digest=context_digest,
                    request=query.model_copy(update={'idempotency_key':'cli-'+uuid4().hex}))
                write_json(operation_file,operation.model_dump(mode='json'))
        except CLIError:
            raise
        except (ValueError,TypeError,OSError):
            raise CLIError() from None
        if known is not None:
            return self.result(known.execution_id)
        result = self._request('POST','/v1/executions',ExecutionView,body=operation.request.model_dump(mode='json'))
        accepted = AcceptedExecution(gateway_origin=self.origin,operation_digest=canonical_digest(operation),
            execution_id=result.execution_id)
        try:
            try:
                write_json(sidecar,accepted.model_dump(mode='json'))
            except FileError:
                # A same-key concurrent sender may already have published it.
                # Durable exact reconciliation never overwrites/removes a winner.
                saved = AcceptedExecution.model_validate(read_json(sidecar,private=True,durable=True))
                if canonical_bytes(saved) != canonical_bytes(accepted):
                    raise ValueError
            self.remaining()
        except (ValueError,TypeError,OSError):
            raise CLIError('ACCEPTANCE_RECEIPT_UNCERTAIN',uncertain=True) from None
        return result

    def result(self, execution_id, *, cursor=None):
        execution_id = _identifier(execution_id)
        if cursor is not None and (type(cursor) is not str or not 1 <= len(cursor) <= 4096):
            raise CLIError()
        result = self._request('GET','/v1/executions/'+execution_id,ResultEnvelope | ExecutionView,
            params={'cursor':cursor} if cursor is not None else None)
        if result.execution_id != execution_id:
            raise CLIError()
        return result
