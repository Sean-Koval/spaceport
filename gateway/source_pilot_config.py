"""Offline configuration for the explicit reviewed-source runtime.

This module parses references, never reads provider credentials or discovers
sources. The complete source catalog must be validated separately by the runtime.
Old demo and synthetic pilot entrypoints deliberately reject this source mode.
"""
import os
from pathlib import PurePosixPath
import re
from sqlalchemy import URL
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from gateway.auth.settings import Settings, validate_mode
from gateway.contracts.canonical import Array, Digest, Identifier, StrictModel
from gateway.pilot_config import PilotOIDCConfig, PilotRegistryConfig, _https_url, _parse_json, _read_file, _validate_ca


class AssetRef(StrictModel):
    path: str = Field(min_length=1,max_length=1024)
    sha256: Digest

    @field_validator('path')
    @classmethod
    def beneath_package_root(cls,value):
        path=PurePosixPath(value)
        if (path.is_absolute() or str(path)!=value or any(part in {'.','..'} for part in path.parts)
            or any(ord(c)<33 or ord(c)==127 for c in value) or any(c in value for c in ('\\',':','?','#','%','~'))):
            raise ValueError('A bounded relative asset path is required')
        return value


class SourceApprovalRef(StrictModel):
    package_id: Identifier
    revision: int = Field(ge=1, le=2**31 - 1)
    approval_id: str
    approval_revision: int = Field(ge=1, le=2**31 - 1)
    approval_digest: Digest

    @field_validator('approval_id')
    @classmethod
    def exact_approval_id(cls, value):
        from uuid import UUID
        if len(value) != 36 or str(UUID(value)) != value:
            raise ValueError('Canonical approval identity required')
        return value

    @field_validator('revision', 'approval_revision', mode='before')
    @classmethod
    def exact_revision(cls, value):
        if type(value) is not int:
            raise ValueError('Integer package revision required')
        return value


DatabaseRole = Literal['app','owner','audit_retention','domo_consumer','domo_fixture']


class RegistryLogin(StrictModel):
    username: str = Field(pattern=r'^[a-z][a-z0-9_]{0,62}$')
    password_file: str = Field(pattern=r'^/run/secrets/[a-z][a-z0-9_]{0,62}$')


class SourceRoleMapping(StrictModel):
    login_name: str = Field(pattern=r'^[a-z][a-z0-9_]{0,62}$')
    source_id: Identifier
    revision: int = Field(ge=1,le=2**31-1)
    context: Literal['consumer','fixture_test']
    role_kind: Literal['runtime','observer']


def _read_password(path):
    try:
        text=_read_file(path,1024).decode('ascii').rstrip('\n')
        if not 1<=len(text)<=1024 or any(ord(c)<33 or ord(c)>126 for c in text):raise ValueError
        return text
    except (ValueError,UnicodeError,OSError):
        raise ValueError('Required registry credential is unavailable') from None


class SourceRuntimeLimits(StrictModel):
    api_replicas: Literal[1] = 1
    worker_replicas: Literal[1] = 1
    shutdown_seconds: int = Field(default=140,ge=140,le=300)

    @field_validator('api_replicas','worker_replicas',mode='before')
    @classmethod
    def exact_integer(cls,value):
        if type(value) is not int:
            raise ValueError('Integer replica count required')
        return value


class SourcePilotConfig(StrictModel):
    schema_version: Literal[1]
    profile: Literal['reviewed-source-pilot']
    public_origin: str
    deployment_epoch: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    database_roles: dict[DatabaseRole,RegistryLogin] = Field(min_length=3,max_length=5,repr=False)
    legacy_source_reader_username: str = Field(default='gateway_source_reader',pattern=r'^[a-z][a-z0-9_]{0,62}$')
    source_role_mappings: Array[SourceRoleMapping] = Field(max_length=100)
    oidc: PilotOIDCConfig
    registry: PilotRegistryConfig
    product_catalog_ref: AssetRef
    source_catalog_ref: AssetRef
    identity_mapping_ref: AssetRef
    source_approval_refs: Array[SourceApprovalRef] = Field(default=(), max_length=25, repr=False)
    probe_product_id: Identifier
    probe_principal_id: Identifier
    credential_hash_file: Literal['/run/auth/credential-hashes.json'] | None = None
    runtime: SourceRuntimeLimits = Field(default_factory=SourceRuntimeLimits)

    @field_validator('schema_version',mode='before')
    @classmethod
    def exact_version(cls,value):
        if type(value) is not int:
            raise ValueError('Integer schema version required')
        return value

    @field_validator('public_origin')
    @classmethod
    def origin(cls,value):
        return _https_url(value,path='origin')

    @model_validator(mode='after')
    def coherent_identity(self):
        if self.oidc.audience != self.mcp_resource_url:
            raise ValueError('The token audience must equal the canonical MCP resource')
        refs = [(ref.package_id, ref.revision) for ref in self.source_approval_refs]
        if len(refs) != len(set(refs)) or len({ref.approval_id for ref in self.source_approval_refs}) != len(refs):
            raise ValueError('Source approval references must be unique')
        roles=self.database_roles
        if (not {'app','owner','audit_retention'}<=set(roles)
            or len({r.username for r in roles.values()})!=len(roles)
            or len({r.password_file for r in roles.values()})!=len(roles)):
            raise ValueError('Independent required registry roles are required')
        if self.legacy_source_reader_username in {r.username for r in roles.values()}:
            raise ValueError('Legacy source reader requires a separate existing login')
        keys=[(r.login_name,r.source_id,r.revision,r.context) for r in self.source_role_mappings]
        partitions={}
        for r in self.source_role_mappings:
            partition=(None if r.role_kind=='observer' else r.context,r.role_kind)
            if r.login_name in partitions and partitions[r.login_name]!=partition:
                raise ValueError('Source logins cannot merge context or role partitions')
            partitions[r.login_name]=partition
        if len(keys)!=len(set(keys)) or any(r.login_name in {roles[k].username for k in ('app','owner','audit_retention')}|{self.legacy_source_reader_username} for r in self.source_role_mappings):
            raise ValueError('Source mappings require distinct source-only logins')
        self.identity_settings()
        return self

    @property
    def mcp_resource_url(self):
        return self.public_origin+'/mcp'

    @property
    def rest_allowed_hosts(self):
        return (urlsplit(self.public_origin).hostname,)

    @property
    def mcp_allowed_hosts(self):
        return (urlsplit(self.public_origin).netloc,)

    @property
    def allowed_origins(self):
        return (self.public_origin,)

    def identity_settings(self):
        settings=Settings(mode='production',source='reviewed_sources',bind_host='0.0.0.0',
            public_ingress=True,trust_forwarded_headers=False,credential_hash_file=self.credential_hash_file,
            oidc_issuer=self.oidc.issuer,oidc_audience=self.oidc.audience,oidc_jwks_url=self.oidc.jwks_url,
            oidc_access_token_profile='rfc9068',oidc_client_ids=self.oidc.client_ids,
            oidc_allowed_algorithms=self.oidc.algorithms,oidc_jwks_cache_seconds=self.oidc.jwks_cache_seconds,
            oidc_request_timeout_seconds=self.oidc.request_timeout_seconds)
        validate_mode(settings)
        return settings


    def database_url(self,role):
        try:
            login=self.database_roles[role]
            query={'sslmode':'verify-full','sslrootcert':self.registry.ca_file}
            if role!='owner':query['options']='-c statement_timeout=4000 -c lock_timeout=2000'
            return URL.create('postgresql+psycopg',username=login.username,password=_read_password(login.password_file),
                host=self.registry.host,port=self.registry.port,database=self.registry.database,
                query=query).render_as_string(hide_password=False)
        except (KeyError,ValueError,TypeError):
            raise ValueError('Required registry configuration is unavailable') from None

    def validate_files(self,*,role):
        # Explicit runtime/bootstrap check only; offline validation never calls it.
        if role not in self.database_roles:raise ValueError('Unknown registry role')
        _validate_ca(self.registry.ca_file)
        self.database_url(role)


def load_source_pilot_config(path=None):
    try:
        path=path if path is not None else os.environ.get('GATEWAY_SOURCE_PILOT_CONFIG_FILE')
        return SourcePilotConfig.model_validate(_parse_json(_read_file(path,65536)))
    except (ValueError,TypeError,OSError,RecursionError):
        raise ValueError('Reviewed-source pilot configuration is unavailable or invalid') from None
