"""Build one fixed fictional template from explicit reviewed source descriptors.

The generated catalog is operator input, never observed source qualification.
No source browsing, SQL input, credentials, grants or network access are supported.
"""
import hashlib
import json

from gateway.contracts.canonical import canonical_digest
from gateway.source_pilot_samples import weekly_manifest,fixed_decisions,CASE_PRINCIPALS,LIMITS,WINDOW,ALL_ROWS


def encoded(value):
    return (json.dumps(value,allow_nan=False,ensure_ascii=True,sort_keys=True,indent=2)+'\n').encode('utf-8')


def build_weekly_files(*,consumer_source,fixture_source,consumer_definition_digest=None,fixture_definition_digest=None):
    if consumer_source.provider=='bigquery':
        return _bigquery_files(consumer_source,fixture_source)
    from gateway.adapters.domo_runtime import DomoSource,validate_domo_source_isolation
    from gateway.adapters.domo_sample import build_weekly_domo_assets
    from gateway.publishing.test_runner import DomoFixtureCatalog,execution_contract_digest
    from gateway.product_packages import ProductPackage,build_source_check_plan
    from gateway.source_pilot_config import SourcePilotConfig
    from gateway.source_pilot_bootstrap import PolicySeed,IdentityMapping
    consumer=DomoSource.model_validate(consumer_source.model_dump(mode='python'))
    fixture=DomoSource.model_validate(fixture_source.model_dump(mode='python'))
    validate_domo_source_isolation(consumer,fixture)
    assets=[]
    for source,definition in ((consumer,consumer_definition_digest),(fixture,fixture_definition_digest)):
        manifest=weekly_manifest(adapter_revision='domo-named-v2',binding_id='weekly-usage-'+source.context.replace('_','-'),model_bundle_digest='0'*64)
        assets.append(build_weekly_domo_assets(manifest=manifest,source=source,dataset_id=source.dataset_ids[0],
            definition_digest=definition,decisions=fixed_decisions(source.context)))
    ca,fa=assets;manifest=ca.manifest
    cases=[dict(fixture=f.model_dump(mode='json'),principal_id=CASE_PRINCIPALS[f.id],
        kind='permission_negative' if f.expected_error=='NOT_FOUND_OR_FORBIDDEN' else ('input_negative' if f.expected_error else 'known_answer'),
        relative_tolerance='0') for f in manifest.fixtures]
    catalog=DomoFixtureCatalog(product_id=manifest.product_id,consumer_binding=ca.binding,fixture_binding=fa.binding,
        consumer_source=consumer,fixture_source=fixture,execution_contract_digest=execution_contract_digest(manifest),cases=cases)
    catalog.approve(manifest,ca.binding)
    endpoints={}
    for name,item,source in (('consumer',ca,consumer),('fixture',fa,fixture)):
        endpoints[name]=dict(endpoint_id='usage-'+name,adapter_revision='domo-named-v2',bundle_digest=item.statements.digest,
            context=source.context,source_id=source.source_id,source_revision=source.revision,source_digest=source.digest)
    return _assemble(consumer,fixture,ca,fa,catalog,{'schema_version':1,'endpoints':endpoints,
        'statement_catalogs':[[s.model_dump(mode='json') for s in item.statement_records] for item in assets]})


def _bigquery_files(consumer,fixture):
    from gateway.source_pilot_bigquery import build_weekly_bigquery_assets
    from gateway.source_pilot_assets import validate_bigquery_isolation
    from gateway.publishing.test_runner import BigQueryFixtureCatalog,execution_contract_digest
    ca,fa=build_weekly_bigquery_assets(consumer),build_weekly_bigquery_assets(fixture)
    consumer,fixture=ca.source,fa.source;validate_bigquery_isolation(consumer,fixture)
    manifest=ca.manifest
    cases=[dict(fixture=f.model_dump(mode='json'),principal_id=CASE_PRINCIPALS[f.id],
        kind='permission_negative' if f.expected_error=='NOT_FOUND_OR_FORBIDDEN' else ('input_negative' if f.expected_error else 'known_answer'),
        relative_tolerance='0') for f in manifest.fixtures]
    catalog=BigQueryFixtureCatalog(product_id=manifest.product_id,consumer_binding=ca.binding,fixture_binding=fa.binding,
        consumer_source=consumer,fixture_source=fixture,execution_contract_digest=execution_contract_digest(manifest),cases=cases)
    catalog.approve(manifest,ca.binding)
    endpoints={name:dict(endpoint_id='usage-'+name,adapter_revision='cube-bigquery-v1',bundle_digest=item.bundle.digest,
        context=item.source.context,url=item.source.cube_endpoint,secret_ref='/run/secrets/cube_'+name)
        for name,item in (('consumer',ca),('fixture',fa))}
    return _assemble(consumer,fixture,ca,fa,catalog,{'schema_version':1,'endpoints':endpoints,
        'cube_bundles':[ca.bundle.model_dump(mode='json'),fa.bundle.model_dump(mode='json')]})


def _assemble(consumer,fixture,ca,fa,catalog,bundle_data):
    from gateway.product_packages import ProductPackage,build_source_check_plan
    from gateway.source_pilot_config import SourcePilotConfig
    from gateway.source_pilot_bootstrap import PolicySeed,IdentityMapping
    manifest=ca.manifest
    files={}
    def put(name,value):
        value=value.model_dump(mode='json') if hasattr(value,'model_dump') else value
        files[name]=value
        return {'path':name,'sha256':hashlib.sha256(encoded(value)).hexdigest()}
    sr=[]
    for name,source in (('consumer',consumer),('fixture',fixture)):
        sr.append(put(name+'-source.json',source))
    source_index=put('sources.json',{'schema_version':1,'sources':sr})
    cr=put('consumer-binding.json',ca.binding);fr=put('fixture-binding.json',fa.binding)
    fixture_ref=put('fixtures.json',catalog)
    bundle_ref=put('bundles.json',bundle_data)
    fixture_principals=[dict(id='fixture-usage-'+group,tenant_id='south' if group=='south' else 'north',
        roles=[] if group=='denied' else ['consumer'],entitlements={'groups':['core','special'] if group=='all' else ['core']})
        for group in ('all','core','denied','south')]
    seed=PolicySeed(policy_id=manifest.policy_id,revision=1,content={'reason':'Reviewed north account usage with fixed group entitlements.',
        'rules':{'tenant_ids':['north'],'tenant_constraint':{'member':'Usage.tenant_id','operator':'eq','source':'grant.tenant_id'},
            'entitlement_constraint':{'member':'Usage.access_group','operator':'in','source':'grant.entitlements.groups'},
            'visible_fields':['day','account','usage_events'],'limits':LIMITS.model_dump(mode='json')}},fixture_principals=fixture_principals)
    policy_ref=put('policy.json',seed)
    def principal(id,role,groups):
        return dict(id=id,kind='user',subject='REPLACE_'+id.upper().replace('-','_')+'_SUBJECT',grants=[dict(
            policy_id=manifest.policy_id,tenant_id='north',roles=[role],entitlements={'groups':groups})])
    identities=IdentityMapping(schema_version=1,principals=[principal('usage-author','author',['core','special']),
        principal('usage-publisher','publisher',['core','special']),principal('usage-consumer-all','consumer',['core','special']),
        principal('usage-consumer-core','consumer',['core'])])
    identity_ref=put('identities.json',identities)
    ref=lambda source:dict(provider=source.provider,source_id=source.source_id,revision=source.revision,descriptor_digest=source.digest,context=source.context)
    package=ProductPackage(schema_version=1,package_id='weekly-account-usage-'+consumer.provider,revision=1,manifest=manifest,
        consumer_binding_ref={**cr,'binding_id':ca.binding.binding_id,'revision':ca.binding.revision,'content_digest':ca.binding.digest},
        fixture_binding_ref={**fr,'binding_id':fa.binding.binding_id,'revision':fa.binding.revision,'content_digest':fa.binding.digest},
        consumer_source_ref=ref(consumer),fixture_source_ref=ref(fixture),policy_seed_ref=policy_ref,fixture_catalog_ref=fixture_ref,
        bundle_catalog_ref=bundle_ref,fixture_equivalence_ref=fixture_ref,
        consumer_check={'request':{'product_id':manifest.product_id,'version':manifest.version,'inputs':dict(WINDOW)},'expected_rows':list(ALL_ROWS)})
    product_index=put('products.json',{'schema_version':1,'packages':[put('package.json',package)]})
    roles={role:{'username':'gateway_'+role,'password_file':'/run/secrets/registry_'+role}
        for role in (('app','owner','audit_retention','domo_consumer','domo_fixture') if consumer.provider=='domo' else ('app','owner','audit_retention'))}
    mappings=[]
    for role,source in (('consumer',consumer),('fixture',fixture)):
        mappings.append(dict(login_name='gateway_'+consumer.provider+'_'+role,source_id=source.source_id,revision=source.revision,
            context=source.context,role_kind='runtime'))
        if consumer.provider=='bigquery':mappings.append(dict(login_name='gateway_bigquery_observer',source_id=source.source_id,
            revision=source.revision,context=source.context,role_kind='observer'))
    config=SourcePilotConfig(schema_version=1,profile='reviewed-source-pilot',deployment_epoch='pilot-2026-10-15',
        public_origin='https://spaceport.example.com',oidc={'issuer':'https://identity.example.com',
            'audience':'https://spaceport.example.com/mcp','jwks_url':'https://identity.example.com/jwks','client_ids':['reviewed-client']},
        registry={'host':'registry.example.com','database':'gateway_registry','ca_file':'/run/source/config/registry-ca.pem'},
        database_roles=roles,source_role_mappings=mappings,
        product_catalog_ref=product_index,source_catalog_ref=source_index,identity_mapping_ref=identity_ref,
        probe_product_id=manifest.product_id,probe_principal_id='usage-consumer-all')
    put('pilot.json',config);put('manifest.json',manifest);put('source-check.json',build_source_check_plan(package))
    return files
