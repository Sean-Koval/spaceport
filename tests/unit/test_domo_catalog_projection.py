"""Public Domo descriptions expose product semantics only."""
import pytest
from gateway.api.catalog import project_product
from tests.contract.test_domo_runtime import setup

pytestmark=pytest.mark.unit


def test_domo_description_uses_explicit_time_and_finite_examples(valid_manifest):
    _,s,b,p,q,d,c=setup(valid_manifest)
    result=project_product(p,d,b,'published')
    assert result.outputs==p.manifest.outputs and result.date_boundaries==p.manifest.date_boundaries
    assert len(result.examples)==1 and result.provenance.links==() and result.links==()
    raw=result.model_dump(mode='json')
    assert not {'source_id','dataset_id','auth','sql','statement_id'}&set(raw)
    assert s.auth.client_id_ref not in str(raw)


def test_unreviewed_finite_decision_has_no_claimed_executable_examples(valid_manifest):
    _,s,b,p,q,d,c=setup(valid_manifest)
    d=d.model_copy(update={'limits':d.limits.model_copy(update={'max_bytes':1000})})
    result=project_product(p,d,b,'published')
    assert result.examples==()


def test_partial_metadata_view_does_not_invent_cube_members(valid_manifest):
    _,s,b,p,q,d,c=setup(valid_manifest)
    d=d.model_copy(update={'visible_fields':('day','account')})
    result=project_product(p,d,b,'published')
    assert result.partial and set(result.outputs)=={'day','account'} and result.examples==()
