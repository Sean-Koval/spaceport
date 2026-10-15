"""Offline setup commands. Templates and receipts do not establish live access."""
import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from pydantic import Field,model_validator

from gateway.contracts.canonical import Array,Digest,Identifier,Scalar,StrictModel
from gateway.product_packages import VersionOne,_source_model,build_source_check_plan
from gateway.source_pilot_config import SourcePilotConfig,PilotOIDCConfig,PilotRegistryConfig,RegistryLogin,DatabaseRole
from scripts.spaceport_client import CLIError
from scripts.spaceport_files import _parent,read_json,write_json


class GatewayOverrides(StrictModel):
    public_origin: str
    oidc: PilotOIDCConfig
    registry: PilotRegistryConfig
    deployment_epoch: Identifier
    database_roles: dict[DatabaseRole,RegistryLogin]
    legacy_source_reader_username: str = Field(pattern=r'^[a-z][a-z0-9_]{0,62}$')


class WeeklySetupInput(VersionOne):
    provider: Literal['domo','bigquery']
    consumer_source: dict = Field(repr=False)
    fixture_source: dict = Field(repr=False)
    consumer_definition_digest: Digest | None = None
    fixture_definition_digest: Digest | None = None
    consumer_expected_rows: Array[dict[Identifier,Scalar]] = Field(max_length=1000,repr=False)
    gateway: GatewayOverrides
    identity_subjects: dict[Identifier,str] = Field(min_length=4,max_length=4,repr=False)

    @model_validator(mode='after')
    def exact_template(self):
        if set(self.identity_subjects)!={'usage-author','usage-publisher','usage-consumer-all','usage-consumer-core'}:
            raise ValueError('The fixed sample requires its four named identities')
        if (self.provider=='domo' and (self.consumer_definition_digest is None or self.fixture_definition_digest is None)
            or self.provider=='bigquery' and (self.consumer_definition_digest is not None or self.fixture_definition_digest is not None)):
            raise ValueError('Provider-specific definition references required')
        from gateway.source_pilot_samples import weekly_manifest,WINDOW
        from gateway.contracts.manifest import validate_row
        manifest=weekly_manifest(adapter_revision='domo-named-v2' if self.provider=='domo' else 'cube-bigquery-v1',
            binding_id='weekly-usage-consumer',model_bundle_digest='0'*64)
        for row in self.consumer_expected_rows:validate_row(manifest.outputs,row)
        if (len(self.consumer_expected_rows)>manifest.limits.max_rows
            or len({(r['day'],r['account']) for r in self.consumer_expected_rows})!=len(self.consumer_expected_rows)
            or any(not WINDOW['start']<=r['day']<WINDOW['end'] for r in self.consumer_expected_rows)):
            raise ValueError('Expected rows must fit the exact bounded daily window and grain')
        for name,context in (('consumer_source','consumer'),('fixture_source','fixture_test')):
            source=_source_model(self.provider).model_validate(getattr(self,name))
            if source.context!=context:raise ValueError('Source context differs')
        return self


def template(provider):
    if provider=='bigquery':return _bigquery_template()
    if provider!='domo':raise CLIError('TEMPLATE_UNAVAILABLE')
    from gateway.adapters.domo_runtime import DomoSource
    from gateway.source_pilot_builder import build_weekly_files
    def source(context):
        return DomoSource(source_id='weekly-usage-'+context,revision=1,context=context,
            auth={'mode':'oauth-client-credentials','client_id_ref':'/run/secrets/domo_'+context+'_id',
                'client_secret_ref':'/run/secrets/domo_'+context+'_secret',
                'client_id_sha256':('a' if context=='consumer' else 'b')*64,
                'expected_customer':'replace-customer','expected_user_id':1 if context=='consumer' else 2},
            dataset_ids=[('11111111-1111-1111-1111-111111111111' if context=='consumer' else '22222222-2222-2222-2222-222222222222')],
            source_policy_digest='c'*64,max_dispatches=100,max_active_sends=2)
    consumer,fixture=source('consumer'),source('fixture_test')
    files=build_weekly_files(consumer_source=consumer,fixture_source=fixture,
        consumer_definition_digest='d'*64,fixture_definition_digest='e'*64)
    config=files['pilot.json']
    return WeeklySetupInput(schema_version=1,provider=provider,consumer_source=consumer.model_dump(mode='json'),
        fixture_source=fixture.model_dump(mode='json'),consumer_definition_digest='d'*64,fixture_definition_digest='e'*64,
        consumer_expected_rows=files['package.json']['consumer_check']['expected_rows'],
        gateway={k:config[k] for k in GatewayOverrides.model_fields},
        identity_subjects={p['id']:p['subject'] for p in files['identities.json']['principals']})


def _write_tree(directory,files):
    opened=[]
    try:
        opened,parent,name=_parent(directory)
        os.mkdir(name,mode=0o700,dir_fd=parent)
        # All file names are fixed builder output; no uploaded archive extraction.
        for filename,value in files.items():
            if Path(filename).name!=filename:raise ValueError('Unsupported generated path')
            write_json(Path(directory)/filename,value)
    finally:
        for fd in reversed(opened):os.close(fd)


def build(value):
    from gateway.source_pilot_builder import build_weekly_files,encoded
    from gateway.source_pilot_bootstrap import IdentityMapping
    value=WeeklySetupInput.model_validate(value)
    source_class=_source_model(value.provider)
    files=build_weekly_files(consumer_source=source_class.model_validate(value.consumer_source),
        fixture_source=source_class.model_validate(value.fixture_source),consumer_definition_digest=value.consumer_definition_digest,
        fixture_definition_digest=value.fixture_definition_digest)
    from gateway.product_packages import ProductPackage
    package=files['package.json'];package['consumer_check']['expected_rows']=[dict(row) for row in value.consumer_expected_rows]
    package=ProductPackage.model_validate(package)
    files['package.json']=package.model_dump(mode='json')
    files['products.json']['packages'][0]['sha256']=hashlib.sha256(encoded(files['package.json'])).hexdigest()
    files['pilot.json']['product_catalog_ref']['sha256']=hashlib.sha256(encoded(files['products.json'])).hexdigest()
    files['source-check.json']=build_source_check_plan(package).model_dump(mode='json')
    identities=files['identities.json']
    for p in identities['principals']:p['subject']=value.identity_subjects[p['id']]
    identities=IdentityMapping.model_validate(identities).model_dump(mode='json');files['identities.json']=identities
    config={**files['pilot.json'],**value.gateway.model_dump(mode='json')}
    config['identity_mapping_ref']['sha256']=hashlib.sha256(encoded(identities)).hexdigest()
    if value.provider=='domo':
        for mapping in config['source_role_mappings']:
            role='domo_consumer' if mapping['context']=='consumer' else 'domo_fixture'
            mapping['login_name']=config['database_roles'][role]['username']
    files['pilot.json']=SourcePilotConfig.model_validate(config).model_dump(mode='json')
    return files


def load_configured(path):
    from gateway.source_pilot_config import load_source_pilot_config
    from gateway.source_pilot_assets import load_source_assets
    path=Path(path).absolute()
    config=load_source_pilot_config(str(path))
    return config,load_source_assets(config,path.parent)


def run(args):
    try:
        if args.action=='init':
            _write_tree(args.directory,{'setup-input.json':template(args.provider).model_dump(mode='json')})
            result={'status':'template-created','requires_operator_configuration':True,'source_observation':'unverified'}
        elif args.action=='build':
            files=build(read_json(args.input_file))
            _write_tree(args.directory,files)
            config,assets=load_configured(Path(args.directory)/'pilot.json')
            from gateway.source_pilot_render import example_deployment
            write_json(Path(args.directory)/'deployment.example.json',example_deployment(config,assets))
            result={'status':'configured','config_valid':True,'source_observation':'unverified','deployment_observation':'unverified'}
        else:
            config,assets=load_configured(args.config)
            if args.action=='render':
                from gateway.source_pilot_render import render
                files=render(config,assets,read_json(args.deployment_file),root=Path(args.config).absolute().parent)
                _write_tree(args.directory,files)
            if args.action=='plan':
                package=next((p.package for p in assets.packages.packages if p.package.package_id==args.package_id
                    and p.package.revision==args.revision),None)
                if package is None:raise ValueError('Unknown package')
                write_json(args.plan_file,build_source_check_plan(package).model_dump(mode='json'))
            result={'status':'rendered' if args.action=='render' else 'validated','config_valid':True,
                'source_observation':'unverified','deployment_observation':'unverified'}
        print(json.dumps(result,sort_keys=True));return 0
    except CLIError:raise
    except (ValueError,TypeError,OSError,ImportError,StopIteration):
        raise CLIError('INVALID_SETUP_INPUT') from None


def _bigquery_template():
    from gateway.adapters.bigquery_runtime import BigQuerySource
    from gateway.source_pilot_builder import build_weekly_files
    def source(context):
        return BigQuerySource(source_id='weekly-usage-'+context.replace('_','-'),revision=1,context=context,
            auth={'mode':'gke-workload-identity','expected_principal':('usage-consumer' if context=='consumer' else 'usage-fixture')+'@replace-project.iam.gserviceaccount.com'},
            billing_project='replace-project',data_project='replace-project',dataset='consumer' if context=='consumer' else 'fixtures',tables=['usage'],
            location='US',dataset_location='US',cube_endpoint='https://'+('usage-consumer' if context=='consumer' else 'usage-fixture')+'.example.com',
            model_bundle_digest='0'*64,source_policy_digest='c'*64,max_jobs_per_execution=2,per_job_maximum_bytes_billed=10000000,
            total_execution_bytes=20000000,max_active_jobs=1,max_global_active_jobs=2,job_timeout_ms=30000)
    consumer,fixture=source('consumer'),source('fixture_test')
    files=build_weekly_files(consumer_source=consumer,fixture_source=fixture);config=files['pilot.json']
    return WeeklySetupInput(schema_version=1,provider='bigquery',consumer_source=consumer.model_dump(mode='json'),
        fixture_source=fixture.model_dump(mode='json'),consumer_expected_rows=files['package.json']['consumer_check']['expected_rows'],
        gateway={k:config[k] for k in GatewayOverrides.model_fields},identity_subjects={p['id']:p['subject'] for p in files['identities.json']['principals']})
