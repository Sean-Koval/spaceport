"""Reusable synthetic Domo setup against the guarded disposable PostgreSQL only.

The caller injects HTTPX provider responses. No credential files, OAuth endpoints,
cloud datasets, or grants outside the fixture's random loopback database exist.
"""
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import secrets


@dataclass(frozen=True)
class DomoSeed:
    config: object
    assets: object
    registry: object
    policy: object
    principals: dict
    bindings: object
    lifecycles: dict
    provider_registries: dict
    credentials: dict

    def secret_resolver(self,context):
        allowed={ref:value for (kind,ref),value in self.credentials.items() if kind==context}
        def resolve(ref):
            if ref not in allowed:raise ValueError('Synthetic source credential context differs')
            return allowed[ref]
        return resolve


@asynccontextmanager
async def seed_domo_pilot(database,root):
    import psycopg
    from psycopg import sql
    from tests.integration.conftest import connection_kwargs,validate_test_admin_url,AUTHORIZATION
    from gateway.adapters.domo_runtime import DomoSource
    from gateway.adapters.base import StaticBindingResolver
    from gateway.auth.principals import PrincipalStore
    from gateway.policy.audit import AuditWriter
    from gateway.policy.service import PolicyService
    from gateway.registry.repository import Registry
    from gateway.source_lifecycle import SourceLifecycle
    from gateway.source_pilot_builder import build_weekly_files,encoded
    from gateway.source_pilot_config import SourcePilotConfig
    from gateway.source_pilot_assets import load_source_assets
    from gateway.source_pilot_bootstrap import provision
    # The preexisting fixture must already have validated explicit authorization.
    validate_test_admin_url(database.admin_url.render_as_string(hide_password=False),AUTHORIZATION)
    created=[];opened=[];urls={};credentials={}
    try:
        with psycopg.connect(**connection_kwargs(database.admin_url),autocommit=True) as admin:
            for context in ('consumer','fixture_test'):
                name='gwd_'+secrets.token_hex(8);password=secrets.token_urlsafe(32)
                admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
                created.append(name)
                admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(database.urls['owner'].database),sql.Identifier(name)))
                urls[context]=database.urls['app'].set(username=name,password=password)
        def source(context):
            client='synthetic-'+context+'-client'
            id_ref='/run/secrets/domo_'+context+'_id';secret_ref='/run/secrets/domo_'+context+'_secret'
            credentials[(context,id_ref)]=client;credentials[(context,secret_ref)]='synthetic-'+context+'-secret'
            return DomoSource(source_id='usage-'+context.replace('_','-'),revision=1,context=context,
                auth={'mode':'oauth-client-credentials','client_id_ref':id_ref,'client_secret_ref':secret_ref,
                    'client_id_sha256':hashlib.sha256(client.encode()).hexdigest(),'expected_customer':'synthetic-customer',
                    'expected_user_id':1 if context=='consumer' else 2},
                dataset_ids=['11111111-1111-1111-1111-111111111111' if context=='consumer' else '22222222-2222-2222-2222-222222222222'],
                source_policy_digest='c'*64,max_dispatches=20,max_active_sends=2)
        files=build_weekly_files(consumer_source=source('consumer'),fixture_source=source('fixture_test'),
            consumer_definition_digest='d'*64,fixture_definition_digest='e'*64)
        raw=files['pilot.json']
        for role in ('app','owner','audit_retention'):raw['database_roles'][role]['username']=database.roles[role]
        raw['legacy_source_reader_username']=database.roles['source_reader']
        for role,context in (('domo_consumer','consumer'),('domo_fixture','fixture_test')):
            raw['database_roles'][role]['username']=urls[context].username
        for row in raw['source_role_mappings']:row['login_name']=urls[row['context']].username
        config=SourcePilotConfig.model_validate(raw)
        root.mkdir(mode=0o700,parents=True,exist_ok=True)
        for name,value in files.items():
            path=root/name
            with path.open('xb') as stream:stream.write(encoded(value))
            path.chmod(0o600)
        assets=load_source_assets(config,root)
        owner=Registry(database.urls['owner'].render_as_string(hide_password=False));opened.append(owner)
        await provision(owner,config,assets)
        registry=Registry(database.urls['app'].render_as_string(hide_password=False));opened.append(registry)
        principal_store=PrincipalStore(registry.sessions)
        policy=PolicyService(registry,principal_store,AuditWriter(b'synthetic-domo-seed-audit-key-32-bytes'))
        provider_registries={}
        lifecycles={}
        for context,url in urls.items():
            provider=Registry(url.render_as_string(hide_password=False));opened.append(provider)
            provider_registries[context]=provider
            lifecycles[context]=SourceLifecycle(registry,policy,assets.sources,deployment_epoch=config.deployment_epoch,
                provider_registries={('domo',context):provider})
        principals={row.id:await principal_store.resolve_principal(row.id) for row in assets.identities.principals}
        yield DomoSeed(config,assets,registry,policy,principals,StaticBindingResolver(list(assets.bindings)),lifecycles,provider_registries,credentials)
    finally:
        for registry in reversed(opened):await registry.close()
        with psycopg.connect(**connection_kwargs(database.admin_url.set(database=database.urls['owner'].database)),autocommit=True) as admin:
            for name in created:admin.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(name)))
        with psycopg.connect(**connection_kwargs(database.admin_url),autocommit=True) as admin:
            for name in created:admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))
