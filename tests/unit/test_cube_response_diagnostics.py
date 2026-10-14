"""Offline diagnostic filtering; synthetic HTTP never proves Cube compatibility."""
import json
from pathlib import Path
import runpy
import secrets

import httpx
import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]


def runtime():
    module = runpy.run_path(str(ROOT / "tests/integration/test_cube_compatibility.py"))
    assert "failure_error_class" in module, "Missing bounded safe Cube failure classification"
    return module


@pytest.mark.parametrize("body,expected", [
    ({"error": "canceling statement due to statement timeout"}, "source_statement_timeout"),
    ({"error": "Query execution timeout after 10 min of waiting"}, "queue_execution_timeout"),
    ({"error": "Unrecognized member", "type": "UserError"}, "user_error"),
    ({"error": "an unrecognized failure"}, "unknown"),
])
def test_known_failure_classification_emits_only_fixed_enum(body, expected):
    classify = runtime()["failure_error_class"]
    response = httpx.Response(400, json=body, extensions={"diagnostic_complete": True})
    assert classify(response) == expected


@pytest.mark.parametrize("raw", [
    b'{"error":"first","error":"canceling statement due to statement timeout"}',
    b'{"error":"canceling statement due to statement timeout","other":"untrusted"}',
    b'{"error":{"nested":"canceling statement due to statement timeout"}}',
    b'{"error":[[[["canceling statement due to statement timeout"]]]]}',
    b'{"error":"canceling statement due to statement timeout","type":NaN}',
    b'{"error":"canceling statement due to statement timeout"} trailing',
    b'{"error":"\xff"}',
    b'{"error":"\\ud800"}',
    b'{"error":"unterminated',
    b'\xff\xfe{\x00"\x00e\x00r\x00r\x00o\x00r\x00"\x00:\x00"\x00x\x00"\x00}\x00',
    json.dumps({"error": "x" * 5000}).encode(),
])
def test_malformed_oversized_or_nonflat_json_is_unknown(raw):
    response = httpx.Response(400, content=raw, extensions={"diagnostic_complete": True})
    assert runtime()["failure_error_class"](response) == "unknown"


def test_incomplete_failure_capture_cannot_classify_a_partial_body():
    response = httpx.Response(400, json={"error": "canceling statement due to statement timeout"},
        extensions={"diagnostic_complete": False})
    assert runtime()["failure_error_class"](response) == "unknown"


class TrackedStream(httpx.SyncByteStream):
    def __init__(self, content, *, fail=False):
        self.content = content
        self.fail = fail
        self.chunks_read = 0
        self.closed = False

    def __iter__(self):
        for offset in range(0, len(self.content), 512):
            self.chunks_read += 1
            yield self.content[offset:offset + 512]
        if self.fail:
            raise httpx.ReadError("Untrusted partial response must never be emitted")

    def close(self):
        self.closed = True


def stack_with_stream(module, stream, *, status=400, headers=None):
    stack = module["CubeStack"].__new__(module["CubeStack"])
    stack.hosts = {"cube": "cube"}
    stack.token = lambda **scope: "unit-only-synthetic-token"

    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(status, stream=stream, headers=headers)

    stack.client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    return stack


@pytest.mark.parametrize("mode", ["oversized", "incomplete", "compressed"])
def test_failed_response_capture_stops_early_and_suppresses_partial_secrets(mode):
    module = runtime()
    value = secrets.token_urlsafe(32)
    content = (b"x" * (4096 - 20) + value.encode() + b"y" * 8192 if mode == "oversized"
        else json.dumps({"error": value}).encode())
    stream = TrackedStream(content, fail=mode == "incomplete")
    stack = stack_with_stream(module, stream, headers={"content-encoding": "gzip"} if mode == "compressed" else None)
    try:
        response = stack.request("/cubejs-api/v1/load", token=stack.token(), body={}, diagnose_failure=True)
        assert response.status_code == 400
        assert response.content == b""
        assert module["failure_error_class"](response) == "unknown"
        assert stream.closed
        assert stream.chunks_read <= 9
    finally:
        stack.client.close()


def test_load_still_fails_on_400_without_echoing_body_keys_values_or_secrets():
    module = runtime()
    values = [secrets.token_urlsafe(32) for _ in range(11)]
    stream = TrackedStream(json.dumps({"error": " ".join(values), "type": "UserError"}).encode())
    stack = stack_with_stream(module, stream)
    try:
        with pytest.raises(pytest.fail.Exception) as caught:
            stack.load({"dimensions": ["Orders.id"]}, require_pending=True)
        message = str(caught.value)
        assert message.startswith("Authenticated synthetic load failed: status=400 error_class=user_error ")
        assert "pending_count=0" in message and "elapsed_seconds=" in message
        if any(value in message or value[:20] in message for value in values):
            pytest.fail("Disposable secret leaked in Cube failure diagnostic", pytrace=False)
        assert caught.value.pytrace is False
        assert all(item not in message for item in ("Orders", "UserError", "unit-only-synthetic-token", "error\":", "type\":"))
    finally:
        stack.client.close()
