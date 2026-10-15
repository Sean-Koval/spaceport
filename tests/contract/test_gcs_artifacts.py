"""Offline artifact contracts; no deployed authorization or GCS evidence."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import socket

import pytest
from pydantic import ValidationError

from gateway.contracts.errors import GatewayError

pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[2]
BODY = b"synthetic artifact\n"
NOW = datetime(2024, 3, 12, 1, tzinfo=timezone.utc)


def api():
    return importlib.import_module("gateway.artifacts.gcs")


def raw_descriptor(**updates):
    value = dict(artifact_id="north-report", revision=1, product_id="revenue", product_version="1.0.0",
        product_digest="a" * 64, owner="analytics", classification="internal", policy_id="revenue-access",
        tenant_id="north", bucket="synthetic-artifacts", object_name="reports/north.txt",
        generation="123", metageneration="2", expected_size=len(BODY), expected_sha256=hashlib.sha256(BODY).hexdigest(),
        media_type="text/plain", source_loaded_at="2024-03-12T00:00:00Z", source_evidence_digest="b" * 64,
        max_source_age_seconds=86400)
    value.update(updates)
    return value


def descriptor(**updates):
    return api().GcsArtifactDescriptor.model_validate(raw_descriptor(**updates))


def authority(item, **updates):
    value = dict(descriptor_digest=item.digest, principal_id="north-user", tenant_id="north",
        policy_id="revenue-access", policy_revision=1, scope_digest="c" * 64,
        max_bytes=1_048_576, timeout_seconds=120.0, max_source_age_seconds=86400)
    value.update(updates)
    return api().ArtifactAuthorization.model_validate(value)


@pytest.fixture(autouse=True)
def deny_sockets(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("Offline GCS contracts cannot use network sockets")
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


class Response:
    def __init__(self, body, head, *, delay=0, error=None, oversized=False):
        self.body, self.head = body, head
        self.delay, self.error, self.oversized = delay, error, oversized
        self.offset = 0
        self.read_sizes = []

    async def read(self, maximum):
        self.read_sizes.append(maximum)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        if self.oversized:
            return b"x" * (maximum + 1)
        chunk = self.body[self.offset:self.offset + maximum]
        self.offset += len(chunk)
        return chunk


class Transport:
    def __init__(self, item, body=BODY):
        self.calls, self.closed = [], []
        self.metadata = dict(bucket=item.bucket, object_name=item.object_name,
            generation=item.generation, metageneration=item.metageneration,
            size=item.expected_size, media_type=item.media_type)
        self.metadata_body = None
        self.metadata_head = {}
        self.content_head = {}
        self.body = body
        self.delay = 0
        self.content_error = None
        self.oversized = False
        self.responses = []

    @asynccontextmanager
    async def open_metadata(self, request):
        self.calls.append(("metadata", request))
        body = self.metadata_body if self.metadata_body is not None else json.dumps(self.metadata).encode()
        head = dict(status_code=200, content_type="application/json", content_length=len(body))
        head.update(self.metadata_head)
        result = Response(body, api().GcsResponseHead.model_validate(head), delay=self.delay)
        self.responses.append(result)
        try:
            yield result
        finally:
            self.closed.append("metadata")

    @asynccontextmanager
    async def open_content(self, request):
        self.calls.append(("content", request))
        head = dict(status_code=200, content_type="text/plain", content_length=len(self.body),
            generation=request.generation, metageneration=request.if_metageneration_match)
        head.update(self.content_head)
        result = Response(self.body, api().GcsResponseHead.model_validate(head), delay=self.delay,
            error=self.content_error, oversized=self.oversized)
        self.responses.append(result)
        try:
            yield result
        finally:
            self.closed.append("content")


class Gate:
    def __init__(self, item):
        self.calls = []
        self.decisions = [authority(item), authority(item)]
        self.delay = 0
        self.error = None

    async def __call__(self, item, principal_id):
        self.calls.append((item, principal_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.decisions[min(len(self.calls) - 1, 1)]


def setup(item=None):
    item = item or descriptor()
    transport = Transport(item)
    gate = Gate(item)
    reader = api().GcsArtifactReader(catalog=api().StaticArtifactCatalog([item]),
        authorize=gate, transport=transport, now=lambda: NOW)
    return item, transport, gate, reader


def read(reader, item, **updates):
    kwargs = dict(artifact_id=item.artifact_id, revision=item.revision, descriptor_digest=item.digest,
        principal_id="north-user")
    kwargs.update(updates)
    return asyncio.run(reader.read(**kwargs))


def failure(reader, item, code=None, **updates):
    with pytest.raises(GatewayError) as caught:
        read(reader, item, **updates)
    if code:
        assert caught.value.code == code
    assert "secret" not in str(caught.value)
    assert "https://" not in str(caught.value)
    return caught.value


def test_complete_buffer_pins_exact_version_and_authorizes_twice():
    item, transport, gate, reader = setup()
    result = read(reader, item)
    assert result.content == BODY
    assert result.descriptor_digest == item.digest
    assert result.sha256 == item.expected_sha256
    assert result.source_loaded_at == item.source_loaded_at
    assert [name for name, _ in transport.calls] == ["metadata", "content"]
    assert transport.closed == ["metadata", "content"]
    assert len(gate.calls) == 2
    for _, request in transport.calls:
        assert (request.bucket, request.object_name, request.generation,
            request.if_generation_match, request.if_metageneration_match) == (
                item.bucket, item.object_name, "123", "123", "2")
        assert 0 < request.timeout_seconds <= 120
    assert transport.calls[0][1].max_response_bytes == 65536
    assert transport.calls[1][1].max_response_bytes == len(BODY)
    assert all(size <= 65536 for response in transport.responses for size in response.read_sizes)
    assert "url" not in type(result).model_fields


@pytest.mark.parametrize("updates", [
    {"generation": "0"}, {"generation": "01"}, {"generation": 1}, {"generation": True},
    {"generation": "1" * 21}, {"metageneration": "-1"}, {"revision": True},
    {"expected_size": True}, {"expected_size": -1}, {"expected_size": 1048577},
    {"expected_sha256": "A" * 64}, {"bucket": "https://secret"}, {"bucket": "wild*"},
    {"object_name": "../secret"}, {"object_name": "reports/*"}, {"object_name": "reports/"},
    {"object_name": "reports//north"}, {"object_name": "a%2fb"}, {"object_name": "a?token=secret"},
    {"media_type": "text/plain\r\nX: secret"}, {"media_type": "text/plain; charset=utf-8"},
    {"product_version": "latest"}, {"url": "https://secret"},
    {"source_loaded_at": "2024-03-12"}, {"source_evidence_digest": None},
])
def test_descriptor_rejects_hostile_or_unbounded_fields(updates):
    with pytest.raises(ValidationError):
        descriptor(**updates)


def test_catalog_is_immutable_copied_bounded_and_digest_pinned():
    item = descriptor()
    values = [item]
    catalog = api().StaticArtifactCatalog(values)
    values.clear()
    with pytest.raises(ValidationError):
        item.bucket = "other-bucket"
    resolved = catalog.resolve(item.artifact_id, item.revision, item.digest)
    object.__setattr__(resolved, "bucket", "other-bucket")
    assert catalog.resolve(item.artifact_id, item.revision, item.digest).bucket == item.bucket
    with pytest.raises(ValueError):
        api().StaticArtifactCatalog([item, item])
    with pytest.raises(ValueError):
        api().StaticArtifactCatalog([item] * 1025)
    with pytest.raises(GatewayError):
        catalog.resolve(item.artifact_id, item.revision, "d" * 64)


@pytest.mark.parametrize("missing", ["transport", "authorize"])
def test_reader_has_no_implicit_transport_or_authorization(missing):
    item = descriptor()
    args = dict(catalog=api().StaticArtifactCatalog([item]), transport=Transport(item), authorize=Gate(item))
    args.pop(missing)
    with pytest.raises(TypeError):
        api().GcsArtifactReader(**args)
    args[missing] = None
    with pytest.raises(ValueError):
        api().GcsArtifactReader(**args)


@pytest.mark.parametrize("updates", [{"artifact_id": "unknown"}, {"revision": 2}, {"descriptor_digest": "d" * 64},
    {"revision": True}, {"timeout_seconds": 121.0}, {"timeout_seconds": 0.0}, {"timeout_seconds": float("nan")}])
def test_invalid_reference_never_reads_or_authorizes(updates):
    item, transport, gate, reader = setup()
    failure(reader, item, **updates)
    assert not transport.calls
    assert not gate.calls


@pytest.mark.parametrize("updates", [{"principal_id": "south-user"}, {"tenant_id": "south"},
    {"policy_id": "different"}, {"descriptor_digest": "d" * 64}, {"max_bytes": 1}])
def test_wrong_initial_authority_never_reads(updates):
    item, transport, gate, reader = setup()
    gate.decisions[0] = authority(item, **updates)
    failure(reader, item)
    assert not transport.calls
    assert len(gate.calls) == 1


@pytest.mark.parametrize("updates", [{"principal_id": "south-user"}, {"tenant_id": "south"},
    {"policy_id": "different"}, {"policy_revision": 2}, {"scope_digest": "d" * 64},
    {"descriptor_digest": "d" * 64}, {"max_bytes": 1}, {"timeout_seconds": 1.0}])
def test_changed_release_authority_discards_complete_buffer(updates):
    item, transport, gate, reader = setup()
    gate.decisions[1] = authority(item, **updates)
    failure(reader, item)
    assert len(gate.calls) == 2
    assert transport.closed == ["metadata", "content"]


@pytest.mark.parametrize("changes", [{"bucket": "different"}, {"object_name": "reports/south.txt"},
    {"generation": "124"}, {"metageneration": "3"}, {"size": 1}, {"media_type": "application/json"},
    {"url": "https://secret"}])
def test_metadata_drift_prevents_content_io(changes):
    item, transport, gate, reader = setup()
    transport.metadata.update(changes)
    failure(reader, item)
    assert [name for name, _ in transport.calls] == ["metadata"]
    assert len(gate.calls) == 1


@pytest.mark.parametrize("stage", ["metadata", "content"])
@pytest.mark.parametrize("changes", [{"status_code": 412}, {"status_code": 302}, {"status_code": 206},
    {"content_encoding": "gzip"}, {"content_encoding": "br"}, {"transcoded": True},
    {"redirected": True}, {"content_range": "bytes 0-1/18"}, {"content_type": "text/html"}])
def test_rejects_precondition_failure_redirect_partial_and_transform(stage, changes):
    item, transport, gate, reader = setup()
    getattr(transport, stage + "_head").update(changes)
    failure(reader, item)
    assert len(transport.calls) == (1 if stage == "metadata" else 2)
    assert len(gate.calls) == 1


@pytest.mark.parametrize("changes", [{"generation": "124"}, {"metageneration": "3"},
    {"generation": None}, {"metageneration": None}, {"content_length": 1}])
def test_content_headers_recheck_identity_and_size(changes):
    item, transport, gate, reader = setup()
    transport.content_head.update(changes)
    failure(reader, item)
    assert len(gate.calls) == 1


@pytest.mark.parametrize("body", [b"wrong same length!\n", BODY[:-1], BODY + b"x", b"x" * 1048577])
def test_content_mismatch_never_releases_bytes(body):
    item, transport, gate, reader = setup()
    transport.body = body
    transport.content_head["content_length"] = len(BODY)
    failure(reader, item)
    assert len(gate.calls) == 1


def test_reader_limits_stream_to_exact_size_plus_one_and_detects_overflow():
    item, transport, gate, reader = setup()
    transport.body = BODY + b"x" * 2000000
    transport.content_head["content_length"] = len(BODY)
    failure(reader, item, "RESOURCE_LIMIT")
    assert transport.responses[-1].offset == len(BODY) + 1
    assert transport.closed == ["metadata", "content"]


@pytest.mark.parametrize("body", [b"x" * 65537, b"{broken", b"[]", b'{"bucket":"a","bucket":"b"}', b'NaN'])
def test_metadata_raw_bound_and_malformed_json(body):
    item, transport, gate, reader = setup()
    transport.metadata_body = body
    transport.metadata_head["content_length"] = min(len(body), 65536)
    failure(reader, item)
    assert len(transport.calls) == 1
    assert transport.responses[0].offset <= 65537


def test_transport_chunk_larger_than_requested_is_rejected():
    item, transport, gate, reader = setup()
    transport.oversized = True
    failure(reader, item, "RESOURCE_LIMIT")
    assert len(gate.calls) == 1


@pytest.mark.parametrize("stage", ["authorize", "metadata", "content"])
def test_whole_deadline_covers_authorization_and_streams(stage):
    item, transport, gate, reader = setup()
    if stage == "authorize":
        gate.delay = 0.03
    elif stage == "metadata":
        transport.delay = 0.03
    else:
        original = transport.open_content
        @asynccontextmanager
        async def slow(request):
            async with original(request) as response:
                response.delay = 0.03
                yield response
        transport.open_content = slow
    failure(reader, item, "EXECUTION_TIMEOUT", timeout_seconds=0.01)
    assert len(gate.calls) == 1


def test_authorization_time_consumes_tighter_policy_deadline():
    item, transport, gate, reader = setup()
    gate.delay = 0.02
    gate.decisions = [authority(item, timeout_seconds=0.01)] * 2
    failure(reader, item, "EXECUTION_TIMEOUT")
    assert not transport.calls


def test_release_authorization_is_within_the_same_deadline():
    item, transport, gate, reader = setup()
    async def release_waits(candidate, principal):
        if gate.calls:
            gate.calls.append((candidate, principal))
            await asyncio.Future()
        return await gate(candidate, principal)
    reader = api().GcsArtifactReader(catalog=api().StaticArtifactCatalog([item]),
        authorize=release_waits, transport=transport, now=lambda: NOW)
    failure(reader, item, "EXECUTION_TIMEOUT", timeout_seconds=0.01)
    assert len(gate.calls) == 2
    assert transport.closed == ["metadata", "content"]


@pytest.mark.parametrize("stage", ["authorize", "content"])
def test_upstream_errors_are_redacted_without_retry(stage):
    item, transport, gate, reader = setup()
    error = GatewayError("UNAUTHENTICATED", "bad", "secret https://signed.example/?Signature=secret")
    if stage == "authorize":
        gate.error = error
    else:
        transport.content_error = error
    failure(reader, item)
    assert len(gate.calls) == 1
    assert len(transport.calls) <= 2


@pytest.mark.parametrize("changes,code", [
    ({"source_loaded_at": None, "source_evidence_digest": None}, "FRESHNESS_UNKNOWN"),
    ({"source_loaded_at": "2024-03-10T00:00:00Z"}, "STALE_DATA"),
    ({"source_loaded_at": "2024-03-13T00:00:00Z"}, "FRESHNESS_UNKNOWN"),
])
def test_freshness_uses_reviewed_source_load_evidence_only(changes, code):
    item, transport, gate, reader = setup(descriptor(**changes))
    failure(reader, item, code)
    assert not transport.calls


def test_synthetic_fixture_contract_matches_reader():
    value = json.loads((ROOT / "fixtures/adapter/gcs.json").read_text())
    item = api().GcsArtifactDescriptor.model_validate(value["descriptor"])
    item, transport, gate, reader = setup(item)
    transport.body = value["content"].encode()
    assert read(reader, item).content == transport.body


def test_authorization_cannot_modify_the_retained_descriptor():
    item, transport, gate, reader = setup()
    calls = []
    async def corrupt_callback(candidate, principal):
        calls.append(candidate)
        object.__setattr__(candidate, "bucket", "other-bucket")
        return authority(item)
    reader = api().GcsArtifactReader(catalog=api().StaticArtifactCatalog([item]),
        authorize=corrupt_callback, transport=transport, now=lambda: NOW)
    result = read(reader, item)
    assert result.content == BODY
    assert len(calls) == 2
    assert all(request.bucket == item.bucket for _, request in transport.calls)


def test_catalog_rejects_ordinary_reassignment_of_allowlist():
    item = descriptor()
    catalog = api().StaticArtifactCatalog([item])
    with pytest.raises((AttributeError, TypeError)):
        catalog._entries = {}
    with pytest.raises((AttributeError, TypeError)):
        del catalog._entries
    assert catalog.resolve(item.artifact_id, item.revision, item.digest) == item


@pytest.mark.parametrize("size", [0, 1048576])
def test_exact_empty_and_maximum_size_artifacts_are_supported(size):
    body = b"x" * size
    item = descriptor(expected_size=size, expected_sha256=hashlib.sha256(body).hexdigest())
    item, transport, gate, reader = setup(item)
    transport.body = body
    result = read(reader, item)
    assert result.content == body
    assert len(gate.calls) == 2
    assert transport.responses[-1].read_sizes[-1] == 1


def test_current_policy_can_require_freshness_even_when_descriptor_does_not():
    item, transport, gate, reader = setup(descriptor(source_loaded_at=None,
        source_evidence_digest=None, max_source_age_seconds=None))
    failure(reader, item, "FRESHNESS_UNKNOWN")
    assert not transport.calls


def test_tighter_policy_freshness_is_enforced():
    item, transport, gate, reader = setup()
    gate.decisions = [authority(item, max_source_age_seconds=60)] * 2
    failure(reader, item, "STALE_DATA")
    assert not transport.calls


def test_freshness_rechecked_at_release_instead_of_using_fetch_time():
    item, transport, gate, reader = setup()
    times = iter([NOW, datetime(2024, 3, 14, tzinfo=timezone.utc)])
    reader = api().GcsArtifactReader(catalog=api().StaticArtifactCatalog([item]),
        authorize=gate, transport=transport, now=lambda: next(times))
    failure(reader, item, "STALE_DATA")
    assert len(gate.calls) == 2


def test_no_required_freshness_never_fabricates_source_checkpoint():
    item, transport, gate, reader = setup(descriptor(source_loaded_at=None,
        source_evidence_digest=None, max_source_age_seconds=None))
    gate.decisions = [authority(item, max_source_age_seconds=None)] * 2
    assert read(reader, item).source_loaded_at is None


@pytest.mark.parametrize("decision", [None, True, {}, "secret", object()])
def test_invalid_authorization_result_fails_closed_before_io(decision):
    item, transport, gate, reader = setup()
    gate.decisions = [decision] * 2
    failure(reader, item, "NOT_FOUND_OR_FORBIDDEN")
    assert not transport.calls


def test_cancelled_request_closes_response_and_returns_no_result():
    item, transport, gate, reader = setup()
    transport.delay = 10
    async def scenario():
        task = asyncio.create_task(reader.read(artifact_id=item.artifact_id, revision=item.revision,
            descriptor_digest=item.digest, principal_id="north-user"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert transport.closed == ["metadata"]
    assert len(gate.calls) == 1


def test_suppressed_cancellation_cannot_release_after_deadline():
    item, transport, gate, reader = setup()
    async def slow_callback(candidate, principal):
        try:
            await asyncio.sleep(0.03)
        except asyncio.CancelledError:
            pass
        return authority(item)
    reader = api().GcsArtifactReader(catalog=api().StaticArtifactCatalog([item]),
        authorize=slow_callback, transport=transport, now=lambda: NOW)
    failure(reader, item, "EXECUTION_TIMEOUT", timeout_seconds=0.01)
    assert not transport.calls


def test_deadline_is_aggregate_across_metadata_and_content(monkeypatch):
    item, transport, gate, reader = setup()
    async def scenario():
        loop = asyncio.get_running_loop()
        elapsed = [loop.time()]
        original_read = Response.read
        async def consumes_budget(response, maximum):
            elapsed[0] += 4.0
            return await original_read(response, maximum)
        with monkeypatch.context() as patch:
            patch.setattr(loop, "time", lambda: elapsed[0])
            patch.setattr(Response, "read", consumes_budget)
            with pytest.raises(GatewayError) as caught:
                await reader.read(artifact_id=item.artifact_id, revision=item.revision,
                    descriptor_digest=item.digest, principal_id="north-user", timeout_seconds=10.0)
            assert caught.value.code == "EXECUTION_TIMEOUT"
    asyncio.run(scenario())
    assert [name for name, _ in transport.calls] == ["metadata", "content"]
    assert transport.closed == ["metadata", "content"]


def test_no_cached_read_or_authorization_is_reused():
    item, transport, gate, reader = setup()
    assert read(reader, item).content == BODY
    gate.error = RuntimeError("secret")
    failure(reader, item)
    assert len(gate.calls) == 3
    assert len(transport.calls) == 2


def test_artifact_model_repr_does_not_expose_content():
    item, transport, gate, reader = setup()
    result = read(reader, item)
    assert "synthetic artifact" not in repr(result)
    assert "synthetic artifact" not in str(result)
    assert result.content == BODY
