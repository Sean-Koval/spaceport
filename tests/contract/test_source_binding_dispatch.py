"""Operator bindings dispatch without pretending Domo has Cube member aliases."""
import pytest
from gateway.contracts.errors import GatewayError
from tests.contract.test_domo_runtime import setup

pytestmark=pytest.mark.contract


def test_generic_binding_resolution_accepts_exact_domo_v2(valid_manifest):
    from gateway.adapters.reviewed import parse_binding
    from gateway.adapters.base import check_binding,StaticBindingResolver
    import asyncio
    a,s,b,p,q,d,c=setup(valid_manifest)
    parsed=parse_binding(b.model_dump(mode='python'))
    assert type(parsed) is a.DomoRuntimeBinding and parsed==b
    check_binding(parsed,p.manifest,digest=p.binding_digest)
    assert asyncio.run(StaticBindingResolver([parsed]).resolve(b.binding_id,b.revision))==b


def test_generic_scope_gate_understands_domo_exact_output_contract(valid_manifest):
    from gateway.adapters.reviewed import validate_binding_decision
    a,s,b,p,q,d,c=setup(valid_manifest)
    assert validate_binding_decision(b,d,'dispatch-test')==d
    for changed in [d.model_copy(update={'visible_fields':('day','account')}),
                    d.model_copy(update={'tenant_id':'south'})]:
        with pytest.raises(GatewayError) as caught:
            validate_binding_decision(b,changed,'dispatch-test')
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'


def test_grain_accessor_uses_explicit_source_keys(valid_manifest):
    from gateway.adapters import reviewed
    a,s,b,p,q,d,c=setup(valid_manifest)
    assert hasattr(reviewed,'binding_grain_fields')
    assert reviewed.binding_grain_fields(b)==('day','account')


def test_domo_bundle_is_context_pinned_without_cube_url(valid_manifest):
    from gateway.adapters.bundles import BundleCatalog
    a,s,b,p,q,d,c=setup(valid_manifest)
    record=dict(endpoint_id='domo-consumer',adapter_revision='domo-named-v2',bundle_digest=c.digest,
        context='consumer',source_id=s.source_id,source_revision=s.revision,source_digest=s.digest)
    catalog=BundleCatalog({'domo-consumer':record})
    endpoint=catalog.resolve_query('domo-consumer',c.digest)
    assert endpoint.source_digest==s.digest and not hasattr(endpoint,'url')
    for extra in [{'url':'https://api.domo.com'},{'secret_ref':'/run/secrets/id'}]:
        with pytest.raises(GatewayError): BundleCatalog({'domo-consumer':{**record,**extra}})


def domo_fixture_catalog(valid_manifest):
    from gateway.publishing import test_runner as runner
    from gateway.contracts.canonical import canonical_digest
    a,s,b,p,q,d,c=setup(valid_manifest)
    fs=s.model_copy(update={'source_id':'fixture','context':'fixture_test',
        'dataset_ids':('00000000-0000-0000-0000-000000000002',),
        'auth':s.auth.model_copy(update={'expected_user_id':124,'client_id_sha256':'c'*64})})
    fb=b.model_copy(update={'binding_id':'usage-domo-fixture','context':'fixture_test',
        'source_id':fs.source_id,'source_digest':fs.digest,'dataset_id':fs.dataset_ids[0],
        'model_bundle_digest':'f'*64,'authoritative_definition_digest':'e'*64})
    case=runner.ApprovedFixture(fixture=p.manifest.fixtures[0],principal_id=d.principal_id,kind='known_answer')
    denied=case.fixture.model_copy(update={'id':'permission-negative','expected_rows':(),
        'expected_error':'NOT_FOUND_OR_FORBIDDEN'})
    payload=dict(product_id=p.manifest.product_id,consumer_binding=b.model_dump(mode='python'),
        fixture_binding=fb.model_dump(mode='python'),consumer_source=s.model_dump(mode='python'),
        fixture_source=fs.model_dump(mode='python'),execution_contract_digest=runner.execution_contract_digest(p.manifest),
        cases=[case.model_dump(mode='python'),runner.ApprovedFixture(fixture=denied,
            principal_id='denied-principal',kind='permission_negative').model_dump(mode='python')])
    return runner,a,s,b,p,payload


def test_domo_fixture_catalog_pins_both_exact_sources_and_semantics(valid_manifest):
    runner,a,s,b,p,payload=domo_fixture_catalog(valid_manifest)
    assert hasattr(runner,'parse_fixture_catalog')
    catalog=runner.parse_fixture_catalog(payload)
    catalog.approve(p.manifest,b)
    fixture_manifest=catalog.execution_manifest(p.manifest)
    assert fixture_manifest.model_bundle_digest==catalog.fixture_binding.model_bundle_digest
    assert catalog.consumer_source.digest==s.digest
    assert runner.parse_fixture_catalog(catalog).digest==catalog.digest


@pytest.mark.parametrize('change',['credential_owner','dataset','source_digest','semantics','request_roster'])
def test_domo_fixture_catalog_rejects_alias_isolation_and_drift(valid_manifest,change):
    runner,a,s,b,p,payload=domo_fixture_catalog(valid_manifest)
    assert hasattr(runner,'parse_fixture_catalog')
    if change=='credential_owner':
        payload['fixture_source']['auth']['expected_user_id']=s.auth.expected_user_id
    elif change=='dataset': payload['fixture_source']['dataset_ids']=list(s.dataset_ids)
    elif change=='source_digest': payload['fixture_binding']['source_digest']='0'*64
    elif change=='semantics': payload['fixture_binding']['grain_keys']=['day']
    elif change=='request_roster': payload['fixture_binding']['variants'][0]['query_digest']='0'*64
    with pytest.raises((GatewayError,ValueError)):
        runner.parse_fixture_catalog(payload)


def test_source_fixture_lookup_is_keyed_to_exact_manifest_and_binding(valid_manifest):
    runner,a,s,b,p,payload=domo_fixture_catalog(valid_manifest)
    calls=[]
    class Catalogs:
        def resolve(self,manifest,binding):
            calls.append((manifest.content_digest,binding.digest))
            return runner.parse_fixture_catalog(payload)
    service=runner.FixtureRunner(registry=None,policy=None,bindings=None,bundles=None,
        catalog_provider=Catalogs(),adapter_factory=lambda:None)
    selected=service.catalog(p.manifest,b)
    assert selected.consumer_binding.digest==b.digest
    assert calls==[(p.manifest.content_digest,b.digest)]
    legacy_callback=runner.FixtureRunner(registry=None,policy=None,bindings=None,bundles=None,
        catalog_provider=lambda:runner.parse_fixture_catalog(payload),adapter_factory=lambda:None)
    with pytest.raises(GatewayError): legacy_callback.catalog(p.manifest,b)
