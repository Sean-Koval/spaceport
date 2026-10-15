"""Typed durable Domo preparation decoding; SQL authority is tested separately."""
from dataclasses import asdict
import importlib
import pytest
from gateway.contracts.canonical import canonical_bytes
from gateway.contracts.errors import GatewayError
from tests.contract.test_domo_http import query_setup
from tests.contract.test_domo_runtime import setup

pytestmark=pytest.mark.unit


def api():
    spec=importlib.util.find_spec('gateway.adapters.source_preparations')
    assert spec is not None,'Typed source preparation persistence is missing'
    return importlib.import_module(spec.name)


def record(valid_manifest,clock):
    s,p,statement,token,permit=query_setup(valid_manifest,clock)
    _,_,binding,*_=setup(valid_manifest)
    value=dict(owner_kind='consumer',owner_id='execution-1',session_id=permit.context.session_id,
        prepared_query=p.model_dump(mode='json'),prepared_canonical=canonical_bytes(p).decode(),prepared_digest=p.digest,
        session_owner_id='execution-1',session_context='consumer',session_plan=asdict(permit.context.spec))
    return p,binding,value


def test_typed_preparation_reconstructs_exact_binding_and_session(valid_manifest,clock):
    p,b,value=record(valid_manifest,clock)
    assert api().validate_source_preparation_record(value,'consumer','execution-1',b)==p


@pytest.mark.parametrize('change',['owner','context','session','prepared_digest','canonical','binding','statement','session_plan'])
def test_mutated_preparation_cannot_cross_owner_or_source(valid_manifest,clock,change):
    p,b,value=record(valid_manifest,clock)
    if change=='owner': value['session_owner_id']='other'
    elif change=='context': value['session_context']='fixture_test'
    elif change=='session': value['session_id']='bad'
    elif change=='prepared_digest': value['prepared_digest']='0'*64
    elif change=='canonical': value['prepared_canonical']=' '+value['prepared_canonical']
    elif change=='binding': b=b.model_copy(update={'source_revision':2})
    elif change=='statement': value['prepared_query']['statement_id']='other'
    elif change=='session_plan': value['session_plan']['prepared_digest']='0'*64
    with pytest.raises(GatewayError):
        api().validate_source_preparation_record(value,'consumer','execution-1',b)
