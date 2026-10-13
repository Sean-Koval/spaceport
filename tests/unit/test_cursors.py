"""Opaque cursor syntax and pure page boundaries; database gates live separately."""
import importlib
import re

import pytest

from gateway.contracts.errors import GatewayError
from tests.unit.test_stored_results import fixture

pytestmark=pytest.mark.unit


def api():
    try: return importlib.import_module("gateway.execution.cursors")
    except ModuleNotFoundError as error: pytest.fail(f"Opaque cursors are missing: {error.name}")


def test_cursor_tokens_are_random_opaque_and_stored_only_as_digests():
    tokens=[api().new_cursor_token() for _ in range(50)]
    assert len(set(tokens))==50
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}",token) for token in tokens)
    assert all(re.fullmatch(r"[0-9a-f]{64}",api().cursor_digest(token)) for token in tokens)
    assert api().cursor_digest(tokens[0])==api().cursor_digest(tokens[0])


@pytest.mark.parametrize("token", [None,False,1,"","a"*42,"a"*44,"é"*43,"="*43,"a.b"*14+"a","a"*2049])
def test_malformed_cursor_is_uniformly_unavailable(token):
    with pytest.raises(GatewayError) as caught: api().cursor_digest(token)
    assert caught.value.code=="NOT_FOUND_OR_FORBIDDEN"
    assert caught.value.safe_message=="The requested product is unavailable."


def test_pages_preserve_total_count_without_marking_execution_truncated(clock):
    _,envelope,_,_=fixture(clock,count=451)
    first=api().result_page(envelope,offset=0,page_size=200,next_cursor="a"*43,max_bytes=1048576)
    second=api().result_page(envelope,offset=200,page_size=200,next_cursor="b"*43,max_bytes=1048576)
    last=api().result_page(envelope,offset=400,page_size=200,next_cursor=None,max_bytes=1048576)
    assert [len(page.rows) for page in (first,second,last)]==[200,200,51]
    assert all(page.row_count==451 and page.truncated is False for page in (first,second,last))
    assert first.rows+second.rows+last.rows==envelope.rows
    assert first.next_cursor and second.next_cursor and last.next_cursor is None


def test_empty_result_has_one_complete_empty_page(clock):
    _,envelope,_,_=fixture(clock,count=0)
    page=api().result_page(envelope,offset=0,page_size=200,next_cursor=None,max_bytes=1048576)
    assert page.rows==() and page.row_count==0 and page.next_cursor is None


@pytest.mark.parametrize("offset,size,cursor", [(-1,200,None),(True,200,None),(3,200,None),(4,200,None),(0,0,None),(0,201,None),(0,2,None),(0,200,"a"*43)])
def test_invalid_page_position_or_cursor_shape_is_rejected(offset,size,cursor,clock):
    _,envelope,_,_=fixture(clock)
    with pytest.raises(GatewayError): api().result_page(envelope,offset=offset,page_size=size,next_cursor=cursor,max_bytes=1048576)


def test_page_includes_cursor_overhead_in_byte_bound(clock):
    from gateway.contracts.canonical import canonical_bytes
    _,envelope,_,_=fixture(clock)
    page=api().result_page(envelope,offset=0,page_size=2,next_cursor="a"*43,max_bytes=1048576)
    with pytest.raises(GatewayError) as caught:
        api().result_page(envelope,offset=0,page_size=2,next_cursor="a"*43,max_bytes=len(canonical_bytes(page))-1)
    assert caught.value.code=="RESOURCE_LIMIT"
