"""Production package/bootstrap seed confined to the disposable local registry.

Only newly created provider logins and fictional signing keys are written to the
Cube child config. The child never receives app, owner or admin authority.
"""
from contextlib import asynccontextmanager
from dataclasses import dataclass
import secrets


@dataclass(frozen=True)
class BigQuerySeed:
    config: object
    assets: object
    registry: object
    policy: object
    principals: dict
    bindings: object
    lifecycle: object
    child_config: object
    signing_keys: dict

    @property
    def origins(self):return {s.context:s.cube_endpoint for s in self.assets.sources.sources}

    def secret_resolver(self,context):
        suffix='consumer' if context=='consumer' else 'fixture'
        ref='/run/secrets/cube_'+suffix
        def resolve(value):
            if value!=ref:raise ValueError('Exact synthetic source context required')
            return self.signing_keys[context]
        return resolve


@asynccontextmanager
async def seed_bigquery_pilot(database,root):
    import psycopg
    from psycopg import sql
    from tests.integration.conftest import connection_kwargs,validate_test_admin_url,AUTHORIZATION
    from gateway.adapters.bigquery_runtime import BigQuerySource
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
    validate_test_admin_url(database.admin_url.render_as_string(hide_password=False),AUTHORIZATION)
    created=[];opened=[];urls={}
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    def write(path,data):
        with path.open('xb') as stream:stream.write(data)
        path.chmod(0o600)
    try:
        with psycopg.connect(**connection_kwargs(database.admin_url),autocommit=True) as admin:
            for context in ('consumer','fixture_test','observer'):
                name='gwb_'+secrets.token_hex(8);password=secrets.token_urlsafe(32)
                admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
                created.append(name)
                admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(database.urls['owner'].database),sql.Identifier(name)))
                urls[context]=database.urls['app'].set(username=name,password=password)
        def source(context):
            name='consumer' if context=='consumer' else 'fixture'
            return BigQuerySource(source_id='usage-'+name,revision=1,context=context,
                auth={'mode':'gke-workload-identity','expected_principal':name+'-source@fixture-project.iam.gserviceaccount.com'},
                billing_project='fixture-project',data_project=name+'-project',dataset='usage_inputs',tables=['usage'],
                location='US',dataset_location='US',cube_endpoint='https://'+name+'-bq.example.com',
                model_bundle_digest='a'*64,source_policy_digest='b'*64,max_jobs_per_execution=2,
                per_job_maximum_bytes_billed=1000000,total_execution_bytes=2000000,
                max_active_jobs=2,max_global_active_jobs=2,job_timeout_ms=30000)
        files=build_weekly_files(consumer_source=source('consumer'),fixture_source=source('fixture_test'))
        raw=files['pilot.json']
        for role in ('app','owner','audit_retention'):raw['database_roles'][role]['username']=database.roles[role]
        raw['legacy_source_reader_username']=database.roles['source_reader']
        for row in raw['source_role_mappings']:
            row['login_name']=urls['observer' if row['role_kind']=='observer' else row['context']].username
        config=SourcePilotConfig.model_validate(raw)
        assets_dir=root/'assets';assets_dir.mkdir(mode=0o700)
        for name,value in files.items():write(assets_dir/name,encoded(value))
        assets=load_source_assets(config,assets_dir)
        owner=Registry(database.urls['owner'].render_as_string(hide_password=False));opened.append(owner)
        await provision(owner,config,assets)
        registry=Registry(database.urls['app'].render_as_string(hide_password=False));opened.append(registry)
        principal_store=PrincipalStore(registry.sessions)
        policy=PolicyService(registry,principal_store,AuditWriter(b'synthetic-bigquery-audit-key-32-bytes'))
        lifecycle=SourceLifecycle(registry,policy,assets.sources,deployment_epoch=config.deployment_epoch)
        principals={row.id:await principal_store.resolve_principal(row.id) for row in assets.identities.principals}
        profiles=[];keys={}
        child_dir=root/'child';child_dir.mkdir(mode=0o700)
        for context in ('consumer','fixture_test'):
            descriptor=next(s for s in assets.sources.sources if s.context==context)
            bundle=assets.cube_bundles[descriptor.model_bundle_digest]
            bundle_dir=child_dir/(context+'-bundle');bundle_dir.mkdir(mode=0o700)
            write(bundle_dir/'security.json',bundle.security_content.encode())
            model_dir=bundle_dir/'model';model_dir.mkdir(mode=0o700)
            for name,content in bundle.model_files.items():write(model_dir/name,content.encode())
            password_file=child_dir/(context+'-password');write(password_file,urls[context].password.encode())
            keys[context]=secrets.token_urlsafe(32)
            key_file=child_dir/(context+'-signing-key');write(key_file,keys[context].encode())
            profiles.append(dict(source=descriptor.model_dump(mode='json'),bundle=dict(root=str(bundle_dir),digest=bundle.digest),
                runtime=dict(user=urls[context].username,password_file=str(password_file)),signing_key_file=str(key_file)))
        child_config=child_dir/'config.json'
        write(child_config,encoded(dict(schema_version=1,deployment_epoch=config.deployment_epoch,
            registry=dict(host=database.urls['app'].host,port=database.urls['app'].port,database=database.urls['app'].database),profiles=profiles)))
        yield BigQuerySeed(config,assets,registry,policy,principals,StaticBindingResolver(list(assets.bindings)),
            lifecycle,child_config,keys)
    finally:
        for registry in reversed(opened):await registry.close()
        with psycopg.connect(**connection_kwargs(database.admin_url.set(database=database.urls['owner'].database)),autocommit=True) as admin:
            for name in created:admin.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(name)))
        with psycopg.connect(**connection_kwargs(database.admin_url),autocommit=True) as admin:
            for name in created:admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(name)))
