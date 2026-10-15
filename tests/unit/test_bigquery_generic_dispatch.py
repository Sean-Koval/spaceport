"""Fixed generic gateway seams for the explicit BigQuery serving revision."""
import pytest
from gateway.adapters.bigquery_runtime import BigQueryBinding
from gateway.adapters.reviewed import parse_binding,security_contract,compile_query,validate_binding_decision
from gateway.adapters.base import check_binding,StaticBindingResolver
from gateway.adapters.bundles import CubeEndpoint
from gateway.contracts.manifest import Manifest
from tests.contract.test_cube_adapter import synthetic

pytestmark=pytest.mark.unit


def arrangement():
    original,product,decision,request=synthetic()
    value=original.model_dump(mode='json')
    value.update(adapter_revision='cube-bigquery-v1',source_id='own-usage',source_revision=1,source_digest='b'*64,
        scope={'tenant_member':'Orders.tenant_id','access_group_member':'Orders.access_group'},
        freshness_mode='unknown',checkpoint_member=None)
    value.pop('constraint_members',None);value.pop('watermark_member',None)
    binding=BigQueryBinding.model_validate(value)
    raw=product.manifest.model_dump(mode='json');raw.update(adapter_revision='cube-bigquery-v1',content_digest=None)
    raw['freshness']['required']=False
    return binding,Manifest.model_validate(raw),decision,request


def test_bigquery_binding_uses_explicit_generic_contracts():
    binding,manifest,decision,request=arrangement()
    assert parse_binding(binding)==binding
    check_binding(binding,manifest,digest=binding.digest)
    assert validate_binding_decision(binding,decision,'test')==decision
    assert security_contract(binding)['adapter_revision']=='cube-bigquery-v1'
    assert compile_query(binding,manifest,request,decision)['limit']==1001
    StaticBindingResolver([binding])


def test_bigquery_endpoint_is_explicit_and_unknown_freshness_cannot_be_required():
    binding,manifest,decision,request=arrangement()
    assert CubeEndpoint(endpoint_id='bq',adapter_revision='cube-bigquery-v1',url='https://cube.invalid',
        bundle_digest=binding.model_bundle_digest,secret_ref='/run/secrets/cube',context='consumer').adapter_revision=='cube-bigquery-v1'
    raw=manifest.model_dump(mode='json');raw['freshness']['required']=True
    with pytest.raises(Exception): check_binding(binding,Manifest.model_validate(raw))


@pytest.mark.parametrize('modules',[
    ['gateway.adapters.normalize','gateway.adapters.reviewed','gateway.adapters.bigquery_runtime'],
    ['gateway.adapters.bigquery_runtime','gateway.adapters.cube','gateway.adapters.reviewed'],
    ['gateway.adapters.reviewed','gateway.adapters.bigquery_runtime','gateway.adapters.normalize']])
def test_provider_imports_do_not_depend_on_test_collection_order(modules):
    import subprocess,sys
    subprocess.run([sys.executable,'-c',';'.join('import '+name for name in modules)],check=True,capture_output=True)
