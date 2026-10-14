"""Fresh server-owned principal grants and an external hash-only credential store."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Literal
from uuid import uuid4

from pydantic import Field, ValidationError, model_validator
from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.contracts.canonical import Array, Digest, Identifier, Scalar, StrictModel, bounded_json
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.identity import Principal
from gateway.registry.models import PrincipalGrant, PrincipalRow
from .settings import trusted_https_url

Capability = Literal["discover", "describe", "execute", "result", "author", "publish", "policy_admin", "operate", "audit_view"]
ROLE_CAPABILITIES = {
    "consumer": ("discover", "describe", "execute", "result"),
    "author": ("author",), "publisher": ("publish",), "operator": ("operate",),
    "policy_admin": ("policy_admin",), "audit_view": ("audit_view",),
}


def unauthenticated() -> GatewayError:
    return GatewayError(ErrorCode.UNAUTHENTICATED, str(uuid4()), "A valid identity is required.")


def unavailable_grants() -> GatewayError:
    return GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE, str(uuid4()), "Current access permissions are unavailable.")


def capabilities_for_roles(roles: list | tuple) -> tuple[Capability, ...]:
    if (not isinstance(roles, (list, tuple)) or len(roles) > len(ROLE_CAPABILITIES)
        or any(type(role) is not str or role not in ROLE_CAPABILITIES for role in roles)
        or len(set(roles)) != len(roles)):
        raise ValueError("Unrecognized server-managed role state")
    return tuple(sorted({capability for role in roles for capability in ROLE_CAPABILITIES[role]}))


class PolicyGrant(StrictModel):
    """One policy/tenant scope. Never union grants from different scopes."""
    policy_id: Identifier
    tenant_id: Identifier
    capabilities: Array[Capability] = Field(max_length=9)
    entitlements: dict[Identifier, Array[Scalar]] = Field(max_length=50)

    @model_validator(mode="after")
    def bounded_entitlements(self):
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("Capabilities must be distinct")
        for values in self.entitlements.values():
            if not values or len(values) > 100 or any(value is None for value in values):
                raise ValueError("Entitlement lists must be bounded nonnull scalar values")
        bounded_json(self.entitlements)
        return self


class PrincipalGrants(StrictModel):
    """Internal only; exact tenant/policy boundaries survive the database read."""
    principal_id: Identifier
    grants: Array[PolicyGrant] = Field(max_length=1000)

    @model_validator(mode="after")
    def distinct_scopes(self):
        scopes = [(grant.policy_id, grant.tenant_id) for grant in self.grants]
        if len(scopes) != len(set(scopes)):
            raise ValueError("Duplicate principal grant scopes")
        return self


def oidc_subject_key(issuer: str, subject: str) -> str:
    """Provisioning/lookup key binds a raw verified subject to exactly one issuer.

    The current registry has one subject column. This domain-separated key avoids
    issuer changes aliasing users without putting an unbounded compound value in
    that column. Returned Principal.subject is still the verified raw subject.
    """
    if not trusted_https_url(issuer) or type(subject) is not str or not 1 <= len(subject) <= 512:
        raise ValueError("Invalid issuer or subject")
    value = json.dumps([issuer, subject], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "oidc-v1-" + hashlib.sha256(b"dpg-oidc-subject-v1\0" + value).hexdigest()


class PrincipalStore:
    """No identity/grant cache. Session factory must use the read-only app role."""
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    @asynccontextmanager
    async def _session(self, session: AsyncSession | None):
        if session is not None:
            yield session
        else:
            async with self.sessions() as own:
                yield own

    async def _resolve(self, field, value: str) -> Principal:
        try:
            async with self.sessions() as session:
                row = await session.scalar(select(PrincipalRow).where(field == value, PrincipalRow.active.is_(True)))
                if row is None:
                    raise unauthenticated()
                return Principal(id=row.id, subject=row.subject, service_client_id=row.service_client_id, kind=row.kind)
        except (SQLAlchemyError, ValidationError, OSError):
            raise unauthenticated() from None

    async def resolve_subject(self, subject: str, *, issuer: str) -> Principal:
        try:
            principal = await self._resolve(PrincipalRow.subject, oidc_subject_key(issuer, subject))
            if principal.kind != "user":
                raise unauthenticated()
            return Principal(id=principal.id, subject=subject, service_client_id=principal.service_client_id, kind="user")
        except ValueError:
            raise unauthenticated() from None

    async def resolve_principal(self, principal_id: str) -> Principal:
        return await self._resolve(PrincipalRow.id, principal_id)

    async def get_current(self, principal_id: str, *, session: AsyncSession | None = None) -> PrincipalGrants:
        try:
            async with self._session(session) as current:
                statement = select(PrincipalRow.id, PrincipalGrant).outerjoin(PrincipalGrant, and_(
                    PrincipalGrant.principal_id == PrincipalRow.id, PrincipalGrant.active.is_(True))).where(
                    PrincipalRow.id == principal_id, PrincipalRow.active.is_(True)).order_by(
                    PrincipalGrant.policy_id, PrincipalGrant.tenant_id).limit(1001).execution_options(populate_existing=True)
                rows = (await current.execute(statement)).all()
                if not rows:
                    raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN, str(uuid4()), "The requested access is unavailable.")
                return PrincipalGrants(principal_id=principal_id, grants=[
                    PolicyGrant(policy_id=grant.policy_id, tenant_id=grant.tenant_id,
                        capabilities=capabilities_for_roles(grant.roles), entitlements=grant.entitlements)
                    for _, grant in rows if grant is not None])
        except (SQLAlchemyError, ValidationError, ValueError, OSError):
            raise unavailable_grants() from None


class CredentialHash(StrictModel):
    digest: Digest
    principal_id: Identifier
    kind: Literal["service", "demo"]
    active: bool


class CredentialFile(StrictModel):
    version: Literal[1]
    credentials: Array[CredentialHash] = Field(max_length=1000)

    @model_validator(mode="before")
    @classmethod
    def exact_version(cls, value):
        if not isinstance(value, dict) or type(value.get("version")) is not int:
            raise ValueError("Credential version must be an integer")
        return value

    @model_validator(mode="after")
    def unique_digests(self):
        if type(self.version) is not int or len({credential.digest for credential in self.credentials}) != len(self.credentials):
            raise ValueError("Invalid credential file version or duplicate hashes")
        return self


def credential_digest(token: str) -> str:
    """SHA-256 is appropriate only for operator-generated high-entropy tokens."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class FileCredentialStore:
    """Operator-owned, atomically replaced file; reopens on every lookup.

    Mount read-only into the application. There is no write/enrolment API and no
    plaintext credential field. The selected path is never received from clients.
    """
    MAX_BYTES = 1_048_576

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise ValueError("The credential file must have an absolute operator-selected path")

    def _read(self) -> CredentialFile:
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o022 or before.st_size > self.MAX_BYTES:
                raise ValueError("Unsafe credential storage")
            data = bytearray()
            while len(data) <= self.MAX_BYTES:
                part = os.read(descriptor, min(65536, self.MAX_BYTES + 1 - len(data)))
                if not part:
                    break
                data.extend(part)
            after = os.fstat(descriptor)
            if len(data) > self.MAX_BYTES or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("Credential storage changed during read")
            def unique_members(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Ambiguous credential storage")
                    result[key] = value
                return result
            value = json.loads(data, object_pairs_hook=unique_members)
            return CredentialFile.model_validate(value)
        finally:
            os.close(descriptor)

    async def get_by_digest(self, digest: str) -> CredentialHash:
        try:
            state = await asyncio.to_thread(self._read)
            for credential in state.credentials:
                if credential.digest == digest and credential.active:
                    return credential
        except (OSError, ValueError, ValidationError):
            pass
        raise unauthenticated() from None
