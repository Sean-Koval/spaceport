"""Lossless Domo result semantics, separate from protocol completion."""
from copy import deepcopy
import importlib
import json
import pytest
from gateway.contracts.errors import GatewayError
from tests.contract.test_domo_runtime import setup, DATASET

pytestmark=pytest.mark.unit


def api():
    spec=importlib.util.find_spec('gateway.adapters.domo_normalize')
    assert spec is not None, 'Domo semantic normalizer is missing'
    return importlib.import_module(spec.name)


def wire(rows=None):
    from gateway.adapters.domo_http import parse_domo_success
    rows=rows if rows is not None else [['2026-10-05','acct-a',9007199254740993]]
    body=json.dumps(dict(datasource=DATASET,columns=['day','account','usage_events'],
        metadata=[{'type':kind,'dataSourceId':DATASET} for kind in ['STRING','STRING','LONG']],
        rows=rows,numRows=len(rows),numColumns=3,fromcache=True)).encode()
    return parse_domo_success(body,dataset_id=DATASET,max_rows=100,max_bytes=65536)


def test_normalizer_preserves_large_integer_as_exact_decimal(valid_manifest,clock):
    _,s,b,p,q,d,c=setup(valid_manifest)
    result=api().normalize_domo_result(b,p,q,d,wire(),now=clock.now())
    assert result.rows==({'day':'2026-10-05','account':'acct-a','usage_events':'9007199254740993'},)
    assert result.source_watermark is None and result.freshness.status=='unknown'
    assert result.cache_status=='hit'
    assert 'acct-a' not in repr(result)


@pytest.mark.parametrize('rows', [
    [['2026-10-12','acct-a',120]],
    [['2026-10-04','acct-a',120]],
    [['2026-10-05T00:00:00Z','acct-a',120]],
    [['2026-10-05','acct-a',120],['2026-10-05','acct-a',1]],
    [['2026-10-05',None,120]],
    [['2026-10-05','acct-a',None]],
])
def test_semantic_drift_withholds_rows_after_protocol_success(valid_manifest,clock,rows):
    _,s,b,p,q,d,c=setup(valid_manifest)
    response=wire(rows)
    with pytest.raises(GatewayError) as caught:
        api().normalize_domo_result(b,p,q,d,response,now=clock.now())
    assert caught.value.code=='BINDING_DRIFT'


def test_empty_optional_freshness_is_valid(valid_manifest,clock):
    _,s,b,p,q,d,c=setup(valid_manifest)
    result=api().normalize_domo_result(b,p,q,d,wire([]),now=clock.now())
    assert result.rows==() and result.freshness.status=='unknown'


def test_required_missing_freshness_rejected(valid_manifest,clock):
    from gateway.contracts.manifest import Manifest
    _,s,b,p,q,d,c=setup(valid_manifest)
    raw=p.manifest.model_dump(mode='python',exclude={'content_digest'})
    raw['freshness']={**raw['freshness'],'required':True}
    p=p.model_copy(update={'manifest':Manifest.model_validate(raw)})
    with pytest.raises(GatewayError) as caught:
        api().normalize_domo_result(b,p,q,d,wire(),now=clock.now())
    assert caught.value.code=='FRESHNESS_UNKNOWN'


def test_current_field_revocation_denies_result(valid_manifest,clock):
    _,s,b,p,q,d,c=setup(valid_manifest)
    d=d.model_copy(update={'visible_fields':('day','account')})
    with pytest.raises(GatewayError) as caught:
        api().normalize_domo_result(b,p,q,d,wire(),now=clock.now())
    assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'


def checkpoint_setup(valid_manifest, *, required=True):
    from gateway.contracts.manifest import Manifest
    a,s,b,p,q,d,c=setup(valid_manifest)
    b=b.model_copy(update={'checkpoint':a.DomoCheckpoint(source_name='_checkpoint',producer_contract_digest='c'*64)})
    raw=p.manifest.model_dump(mode='python',exclude={'content_digest'})
    raw['freshness']={**raw['freshness'],'required':required}
    p=p.model_copy(update={'manifest':Manifest.model_validate(raw),'binding_digest':b.digest})
    return b,p,q,d


def checkpoint_wire(values):
    from gateway.adapters.domo_http import parse_domo_success
    rows=[['2026-10-05',f'acct-{i}',120,stamp] for i,stamp in enumerate(values)]
    body=json.dumps(dict(datasource=DATASET,columns=['day','account','usage_events','_checkpoint'],
        metadata=[{'type':kind,'dataSourceId':DATASET} for kind in ['STRING','STRING','LONG','STRING']],
        rows=rows,numRows=len(rows),numColumns=4,fromcache=False)).encode()
    return parse_domo_success(body,dataset_id=DATASET,max_rows=100,max_bytes=65536)


def test_same_response_checkpoint_is_hidden_freshness_evidence(valid_manifest,clock):
    from datetime import timedelta
    b,p,q,d=checkpoint_setup(valid_manifest)
    stamp=(clock.now()-timedelta(minutes=1)).isoformat()
    result=api().normalize_domo_result(b,p,q,d,checkpoint_wire([stamp,stamp]),now=clock.now())
    assert result.freshness.status=='fresh' and result.freshness.age_seconds==60
    assert '_checkpoint' not in result.typed_schema
    assert all('_checkpoint' not in row for row in result.rows)


@pytest.mark.parametrize('values,code', [
    ([], 'FRESHNESS_UNKNOWN'), ([None], 'FRESHNESS_UNKNOWN'),
    (['bad'], 'BINDING_DRIFT'),
    (['2099-01-01T00:00:00Z'], 'BINDING_DRIFT'),
    (['2020-01-01T00:00:00Z'], 'STALE_DATA'),
    (['2020-01-01T00:00:00Z','2020-01-02T00:00:00Z'], 'BINDING_DRIFT'),
])
def test_checkpoint_cannot_guess_or_mix_evidence(valid_manifest,clock,values,code):
    b,p,q,d=checkpoint_setup(valid_manifest)
    with pytest.raises(GatewayError) as caught:
        api().normalize_domo_result(b,p,q,d,checkpoint_wire(values),now=clock.now())
    assert caught.value.code==code


def test_product_sentinel_rejected_even_when_protocol_complete(valid_manifest,clock):
    _,s,b,p,q,d,c=setup(valid_manifest)
    d=d.model_copy(update={'limits':d.limits.model_copy(update={'max_rows':1,'page_size':1})})
    response=wire([['2026-10-05','acct-a',120],['2026-10-06','acct-a',180]])
    with pytest.raises(GatewayError) as caught:
        api().normalize_domo_result(b,p,q,d,response,now=clock.now())
    assert caught.value.code=='RESOURCE_LIMIT'


def test_source_type_drift_rejected(valid_manifest,clock):
    _,s,b,p,q,d,c=setup(valid_manifest)
    response=wire()
    object.__setattr__(response,'source_types',('STRING','STRING','DOUBLE'))
    with pytest.raises(GatewayError) as caught:
        api().normalize_domo_result(b,p,q,d,response,now=clock.now())
    assert caught.value.code=='BINDING_DRIFT'


def test_returned_grain_must_match_nonnull_reviewed_input(valid_manifest,clock):
    _,s,b,p,q,d,c=setup(valid_manifest)
    q=q.model_copy(update={'inputs':{**q.inputs,'account':'acct-a'}})
    with pytest.raises(GatewayError) as caught:
        api().normalize_domo_result(b,p,q,d,wire([['2026-10-05','acct-b',120]]),now=clock.now())
    assert caught.value.code=='BINDING_DRIFT'


@pytest.mark.parametrize('field,value', [('rows',(('2026-10-05','acct-a',77),)),('response_sha256','f'*64)])
def test_mutated_protocol_evidence_cannot_reuse_response_proof(valid_manifest,clock,field,value):
    _,s,b,p,q,d,c=setup(valid_manifest)
    response=wire()
    object.__setattr__(response,field,value)
    with pytest.raises(GatewayError) as caught:
        api().normalize_domo_result(b,p,q,d,response,now=clock.now())
    assert caught.value.code=='BINDING_DRIFT'
