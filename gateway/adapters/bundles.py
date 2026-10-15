"""Fail-closed immutable operator deployment catalog, never populated from a request."""
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator

from gateway.contracts.canonical import Digest, Identifier, StrictModel
from gateway.contracts.errors import ErrorCode, GatewayError


class CubeEndpoint(StrictModel):
    endpoint_id: Identifier
    adapter_revision: Literal["cube-rest-v1", "cube-rest-v2"] = "cube-rest-v1"
    url: Annotated[str,Field(min_length=1,max_length=2048)]
    bundle_digest: Digest
    secret_ref: Annotated[str,Field(min_length=1,max_length=1024)]
    context: Literal["consumer","fixture_test"]

    @field_validator("url")
    @classmethod
    def fixed_base_url(cls, value):
        parsed=urlsplit(value)
        if (parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"","/"}
            or any(ch.isspace() or ord(ch)<32 for ch in value)):
            raise ValueError("Operator endpoints require a credential-free HTTP base URL")
        try: parsed.port
        except ValueError: raise ValueError("Invalid operator endpoint port") from None
        return value.rstrip("/")

    @field_validator("secret_ref")
    @classmethod
    def mounted_secret(cls, value):
        if not value.startswith("/run/secrets/") or ".." in value or len(Path(value).parts) != 4:
            raise ValueError("Only a mounted operator secret reference is supported")
        return value


class BundleCatalog:
    def __init__(self, records: dict):
        try:
            if not isinstance(records,dict) or not records: raise ValueError
            entries=[CubeEndpoint.model_validate(value) for value in records.values()]
            if any(type(key) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",key) for key in records): raise ValueError
            if len({item.endpoint_id for item in entries}) != len(entries): raise ValueError
            if len({item.bundle_digest for item in entries}) != len(entries): raise ValueError
        except (ValueError,TypeError,ValidationError):
            raise GatewayError(ErrorCode.BINDING_DRIFT,"bundle-catalog","The operator bundle catalog is invalid.") from None
        self._entries=MappingProxyType({item.bundle_digest:item for item in entries})

    @classmethod
    def from_file(cls, path: Path | str) -> "BundleCatalog":
        try:
            return cls(json.loads(Path(path).read_text()))
        except (OSError,ValueError):
            raise GatewayError(ErrorCode.BINDING_DRIFT,"bundle-catalog","The operator bundle catalog is unavailable.") from None

    def resolve(self, bundle_digest: str) -> CubeEndpoint:
        try:
            return self._entries[bundle_digest]
        except KeyError:
            raise GatewayError(ErrorCode.BINDING_DRIFT,"bundle-resolution","The retained model bundle is unavailable.") from None

    def resolve_query(self, endpoint_id: str, bundle_digest: str) -> CubeEndpoint:
        endpoint=self.resolve(bundle_digest)
        if endpoint.endpoint_id != endpoint_id:
            raise GatewayError(ErrorCode.BINDING_DRIFT,"bundle-resolution","The pinned model endpoint is incompatible.")
        return endpoint
