"""Bearer authentication with a fixed RFC 9068 profile and bounded trusted JWKS."""
import asyncio
import json
import re
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec, ed25519, ed448, rsa
import httpx
import jwt

from gateway.contracts.clock import Clock, SystemClock
from gateway.contracts.identity import Principal
from gateway.source_delegation import has_unsupported_delegation_claims
from .principals import FileCredentialStore, PrincipalStore, credential_digest, unauthenticated
from .settings import Settings, validate_mode


MAX_TOKEN_BYTES = 16_384
MAX_JWKS_BYTES = 65_536
MAX_JWKS_KEYS = 20
UNKNOWN_KEY_REFRESH_INTERVAL = 30
OPAQUE_TOKEN = re.compile(r"^dpg_(svc|demo)_[A-Za-z0-9_-]{43}$")


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Ambiguous JSON")
        result[key] = value
    return result


def _json(data: str | bytes) -> Any:
    return json.loads(data, object_pairs_hook=_object_without_duplicates,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))


class JWKSCache:
    """A per-authenticator bounded cache, never a token-selected discovery client."""
    def __init__(self, settings: Settings, client: httpx.AsyncClient, clock: Clock):
        self.settings = settings
        self.client = client
        self.clock = clock
        self._keys: dict[str, dict] = {}
        self._expires_at = 0.0
        self._unknown_refresh_after = 0.0
        self._lock = asyncio.Lock()

    async def _download(self) -> dict[str, dict]:
        async with self.client.stream("GET", self.settings.oidc_jwks_url,
            timeout=self.settings.oidc_request_timeout_seconds, follow_redirects=False,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"}) as response:
            if response.status_code != 200:
                raise ValueError("Issuer keys unavailable")
            # HTTPX's decoded iterator can expand a compressed chunk without an
            # output bound before yielding. Refuse compression and bypass its
            # decoder entirely; check remaining raw budget BEFORE any copy.
            if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                raise ValueError("Compressed issuer keys are unsupported")
            body = bytearray()
            async for part in response.aiter_raw():
                if len(part) > MAX_JWKS_BYTES - len(body):
                    raise ValueError("Issuer key response too large")
                body.extend(part)
        state = _json(body)
        if not isinstance(state, dict) or not isinstance(state.get("keys"), list) or not 1 <= len(state["keys"]) <= MAX_JWKS_KEYS:
            raise ValueError("Invalid issuer key set")
        keys = {}
        for value in state["keys"]:
            if (not isinstance(value, dict) or type(value.get("kid")) is not str
                or not 1 <= len(value["kid"]) <= 128 or value["kid"] in keys
                or value.get("use", "sig") != "sig"
                or value.get("key_ops", ["verify"]) != ["verify"]
                or any(field in value for field in ("d", "p", "q", "dp", "dq", "qi", "oth", "k", "jku", "x5u"))
                or value.get("kty") not in {"RSA", "EC", "OKP"}):
                raise ValueError("Ambiguous, private or unsupported issuer key")
            alg = value.get("alg")
            if alg is not None and alg not in self.settings.oidc_allowed_algorithms:
                raise ValueError("Unsupported issuer key algorithm")
            keys[value["kid"]] = value
        return keys

    async def _refresh(self):
        # HTTPX bounds connection/read inactivity; wait_for also bounds TOTAL time,
        # including malicious slow streaming. Expired keys never rescue a failure.
        keys = await asyncio.wait_for(self._download(), timeout=self.settings.oidc_request_timeout_seconds)
        self._keys = keys
        self._expires_at = self.clock.now().timestamp() + self.settings.oidc_jwks_cache_seconds

    async def key(self, kid: str, algorithm: str):
        async with self._lock:
            now = self.clock.now().timestamp()
            if not self._keys or now >= self._expires_at:
                await self._refresh()
            elif kid not in self._keys and now >= self._unknown_refresh_after:
                # At most one refresh for this lookup; a global cooldown also
                # prevents distinct attacker-selected kids from driving fetches.
                self._unknown_refresh_after = now + UNKNOWN_KEY_REFRESH_INTERVAL
                await self._refresh()
            value = self._keys.get(kid)
            if value is None or value.get("alg", algorithm) != algorithm:
                raise ValueError("Unknown issuer key")
            key = jwt.PyJWK(value, algorithm=algorithm).key
            if algorithm.startswith(("RS", "PS")):
                valid = isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048
            elif algorithm.startswith("ES"):
                valid = isinstance(key, ec.EllipticCurvePublicKey) and key.curve.name == {
                    "ES256": "secp256r1", "ES384": "secp384r1", "ES512": "secp521r1"}[algorithm]
            else:
                valid = isinstance(key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey))
            if not valid:
                raise ValueError("Issuer key does not match the configured algorithm")
            return key


class Authenticator:
    """Authenticate only the Authorization string; identity headers are irrelevant.

    Owns one bounded, reusable HTTP client unless an internal caller supplies one.
    Construct once per event-loop/process, then close at application shutdown.
    """
    def __init__(self, settings: Settings, principal_store: PrincipalStore, *,
                 clock: Clock | None = None, http_client: httpx.AsyncClient | None = None,
                 credential_store: FileCredentialStore | None = None):
        validate_mode(settings)
        self.settings = Settings.model_validate(settings.model_dump())
        self.principal_store = principal_store
        self.credential_store = credential_store or (FileCredentialStore(settings.credential_hash_file) if settings.credential_hash_file else None)
        self.clock = clock or SystemClock()
        self._owns_client = http_client is None
        self.client = http_client or httpx.AsyncClient(
            timeout=settings.oidc_request_timeout_seconds, follow_redirects=False,
            trust_env=False, limits=httpx.Limits(max_connections=4, max_keepalive_connections=2))
        self.jwks = JWKSCache(self.settings, self.client, self.clock)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def authenticate(self, authorization: str | None) -> Principal:
        try:
            if (type(authorization) is not str or not 1 <= len(authorization) <= MAX_TOKEN_BYTES
                or not authorization.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in authorization)):
                raise ValueError("Missing or malformed authorization")
            parts = authorization.split(" ")
            if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
                raise ValueError("Missing bearer authorization")
            token = parts[1]
            opaque = OPAQUE_TOKEN.fullmatch(token)
            if opaque:
                return await self._opaque(token, opaque.group(1))
            if self.settings.mode != "production":
                raise ValueError("OIDC is disabled in demo mode")
            return await self._oidc(token)
        except Exception:
            # Includes identity-store, parser, issuer and crypto failures. Never
            # serialize exception text, claims, key bodies, headers or tokens.
            raise unauthenticated() from None

    async def _opaque(self, token: str, prefix: str) -> Principal:
        kind = "service" if prefix == "svc" else "demo"
        if self.credential_store is None or (self.settings.mode == "demo") != (kind == "demo"):
            raise ValueError("Credential mode mismatch")
        credential = await self.credential_store.get_by_digest(credential_digest(token))
        if credential.kind != kind:
            raise ValueError("Credential principal kind mismatch")
        principal = await self.principal_store.resolve_principal(credential.principal_id)
        if principal.kind != kind or (kind == "service" and not principal.service_client_id):
            raise ValueError("Stored service/demo identity is invalid")
        return principal

    async def _oidc(self, token: str) -> Principal:
        pieces = token.split(".")
        if len(pieces) != 3:
            raise ValueError("Malformed access token")
        header = _json(jwt.utils.base64url_decode(pieces[0].encode("ascii")))
        # Reject any token-directed key location, embedded key or unknown critical
        # extension before contacting even the operator-selected JWKS endpoint.
        if (not isinstance(header, dict) or any(name in header for name in ("jku", "x5u", "jwk", "x5c", "crit", "b64"))
            or header.get("alg") not in self.settings.oidc_allowed_algorithms
            or type(header.get("kid")) is not str or not 1 <= len(header["kid"]) <= 128
            or type(header.get("typ")) is not str or header["typ"].lower() not in {"at+jwt", "application/at+jwt"}):
            raise ValueError("Unacceptable access-token header")
        # Parse signed claims independently to reject duplicate members rather
        # than allowing last-member-wins JSON semantics at the trust boundary.
        raw_claims = _json(jwt.utils.base64url_decode(pieces[1].encode("ascii")))
        if not isinstance(raw_claims, dict):
            raise ValueError("Malformed access-token claims")
        key = await self.jwks.key(header["kid"], header["alg"])
        claims = jwt.decode(token, key, algorithms=list(self.settings.oidc_allowed_algorithms),
            issuer=self.settings.oidc_issuer, audience=self.settings.oidc_audience,
            options={"require": ["iss", "aud", "sub", "exp", "iat", "client_id", "jti"],
                "verify_exp": False, "verify_iat": False, "verify_nbf": False})
        if (self.settings.source == "reviewed_sources"
            and has_unsupported_delegation_claims(claims)):
            raise ValueError("Unsupported delegation assertion")
        now = self.clock.now().timestamp()
        if (type(claims.get("exp")) is not int or type(claims.get("iat")) is not int
            or not claims["iat"] <= now < claims["exp"] or claims["exp"] <= claims["iat"]
            or ("nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > now))
            or type(claims.get("sub")) is not str or not 1 <= len(claims["sub"]) <= 512
            or type(claims.get("jti")) is not str or not 1 <= len(claims["jti"]) <= 512
            or claims.get("client_id") not in self.settings.oidc_client_ids or claims.get("token_use") == "id"):
            raise ValueError("Unacceptable access-token profile")
        principal = await self.principal_store.resolve_subject(claims["sub"], issuer=self.settings.oidc_issuer)
        if principal.kind != "user":
            raise ValueError("OAuth client identity cannot impersonate a delegated user")
        return Principal(id=principal.id, subject=principal.subject,
                         service_client_id=claims["client_id"], kind="user")
