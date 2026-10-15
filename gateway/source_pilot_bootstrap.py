"""Explicit owner bootstrap for reviewed sources, identities and policy seeds.

Parsing and preflight are offline. Provisioning is an explicit operator action;
this module creates neither database roles nor provider credentials/grants.
"""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from gateway.auth.principals import oidc_subject_key
from gateway.contracts.canonical import Array, Identifier, StrictModel, canonical_digest
from gateway.policy.service import PolicyContent
from gateway.product_packages import VersionOne


class ApprovedGrant(StrictModel):
    policy_id: Identifier
    tenant_id: Identifier
    roles: Array[Literal['consumer','author','publisher']] = Field(min_length=1,max_length=3)
    entitlements: dict[Identifier,Array[Identifier]] = Field(max_length=10)

    @model_validator(mode='after')
    def exact_scope(self):
        if (len(set(self.roles))!=len(self.roles) or {'author','publisher'}<=set(self.roles)
            or any(not 1<=len(values)<=100 or len(set(values))!=len(values) for values in self.entitlements.values())):
            raise ValueError('Independent bounded roles and entitlements required')
        return self


class ApprovedIdentity(StrictModel):
    id: Identifier
    grants: Array[ApprovedGrant] = Field(min_length=1,max_length=25)

    @model_validator(mode='after')
    def distinct_scope(self):
        if (self.id.lower().startswith(('fixture-','demo-','source-internal-'))
            or len({g.policy_id for g in self.grants})!=len(self.grants)):
            raise ValueError('External identities require one unambiguous scope per policy')
        return self


class UserIdentity(ApprovedIdentity):
    kind: Literal['user']
    subject: str = Field(min_length=1,max_length=512,repr=False)

    @model_validator(mode='after')
    def bounded_subject(self):
        if any(ord(c)<32 or ord(c)==127 for c in self.subject):raise ValueError('Invalid subject')
        return self


class ServiceIdentity(ApprovedIdentity):
    kind: Literal['service']
    service_client_id: Identifier


class IdentityMapping(VersionOne):
    principals: Array[Annotated[UserIdentity|ServiceIdentity,Field(discriminator='kind')]] = Field(min_length=3,max_length=100,repr=False)

    @model_validator(mode='after')
    def distinct(self):
        ids=[p.id for p in self.principals]
        external=[(p.kind,p.subject if p.kind=='user' else p.service_client_id) for p in self.principals]
        if len(ids)!=len(set(ids)) or len(external)!=len(set(external)):
            raise ValueError('Ambiguous external identities')
        return self

    @property
    def digest(self):return canonical_digest(self)

    def principal_rows(self,issuer):
        from gateway.registry.models import PrincipalRow
        return tuple(PrincipalRow(id=p.id,kind=p.kind,
            subject=oidc_subject_key(issuer,p.subject) if p.kind=='user' else 'service-v1-'+canonical_digest({'service_client_id':p.service_client_id}),
            service_client_id=None if p.kind=='user' else p.service_client_id) for p in self.principals)


class FixturePrincipal(StrictModel):
    id: Identifier
    tenant_id: Identifier
    roles: Array[Literal['consumer']] = Field(max_length=1)
    entitlements: dict[Identifier,Array[Identifier]] = Field(max_length=10)

    @model_validator(mode='after')
    def internal_scope(self):
        if (not self.id.startswith('fixture-') or any(not 1<=len(v)<=100 or len(set(v))!=len(v) for v in self.entitlements.values())):
            raise ValueError('Bounded internal fixture principal required')
        return self


class PolicySeed(StrictModel):
    policy_id: Identifier
    revision: int = Field(ge=1)
    content: PolicyContent
    fixture_principals: Array[FixturePrincipal] = Field(min_length=2,max_length=100)

    @model_validator(mode='after')
    def coherent(self):
        if len({p.id for p in self.fixture_principals})!=len(self.fixture_principals):
            raise ValueError('Invalid fixture policy scope')
        return self


def validate_identity_mapping(mapping,config,packages):
    """Check operator intent against exact retained packages, without I/O."""
    mapping=IdentityMapping.model_validate(mapping.model_dump(mode='python'))
    policies={p.package.manifest.policy_id for p in packages}
    retained_seeds={}
    for retained in packages:
        seed=PolicySeed.model_validate(retained.assets['policy_seed_ref'])
        digest=canonical_digest(seed)
        if seed.policy_id in retained_seeds and retained_seeds[seed.policy_id]!=digest:
            raise ValueError('Retained versions require one exact seed for each policy')
        retained_seeds[seed.policy_id]=digest
    grants={policy:[] for policy in policies}
    for principal in mapping.principals:
        for grant in principal.grants:
            if grant.policy_id not in grants:raise ValueError('Unknown policy grant')
            grants[grant.policy_id].append((principal,grant))
    for retained in packages:
        seed=PolicySeed.model_validate(retained.assets['policy_seed_ref'])
        if seed.policy_id!=retained.package.manifest.policy_id:raise ValueError('Wrong policy seed')
        scopes=grants[seed.policy_id]
        if not {'author','publisher'}<={r for _,g in scopes for r in g.roles}:raise ValueError('Separate author and publisher required')
        if any(g.tenant_id not in seed.content.rules.tenant_ids for _,g in scopes):raise ValueError('Unknown policy tenant')
    probe=next((p for p in mapping.principals if p.id==config.probe_principal_id),None)
    product=next((p.package for p in packages if p.package.manifest.product_id==config.probe_product_id),None)
    if probe is None or product is None or not any(g.policy_id==product.manifest.policy_id and 'consumer' in g.roles for g in probe.grants):
        raise ValueError('Readiness requires the configured mapped consumer and product')
    if any(p.kind=='service' for p in mapping.principals) and not config.credential_hash_file:
        raise ValueError('Mapped services require an existing hash-only credential reference')
    return mapping


def seed_identity(seed,mapping,issuer):
    identities=[]
    rows={p.id:p for p in mapping.principal_rows(issuer)}
    for principal in mapping.principals:
        for grant in principal.grants:
            if grant.policy_id==seed.policy_id:
                row=rows[principal.id]
                identities.append(dict(id=row.id,kind=row.kind,subject=row.subject,
                    service_client_id=row.service_client_id,grant=grant.model_dump(mode='json')))
    return 'source-seed-v1-'+canonical_digest({'seed':seed.model_dump(mode='json'),
        'issuer':issuer,'identities':sorted(identities,key=lambda row:row['id'])})


def verify_principal(actual,expected):
    # Active status/grants may have been revoked. A rerun never restores them.
    if any(getattr(actual,k)!=getattr(expected,k) for k in ('id','kind','subject','service_client_id')):
        raise ValueError('Existing identity differs from the approved source seed')




def bootstrap_plan_digest(config,assets):
    return canonical_digest({'config':config,'packages':[p.package.digest for p in assets.packages.packages]})


def validate_service_credentials(mapping,config):
    """Explicit startup/bootstrap check of existing hash-only service mappings."""
    from gateway.auth.principals import CredentialFile,FileCredentialStore
    from gateway.pilot_config import _read_file,_parse_json
    services={p.id for p in mapping.principals if p.kind=='service'}
    if services and not config.credential_hash_file:raise ValueError('Mapped services require existing credential hashes')
    if config.credential_hash_file:
        value=CredentialFile.model_validate(_parse_json(_read_file(config.credential_hash_file,FileCredentialStore.MAX_BYTES)))
        if (any(r.kind!='service' or r.principal_id not in services for r in value.credentials)
            or {r.principal_id for r in value.credentials}!=services):
            raise ValueError('Credential mappings must name only approved services')
        # Inactive hashes intentionally remain inactive across bootstrap reruns.


async def provision(owner,config,assets):
    """Atomically append exact immutable seeds; never publish or restore grants."""
    from sqlalchemy import select,text
    from gateway.registry.models import BindingRevision,PolicyRevision,PolicyRow,PrincipalGrant,PrincipalRow
    from gateway.source_lifecycle import provision_sources
    mapping=validate_identity_mapping(assets.identities,config,assets.packages.packages)
    expected={p.id:p for p in mapping.principal_rows(config.oidc.issuer)}
    seeds={}
    for retained in assets.packages.packages:
        seed=PolicySeed.model_validate(retained.assets['policy_seed_ref'])
        key=(seed.policy_id,seed.revision)
        if key in seeds and canonical_digest(seeds[key])!=canonical_digest(seed):raise ValueError('Conflicting policy seed')
        seeds[key]=seed
        for p in seed.fixture_principals:
            row=PrincipalRow(id=p.id,kind='demo',subject='source-fixture-v1-'+canonical_digest({'id':p.id}),service_client_id=None)
            if p.id in expected:verify_principal(expected[p.id],row)
            expected[p.id]=row
    async with owner.transaction() as uow:
        session=uow.session
        if await session.scalar(text("SELECT current_user = pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'gateway'")) is not True:
            raise ValueError('Source bootstrap requires the schema owner')
        await session.execute(text('SELECT pg_advisory_xact_lock(72140214)'))
        from gateway.execution.queue import lock_execution_capacity
        await lock_execution_capacity(uow)
        installed={}
        required_existing=set()
        for key,seed in seeds.items():
            head=await session.get(PolicyRow,seed.policy_id,with_for_update=True)
            row=await session.scalar(select(PolicyRevision).where(PolicyRevision.policy_id==seed.policy_id,
                PolicyRevision.revision==seed.revision))
            identity=seed_identity(seed,mapping,config.oidc.issuer)
            digest=canonical_digest(seed.content)
            if row is not None:
                if row.id!=identity or row.content_digest!=digest or canonical_digest(row.content)!=digest:
                    raise ValueError('Existing policy differs from the approved source seed')
                required_existing.update(p.id for p in seed.fixture_principals)
                required_existing.update(p.id for p in mapping.principals if any(g.policy_id==seed.policy_id for g in p.grants))
            elif head is not None:
                # Bootstrap never rewinds a policy head or repairs missing seeds.
                raise ValueError('Existing policy needs explicit policy revision management')
            installed[key]=row is not None
        for expected_row in expected.values():
            actual=await session.get(PrincipalRow,expected_row.id,with_for_update=True)
            if actual is None:
                if expected_row.id in required_existing:
                    raise ValueError('An installed seed identity requires explicit identity management')
                session.add(expected_row)
            else:verify_principal(actual,expected_row)
        await session.flush()
        for key,seed in seeds.items():
            if installed[key]:continue
            identity=seed_identity(seed,mapping,config.oidc.issuer)
            digest=canonical_digest(seed.content)
            session.add(PolicyRow(id=seed.policy_id,current_revision=seed.revision))
            await session.flush()
            session.add(PolicyRevision(id=identity,policy_id=seed.policy_id,revision=seed.revision,
                content=seed.content.model_dump(mode='json'),content_digest=digest,actor_id='source-pilot-operator'))
            for principal in mapping.principals:
                for grant in principal.grants:
                    if grant.policy_id==seed.policy_id:
                        session.add(PrincipalGrant(principal_id=principal.id,policy_id=seed.policy_id,tenant_id=grant.tenant_id,
                            roles=list(grant.roles),entitlements={k:list(v) for k,v in grant.entitlements.items()}))
            for p in seed.fixture_principals:
                session.add(PrincipalGrant(principal_id=p.id,policy_id=seed.policy_id,tenant_id=p.tenant_id,
                    roles=list(p.roles),entitlements={k:list(v) for k,v in p.entitlements.items()}))
        for binding in assets.bindings:
            actual=await session.scalar(select(BindingRevision).where(BindingRevision.binding_id==binding.binding_id,
                BindingRevision.revision==binding.revision))
            if actual is not None:
                if (actual.content_digest!=binding.digest or canonical_digest(actual.content)!=binding.digest
                    or actual.model_bundle_digest!=binding.model_bundle_digest or actual.adapter_revision!=binding.adapter_revision):
                    raise ValueError('Existing binding differs from the approved source seed')
            else:
                session.add(BindingRevision(id='source-binding-v1-'+binding.digest,binding_id=binding.binding_id,
                    revision=binding.revision,content=binding.model_dump(mode='json'),content_digest=binding.digest,
                    model_bundle_digest=binding.model_bundle_digest,adapter_revision=binding.adapter_revision,actor_id='source-pilot-operator'))
        await session.flush()
        connection=await session.connection()
        await connection.run_sync(lambda sync:provision_sources(sync,list(assets.sources.sources),
            [r.model_dump(mode='json') for r in config.source_role_mappings],deployment_epoch=config.deployment_epoch))


def migrate(config):
    """Legacy roles must exist; source-specific roles are checked at provision."""
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine,text
    config.validate_files(role='owner')
    root=Path(__file__).resolve().parents[1]
    alembic=Config(str(root/'alembic.ini'));alembic.set_main_option('script_location',str(root/'migrations'))
    alembic.attributes['registry_roles']={role:config.database_roles[role].username for role in ('owner','app','audit_retention')}
    alembic.attributes['registry_roles']['source_reader']=config.legacy_source_reader_username
    engine=create_engine(config.database_url('owner'),echo=False,hide_parameters=True,connect_args={'connect_timeout':5})
    try:
        with engine.begin() as connection:
            connection.execute(text('SELECT pg_advisory_xact_lock(72140214)'))
            alembic.attributes['connection']=connection
            command.upgrade(alembic,'head')
    finally:engine.dispose()


async def run_provision(config,assets):
    from gateway.registry.repository import Registry
    validate_service_credentials(assets.identities,config)
    config.validate_files(role='owner')
    owner=Registry(config.database_url('owner'))
    try:await provision(owner,config,assets)
    finally:await owner.close()


def main(argv=None):
    import asyncio,json,logging,os
    from pathlib import Path
    from scripts.spaceport import Parser
    from gateway.logging import configure_logging
    from gateway.source_pilot_config import load_source_pilot_config
    from gateway.source_pilot_assets import load_source_assets
    configure_logging()
    try:
        parser=Parser(description='Explicit reviewed-source owner bootstrap')
        commands=parser.add_subparsers(dest='operation',required=True)
        commands.add_parser('migrate');commands.add_parser('plan')
        command=commands.add_parser('provision');command.add_argument('--expected-plan-digest',required=True)
        args=parser.parse_args(argv)
        path=os.environ.get('GATEWAY_SOURCE_PILOT_CONFIG_FILE');config=load_source_pilot_config(path)
        if args.operation=='migrate':migrate(config)
        else:
            assets=load_source_assets(config,Path(path).parent)
            digest=bootstrap_plan_digest(config,assets)
            if args.operation=='plan':
                print(json.dumps({'plan_digest':digest,'source_observation':'unverified','package_count':len(assets.packages.packages)},sort_keys=True))
            elif args.expected_plan_digest!=digest:raise ValueError('Reviewed bootstrap plan changed')
            else:asyncio.run(run_provision(config,assets))
    except Exception:
        logging.error('',extra={'event':'operation_failed'});return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
