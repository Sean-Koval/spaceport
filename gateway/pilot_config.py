"""Explicit OIDC/synthetic-source pilot configuration, never a production switch.

Operator-owned files are regular, bounded snapshots. For projected Kubernetes
volumes, an approved init step must materialize these paths into read-only runtime
mounts; configuration/key rotation requires a rollout, not implicit hot reload.
No parsing or preflight operation opens a socket or generates credentials.
"""
from dataclasses import replace
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import stat
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from sqlalchemy import URL

from gateway.adapters.bundles import BundleCatalog
from gateway.auth.principals import CredentialFile, FileCredentialStore
from gateway.auth.settings import Settings, validate_mode
from gateway.contracts.canonical import Array, Identifier, StrictModel
from gateway.runtime_config import load_assets as load_synthetic_assets, read_secret

MAX_CONFIG_BYTES = 65_536
MAX_CA_BYTES = 1_048_576
CONSUMER_BUNDLE_DIGEST = '228090dcbeaf8416a1e8a14d4e17c2873f9963ec3346d728650308ee5684d8ba'
FIXTURE_BUNDLE_DIGEST = '8c3e9a8b030fb495bbdbbec19061df43c17d508ffadaf390a025d19120e43f17'
_CONFIG_ERROR = 'Pilot configuration is unavailable or invalid'
_FILE_ERROR = 'Required pilot files are unavailable or invalid'
_ROLES = frozenset({'app', 'owner', 'audit_retention'})
Algorithm = Literal['RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512', 'ES256', 'ES384', 'ES512', 'EdDSA']


def _safe_absolute_path(value: str) -> Path:
    if type(value) is not str or not value or len(value) > 4096 or '\\' in value or '\x00' in value:
        raise ValueError(_FILE_ERROR)
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or any(ord(c) < 32 for c in value):
        raise ValueError(_FILE_ERROR)
    return path


def _host(value: str) -> str:
    if type(value) is not str or not value or len(value) > 253:
        raise ValueError('An explicit host is required')
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if (value.endswith('.') or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part)
                                          for part in value.split('.'))):
            raise ValueError('A fixed DNS name or IP address is required') from None
        return value.lower()


def _https_url(value: str, *, path: Literal['origin', 'base', 'endpoint']) -> str:
    if (type(value) is not str or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value)
        or '\\' in value or '%' in urlsplit(value).netloc):
        raise ValueError('A fixed HTTPS URL is required')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or '?' in value or '#' in value or parsed.netloc.endswith(':')):
        raise ValueError('A credential-free HTTPS URL is required')
    host = _host(parsed.hostname)
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('A valid HTTPS port is required')
    if (path == 'origin' and parsed.path) or (path == 'base' and parsed.path not in {'', '/'}):
        raise ValueError('A fixed HTTPS origin or base is required')
    if path == 'origin' and port not in {None, 443}:
        raise ValueError('The pilot HTTPS resource requires the standard TLS port')
    if path == 'endpoint':
        return value  # Issuer paths and exact spelling have identity meaning.
    authority = f'[{host}]' if ':' in host else host
    if port not in {None, 443}: authority += ':' + str(port)
    return 'https://' + authority


def _fingerprint(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _read_file(path: str, max_bytes: int) -> bytes:
    """No-follow each path component; reject observed mutation or replacement."""
    descriptors = []
    traversed = []
    try:
        parts = _safe_absolute_path(path).parts
        parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        descriptors.append(parent)
        for name in parts[1:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            descriptors.append(child)
            observed = os.fstat(child)
            traversed.append((parent, name, observed.st_dev, observed.st_ino))
            parent = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o022 or before.st_size > max_bytes:
            raise ValueError(_FILE_ERROR)
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = os.read(descriptor, min(65_536, max_bytes + 1 - len(data)))
            if not chunk: break
            data.extend(chunk)
        after = os.fstat(descriptor)
        named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if (len(data) > max_bytes or len(data) != before.st_size or _fingerprint(before) != _fingerprint(after)
            or _fingerprint(after) != _fingerprint(named)):
            raise ValueError(_FILE_ERROR)
        for directory, name, device, inode in traversed:
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (device, inode):
                raise ValueError(_FILE_ERROR)
        return bytes(data)
    except (OSError, ValueError, IndexError):
        raise ValueError(_FILE_ERROR) from None
    finally:
        for descriptor in reversed(descriptors): os.close(descriptor)


def _parse_json(data: bytes):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: raise ValueError(_CONFIG_ERROR)
            value[key] = item
        return value
    def constant(_): raise ValueError(_CONFIG_ERROR)
    return json.loads(data.decode('utf-8'), object_pairs_hook=unique, parse_constant=constant)


def _validate_ca(path: str) -> None:
    text = _read_file(path, MAX_CA_BYTES).decode('ascii')
    certificates = re.findall(r'-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----', text)
    if not certificates or re.sub(r'-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----', '', text).strip():
        raise ValueError(_FILE_ERROR)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=text)
    if not context.get_ca_certs(): raise ValueError(_FILE_ERROR)


class PilotOIDCConfig(StrictModel):
    issuer: Annotated[str, Field(min_length=1, max_length=2048)]
    audience: Annotated[str, Field(min_length=1, max_length=2048)]
    jwks_url: Annotated[str, Field(min_length=1, max_length=2048)]
    client_ids: Array[Identifier] = Field(min_length=1, max_length=100)
    algorithms: Array[Algorithm] = Field(default=('RS256',), min_length=1, max_length=10)
    jwks_cache_seconds: Annotated[int, Field(ge=1, le=300)] = 300
    request_timeout_seconds: Annotated[float, Field(gt=0, le=2)] = 2.0

    @field_validator('issuer', 'jwks_url')
    @classmethod
    def https_endpoint(cls, value):
        return _https_url(value, path='endpoint')


class PilotRegistryConfig(StrictModel):
    host: Annotated[str, Field(min_length=1, max_length=253)]
    port: Annotated[int, Field(ge=1, le=65535)] = 5432
    database: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_]{0,62}$')]
    ca_file: Annotated[str, Field(min_length=1, max_length=4096)]

    @field_validator('host')
    @classmethod
    def fixed_host(cls, value): return _host(value)

    @field_validator('ca_file')
    @classmethod
    def fixed_ca(cls, value):
        _safe_absolute_path(value)
        return value


class PilotRuntimeConfig(StrictModel):
    schema_version: Literal[1]
    profile: Literal['oidc-synthetic-pilot']
    public_origin: str
    oidc: PilotOIDCConfig
    registry: PilotRegistryConfig
    cube_consumer_url: str
    cube_fixture_url: str
    probe_principal_id: Identifier
    credential_hash_file: Literal['/run/auth/credential-hashes.json'] | None = None

    @field_validator('schema_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int: raise ValueError('Integer schema version required')
        return value

    @field_validator('public_origin')
    @classmethod
    def origin(cls, value): return _https_url(value, path='origin')

    @field_validator('cube_consumer_url', 'cube_fixture_url')
    @classmethod
    def cube_base(cls, value): return _https_url(value, path='base')

    @model_validator(mode='after')
    def coherent_profile(self):
        if self.cube_consumer_url == self.cube_fixture_url:
            raise ValueError('Consumer and fixture endpoints must be distinct')
        if self.oidc.audience != self.mcp_resource_url:
            raise ValueError('The pilot token audience must be the canonical MCP resource')
        self.identity_settings()
        return self

    @property
    def probe_product_id(self): return 'revenue'

    @property
    def rest_allowed_hosts(self): return (urlsplit(self.public_origin).hostname,)

    @property
    def allowed_origins(self): return (self.public_origin,)

    @property
    def mcp_allowed_hosts(self): return (urlsplit(self.public_origin).netloc,)

    @property
    def mcp_resource_url(self): return self.public_origin + '/mcp'

    def identity_settings(self) -> Settings:
        settings = Settings(mode='production', source='synthetic_postgres',
            bind_host='0.0.0.0', public_ingress=True, trust_forwarded_headers=False,
            credential_hash_file=self.credential_hash_file,
            oidc_issuer=self.oidc.issuer, oidc_audience=self.oidc.audience,
            oidc_jwks_url=self.oidc.jwks_url, oidc_access_token_profile='rfc9068',
            oidc_client_ids=self.oidc.client_ids, oidc_allowed_algorithms=self.oidc.algorithms,
            oidc_jwks_cache_seconds=self.oidc.jwks_cache_seconds,
            oidc_request_timeout_seconds=self.oidc.request_timeout_seconds)
        validate_mode(settings)
        return settings

    def database_url(self, role: str) -> str:
        if role not in _ROLES: raise ValueError('Unknown pilot registry role')
        try:
            query = {'sslmode': 'verify-full', 'sslrootcert': self.registry.ca_file}
            if role != 'owner':
                # Preserve bounded serving-role SQL independently of managed-DB
                # role defaults. Owner migrations have a separate reviewed budget.
                query['options'] = '-c statement_timeout=4000 -c lock_timeout=2000'
            return URL.create('postgresql+psycopg', username='gateway_' + role,
                password=read_secret('/run/secrets/registry_' + role), host=self.registry.host,
                port=self.registry.port, database=self.registry.database,
                query=query).render_as_string(hide_password=False)
        except Exception:
            raise ValueError(_FILE_ERROR) from None

    def load_assets(self):
        try:
            assets = load_synthetic_assets()
            if (assets.manifest.product_id != 'revenue' or assets.manifest.model_bundle_digest != CONSUMER_BUNDLE_DIGEST
                or len(assets.bindings) != 2
                or tuple(binding.model_bundle_digest for binding in assets.bindings) != (CONSUMER_BUNDLE_DIGEST, FIXTURE_BUNDLE_DIGEST)):
                raise ValueError
            records = {}
            for name, digest, url, endpoint_id, context, secret in (
                ('consumer', CONSUMER_BUNDLE_DIGEST, self.cube_consumer_url, 'synthetic-consumer', 'consumer', 'cube_signing_key'),
                ('fixture_test', FIXTURE_BUNDLE_DIGEST, self.cube_fixture_url, 'synthetic-fixture', 'fixture_test', 'cube_test_signing_key')):
                endpoint = assets.bundles.resolve(digest)
                if (endpoint.endpoint_id, endpoint.context, endpoint.secret_ref) != (endpoint_id, context, '/run/secrets/' + secret):
                    raise ValueError
                records[name] = {**endpoint.model_dump(), 'url': url}
            return replace(assets, bundles=BundleCatalog(records))
        except Exception:
            raise ValueError('Reviewed pilot assets are unavailable or invalid') from None

    def validate_files(self, role: str = 'app') -> None:
        if role not in _ROLES: raise ValueError('Unknown pilot registry role')
        required = {
            'app': ('registry_app', 'cube_signing_key', 'cube_test_signing_key', 'audit_hmac', 'cursor_key'),
            'owner': ('registry_owner',),
            'audit_retention': ('registry_app', 'registry_audit_retention'),
        }[role]
        try:
            for name in required: read_secret('/run/secrets/' + name)
            _validate_ca(self.registry.ca_file)
            if role == 'app' and self.credential_hash_file:
                value = CredentialFile.model_validate(_parse_json(_read_file(self.credential_hash_file, FileCredentialStore.MAX_BYTES)))
                if any(record.kind != 'service' for record in value.credentials): raise ValueError
        except Exception:
            raise ValueError(_FILE_ERROR) from None


def load_pilot_config() -> PilotRuntimeConfig:
    """Load only the explicit operator file, without environment setting overlays."""
    try:
        path = os.environ.get('GATEWAY_PILOT_CONFIG_FILE')
        return PilotRuntimeConfig.model_validate(_parse_json(_read_file(path, MAX_CONFIG_BYTES)))
    except Exception:
        raise ValueError(_CONFIG_ERROR) from None
