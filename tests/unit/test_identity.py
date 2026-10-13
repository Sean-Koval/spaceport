"""Identity boundary tests: synthetic tokens exist only in test-process memory."""
import asyncio
from datetime import timedelta
import hashlib
import importlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
import httpx
import jwt
import pytest

from gateway.contracts.errors import GatewayError
from gateway.contracts.identity import Principal

pytestmark = pytest.mark.unit

# Fixed, test-only RSA key; never used by the application or a real issuer.
TEST_SIGNING_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC2jhQ15ogYWWuE
rp3nbxFxbzBrRuV8z/+CoFYcgaOMyrlC+hjFXGDrlXacEa0Co+RQoMHU1BEWbQAK
U0ovqnwK7bGeb9b0d685sanXenKK4BFXjxwYWaufdisH8ymnoxc7xDartr+r7+/b
aBcOD9sSdiCRPOWrDzVWPXsB6OJOVHz91WtdJNP2YDcexyEqzJnI/5vW/meHHQd/
1Vz5zeDLT/oDi5+sFq+j5RqkpFBdavGaNLVaYmIYAKOs6nt0SAYV+R4OLy0Lsslx
d6Dy3a52p+EpZ22yl0cxPDrRc0Tel3pGFZZjN+ojnk47JF6dO7DWO1SCbUxcRAO9
tNZbDkl1AgMBAAECggEADH0o1c0pJ11KtT+K7u/SxitgqR4nCgRiywl0Cqw1x6aA
yRu6ARqCSOunOyMcbm9O6dMtn4VIi/u2An7TNvKXXNoJCjlFf+EGPFCyuVqXpneQ
yCgyuFm+G4EmjXstsNm/96dDRDkS4Mzf4Gc9GImuJ5OsQ7NiVPML55P6K9XGUaxp
LkWs0u6zxl1F2pI0gWD6W2rzumQxCc+b+0CPTx+6UJb46A10pjSSDWakVX3/pAra
NQWon6GbJ/6jPQ8Aqnsx22frIORccbvz75ZC5o61Ju8SxIFzLQux5In6V9kD5/Ax
h84uqWWuJW2f1NP0kEXqiNlHVh3AQvAEvHdb3/5yuwKBgQD3MYo2fiPfT8XZ0YKt
yp2jvFWHhFEC2ijvNKpB6guNZhaWPdBUKtexn1pLhY3oB/U/wuOWVMxD5jJSZpMC
ap2zetmk3NOddcgfAoqpeVQd89elbrNggXPrbQfpSDDYSPGCTF8tycrmxWCuKN3C
JIvWFK5lnHkEp6SLeftqyr+5vwKBgQC9DwV0nXtkGk9TLQDmKr2M8AMPBZIatGV5
rr7QvNmutLeTu8aeD0tA8ELhYp2jHTfmb4xozgRFyVPxT1l1Wxhq1qHTpbIo45N+
3XTjyUTVtLujTQ/w18lnWBFS8e27o8ZCbn6uvW9v9npOTzY670CqqglllT2oiI4+
pz2500PBywKBgF/n7Zr019AlcDrxaYUqLn0/beO1N4UmxWkBgukt+Ct1J2BFPVXa
hXMZw3u3pnYW2FVTtN83qUTSxEVqHLF8UhE3ocewEb5u2yUE5G2acENqvwklaR0R
PLtjwwUEiEbyDhmire7i8wEUJU2M93UCnuqBfaRY0C85J8MhoaUuYrKbAoGBAKHJ
/Q0yx6l+qmuvk5lk8hluO+/bAJFB16UuWbUgJ9lOZdtTUkFpLSlBcrqWPficxH26
8BmwVA+0ntVe5lt7vYWQb6RDpBdBybNDqQhV8UZBCGJTTFT7FSwPPaQRzTRKjxvC
mz7GdlsGPtxNBUeh/2ENakU4Vi+nzLLpQ/WGSfHJAoGAN6JXv6K9zcIOMGBPPzZi
lPoJBfWAdThDUhMFRnr6ic8cI1c05mj7JSylIBvyf7tZuAm46iAxUJZPAI6fcbvd
RxjdG3jzF/vJ4M2grKjZWu/xiGwhKwCDsIV+KMA43+tCBG0tOPLNH5JtXtCyhIXe
C1UXWiAYChGd0wTh32hOBpI=
-----END PRIVATE KEY-----
"""


def api():
    try:
        return SimpleNamespace(
            settings=importlib.import_module("gateway.auth.settings"),
            tokens=importlib.import_module("gateway.auth.tokens"),
            principals=importlib.import_module("gateway.auth.principals"),
        )
    except ModuleNotFoundError as error:
        pytest.fail(f"Identity implementation is missing: {error.name}")


def production_settings(**changes):
    return api().settings.Settings(mode="production", source="bigquery",
        oidc_issuer="https://issuer.example.test", oidc_audience="gateway-api",
        oidc_jwks_url="https://issuer.example.test/keys", oidc_access_token_profile="rfc9068",
        oidc_client_ids=("studio-client",), **changes)


@pytest.mark.parametrize("changes", [
    {"source": "bigquery"}, {"bind_host": "0.0.0.0"}, {"bind_host": "::"},
    {"bind_host": "localhost"}, {"public_ingress": True}, {"trust_forwarded_headers": True},
    {"oidc_issuer": "https://issuer.example.test"},
])
def test_demo_rejects_real_sources_public_ingress_and_oidc(changes):
    with pytest.raises(ValueError):
        api().settings.validate_mode(api().settings.Settings(**changes))


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_demo_accepts_only_explicit_loopback_synthetic_configuration(host):
    api().settings.validate_mode(api().settings.Settings(bind_host=host))


@pytest.mark.parametrize("changes", [
    {"oidc_access_token_profile": "disabled"}, {"oidc_jwks_url": "http://issuer.example.test/keys"},
    {"oidc_jwks_url": "https://user:secret@issuer.example.test/keys"},
    {"oidc_issuer": "https://issuer.example.test/#fragment"},
    {"oidc_jwks_cache_seconds": 301}, {"oidc_request_timeout_seconds": 2.1},
    {"oidc_allowed_algorithms": ("HS256",)}, {"oidc_allowed_algorithms": ("none",)},
    {"oidc_client_ids": ()},
])
def test_production_rejects_unverifiable_or_unbounded_identity_configuration(changes):
    args = dict(mode="production", source="bigquery", oidc_issuer="https://issuer.example.test",
        oidc_audience="gateway-api", oidc_jwks_url="https://issuer.example.test/keys",
        oidc_access_token_profile="rfc9068", oidc_client_ids=("studio-client",))
    args.update(changes)
    with pytest.raises(ValueError):
        api().settings.validate_mode(api().settings.Settings(**args))


class SubjectStore:
    """Only PostgreSQL I/O is replaced; signature/profile/network code is real."""
    def __init__(self):
        self.unavailable = False
        self.active = True

    async def resolve_subject(self, subject, *, issuer):
        if self.unavailable:
            raise OSError("synthetic store failure")
        if subject != "verified-user" or issuer != "https://issuer.example.test" or not self.active:
            raise GatewayError("UNAUTHENTICATED", "synthetic-request", "Identity unavailable.")
        return Principal(id="db-user", subject="verified-user", service_client_id=None, kind="user")

    async def resolve_principal(self, principal_id):
        if self.unavailable:
            raise OSError("synthetic store failure")
        if principal_id == "service-only" and self.active:
            return Principal(id="service-only", subject="service-subject", service_client_id="service-client", kind="service")
        if principal_id == "demo-north" and self.active:
            return Principal(id="demo-north", subject="synthetic-north", service_client_id=None, kind="demo")
        raise GatewayError("UNAUTHENTICATED", "synthetic-request", "Identity unavailable.")


def jwk(kid="key-1", **changes):
    key = serialization.load_pem_private_key(TEST_SIGNING_KEY.encode(), password=None)
    value = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    value.update(kid=kid, use="sig", alg="RS256")
    value.update(changes)
    return value


def signed_token(clock, *, headers=None, algorithm="RS256", **changes):
    claims = {"iss": "https://issuer.example.test", "aud": "gateway-api", "sub": "verified-user",
        "exp": int(clock.now().timestamp()) + 60, "iat": int(clock.now().timestamp()),
        "client_id": "studio-client", "jti": "synthetic-access-token"}
    claims.update(changes)
    token_headers = {"kid": "key-1", "typ": "at+jwt"}
    token_headers.update(headers or {})
    key = TEST_SIGNING_KEY if algorithm.startswith(("RS", "PS")) else secrets.token_urlsafe(32)
    return jwt.encode(claims, key, algorithm=algorithm, headers=token_headers)


def authenticator(clock, *, settings=None, store=None, response=None):
    requests = []
    def handler(request):
        requests.append(request)
        reply = response(request) if response is not None else httpx.Response(200, json={"keys": [jwk()]})
        # Real network responses are unread streams. Keep mock response helpers
        # faithful to that boundary rather than supplying predecoded content.
        if reply.is_stream_consumed:
            return httpx.Response(reply.status_code, headers=reply.headers, stream=httpx.ByteStream(reply.content))
        return reply
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = api().tokens.Authenticator(settings or production_settings(), store or SubjectStore(),
        clock=clock, http_client=client)
    return auth, requests, client


async def code(auth, authorization):
    try:
        await auth.authenticate(authorization)
    except GatewayError as error:
        assert error.request_id and error.safe_message
        return error.code.value
    return "accepted"


def test_verified_subject_maps_to_user_and_preserves_oauth_client(clock):
    async def scenario():
        auth, requests, client = authenticator(clock)
        try:
            principal = await auth.authenticate("Bearer " + signed_token(clock, roles=["operator"], tenant_id="other", claimed_user_id="admin"))
            assert principal.id == "db-user"
            assert principal.subject == "verified-user"
            assert principal.kind == "user"
            assert principal.service_client_id == "studio-client"
            assert set(principal.model_dump()) == {"id", "subject", "service_client_id", "kind"}
            assert len(requests) == 1
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("changes,headers,algorithm", [
    ({"exp": 0}, {}, "RS256"), ({"aud": "wrong-audience"}, {}, "RS256"),
    ({"iss": "https://other-issuer.test"}, {}, "RS256"), ({}, {"kid": "unknown"}, "RS256"),
    ({}, {"typ": "JWT"}, "RS256"), ({"token_use": "id"}, {"typ": "JWT"}, "RS256"),
    ({}, {}, "HS256"), ({}, {}, "RS512"), ({"sub": "unknown-user"}, {}, "RS256"),
    ({"client_id": "unapproved-client"}, {}, "RS256"), ({"jti": None}, {}, "RS256"),
    ({"iat": None}, {}, "RS256"), ({"exp": None}, {}, "RS256"),
    ({"exp": "9999999999"}, {}, "RS256"), ({"iat": True}, {}, "RS256"),
    ({"nbf": 9999999999}, {}, "RS256"), ({}, {"crit": ["unknown"]}, "RS256"),
    ({}, {"jku": "https://attacker.test/keys"}, "RS256"),
    ({}, {"x5u": "https://attacker.test/cert"}, "RS256"),
    ({}, {"jwk": {"kty": "oct", "k": "irrelevant"}}, "RS256"),
])
def test_invalid_or_non_access_tokens_fail_closed_without_untrusted_fetch(clock, changes, headers, algorithm):
    async def scenario():
        auth, requests, client = authenticator(clock)
        try:
            assert await code(auth, "Bearer " + signed_token(clock, headers=headers, algorithm=algorithm, **changes)) == "UNAUTHENTICATED"
            assert all(str(request.url) == "https://issuer.example.test/keys" for request in requests)
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("authorization", [None, "", "Basic ignored", "Bearer", "Bearer x y", "Bearer\nignored", "Bearer " + "x" * 17000])
def test_missing_malformed_or_oversized_authorization_never_fetches(clock, authorization):
    async def scenario():
        auth, requests, client = authenticator(clock)
        try:
            assert await code(auth, authorization) == "UNAUTHENTICATED"
            assert requests == []
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_wrong_signature_is_rejected(clock):
    async def scenario():
        auth, _, client = authenticator(clock)
        try:
            token = signed_token(clock)
            head, body, signature = token.split(".")
            signature = ("A" if signature[0] != "A" else "B") + signature[1:]
            assert await code(auth, "Bearer " + ".".join((head, body, signature))) == "UNAUTHENTICATED"
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_jwks_cache_expires_without_stale_fallback_and_bounds_unknown_refresh(clock):
    async def scenario():
        outage = False
        def response(request):
            if outage:
                raise httpx.ConnectError("synthetic issuer failure", request=request)
            return httpx.Response(200, json={"keys": [jwk()]})
        auth, requests, client = authenticator(clock, response=response)
        try:
            assert await code(auth, "Bearer " + signed_token(clock)) == "accepted"
            for _ in range(4):
                assert await code(auth, "Bearer " + signed_token(clock, headers={"kid": "unknown"})) == "UNAUTHENTICATED"
            assert len(requests) == 2
            outage = True
            # A known key remains usable only within its bounded existing cache.
            assert await code(auth, "Bearer " + signed_token(clock)) == "accepted"
            clock.instant += timedelta(seconds=300)
            assert await code(auth, "Bearer " + signed_token(clock)) == "UNAUTHENTICATED"
            assert len(requests) == 3
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_unknown_key_can_rotate_once_from_only_operator_endpoint(clock):
    async def scenario():
        def response(request):
            return httpx.Response(200, json={"keys": [jwk(kid="key-1"), jwk(kid="rotated-key")]})
        auth, requests, client = authenticator(clock, response=response)
        try:
            assert await code(auth, "Bearer " + signed_token(clock, headers={"kid": "rotated-key"})) == "accepted"
            assert len(requests) == 1
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("jwks_body", [
    {"keys": []}, {"keys": [jwk(), jwk()]}, {"keys": [jwk(use="enc")]},
    {"keys": [jwk(alg="RS512")]}, {"keys": [jwk(d="private-key-material")]},
    {"keys": [jwk(key_ops=["sign"])]}, {"keys": [jwk(kid=str(i)) for i in range(21)]},
])
def test_malformed_ambiguous_private_or_unbounded_jwks_rejected(clock, jwks_body):
    async def scenario():
        auth, _, client = authenticator(clock, response=lambda _: httpx.Response(200, json=jwks_body))
        try:
            assert await code(auth, "Bearer " + signed_token(clock)) == "UNAUTHENTICATED"
        finally:
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("response", [
    lambda _: httpx.Response(302, headers={"location": "https://attacker.test/keys"}),
    lambda _: httpx.Response(200, content=b"x" * 65537),
    lambda _: httpx.Response(200, content=b"invalid-json"),
])
def test_issuer_redirects_oversized_and_non_json_responses_fail_closed(clock, response):
    async def scenario():
        auth, requests, client = authenticator(clock, response=response)
        try:
            assert await code(auth, "Bearer " + signed_token(clock)) == "UNAUTHENTICATED"
            assert len(requests) == 1
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_unavailable_or_revoked_subject_store_fails_closed(clock):
    async def scenario():
        store = SubjectStore()
        auth, _, client = authenticator(clock, store=store)
        try:
            store.unavailable = True
            assert await code(auth, "Bearer " + signed_token(clock)) == "UNAUTHENTICATED"
            store.unavailable = False
            store.active = False
            assert await code(auth, "Bearer " + signed_token(clock)) == "UNAUTHENTICATED"
        finally:
            await client.aclose()
    asyncio.run(scenario())


def write_credentials(path, *, token, principal_id="service-only", kind="service", active=True):
    path.write_text(json.dumps({"version": 1, "credentials": [{
        "digest": hashlib.sha256(token.encode()).hexdigest(), "principal_id": principal_id,
        "kind": kind, "active": active}]}))
    path.chmod(0o600)


def test_hash_only_service_credentials_reload_revocation_and_cannot_impersonate(clock, tmp_path):
    async def scenario():
        token = "dpg_svc_" + secrets.token_urlsafe(32)
        path = tmp_path / "hashes.json"
        write_credentials(path, token=token)
        auth = api().tokens.Authenticator(production_settings(), SubjectStore(), clock=clock,
            credential_store=api().principals.FileCredentialStore(path))
        try:
            principal = await auth.authenticate("Bearer " + token)
            assert principal.id == "service-only" and principal.kind == "service"
            assert principal.subject == "service-subject"
            assert token not in path.read_text()
            write_credentials(path, token=token, active=False)
            assert await code(auth, "Bearer " + token) == "UNAUTHENTICATED"
            write_credentials(path, token=token, principal_id="demo-north")
            assert await code(auth, "Bearer " + token) == "UNAUTHENTICATED"
        finally:
            await auth.close()
    asyncio.run(scenario())


def test_demo_tokens_are_unusable_in_production_and_service_tokens_in_demo(clock, tmp_path):
    async def scenario():
        token = "dpg_demo_" + secrets.token_urlsafe(32)
        path = tmp_path / "hashes.json"
        write_credentials(path, token=token, principal_id="demo-north", kind="demo")
        credentials = api().principals.FileCredentialStore(path)
        demo = api().tokens.Authenticator(api().settings.Settings(), SubjectStore(), clock=clock, credential_store=credentials)
        prod = api().tokens.Authenticator(production_settings(), SubjectStore(), clock=clock, credential_store=credentials)
        try:
            assert (await demo.authenticate("Bearer " + token)).id == "demo-north"
            assert await code(prod, "Bearer " + token) == "UNAUTHENTICATED"
            assert await code(demo, "Bearer " + signed_token(clock)) == "UNAUTHENTICATED"
            assert await code(demo, "Bearer dpg_svc_" + secrets.token_urlsafe(32)) == "UNAUTHENTICATED"
        finally:
            await demo.close()
            await prod.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["missing", "symlink", "world_writable", "oversized", "duplicate", "plaintext", "unknown_field", "invalid_version", "directory"])
def test_unsafe_credential_file_fails_closed(clock, tmp_path, state):
    async def scenario():
        token = "dpg_svc_" + secrets.token_urlsafe(32)
        path = tmp_path / "hashes.json"
        write_credentials(path, token=token)
        if state == "missing":
            path.unlink()
        elif state == "symlink":
            target = tmp_path / "target.json"
            path.rename(target)
            path.symlink_to(target)
        elif state == "world_writable":
            path.chmod(0o666)
        elif state == "oversized":
            path.write_bytes(b"x" * 1048577)
        elif state == "directory":
            path.unlink()
            path.mkdir()
        else:
            value = json.loads(path.read_text())
            if state == "invalid_version":
                value["version"] = True
            elif state == "duplicate":
                value["credentials"].append(value["credentials"][0])
            elif state == "plaintext":
                value["credentials"][0]["digest"] = token
            else:
                value["credentials"][0]["roles"] = ["operator"]
            path.write_text(json.dumps(value))
        auth = api().tokens.Authenticator(production_settings(), SubjectStore(), clock=clock,
            credential_store=api().principals.FileCredentialStore(path))
        try:
            assert await code(auth, "Bearer " + token) == "UNAUTHENTICATED"
        finally:
            await auth.close()
    asyncio.run(scenario())


def test_demo_generator_emits_local_synthetic_tokens_outside_repo_and_no_stdout(tmp_path):
    output = tmp_path / "demo-accounts"
    completed = subprocess.run([sys.executable, "scripts/demo_accounts.py", "--output-dir", str(output)],
        capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    tokens = json.loads((output / "demo-tokens.json").read_text())
    hashes = json.loads((output / "credential-hashes.json").read_text())
    assert set(tokens) == {"demo-north", "demo-south", "demo-author", "demo-publisher"}
    assert len(set(tokens.values())) == 4
    assert all(value.startswith("dpg_demo_") and len(value) >= 50 for value in tokens.values())
    for token in tokens.values():
        assert token not in completed.stdout + completed.stderr
        assert token not in (output / "credential-hashes.json").read_text()
        assert token not in (output / "bootstrap.sql").read_text()
        assert hashlib.sha256(token.encode()).hexdigest() in {item["digest"] for item in hashes["credentials"]}
    assert all(path.stat().st_mode & 0o077 == 0 for path in output.iterdir())
    again = subprocess.run([sys.executable, "scripts/demo_accounts.py", "--output-dir", str(output)], capture_output=True, text=True, timeout=10)
    assert again.returncode != 0


def test_demo_generator_refuses_repository_output():
    completed = subprocess.run([sys.executable, "scripts/demo_accounts.py", "--output-dir", str(Path.cwd() / ".superpowers" / "should-not-exist")],
        capture_output=True, text=True, timeout=10)
    assert completed.returncode != 0
    assert "outside the repository" in completed.stderr
    assert not (Path.cwd() / ".superpowers" / "should-not-exist").exists()


def test_server_roles_map_to_scoped_capabilities_and_unknown_roles_fail_closed():
    grants = api().principals
    assert grants.capabilities_for_roles(["consumer", "publisher"]) == ("describe", "discover", "execute", "publish", "result")
    assert grants.capabilities_for_roles(["author"]) == ("author",)
    assert grants.capabilities_for_roles(["operator"]) == ("operate",)
    for roles in (["unknown"], ["consumer", "consumer"], "consumer", [1]):
        with pytest.raises(ValueError):
            grants.capabilities_for_roles(roles)


def test_grant_objects_reject_invalid_or_mutable_entitlement_shapes():
    types = api().principals
    grant = types.PolicyGrant(policy_id="revenue-access", tenant_id="north", capabilities=("execute",), entitlements={"groups": ["north"]})
    assert grant.entitlements["groups"] == ("north",)
    with pytest.raises(TypeError):
        grant.entitlements["groups"] = ("south",)
    for entitlements in ({"groups": "north"}, {"groups": [None]}, {"groups": [[]]}, {"groups": ["x"] * 101}):
        with pytest.raises(ValueError):
            types.PolicyGrant(policy_id="revenue-access", tenant_id="north", capabilities=("execute",), entitlements=entitlements)


def test_settings_environment_parser_uses_guarded_values(monkeypatch):
    monkeypatch.setenv("GATEWAY_IDENTITY_MODE", "demo")
    monkeypatch.setenv("GATEWAY_SOURCE", "bigquery")
    with pytest.raises(ValueError):
        api().settings.Settings.from_env()
    monkeypatch.setenv("GATEWAY_SOURCE", "synthetic_postgres")
    monkeypatch.setenv("GATEWAY_PUBLIC_INGRESS", "yes")
    with pytest.raises(ValueError):
        api().settings.Settings.from_env()


def test_jwks_total_deadline_stops_trickling_response(clock):
    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            data = json.dumps({"keys": [jwk()]}).encode()
            yield data[:1]
            await asyncio.sleep(0.15)
            yield data[1:]
    async def scenario():
        auth, _, client = authenticator(clock, settings=production_settings(oidc_request_timeout_seconds=0.05),
            response=lambda _: httpx.Response(200, stream=Trickle()))
        try:
            assert await code(auth, "Bearer " + signed_token(clock)) == "UNAUTHENTICATED"
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_jwks_unknown_rotation_refreshes_once_after_warm_cache(clock):
    async def scenario():
        count = 0
        def response(request):
            nonlocal count
            count += 1
            return httpx.Response(200, json={"keys": [jwk(kid="key-1" if count == 1 else "new-key")]})
        auth, requests, client = authenticator(clock, response=response)
        try:
            assert await code(auth, "Bearer " + signed_token(clock)) == "accepted"
            assert await code(auth, "Bearer " + signed_token(clock, headers={"kid": "new-key"})) == "accepted"
            assert len(requests) == 2
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_duplicate_credential_json_fields_fail_closed(clock, tmp_path):
    async def scenario():
        token = "dpg_svc_" + secrets.token_urlsafe(32)
        path = tmp_path / "hashes.json"
        digest = hashlib.sha256(token.encode()).hexdigest()
        path.write_text('{"version":1,"credentials":[{"digest":"' + digest + '","principal_id":"service-only","kind":"service","active":false,"active":true}]}')
        path.chmod(0o600)
        auth = api().tokens.Authenticator(production_settings(), SubjectStore(), clock=clock,
            credential_store=api().principals.FileCredentialStore(path))
        try:
            assert await code(auth, "Bearer " + token) == "UNAUTHENTICATED"
        finally:
            await auth.close()
    asyncio.run(scenario())


def test_credentials_survive_atomic_operator_replacement_and_fail_on_unavailable_store(clock, tmp_path):
    async def scenario():
        token = "dpg_svc_" + secrets.token_urlsafe(32)
        path = tmp_path / "hashes.json"
        replacement = tmp_path / "replacement.json"
        write_credentials(path, token=token)
        store = SubjectStore()
        auth = api().tokens.Authenticator(production_settings(), store, clock=clock,
            credential_store=api().principals.FileCredentialStore(path))
        try:
            assert await code(auth, "Bearer " + token) == "accepted"
            write_credentials(replacement, token=token, active=False)
            replacement.replace(path)
            assert await code(auth, "Bearer " + token) == "UNAUTHENTICATED"
            write_credentials(replacement, token=token)
            replacement.replace(path)
            store.unavailable = True
            assert await code(auth, "Bearer " + token) == "UNAUTHENTICATED"
        finally:
            await auth.close()
    asyncio.run(scenario())


def test_operator_settings_can_supply_external_hash_file(clock, tmp_path):
    async def scenario():
        token = "dpg_demo_" + secrets.token_urlsafe(32)
        path = tmp_path / "hashes.json"
        write_credentials(path, token=token, principal_id="demo-north", kind="demo")
        settings = api().settings.Settings(credential_hash_file=str(path))
        auth = api().tokens.Authenticator(settings, SubjectStore(), clock=clock)
        try:
            assert (await auth.authenticate("Bearer " + token)).id == "demo-north"
        finally:
            await auth.close()
    asyncio.run(scenario())


def test_oidc_registry_subject_key_is_bound_to_issuer_and_unambiguous():
    key = api().principals.oidc_subject_key
    assert key("https://issuer-one.test", "same-subject") != key("https://issuer-two.test", "same-subject")
    assert key("https://issuer-one.test", "same-subject") == key("https://issuer-one.test", "same-subject")
    assert "same-subject" not in key("https://issuer-one.test", "same-subject")
    assert len(key("https://issuer-one.test", "s" * 512)) < 128
    for subject in ("", "s" * 513, None):
        with pytest.raises(ValueError):
            key("https://issuer-one.test", subject)


def test_expiry_boundary_and_not_before_are_checked_against_injected_clock(clock):
    async def scenario():
        auth, _, client = authenticator(clock)
        now = int(clock.now().timestamp())
        try:
            assert await code(auth, "Bearer " + signed_token(clock, exp=now)) == "UNAUTHENTICATED"
            assert await code(auth, "Bearer " + signed_token(clock, exp=now + 1, nbf=now)) == "accepted"
            assert await code(auth, "Bearer " + signed_token(clock, iat=now + 1)) == "UNAUTHENTICATED"
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_allowlisted_asymmetric_alternative_requires_matching_jwk_and_config(clock):
    async def scenario():
        settings = production_settings(oidc_allowed_algorithms=("RS512",))
        auth, _, client = authenticator(clock, settings=settings,
            response=lambda _: httpx.Response(200, json={"keys": [jwk(alg="RS512")]}))
        try:
            assert await code(auth, "Bearer " + signed_token(clock, algorithm="RS512")) == "accepted"
            assert await code(auth, "Bearer " + signed_token(clock)) == "UNAUTHENTICATED"
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_duplicate_signed_claims_are_rejected_before_fetch(clock):
    async def scenario():
        header = {"alg": "RS256", "kid": "key-1", "typ": "at+jwt"}
        now = int(clock.now().timestamp())
        payload = ('{"iss":"https://issuer.example.test","aud":"wrong", "aud":"gateway-api",'
            '"sub":"verified-user","exp":' + str(now + 60) + ',"iat":' + str(now) + ',"client_id":"studio-client","jti":"synthetic"}').encode()
        # Sign the actual ambiguous bytes, so rejection cannot be attributed to
        # a bad signature. The generated bearer token is never written to disk.
        token = jwt.api_jws.encode(payload, TEST_SIGNING_KEY, algorithm="RS256", headers=header)
        auth, requests, client = authenticator(clock)
        try:
            assert await code(auth, "Bearer " + token) == "UNAUTHENTICATED"
            assert requests == []
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_production_can_select_real_postgres_but_demo_cannot():
    args = dict(mode="production", source="postgres", oidc_issuer="https://issuer.example.test",
        oidc_audience="gateway-api", oidc_jwks_url="https://issuer.example.test/keys",
        oidc_access_token_profile="rfc9068", oidc_client_ids=("studio-client",))
    api().settings.validate_mode(api().settings.Settings(**args))
    with pytest.raises(ValueError):
        api().settings.validate_mode(api().settings.Settings(source="postgres"))


def test_compressed_oversized_jwks_is_rejected_before_read_or_decompression(clock):
    import gzip
    class CompressedResponse(httpx.AsyncByteStream):
        def __init__(self):
            # Small wire representation, very large decoded body. All data is
            # synthetic; the verifier must reject the encoding without reading it.
            self.wire = gzip.compress(b" " * (8 * 1024 * 1024))
            self.reads = 0
        async def __aiter__(self):
            self.reads += 1
            yield self.wire
    async def scenario():
        stream = CompressedResponse()
        auth, requests, client = authenticator(clock, response=lambda _: httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=stream))
        try:
            assert await code(auth, "Bearer " + signed_token(clock)) == "UNAUTHENTICATED"
            assert stream.reads == 0
            assert requests[0].headers["Accept-Encoding"] == "identity"
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_oversized_raw_jwks_chunk_is_not_copied_into_application_buffer(clock):
    import tracemalloc
    class OversizedChunk(httpx.AsyncByteStream):
        def __init__(self):
            self.data = b" " * (8 * 1024 * 1024)
        async def __aiter__(self):
            yield self.data
    async def scenario():
        stream = OversizedChunk()
        auth, _, client = authenticator(clock, response=lambda _: httpx.Response(200, stream=stream))
        authorization = "Bearer " + signed_token(clock)
        try:
            tracemalloc.start()
            assert await code(auth, authorization) == "UNAUTHENTICATED"
            peak = tracemalloc.get_traced_memory()[1]
            # The preallocated transport chunk must not be copied wholesale.
            # A liberal bound tolerates parser/client overhead on the CI runner.
            assert peak < 1024 * 1024
        finally:
            tracemalloc.stop()
            await client.aclose()
    asyncio.run(scenario())


def test_demo_generator_refuses_existing_dangling_output_symlink(tmp_path):
    target = tmp_path / "must-not-create"
    output = tmp_path / "existing-link"
    output.symlink_to(target, target_is_directory=True)
    completed = subprocess.run([sys.executable, "scripts/demo_accounts.py", "--output-dir", str(output)],
        capture_output=True, text=True, timeout=10)
    assert completed.returncode != 0
    assert output.is_symlink()
    assert not target.exists()
