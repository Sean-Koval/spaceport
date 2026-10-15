"""Pure seed-package contracts only; disposable SQL tests qualify ownership."""
from types import SimpleNamespace

import pytest

from gateway.adapters.bigquery_runtime import check_bigquery_binding
from gateway.contracts.canonical import canonical_bytes
from gateway.contracts.execution import PinnedProduct
from gateway.source_access_contracts import validate_source_approval,evaluate_source_access
from gateway.auth.principals import PrincipalGrants
from gateway.policy.service import derive_decision,PolicyRules
from tests.integration.source_result_seed import result_approval_assets,result_principal
from tests.unit.test_policy import grant,rules

pytestmark=pytest.mark.unit


@pytest.mark.parametrize('required',[True,False])
def test_result_seed_declares_known_checkpoint_and_independent_authenticated_actor(valid_manifest,monkeypatch,required):
    import tests.integration.test_audit as audit
    monkeypatch.setattr(audit,'seed',lambda *args,**kwargs:None)
    calls=[]
    db=SimpleNamespace(execute_owner=lambda *args:calls.append(args))
    valid_manifest['freshness']['required']=required
    doc,manifest,bindings,sources=result_approval_assets(db,valid_manifest)
    approval=validate_source_approval(canonical_bytes(doc))
    pins=approval.package_pins(manifest,approval_digest=approval.content_digest)
    product=PinnedProduct(manifest=manifest,binding_digest=bindings[0].digest,version_id='version-1')
    actor=result_principal()
    decision=derive_decision(PrincipalGrants(principal_id=actor.id,grants=[grant()]),'result',product,PolicyRules.model_validate(rules()),1)
    assert evaluate_source_access(approval,actor,decision,action='result',context='consumer',pins=pins)=='retained-result-service'
    assert manifest.freshness.required is required and actor.service_client_id=='result-client'
    assert sources[0].auth!=sources[1].auth
    check_bigquery_binding(bindings[0],manifest)
    assert all(binding.freshness_mode=='same-query-checkpoint' for binding in bindings)
    assert {(entry.source_context,entry.purpose) for entry in approval.lineage if entry.purpose=='checkpoint'}=={
        ('consumer','checkpoint'),('fixture_test','checkpoint')}
    for changed in (actor.model_copy(update={'service_client_id':'other-client'}),actor.model_copy(update={'id':'north-user'})):
        with pytest.raises(ValueError):
            evaluate_source_access(approval,changed,decision,action='result',context='consumer',pins=pins)
