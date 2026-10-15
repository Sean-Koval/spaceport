"""Internal, offline-only exact-object artifact reader with no network factory.

A transport and a fresh audited authorization gate must be supplied explicitly.
Neither this module nor its synthetic fixture establishes deployed source access,
policy inheritance, billing, or provider timeout/cancellation guarantees.
"""
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import hashlib
import json
from types import MappingProxyType
from typing import Annotated, AsyncContextManager, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from gateway.contracts.canonical import AwareTimestamp, Digest, Identifier, StrictModel, Version, canonical_bytes, canonical_digest
from gateway.contracts.errors import ErrorCode, GatewayError

MAX_METADATA_BYTES = 65_536
MAX_ARTIFACT_BYTES = 1_048_576
MAX_TIMEOUT_SECONDS = 120.0
Generation = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$", max_length=20)]
Bucket = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", max_length=63)]
ObjectName = Annotated[str, Field(min_length=1, max_length=1024, pattern=r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")]
MediaType = Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")]
Revision = Annotated[int, Field(ge=1, le=2_147_483_647)]
SourceAge = Annotated[int, Field(ge=1, le=31_536_000)]


class GcsArtifactDescriptor(StrictModel):
    """Operator-reviewed, URL-free allowlist entry; never ordinary query input."""
    artifact_id: Identifier
    revision: Revision
    product_id: Identifier
    product_version: Version
    product_digest: Digest
    owner: Identifier
    classification: Literal["public", "internal", "confidential", "restricted"]
    policy_id: Identifier
    tenant_id: Identifier
    bucket: Bucket
    object_name: ObjectName
    generation: Generation
    metageneration: Generation
    expected_size: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    expected_sha256: Digest
    media_type: MediaType
    source_loaded_at: AwareTimestamp | None = None
    source_evidence_digest: Digest | None = None
    max_source_age_seconds: SourceAge | None = None

    @field_validator("object_name")
    @classmethod
    def exact_object(cls, value: str) -> str:
        if any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("An artifact requires an exact canonical object name")
        return value

    @model_validator(mode="after")
    def paired_source_evidence(self):
        if (self.source_loaded_at is None) != (self.source_evidence_digest is None):
            raise ValueError("A source checkpoint requires its reviewed evidence digest")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True, slots=True)
class StaticArtifactCatalog:
    """Snapshot immutable descriptor bytes and return fresh validated copies."""
    _entries: Mapping[tuple[str, int], tuple[str, bytes]]

    def __init__(self, entries: Sequence[GcsArtifactDescriptor]):
        if not isinstance(entries, (list, tuple)) or not 1 <= len(entries) <= 1024:
            raise ValueError("An artifact catalog requires 1 to 1024 reviewed entries")
        retained = {}
        for entry in entries:
            item = GcsArtifactDescriptor.model_validate(entry.model_dump(mode="python"))
            key = (item.artifact_id, item.revision)
            if key in retained:
                raise ValueError("Duplicate artifact revision")
            retained[key] = (item.digest, canonical_bytes(item))
        object.__setattr__(self, "_entries", MappingProxyType(retained))

    def resolve(self, artifact_id: str, revision: int, descriptor_digest: str) -> GcsArtifactDescriptor:
        try:
            reference = _ArtifactReference(artifact_id=artifact_id, revision=revision,
                descriptor_digest=descriptor_digest)
            digest, data = self._entries[(reference.artifact_id, reference.revision)]
            if digest != reference.descriptor_digest:
                raise ValueError
            return GcsArtifactDescriptor.model_validate_json(data)
        except (KeyError, ValueError, TypeError):
            raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN, "gcs-artifact-read",
                "The reviewed artifact is unavailable.") from None


class ArtifactAuthorization(StrictModel):
    """Fresh internal grant from the injected audited gate, never a client token.

    descriptor_digest pins associated product, ownership, classification, policy,
    tenant and exact source object. The gate must resolve current policy itself.
    """
    descriptor_digest: Digest
    principal_id: Identifier
    tenant_id: Identifier
    policy_id: Identifier
    policy_revision: Revision
    scope_digest: Digest
    max_bytes: int = Field(ge=1, le=MAX_ARTIFACT_BYTES)
    timeout_seconds: float = Field(gt=0, le=MAX_TIMEOUT_SECONDS)
    max_source_age_seconds: SourceAge | None = None


class GcsReadRequest(StrictModel):
    """Exact-object preconditions and hard budgets for an injected transport."""
    bucket: Bucket
    object_name: ObjectName
    generation: Generation
    if_generation_match: Generation
    if_metageneration_match: Generation
    max_response_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    timeout_seconds: float = Field(gt=0, le=MAX_TIMEOUT_SECONDS)


class GcsResponseHead(StrictModel):
    """Small transport-owned response envelope, without arbitrary headers/URLs."""
    status_code: int = Field(ge=100, le=599)
    content_type: MediaType
    content_length: int = Field(ge=0, le=2**53 - 1)
    generation: Generation | None = None
    metageneration: Generation | None = None
    content_encoding: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    content_range: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    redirected: bool = False
    transcoded: bool = False


class GcsObjectResponse(Protocol):
    head: GcsResponseHead

    async def read(self, maximum_bytes: int) -> bytes:
        """Read at most maximum_bytes raw bytes; b'' means EOF. Never decode."""
        ...


class GcsObjectTransport(Protocol):
    """Trusted injected implementation; no listing, URL, auth or write surface.

    Each operation must perform exactly one source request using all preconditions,
    refuse redirects, retries, decompression/transcoding and range requests, and
    close its response on context exit. read() must honor its requested bound.
    There is deliberately no HTTP/SDK implementation or default credential path.
    """
    def open_metadata(self, request: GcsReadRequest) -> AsyncContextManager[GcsObjectResponse]: ...

    def open_content(self, request: GcsReadRequest) -> AsyncContextManager[GcsObjectResponse]: ...


class ArtifactBytes(StrictModel):
    """Complete verified buffer, available only after release authorization."""
    artifact_id: Identifier
    revision: Revision
    descriptor_digest: Digest
    media_type: MediaType
    sha256: Digest
    source_loaded_at: AwareTimestamp | None
    content: bytes = Field(max_length=MAX_ARTIFACT_BYTES, repr=False)


class _ArtifactReference(StrictModel):
    artifact_id: Identifier
    revision: Revision
    descriptor_digest: Digest


class _ReadInput(_ArtifactReference):
    principal_id: Identifier
    timeout_seconds: float = Field(gt=0, le=MAX_TIMEOUT_SECONDS)


class _Metadata(StrictModel):
    bucket: Bucket
    object_name: ObjectName
    generation: Generation
    metageneration: Generation
    size: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    media_type: MediaType


class _Rejected(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code


_EXPLANATIONS = {
    ErrorCode.INVALID_INPUT: "The artifact read contract is invalid.",
    ErrorCode.NOT_FOUND_OR_FORBIDDEN: "The current scope cannot read this artifact.",
    ErrorCode.BINDING_DRIFT: "The reviewed artifact does not match the source response.",
    ErrorCode.RESOURCE_LIMIT: "The artifact exceeds its approved size limit.",
    ErrorCode.EXECUTION_TIMEOUT: "The artifact read exceeded its deadline.",
    ErrorCode.FRESHNESS_UNKNOWN: "The artifact source-load freshness is unknown.",
    ErrorCode.STALE_DATA: "The artifact source-load checkpoint is too old.",
    ErrorCode.UPSTREAM_UNAVAILABLE: "The artifact could not be read safely.",
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate metadata key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Nonfinite metadata number")


class GcsArtifactReader:
    """One metadata read plus one content stream; no partial result or retry."""
    def __init__(self, *, catalog: StaticArtifactCatalog,
                 authorize: Callable[[GcsArtifactDescriptor, str], Awaitable[ArtifactAuthorization]],
                 transport: GcsObjectTransport, now: Callable[[], datetime] | None = None):
        if (not isinstance(catalog, StaticArtifactCatalog) or not callable(authorize)
            or not callable(getattr(transport, "open_metadata", None))
            or not callable(getattr(transport, "open_content", None))):
            raise ValueError("An immutable catalog, fresh authorization gate and transport are required")
        self._catalog, self._authorize, self._transport = catalog, authorize, transport
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def read(self, *, artifact_id: str, revision: int, descriptor_digest: str,
                   principal_id: str, timeout_seconds: float = MAX_TIMEOUT_SECONDS) -> ArtifactBytes:
        # asyncio uses a monotonic clock. One deadline begins before resolution and
        # authorization, and is shortened from that same start by current policy.
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            try:
                request = _ReadInput(artifact_id=artifact_id, revision=revision,
                    descriptor_digest=descriptor_digest, principal_id=principal_id,
                    timeout_seconds=timeout_seconds)
            except (ValueError, TypeError):
                raise _Rejected(ErrorCode.INVALID_INPUT) from None
            try:
                item = self._catalog.resolve(request.artifact_id, request.revision, request.descriptor_digest)
            except GatewayError:
                raise _Rejected(ErrorCode.NOT_FOUND_OR_FORBIDDEN) from None
            deadline = started + request.timeout_seconds
            async with asyncio.timeout_at(deadline) as timer:
                self._remaining(deadline)
                first = await self._authorization(item, request.principal_id)
                deadline = min(deadline, started + first.timeout_seconds)
                self._remaining(deadline)
                timer.reschedule(deadline)
                self._check_freshness(item, first)
                if item.expected_size > first.max_bytes:
                    raise _Rejected(ErrorCode.RESOURCE_LIMIT)
                metadata_request = self._source_request(item, MAX_METADATA_BYTES, deadline)
                async with self._transport.open_metadata(metadata_request) as response:
                    head = self._head(response, item, metadata=True)
                    data = await self._bounded_read(response, MAX_METADATA_BYTES, deadline)
                    if len(data) != head.content_length:
                        raise _Rejected(ErrorCode.BINDING_DRIFT)
                self._remaining(deadline)
                try:
                    metadata = _Metadata.model_validate(json.loads(data,
                        object_pairs_hook=_unique_object, parse_constant=_reject_constant))
                    expected = (item.bucket, item.object_name, item.generation,
                        item.metageneration, item.expected_size, item.media_type)
                    actual = (metadata.bucket, metadata.object_name, metadata.generation,
                        metadata.metageneration, metadata.size, metadata.media_type)
                    if actual != expected:
                        raise ValueError
                except (ValueError, TypeError, RecursionError, UnicodeError):
                    raise _Rejected(ErrorCode.BINDING_DRIFT) from None
                content_request = self._source_request(item, item.expected_size, deadline)
                async with self._transport.open_content(content_request) as response:
                    self._head(response, item, metadata=False)
                    content = await self._bounded_read(response, item.expected_size, deadline)
                    if len(content) != item.expected_size or hashlib.sha256(content).hexdigest() != item.expected_sha256:
                        raise _Rejected(ErrorCode.BINDING_DRIFT)
                self._remaining(deadline)
                second = await self._authorization(item, request.principal_id)
                self._remaining(deadline)
                if second != first:
                    raise _Rejected(ErrorCode.NOT_FOUND_OR_FORBIDDEN)
                self._check_freshness(item, second)
                result = ArtifactBytes(artifact_id=item.artifact_id, revision=item.revision,
                    descriptor_digest=item.digest, media_type=item.media_type, sha256=item.expected_sha256,
                    source_loaded_at=item.source_loaded_at, content=content)
                self._remaining(deadline)
                return result
        except _Rejected as exc:
            raise GatewayError(exc.code, "gcs-artifact-read", _EXPLANATIONS[exc.code]) from None
        except TimeoutError:
            raise GatewayError(ErrorCode.EXECUTION_TIMEOUT, "gcs-artifact-read",
                _EXPLANATIONS[ErrorCode.EXECUTION_TIMEOUT]) from None
        except Exception:
            # Even a GatewayError raised by a callback/transport is untrusted text.
            raise GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE, "gcs-artifact-read",
                _EXPLANATIONS[ErrorCode.UPSTREAM_UNAVAILABLE]) from None

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise _Rejected(ErrorCode.EXECUTION_TIMEOUT)
        return min(remaining, MAX_TIMEOUT_SECONDS)

    async def _authorization(self, item: GcsArtifactDescriptor, principal_id: str) -> ArtifactAuthorization:
        try:
            # A callback receives its own snapshot; even bypassing a frozen
            # model cannot rewrite the retained authority used for source I/O.
            snapshot = GcsArtifactDescriptor.model_validate(item.model_dump(mode="python"))
            decision = await self._authorize(snapshot, principal_id)
            decision = ArtifactAuthorization.model_validate(decision.model_dump(mode="python"))
            if (decision.descriptor_digest != item.digest or decision.principal_id != principal_id
                or decision.tenant_id != item.tenant_id or decision.policy_id != item.policy_id):
                raise ValueError
            return decision
        except (ValueError, TypeError, AttributeError):
            raise _Rejected(ErrorCode.NOT_FOUND_OR_FORBIDDEN) from None

    def _check_freshness(self, item: GcsArtifactDescriptor, decision: ArtifactAuthorization) -> None:
        ages = [age for age in (item.max_source_age_seconds, decision.max_source_age_seconds) if age is not None]
        if not ages:
            return
        if item.source_loaded_at is None or item.source_evidence_digest is None:
            raise _Rejected(ErrorCode.FRESHNESS_UNKNOWN)
        now = self._now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise _Rejected(ErrorCode.FRESHNESS_UNKNOWN)
        age = (now - item.source_loaded_at).total_seconds()
        if age < 0:
            raise _Rejected(ErrorCode.FRESHNESS_UNKNOWN)
        if age > min(ages):
            raise _Rejected(ErrorCode.STALE_DATA)

    def _source_request(self, item: GcsArtifactDescriptor, maximum: int, deadline: float) -> GcsReadRequest:
        return GcsReadRequest(bucket=item.bucket, object_name=item.object_name, generation=item.generation,
            if_generation_match=item.generation, if_metageneration_match=item.metageneration,
            max_response_bytes=maximum, timeout_seconds=self._remaining(deadline))

    @staticmethod
    def _head(response: GcsObjectResponse, item: GcsArtifactDescriptor, *, metadata: bool) -> GcsResponseHead:
        head = GcsResponseHead.model_validate(response.head.model_dump(mode="python"))
        if head.status_code == 412:
            raise _Rejected(ErrorCode.BINDING_DRIFT)
        if head.status_code != 200 or head.redirected or head.transcoded or head.content_range is not None:
            raise _Rejected(ErrorCode.BINDING_DRIFT)
        if head.content_encoding not in (None, "identity"):
            raise _Rejected(ErrorCode.BINDING_DRIFT)
        if (head.generation is not None and head.generation != item.generation
            or head.metageneration is not None and head.metageneration != item.metageneration):
            raise _Rejected(ErrorCode.BINDING_DRIFT)
        if metadata:
            if head.content_type != "application/json":
                raise _Rejected(ErrorCode.BINDING_DRIFT)
            if head.content_length > MAX_METADATA_BYTES:
                raise _Rejected(ErrorCode.RESOURCE_LIMIT)
        elif (head.content_type != item.media_type or head.content_length != item.expected_size
            or head.generation != item.generation or head.metageneration != item.metageneration):
            raise _Rejected(ErrorCode.BINDING_DRIFT)
        return head

    async def _bounded_read(self, response: GcsObjectResponse, maximum: int, deadline: float) -> bytes:
        chunks = bytearray()
        while True:
            self._remaining(deadline)
            requested = min(MAX_METADATA_BYTES, maximum + 1 - len(chunks))
            chunk = await response.read(requested)
            self._remaining(deadline)
            if type(chunk) is not bytes:
                raise _Rejected(ErrorCode.BINDING_DRIFT)
            if len(chunk) > requested or len(chunks) + len(chunk) > maximum:
                raise _Rejected(ErrorCode.RESOURCE_LIMIT)
            if not chunk:
                return bytes(chunks)
            chunks.extend(chunk)
