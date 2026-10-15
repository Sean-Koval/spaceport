"""Bounded Python serving transport; provider qualification is a separate gate."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pytest

from gateway.adapters.bigquery_runtime import BigQueryBinding,BigQuerySource,BigQueryServingAdapter
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.execution import ExecutionSpec,PinnedProduct,Pending
from gateway.contracts.manifest import Manifest
from gateway.contracts.query import validate_query
from gateway.contracts.errors import GatewayError
from gateway.source_lifecycle import SourceAttemptContext
from tests.contract.test_cube_adapter import synthetic,upstream_response
from tests.unit.test_bigquery_serving_source import source_value

pytestmark=pytest.mark.contract


def setup(clock,handler):
    original,product,decision,request=synthetic()
    source=BigQuerySource.model_validate(source_value())
    data=original.model_dump(mode='json');data.pop('watermark_member');data.pop('constraint_members')
    data.update(adapter_revision='cube-bigquery-v1',source_id=source.source_id,source_revision=1,source_digest=source.digest,
        model_bundle_digest=source.model_bundle_digest,scope={'tenant_member':'Orders.tenant_id','access_group_member':'Orders.access_group'},
        freshness_mode='unknown',checkpoint_member=None)
    binding=BigQueryBinding.model_validate(data)
    data=product.manifest.model_dump(mode='json');data.update(adapter_revision=binding.adapter_revision,model_bundle_digest=binding.model_bundle_digest,content_digest=None)
    data['freshness']['required']=False
    product=PinnedProduct(manifest=Manifest.model_validate(data),binding_digest=binding.digest,version_id='source-version')
    request=validate_query(product.manifest,request)
    endpoint=SimpleNamespace(endpoint_id='source-endpoint',adapter_revision=binding.adapter_revision,context='consumer',
        bundle_digest=binding.model_bundle_digest,url=source.cube_endpoint,secret_ref='/run/secrets/signing')
    async def resolve(*args):return binding
    def resolve_query(endpoint_id,digest):
        if (endpoint_id,digest)!=(endpoint.endpoint_id,endpoint.bundle_digest):raise ValueError
        return endpoint
    adapter=BigQueryServingAdapter(catalog=SimpleNamespace(resolve=lambda digest:endpoint,resolve_query=resolve_query),
        bindings=SimpleNamespace(resolve=resolve),sources=SimpleNamespace(resolve=lambda ref:source),
        secret_resolver=lambda ref:'a'*43,clock=clock,transport=httpx.MockTransport(handler))
    canonical=request.model_dump(mode='json',exclude={'idempotency_key'})
    spec=ExecutionSpec(principal_id=decision.principal_id,pinned_product=product,canonical_query=canonical,
        canonical_hash=canonical_digest(canonical),policy_revision=1,context='consumer',request_id='source-request')
    return adapter,product,request,decision,spec


async def prepare(adapter,product,request,decision,spec,clock):
    deadline=clock.now()+timedelta(seconds=30)
    prepared=await adapter.prepare(product,request,decision,context='consumer',deadline=deadline,request_id=spec.request_id)
    plan=await adapter.source_plan(spec,prepared,decision)
    context=SourceAttemptContext(group_id=str(uuid4()),session_id=str(uuid4()),generation=1,authority_stamp='b'*64,
        context_digest='c'*64,deadline=deadline,spec=plan)
    return prepared,context


def test_prepare_is_zero_io_and_poll_signs_exact_retained_query_and_fence(clock):
    calls=[]
    def handler(request):
        calls.append(request)
        claims=jwt.decode(request.headers['Authorization'].removeprefix('Bearer '),'a'*43,algorithms=['HS256'],audience='dpg-cube-source-v1',options={'verify_exp':False})
        import json
        raw=json.loads(request.content)
        assert claims['cube_query_digest']==canonical_digest(raw['query'])==claims['source_attempt']['prepared_digest']
        assert request.headers['X-Request-Id']==claims['source_attempt']['session_id']+'.g1'
        assert claims['exp']-claims['iat']<=30
        return upstream_response(200,json={'data':[]})
    async def scenario():
        adapter,product,request,decision,spec=setup(clock,handler)
        prepared,context=await prepare(adapter,product,request,decision,spec,clock)
        assert calls==[]
        result=await adapter.poll(prepared,decision,spec=spec,executed_decision=decision,source_context=context,
            deadline=context.deadline,request_id=spec.request_id)
        assert result.source_watermark is None and result.raw_rows==()
        assert len(calls)==1 and calls[0].url.path=='/cubejs-api/v1/load'
        await adapter.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['missing','query','scope','source','unknown_checkpoint'])
def test_untrusted_context_and_spoofed_hidden_result_fail_closed(clock,change):
    calls=[]
    def handler(request):
        calls.append(request)
        return upstream_response(200,json={'data':[{'Orders.ordered_at.day':'2024-03-09T00:00:00.000',
            'Orders.segment':'x','Orders.revenue':'1','Orders.loaded_at':'2024-03-09T00:00:00Z'}]})
    async def scenario():
        adapter,product,request,decision,spec=setup(clock,handler)
        prepared,context=await prepare(adapter,product,request,decision,spec,clock)
        if change=='missing':context=None
        elif change=='query':prepared=prepared.model_copy(update={'cube_query':dict(prepared.cube_query)|{'limit':2}})
        elif change=='scope':context=replace(context,spec=replace(context.spec,scope_digest='f'*64))
        elif change=='source':context=replace(context,spec=replace(context.spec,source_id='alias'))
        with pytest.raises(GatewayError):
            await adapter.poll(prepared,decision,spec=spec,executed_decision=decision,source_context=context,
                deadline=clock.now()+timedelta(seconds=30),request_id=spec.request_id)
        assert len(calls)==(1 if change=='unknown_checkpoint' else 0)
        await adapter.close()
    asyncio.run(scenario())


def test_pending_poll_uses_same_context_and_no_metadata_queries(clock):
    calls=[]
    def handler(request):
        calls.append(request)
        return upstream_response(200,json={'error':'Continue wait'} if len(calls)==1 else {'data':[]})
    async def scenario():
        adapter,product,request,decision,spec=setup(clock,handler)
        prepared,context=await prepare(adapter,product,request,decision,spec,clock)
        args=dict(spec=spec,executed_decision=decision,source_context=context,deadline=context.deadline,request_id=spec.request_id)
        assert isinstance(await adapter.poll(prepared,decision,**args),Pending)
        assert (await adapter.poll(prepared,decision,**args)).source_watermark is None
        assert len({call.headers['X-Request-Id'] for call in calls})==1
        assert all(call.url.path=='/cubejs-api/v1/load' for call in calls)
        with pytest.raises(GatewayError):
            await adapter.validate_binding(product.manifest,decision=decision)
        await adapter.close()
    asyncio.run(scenario())


def test_valid_different_endpoint_and_bundle_are_rejected_before_secret_or_http(clock):
    calls=[];secrets=[]
    async def scenario():
        adapter,product,request,decision,spec=setup(clock,lambda request:calls.append(request))
        prepared,context=await prepare(adapter,product,request,decision,spec,clock)
        other=SimpleNamespace(endpoint_id='other-valid',bundle_digest='f'*64,adapter_revision='cube-bigquery-v1',
            context='consumer',url='https://other.spaceport.svc.cluster.local',secret_ref='/run/secrets/other')
        adapter.catalog.resolve_query=lambda endpoint,digest:other
        adapter._secrets=lambda ref:secrets.append(ref) or 'a'*43
        changed=prepared.model_copy(update={'endpoint_id':other.endpoint_id,'model_bundle_digest':other.bundle_digest})
        with pytest.raises(GatewayError):
            await adapter.poll(changed,decision,spec=spec,executed_decision=decision,source_context=context,
                deadline=context.deadline,request_id=spec.request_id)
        assert calls==[] and secrets==[]
        await adapter.close()
    asyncio.run(scenario())
