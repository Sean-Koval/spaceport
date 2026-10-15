"""Explicit owner-only provisioning for the OIDC synthetic pilot.

No application import invokes this module. Inputs are operator-reviewed mappings,
never token claims. This module neither generates credentials nor publishes data.
"""
import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text

from gateway.auth.principals import CredentialFile, FileCredentialStore, oidc_subject_key
from gateway.auth.settings import validate_mode
from gateway.contracts.canonical import Array, Identifier, StrictModel, bounded_json, canonical_digest
from gateway.logging import configure_logging
from gateway.pilot_config import _read_file
from gateway.registry.models import Base, BindingRevision, PolicyRevision, PolicyRow, PrincipalGrant, PrincipalRow
from gateway.registry.repository import Registry

MAX_IDENTITY_BYTES = 262_144
MAX_PRINCIPALS = 100
_INVALID_MAPPING = 'The approved identity mapping is unavailable or invalid'
_RESERVED_PREFIXES = ('demo-', 'fixture-', 'pilot-')
_BOOTSTRAP_LOCK = 72140214  # Also serializes with the separate demo bootstrap.
_ROOT = Path(__file__).resolve().parents[1]
_ACTOR = 'pilot-operator'


class ApprovedGrant(StrictModel):
    tenant_id: Literal['north', 'south']
    roles: Array[Literal['consumer', 'author', 'publisher']] = Field(min_length=1, max_length=3)

    @model_validator(mode='after')
    def separated_roles(self):
        if len(set(self.roles)) != len(self.roles) or {'author', 'publisher'} <= set(self.roles):
            raise ValueError('Roles must be distinct and author/publisher must be separate')
        return self


class _Identity(StrictModel):
    id: Identifier
    # The current policy deliberately refuses ambiguous multi-tenant scopes.
    grants: Array[ApprovedGrant] = Field(min_length=1, max_length=1)

    @model_validator(mode='after')
    def reserved_identity(self):
        if self.id.lower().startswith(_RESERVED_PREFIXES):
            raise ValueError('Reserved internal identity')
        return self


class _User(_Identity):
    kind: Literal['user']
    subject: Annotated[str, Field(min_length=1, max_length=512, repr=False)]

    @model_validator(mode='after')
    def subject_characters(self):
        if any(ord(char) < 32 or ord(char) == 127 for char in self.subject):
            raise ValueError('Invalid subject')
        self.subject.encode('utf-8')
        return self


class _Service(_Identity):
    kind: Literal['service']
    service_client_id: Identifier

    @model_validator(mode='after')
    def reserved_client(self):
        if self.service_client_id.lower().startswith(_RESERVED_PREFIXES):
            raise ValueError('Reserved internal service')
        return self


class _MappingFile(StrictModel):
    version: Literal[1]
    principals: Array[Annotated[_User | _Service, Field(discriminator='kind')]] = Field(
        min_length=1, max_length=MAX_PRINCIPALS)

    @model_validator(mode='before')
    @classmethod
    def exact_version(cls, value):
        if not isinstance(value, dict) or type(value.get('version')) is not int:
            raise ValueError('Invalid mapping version')
        return value


class ApprovedPrincipal(_Identity):
    kind: Literal['user', 'service']
    subject: Annotated[str, Field(pattern=r'^(oidc|service)-v1-[a-f0-9]{64}$', repr=False)]
    service_client_id: Identifier | None

    @model_validator(mode='after')
    def distinct_identity_kind(self):
        prefix = 'oidc-v1-' if self.kind == 'user' else 'service-v1-'
        if not self.subject.startswith(prefix) or (self.service_client_id is None) != (self.kind == 'user'):
            raise ValueError('Invalid mapped identity kind')
        return self


class ApprovedIdentityMapping(StrictModel):
    issuer: str = Field(repr=False)
    principals: Array[ApprovedPrincipal] = Field(min_length=1, max_length=MAX_PRINCIPALS)

    @model_validator(mode='after')
    def unique_identities(self):
        for values in ([p.id for p in self.principals], [p.subject for p in self.principals],
                       [p.service_client_id for p in self.principals if p.kind == 'service']):
            if len(values) != len(set(values)):
                raise ValueError('Ambiguous mapped identities')
        return self

    @property
    def digest(self):
        identities = []
        for principal in sorted(self.principals, key=lambda row: row.id):
            value = principal.model_dump(mode='json')
            value['grants'] = [{'tenant_id': grant.tenant_id, 'roles': sorted(grant.roles)}
                               for grant in sorted(principal.grants, key=lambda grant: grant.tenant_id)]
            identities.append(value)
        return canonical_digest({'version': 1, 'issuer': self.issuer, 'principals': identities})


def _unique_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Ambiguous JSON')
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError('Nonfinite JSON')


def _read_mapping(path):
    # Share the pilot's no-follow-every-component, bounded snapshot reader.
    data = _read_file(str(path), MAX_IDENTITY_BYTES)
    value = json.loads(data, object_pairs_hook=_unique_members, parse_constant=_reject_constant)
    return _MappingFile.model_validate(bounded_json(value, max_depth=8, max_nodes=5000, max_string=512))


def _validate_support(mapping, config):
    settings = config.identity_settings()
    validate_mode(settings)
    if settings.mode != 'production' or settings.source != 'synthetic_postgres' or settings.oidc_issuer != mapping.issuer:
        raise ValueError('The mapping requires the configured pilot issuer')
    probe = next((p for p in mapping.principals if p.id == config.probe_principal_id), None)
    if probe is None or len(probe.grants) != 1 or 'consumer' not in probe.grants[0].roles:
        raise ValueError('The probe requires an approved consumer')
    roles = {role for principal in mapping.principals for grant in principal.grants for role in grant.roles}
    if not {'author', 'publisher'} <= roles:
        raise ValueError('The initial mapping requires separate author and publisher principals')
    services = {p.id for p in mapping.principals if p.kind == 'service'}
    if services and not settings.credential_hash_file:
        raise ValueError('Services require an existing hash-only credential file')
    if settings.credential_hash_file:
        data = _read_file(settings.credential_hash_file, FileCredentialStore.MAX_BYTES)
        credentials = CredentialFile.model_validate(json.loads(data, object_pairs_hook=_unique_members,
                                                               parse_constant=_reject_constant))
        if (any(record.kind != 'service' or record.principal_id not in services for record in credentials.credentials)
            or {record.principal_id for record in credentials.credentials} != services):
            raise ValueError('Credential mappings must name only approved services')
        # Deliberately do not require active hashes: revocations survive reruns.


def load_identity_mapping(path, config) -> ApprovedIdentityMapping:
    """Read/validate a bounded approved mapping without opening any connection."""
    try:
        raw = _read_mapping(path)
        settings = config.identity_settings()
        principals = []
        for row in raw.principals:
            service = row.kind == 'service'
            subject = ('service-v1-' + canonical_digest({'service_client_id': row.service_client_id})
                       if service else oidc_subject_key(settings.oidc_issuer, row.subject))
            principals.append(ApprovedPrincipal(id=row.id, kind=row.kind, subject=subject,
                service_client_id=row.service_client_id if service else None, grants=row.grants))
        mapping = ApprovedIdentityMapping(issuer=settings.oidc_issuer, principals=principals)
        _validate_support(mapping, config)
        return mapping
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError(_INVALID_MAPPING) from None


def _fixture_principals():
    return tuple(PrincipalRow(id='fixture-' + name, subject='synthetic-fixture-' + name,
                              kind='demo', service_client_id=None)
                 for name in ('north', 'south', 'denied'))


def _binding_id(binding):
    return 'pilot-binding-v1-' + binding.digest


async def _verify_existing(session, assets, mapping, expected_principals):
    revision = await session.scalar(select(PolicyRevision).where(
        PolicyRevision.policy_id == assets.policy_id, PolicyRevision.revision == assets.policy_revision))
    if (revision is None or revision.id != 'pilot-policy-v1-' + mapping.digest
        or revision.content_digest != canonical_digest(assets.policy_content)
        or canonical_digest(revision.content) != revision.content_digest):
        raise ValueError('Existing registry differs from the reviewed bootstrap')
    for binding in assets.bindings:
        row = await session.scalar(select(BindingRevision).where(
            BindingRevision.binding_id == binding.binding_id, BindingRevision.revision == binding.revision))
        if (row is None or row.id != _binding_id(binding) or row.content_digest != binding.digest
            or canonical_digest(row.content) != binding.digest
            or row.model_bundle_digest != binding.model_bundle_digest
            or row.adapter_revision != binding.adapter_revision):
            raise ValueError('Existing binding differs from the reviewed bootstrap')
    for principal in expected_principals:
        row = await session.get(PrincipalRow, principal.id, with_for_update=True)
        if (row is None or (row.kind, row.subject, row.service_client_id)
            != (principal.kind, principal.subject, principal.service_client_id)):
            raise ValueError('Existing identity differs from the reviewed bootstrap')
    # Active flags, current policy heads, grants and additional operator-managed
    # principals deliberately are not reset or compared with their initial state.


async def provision(owner: Registry, config, mapping: ApprovedIdentityMapping):
    """Atomically install an empty registry or verify the immutable original seed."""
    mapping = ApprovedIdentityMapping.model_validate(mapping.model_dump(mode='python'))
    _validate_support(mapping, config)
    assets = config.load_assets()
    principals = [PrincipalRow(id=row.id, kind=row.kind, subject=row.subject,
                              service_client_id=row.service_client_id) for row in mapping.principals]
    principals.extend(_fixture_principals())
    async with owner.transaction() as uow:
        session = uow.session
        is_owner = await session.scalar(text(
            "SELECT current_user = pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'gateway'"))
        if is_owner is not True:
            raise ValueError('Provisioning requires the registry schema owner')
        await session.execute(text(f'SELECT pg_advisory_xact_lock({_BOOTSTRAP_LOCK})'))
        head = await session.get(PolicyRow, assets.policy_id, with_for_update=True)
        if head is not None:
            await _verify_existing(session, assets, mapping, principals)
            return
        # Migration creates the deployment capacity lock; no other persisted
        # application state is accepted as an empty registry.
        for table in Base.metadata.tables.values():
            statement = select(table).limit(1)
            if table.name == 'capacity_locks':
                statement = statement.where(table.c.scope_id != 'deployment')
            if await session.scalar(statement) is not None:
                raise ValueError('Bootstrap requires an empty registry or its exact reviewed bootstrap')
        session.add(PolicyRow(id=assets.policy_id, current_revision=assets.policy_revision))
        await session.flush()
        # The immutable row ID binds the original mapping, including its issuer
        # and initial grants, without confusing those grants with mutable access.
        session.add(PolicyRevision(id='pilot-policy-v1-' + mapping.digest, policy_id=assets.policy_id,
            revision=assets.policy_revision, content=assets.policy_content,
            content_digest=canonical_digest(assets.policy_content), actor_id=_ACTOR))
        for binding in assets.bindings:
            session.add(BindingRevision(id=_binding_id(binding), binding_id=binding.binding_id,
                revision=binding.revision, content=binding.model_dump(mode='json'), content_digest=binding.digest,
                model_bundle_digest=binding.model_bundle_digest, adapter_revision=binding.adapter_revision,
                actor_id=_ACTOR))
        for principal in principals:
            session.add(principal)
        await session.flush()
        for principal in mapping.principals:
            for grant in principal.grants:
                session.add(PrincipalGrant(principal_id=principal.id, policy_id=assets.policy_id,
                    tenant_id=grant.tenant_id, roles=list(grant.roles), entitlements={'groups': [grant.tenant_id]}))
        for tenant in ('north', 'south'):
            session.add(PrincipalGrant(principal_id='fixture-' + tenant, policy_id=assets.policy_id,
                tenant_id=tenant, roles=['consumer'], entitlements={'groups': [tenant]}))


def migrate(config):
    """Apply reviewed migrations with the configured owner and verified TLS URL."""
    config.validate_files(role='owner')
    alembic = Config(str(_ROOT / 'alembic.ini'))
    alembic.set_main_option('script_location', str(_ROOT / 'migrations'))
    # Prevent migration environment overrides from changing the fixed pilot roles.
    alembic.attributes['registry_roles'] = {
        role: 'gateway_' + role for role in ('owner', 'app', 'audit_retention', 'source_reader')}
    engine = create_engine(config.database_url('owner'), echo=False, hide_parameters=True,
                           connect_args={'connect_timeout': 5})
    try:
        with engine.begin() as connection:
            connection.execute(text(f'SELECT pg_advisory_xact_lock({_BOOTSTRAP_LOCK})'))
            alembic.attributes['connection'] = connection
            command.upgrade(alembic, 'head')
    finally:
        engine.dispose()


async def run_provision(config, mapping):
    config.validate_files(role='owner')
    _validate_support(mapping, config)
    owner = Registry(config.database_url('owner'))
    try:
        await provision(owner, config, mapping)
    finally:
        await owner.close()


def load_pilot_config():
    # Lazy import keeps module import inert, including in independent tooling.
    from gateway.pilot_config import load_pilot_config as load
    return load()


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse ordinarily echoes invalid arguments, which might be secrets.
        raise ValueError('Invalid operator command')


def main(argv=None):
    configure_logging()
    try:
        parser = _Parser(description='Owner-only OIDC synthetic pilot provisioning')
        commands = parser.add_subparsers(dest='operation', required=True)
        commands.add_parser('migrate', help='Apply reviewed registry migrations')
        provisioning = commands.add_parser('provision', help='Import an explicitly approved identity mapping')
        provisioning.add_argument('--identities', required=True)
        args = parser.parse_args(argv)
        config = load_pilot_config()
        if args.operation == 'migrate':
            migrate(config)
        else:
            mapping = load_identity_mapping(args.identities, config)
            asyncio.run(run_provision(config, mapping))
    except Exception:
        logging.error('', extra={'event': 'operation_failed'})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
