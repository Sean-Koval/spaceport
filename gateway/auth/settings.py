"""Operator-selected identity settings; deployment entry points must validate them."""
import ipaddress
import os
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field

from gateway.contracts.canonical import Array, Identifier, StrictModel


class Settings(StrictModel):
    mode: Literal["demo", "production"] = "demo"
    source: Literal["synthetic_postgres", "postgres", "bigquery"] = "synthetic_postgres"
    bind_host: Annotated[str, Field(min_length=1, max_length=255)] = "127.0.0.1"
    public_ingress: bool = False
    trust_forwarded_headers: bool = False
    credential_hash_file: str | None = None
    oidc_issuer: str | None = None
    oidc_audience: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    oidc_jwks_url: str | None = None
    oidc_access_token_profile: Literal["disabled", "rfc9068"] = "disabled"
    oidc_allowed_algorithms: Array[Literal["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]] = ("RS256",)
    oidc_client_ids: Array[Identifier] = Field(default=(), max_length=100)
    oidc_jwks_cache_seconds: Annotated[int, Field(ge=1, le=300)] = 300
    oidc_request_timeout_seconds: Annotated[float, Field(gt=0, le=2)] = 2.0

    @classmethod
    def from_env(cls) -> "Settings":
        """Parse only explicit operator environment keys and fail on unsafe values."""
        keys = {
            "GATEWAY_IDENTITY_MODE": "mode", "GATEWAY_SOURCE": "source", "GATEWAY_BIND_HOST": "bind_host",
            "GATEWAY_CREDENTIAL_HASH_FILE": "credential_hash_file", "GATEWAY_PUBLIC_INGRESS": "public_ingress", "GATEWAY_TRUST_FORWARDED_HEADERS": "trust_forwarded_headers",
            "GATEWAY_OIDC_ISSUER": "oidc_issuer", "GATEWAY_OIDC_AUDIENCE": "oidc_audience",
            "GATEWAY_OIDC_JWKS_URL": "oidc_jwks_url", "GATEWAY_OIDC_ACCESS_TOKEN_PROFILE": "oidc_access_token_profile",
            "GATEWAY_OIDC_ALLOWED_ALGORITHMS": "oidc_allowed_algorithms", "GATEWAY_OIDC_CLIENT_IDS": "oidc_client_ids",
            "GATEWAY_OIDC_JWKS_CACHE_SECONDS": "oidc_jwks_cache_seconds", "GATEWAY_OIDC_REQUEST_TIMEOUT_SECONDS": "oidc_request_timeout_seconds",
        }
        raw = {field: os.environ[key] for key, field in keys.items() if key in os.environ}
        for field in ("public_ingress", "trust_forwarded_headers"):
            if field in raw:
                if raw[field] not in {"true", "false"}:
                    raise ValueError("Boolean identity settings require true or false")
                raw[field] = raw[field] == "true"
        for field in ("oidc_allowed_algorithms", "oidc_client_ids"):
            if field in raw:
                raw[field] = tuple(item.strip() for item in raw[field].split(",")) if raw[field] else ()
        if "oidc_jwks_cache_seconds" in raw:
            raw["oidc_jwks_cache_seconds"] = int(raw["oidc_jwks_cache_seconds"])
        if "oidc_request_timeout_seconds" in raw:
            raw["oidc_request_timeout_seconds"] = float(raw["oidc_request_timeout_seconds"])
        settings = cls(**raw)
        validate_mode(settings)
        return settings


def trusted_https_url(value: str | None) -> bool:
    if not isinstance(value, str) or len(value) > 2048 or any(char.isspace() for char in value):
        return False
    try:
        parts = urlsplit(value)
        return bool(parts.scheme == "https" and parts.hostname and not parts.username and not parts.password
                    and not parts.fragment and not parts.query and parts.port in {None, 443})
    except ValueError:
        return False


def validate_mode(settings: Settings) -> None:
    """Reject unsafe combinations before any socket, source or identity is opened."""
    settings = Settings.model_validate(settings.model_dump())
    if settings.credential_hash_file is not None:
        path = Path(settings.credential_hash_file)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("The credential file requires an absolute operator-selected path")
    if settings.mode == "demo":
        try:
            loopback = ipaddress.ip_address(settings.bind_host).is_loopback
        except ValueError:
            loopback = False
        if (settings.source != "synthetic_postgres" or not loopback or settings.public_ingress
            or settings.trust_forwarded_headers or settings.oidc_issuer is not None
            or settings.oidc_audience is not None or settings.oidc_jwks_url is not None
            or settings.oidc_access_token_profile != "disabled" or settings.oidc_client_ids):
            raise ValueError("Demo identity requires synthetic sources, explicit loopback ingress and no OIDC")
        return
    if (not trusted_https_url(settings.oidc_issuer) or not trusted_https_url(settings.oidc_jwks_url)
        or not settings.oidc_audience or settings.oidc_access_token_profile != "rfc9068"
        or not settings.oidc_client_ids or not settings.oidc_allowed_algorithms
        or len(set(settings.oidc_client_ids)) != len(settings.oidc_client_ids)
        or len(set(settings.oidc_allowed_algorithms)) != len(settings.oidc_allowed_algorithms)):
        raise ValueError("Production identity requires trusted issuer/JWKS, audience, clients and an access-token profile")
