"""Pure package identity validation; no source transport or credentials."""
import importlib.util
import hashlib
import json
import pytest
from gateway.contracts.canonical import canonical_digest

pytestmark=pytest.mark.unit


def package_api():
    assert importlib.util.find_spec('gateway.product_packages') is not None, 'Reviewed product package loader missing'
    from gateway.product_packages import read_asset,SourceRef,ProductPackage,PackageIndex
    return read_asset,SourceRef,ProductPackage,PackageIndex


def ref(path,data):
    encoded=json.dumps(data).encode();path.write_bytes(encoded)
    return {'path':path.name,'sha256':hashlib.sha256(encoded).hexdigest()}


def package(valid_manifest):
    source=lambda context:dict(provider='bigquery',source_id='usage-'+context,revision=1,descriptor_digest='a'*64,context=context)
    asset=lambda name:dict(path=name+'.json',sha256='b'*64)
    binding=lambda name:dict(**asset(name),binding_id=name,revision=1,content_digest='c'*64)
    manifest={**valid_manifest,'binding_id':'usage','policy_id':'usage-policy','content_digest':None}
    return dict(schema_version=1,package_id='usage',revision=1,manifest=manifest,
        consumer_binding_ref=binding('usage'),fixture_binding_ref=binding('usage-fixture'),
        consumer_source_ref=source('consumer'),fixture_source_ref=source('fixture_test'),
        policy_seed_ref=asset('policy'),fixture_catalog_ref=asset('fixtures'),bundle_catalog_ref=asset('bundles'),
        consumer_check={'request':{'product_id':manifest['product_id'],'version':manifest['version'],
            'inputs':{'start':'2026-10-05','end':'2026-10-12'}},'expected_rows':[]},
        fixture_equivalence_ref=asset('equivalence'))


def test_asset_bytes_pinned_and_json_strict(tmp_path):
    read_asset,*_=package_api()
    reference=ref(tmp_path/'asset.json',{'value':1})
    assert read_asset(tmp_path,reference)=={'value':1}
    (tmp_path/'asset.json').write_text('{"value":2}')
    with pytest.raises(ValueError):read_asset(tmp_path,reference)


def test_asset_parent_symlink_and_escape_refused(tmp_path):
    read_asset,*_=package_api()
    real=tmp_path/'real';real.mkdir();target=ref(real/'asset.json',{'value':1})
    (tmp_path/'alias').symlink_to(real,target_is_directory=True)
    with pytest.raises(ValueError):read_asset(tmp_path,{**target,'path':'alias/asset.json'})
    with pytest.raises(ValueError):read_asset(tmp_path,{**target,'path':'../asset.json'})


def test_package_has_stable_digest_and_independent_contexts(valid_manifest):
    _,_,ProductPackage,_=package_api()
    value=ProductPackage.model_validate(package(valid_manifest))
    assert value.digest==canonical_digest(value)
    assert value.consumer_source_ref.context=='consumer'
    assert value.fixture_source_ref.context=='fixture_test'


@pytest.mark.parametrize('change',['same_source','same_binding','context','provider','manifest_binding','version_bool','credential'])
def test_package_rejects_incoherent_identity(valid_manifest,change):
    _,_,ProductPackage,_=package_api();raw=package(valid_manifest)
    if change=='same_source':raw['fixture_source_ref']['source_id']=raw['consumer_source_ref']['source_id']
    if change=='same_binding':raw['fixture_binding_ref']=dict(raw['consumer_binding_ref'])
    if change=='context':raw['consumer_source_ref']['context']='fixture_test'
    if change=='provider':raw['fixture_source_ref']['provider']='domo'
    if change=='manifest_binding':raw['consumer_binding_ref']['binding_id']='other'
    if change=='version_bool':raw['schema_version']=True
    if change=='credential':raw['credentials']='token-value'
    with pytest.raises(ValueError):ProductPackage.model_validate(raw)


def test_package_index_is_bounded_and_no_duplicate_assets():
    *_,PackageIndex=package_api()
    item={'path':'package.json','sha256':'a'*64}
    with pytest.raises(ValueError):PackageIndex.model_validate({'schema_version':1,'packages':[item,item]})
    with pytest.raises(ValueError):PackageIndex.model_validate({'schema_version':1,'packages':[dict(path=f'{n}.json',sha256='a'*64) for n in range(26)]})


def test_source_catalog_provider_dispatch_is_fixed_and_unknown_fails(tmp_path):
    from gateway.product_packages import SourceCatalog,PackageError
    one=ref(tmp_path/'one.json',{'provider':'arbitrary-python-module'})
    two=ref(tmp_path/'two.json',{'provider':'domo','source_id':'fake'})
    index=ref(tmp_path/'index.json',{'schema_version':1,'sources':[one,two]})
    with pytest.raises(PackageError):SourceCatalog.load(tmp_path,index)


def test_source_catalog_exact_reference_rejects_wrong_context_digest_or_revision(monkeypatch):
    from gateway.product_packages import SourceCatalog,SourceRef,PackageError
    from gateway.contracts.canonical import StrictModel,canonical_digest
    from typing import Literal
    class Source(StrictModel):
        provider:Literal['domo']='domo'
        source_id:str
        revision:int=1
        context:Literal['consumer','fixture_test']
        @property
        def digest(self):return canonical_digest(self)
    consumer=Source(source_id='consumer',context='consumer')
    fixture=Source(source_id='fixture',context='fixture_test')
    catalog=SourceCatalog([consumer,fixture])
    ref=dict(provider='domo',source_id='consumer',revision=1,context='consumer',descriptor_digest=consumer.digest)
    assert catalog.resolve(ref)==consumer
    for replacement in ({'context':'fixture_test'},{'descriptor_digest':'a'*64},{'revision':2},{'provider':'bigquery'}):
        with pytest.raises(PackageError):catalog.resolve({**ref,**replacement})


def test_source_catalog_model_validation_is_offline_and_retains_immutable_sources(tmp_path,monkeypatch):
    import socket
    import gateway.product_packages as module
    from gateway.contracts.canonical import StrictModel,canonical_digest
    from typing import Literal
    class Source(StrictModel):
        provider:Literal['domo']
        source_id:str
        revision:int
        context:Literal['consumer','fixture_test']
        @property
        def digest(self):return canonical_digest(self)
    monkeypatch.setattr(module,'_source_model',lambda provider:Source)
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('Catalog opened a socket'))
    sources=[ref(tmp_path/(name+'.json'),dict(provider='domo',source_id=name,revision=1,context=context))
        for name,context in [('consumer','consumer'),('fixture','fixture_test')]]
    index=ref(tmp_path/'index.json',{'schema_version':1,'sources':sources})
    catalog=module.SourceCatalog.load(tmp_path,index)
    assert len(catalog.sources)==2
    with pytest.raises(ValueError):catalog.sources[0].source_id='changed'


def test_check_plan_is_derived_without_circular_package_hash(valid_manifest):
    from gateway.product_packages import ProductPackage, build_source_check_plan
    raw=package(valid_manifest)
    raw.pop('source_check_plan_ref',None)
    raw['consumer_check']={'request':{'product_id':raw['manifest']['product_id'],'version':raw['manifest']['version'],
        'inputs':{'start':'2026-10-05','end':'2026-10-12'}},'expected_rows':[]}
    value=ProductPackage.model_validate(raw)
    plan=build_source_check_plan(value)
    assert plan.configured_package_digest==value.digest
    assert plan.expected_product_digest==value.manifest.content_digest
    assert plan.expected_provenance.binding_id==value.manifest.binding_id


def test_consumer_check_cannot_query_another_product_or_supply_idempotency(valid_manifest):
    from gateway.product_packages import ProductPackage
    for request in ({'product_id':'unapproved','version':'1.0.0'},
                    {'product_id':valid_manifest['product_id'],'version':valid_manifest['version'],'idempotency_key':'external'}):
        raw=package(valid_manifest);raw.pop('source_check_plan_ref',None);raw['consumer_check']={'request':request,'expected_rows':[]}
        with pytest.raises(ValueError):ProductPackage.model_validate(raw)


def test_source_catalog_parses_real_bigquery_descriptor_without_source_io(tmp_path,monkeypatch):
    from gateway.product_packages import SourceCatalog
    import socket
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('Provider socket'))
    sources=[]
    for name,context in [('consumer','consumer'),('fixture','fixture_test')]:
        source=dict(provider='bigquery',source_id='usage-'+name,revision=1,context=context,
            auth={'mode':'gke-workload-identity','expected_principal':'usage-'+name+'@sample-project.iam.gserviceaccount.com'},
            billing_project='sample-project',data_project='sample-project',dataset='usage_'+name,tables=['daily_usage'],
            location='US',dataset_location='US',cube_endpoint='https://cube-'+name+'.example.com',
            model_bundle_digest=('a' if name=='consumer' else 'b')*64,source_policy_digest='c'*64,
            max_jobs_per_execution=1,per_job_maximum_bytes_billed=1000000,total_execution_bytes=1000000,
            max_active_jobs=1,max_global_active_jobs=2,job_timeout_ms=30000)
        sources.append(ref(tmp_path/(name+'.json'),source))
    index=ref(tmp_path/'index.json',{'schema_version':1,'sources':sources})
    catalog=SourceCatalog.load(tmp_path,index)
    assert len(catalog.sources)==2 and catalog.sources[0].capacity_pool_id==catalog.sources[1].capacity_pool_id


def test_generated_plan_pins_change_with_recipe_source_and_public_provenance(valid_manifest):
    from gateway.product_packages import ProductPackage,build_source_check_plan
    from gateway.contracts.canonical import canonical_digest
    raw=package(valid_manifest);first=build_source_check_plan(ProductPackage.model_validate(raw))
    for kind in ('recipe','source','provenance'):
        changed=package(valid_manifest)
        if kind=='recipe':changed['consumer_check']['request']['inputs']['end']='2026-10-11'
        if kind=='source':changed['consumer_source_ref']['descriptor_digest']='d'*64
        if kind=='provenance':changed['manifest']['links']=[{'label':'Definition','url':'https://example.com/changed'}]
        next_plan=build_source_check_plan(ProductPackage.model_validate(changed))
        assert next_plan.configured_package_digest!=first.configured_package_digest
        assert canonical_digest(next_plan)!=canonical_digest(first)
        if kind=='provenance':
            assert next_plan.expected_provenance==first.expected_provenance
            assert next_plan.expected_product_digest!=first.expected_product_digest


def test_generated_plan_matches_actual_public_describe_and_result_projection(clock):
    from tests.unit.test_normalization import setup,completed
    from gateway.api.catalog import project_product
    from gateway.product_packages import ProductPackage,build_source_check_plan
    module,product,context,view=setup(clock)
    assert product.manifest.links
    raw=package(product.manifest.model_dump(mode='json'))
    raw['manifest']=product.manifest.model_dump(mode='json')
    raw['consumer_binding_ref']['binding_id']=product.manifest.binding_id
    plan=build_source_check_plan(ProductPackage.model_validate(raw))
    described=project_product(product,context.decision,context.binding,'published')
    result=module.normalize(product,completed(),view,context=context)
    assert plan.expected_provenance==described.provenance==result.provenance
    assert plan.expected_product_digest==described.digest==result.product_digest
