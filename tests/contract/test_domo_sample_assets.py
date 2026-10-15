"""Offline sample generation is reproducible configuration, not live qualification."""
import importlib
import pytest
from gateway.source_pilot_samples import weekly_manifest,fixed_decisions
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.errors import GatewayError
from gateway.contracts.query import QueryRequest,validate_query
from tests.unit.test_domo_source_config import source_dict
from gateway.adapters.domo_runtime import DomoSource,compile_domo_query

pytestmark=pytest.mark.contract


def api():
    spec=importlib.util.find_spec('gateway.adapters.domo_sample')
    assert spec is not None,'Domo sample asset builder is missing'
    return importlib.import_module(spec.name)


@pytest.mark.parametrize('context',['consumer','fixture_test'])
def test_weekly_sample_compiles_all_four_exact_source_variants(context):
    raw=source_dict();raw['context']=context
    source=DomoSource.model_validate(raw)
    template=weekly_manifest(adapter_revision='domo-named-v2',binding_id='usage-'+context,model_bundle_digest='0'*64)
    decisions=fixed_decisions(context)
    assets=api().build_weekly_domo_assets(manifest=template,source=source,dataset_id=source.dataset_ids[0],
        definition_digest='b'*64,decisions=decisions)
    assert assets.manifest.model_bundle_digest==assets.statements.digest==assets.binding.model_bundle_digest
    assert assets.manifest.content_digest!=template.content_digest
    product=PinnedProduct(manifest=assets.manifest,binding_digest=assets.binding.digest,version_id='sample-product')
    for case,decision in zip(assets.manifest.fixtures[:4],decisions,strict=True):
        query=validate_query(assets.manifest,QueryRequest(product_id=assets.manifest.product_id,
            version=assets.manifest.version,inputs=case.inputs))
        plan=compile_domo_query(assets.binding,product,query,decision,assets.statements,source=source)
        statement=assets.statements.resolve(plan.statement_id,plan.statement_revision,plan.statement_digest)
        assert statement.text.endswith('LIMIT 101') and 'CAST' not in statement.text
    assert len(assets.binding.variants)==4
    repeated=api().build_weekly_domo_assets(manifest=template,source=source,dataset_id=source.dataset_ids[0],
        definition_digest='b'*64,decisions=decisions)
    assert repeated.binding.digest==assets.binding.digest


def test_sample_refuses_unreviewed_source_or_decision(valid_manifest):
    source=DomoSource.model_validate(source_dict())
    template=weekly_manifest(adapter_revision='domo-named-v2',binding_id='usage-consumer',model_bundle_digest='0'*64)
    decisions=list(fixed_decisions('consumer'))
    decisions[0]=decisions[0].model_copy(update={'tenant_id':'south'})
    with pytest.raises((ValueError,GatewayError)):
        api().build_weekly_domo_assets(manifest=template,source=source,dataset_id=source.dataset_ids[0],
            definition_digest='b'*64,decisions=decisions)
