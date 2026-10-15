"""Gateway envelope semantics use actual HTTP evidence; no database acceptance claim."""
import asyncio
from datetime import timedelta
import importlib
import json
import httpx
import pytest
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import ExecutionSpec,ExecutionView
from gateway.adapters.normalize import NormalizationContext,normalize
from tests.contract.test_domo_runtime import setup
from tests.contract.test_domo_http import query_setup,wire,secrets

pytestmark=pytest.mark.unit


def complete(valid_manifest,clock):
    spec=importlib.util.find_spec('gateway.adapters.domo_completed')
    assert spec is not None,'Domo completion contract is missing'
    module=importlib.import_module(spec.name)
    from gateway.adapters.domo_http import DomoHttpClient
    a,s,b,p,q,d,c=setup(valid_manifest)
    s,plan,statement,token,permit=query_setup(valid_manifest,clock)
    async def handler(request):
        return httpx.Response(200,headers={'content-type':'application/json'},stream=httpx.ByteStream(json.dumps(wire()).encode()))
    async def run():
        client=DomoHttpClient(source=s,secret_resolver=secrets,transport=httpx.MockTransport(handler),clock=clock)
        try:
            response=await client.query(plan=plan,statement=statement,token=token,permit=permit)
            return module.DomoCompleted(prepared=plan,response=response,client=client,permit=permit,source_watermark=None)
        finally: await client.close()
    completed=asyncio.run(run())
    query=q.model_dump(mode='json',exclude={'idempotency_key'})
    accepted=ExecutionSpec(principal_id=d.principal_id,pinned_product=p,canonical_query=query,
        canonical_hash=canonical_digest(query),policy_revision=1,context='consumer',request_id='domo-prepare')
    context=NormalizationContext(spec=accepted,decision=d,executed_decision=d,binding=b,clock=clock)
    view=ExecutionView(execution_id='execution-1',state='succeeded',accepted_at=clock.now()-timedelta(seconds=2),
        started_at=clock.now()-timedelta(seconds=1),completed_at=clock.now())
    return p,completed,view,context


def test_generic_normalizer_builds_domo_envelope_without_cube_aliases(valid_manifest,clock):
    p,c,v,ctx=complete(valid_manifest,clock)
    result=normalize(p,c,v,context=ctx)
    assert result.rows==({'day':'2026-10-05','account':'acct-a','usage_events':'9007199254740993'},)
    assert result.provenance.adapter_revision=='domo-named-v2'
    assert result.freshness.status=='unknown' and result.truncated is False and result.next_cursor is None
    assert result.typed_schema==p.manifest.outputs


@pytest.mark.parametrize('change',['request_id','product','scope','limits','response','deadline'])
def test_domo_completion_rechecks_trusted_context_and_deadline(valid_manifest,clock,change):
    from dataclasses import replace
    p,c,v,ctx=complete(valid_manifest,clock)
    if change=='request_id': ctx=replace(ctx,spec=ctx.spec.model_copy(update={'request_id':'other'}))
    elif change=='product': p=p.model_copy(update={'version_id':'other'})
    elif change=='scope': ctx=replace(ctx,decision=ctx.decision.model_copy(update={'principal_id':'other'}))
    elif change=='limits': ctx=replace(ctx,decision=ctx.decision.model_copy(update={
        'limits':ctx.decision.limits.model_copy(update={'max_bytes':1000})}))
    elif change=='response': object.__setattr__(c.response,'rows',(('2026-10-05','acct-a',1),))
    elif change=='deadline': clock.instant+=timedelta(seconds=31)
    with pytest.raises(GatewayError): normalize(p,c,v,context=ctx)


def test_domo_admission_scope_gate_uses_explicit_grain_and_full_decision(valid_manifest):
    from gateway.execution.service import check_current_scope
    a,s,b,p,q,d,catalog=setup(valid_manifest)
    assert check_current_scope(p,q,b,d,d,'source-test')==d
    tightened=d.model_copy(update={'limits':d.limits.model_copy(update={'max_bytes':1000})})
    with pytest.raises(GatewayError) as caught:
        check_current_scope(p,q,b,tightened,d,'source-test')
    assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'


def test_domo_stored_result_rechecks_source_grain_and_current_scope(valid_manifest,clock):
    from gateway.execution.results import prepare_stored_result,project_stored_result
    from gateway.registry.models import Execution
    p,c,v,ctx=complete(valid_manifest,clock)
    envelope=normalize(p,c,v,context=ctx)
    execution=Execution(id=v.execution_id,created_at=v.accepted_at,principal_id=ctx.spec.principal_id,
        product_id=p.manifest.product_id,version_id=p.version_id,policy_id=p.manifest.policy_id,policy_revision=1,
        canonical_query=ctx.spec.model_dump(mode='json')['canonical_query'],canonical_hash=ctx.spec.canonical_hash,
        context='consumer',request_id=ctx.spec.request_id,deadline_at=clock.now()+timedelta(seconds=30))
    stored=prepare_stored_result(execution,envelope,ctx.executed_decision,clock.now())
    assert stored.envelope['rows'][0]['usage_events']=='9007199254740993'
    assert project_stored_result(execution,envelope,p,ctx.binding,ctx.executed_decision,ctx.decision,now=clock.now())==envelope
    changed=ctx.decision.model_copy(update={'limits':ctx.decision.limits.model_copy(update={'max_bytes':1000})})
    with pytest.raises(GatewayError):
        project_stored_result(execution,envelope,p,ctx.binding,ctx.executed_decision,changed,now=clock.now())


def test_data_completion_requires_closed_transport(valid_manifest,clock):
    p,c,v,ctx=complete(valid_manifest,clock)
    class OpenClient:
        is_closed=False
    c.client._client=OpenClient()
    with pytest.raises(GatewayError): normalize(p,c,v,context=ctx)
