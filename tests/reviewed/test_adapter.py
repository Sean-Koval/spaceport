"""Wire-byte mocked v2 lifecycle and hostile replay tests, not live evidence."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json

import httpx
import jwt
import pytest

from gateway.adapters.cube import CubeAdapter
from gateway.adapters.normalize import normalize, NormalizationContext
from gateway.adapters.reviewed import compile_query, security_contract
from gateway.contracts.canonical import canonical_bytes, canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import ExecutionView, PreparedQuery

pytestmark=[pytest.mark.unit,pytest.mark.contract]


class Wire(httpx.AsyncByteStream):
    def __init__(self,value):self.value=json.dumps(value).encode()
    async def __aiter__(self): yield self.value


def source(case,clock,*,rows=None,meta=None):
    seen=[]
    def handler(request):
        seen.append(request)
        value=(meta or case.meta) if request.url.path.endswith('/meta') else {'data':rows if rows is not None else [case.row]}
        return httpx.Response(200,stream=Wire(value))
    return CubeAdapter(catalog=case.catalog,bindings=case.bindings,secret_resolver=lambda _: 'a'*43,
        clock=clock,transport=httpx.MockTransport(handler)),seen


def prepared(case):
    return PreparedQuery(endpoint_id='reviewed-consumer',model_bundle_digest=case.binding.model_bundle_digest,
        cube_query=compile_query(case.binding,case.manifest,case.request,case.decision))


async def poll(adapter,case,clock,query=None,**kwargs):
    return await adapter.poll(query or prepared(case),case.decision,spec=case.spec,
        executed_decision=case.decision,deadline=clock.now()+timedelta(seconds=120),request_id=case.spec.request_id,**kwargs)


def test_complete_non_orders_lifecycle_and_inline_checkpoint(reviewed_case,clock):
    case=reviewed_case
    async def scenario():
        adapter,seen=source(case,clock)
        async with adapter:
            q=await adapter.prepare(case.product,case.request,case.decision)
            assert canonical_bytes(q)==canonical_bytes(prepared(case))
            done=await poll(adapter,case,clock,q)
        assert len([r for r in seen if r.url.path.endswith('/load')])==1
        assert all('Bookings.loaded_at' not in row for row in done.raw_rows)
        token=seen[0].headers['authorization'].split(' ',1)[1]
        claims=jwt.decode(token,'a'*43,algorithms=['HS256'],audience='dpg-cube',options={'verify_exp':False})
        assert claims['adapter_revision']=='cube-rest-v2'
        assert claims['contract_digest']==canonical_digest(security_contract(case.binding))
        view=ExecutionView(execution_id='execution-1',state='succeeded',accepted_at=clock.now(),completed_at=clock.now())
        result=normalize(case.product,done,view,context=NormalizationContext(spec=case.spec,decision=case.decision,
            executed_decision=case.decision,binding=case.binding,clock=clock))
        assert result.rows==({'day':'2024-03-09','segment':'A','revenue':'9007199254740993.01'},)
        assert result.freshness.status=='fresh'
    asyncio.run(scenario())


@pytest.mark.parametrize('mutate',[
    lambda q:q['filters'][0]['and'][0]['values'].__setitem__(0,'2024-03-08T00:00:00'),
    lambda q:q['filters'][0]['and'].pop(),
    lambda q:q['filters'][0]['and'].append({'member':'Bookings.category','operator':'equals','values':['B']}),
    lambda q:q.update(limit=10),
    lambda q:q.update(timezone='UTC'),
    lambda q:q.update(order=[['Bookings.category','desc']]),
    lambda q:q['dimensions'].remove('Bookings.loaded_at'),
    lambda q:q['measures'].append('Bookings.secret'),
])
def test_well_shaped_but_changed_preparation_rejected_before_io(reviewed_case,clock,mutate):
    case=reviewed_case;q=prepared(case).model_dump(mode='json');mutate(q['cube_query'])
    async def scenario():
        adapter,seen=source(case,clock)
        async with adapter:
            with pytest.raises(GatewayError) as error: await poll(adapter,case,clock,PreparedQuery.model_validate(q))
        assert error.value.code=='BINDING_DRIFT' and seen==[]
    asyncio.run(scenario())


def test_v2_requires_execution_authority_before_io(reviewed_case,clock):
    async def scenario():
        case=reviewed_case;adapter,seen=source(case,clock)
        async with adapter:
            with pytest.raises(GatewayError):
                await adapter.poll(prepared(case),case.decision,deadline=clock.now()+timedelta(seconds=120),request_id='request')
        assert seen==[]
    asyncio.run(scenario())


@pytest.mark.parametrize('which',['tenant','group','loaded_at','total'])
def test_live_metadata_kind_or_type_drift_is_fatal(reviewed_case,clock,which):
    case=reviewed_case;meta=deepcopy(case.meta)
    for collection in ['measures','dimensions']:
        for entry in meta['cubes'][0][collection]:
            if entry['name']=='Bookings.'+which:entry['type']='boolean'
    async def scenario():
        adapter,seen=source(case,clock,meta=meta)
        async with adapter:
            with pytest.raises(GatewayError) as error: await poll(adapter,case,clock)
        assert error.value.code=='BINDING_DRIFT'
        assert not any(r.url.path.endswith('/load') for r in seen)
    asyncio.run(scenario())


@pytest.mark.parametrize('checkpoint',['2024-03-13T00:00:00','not-a-date',None])
def test_invalid_checkpoint_fails_without_rows(reviewed_case,clock,checkpoint):
    case=reviewed_case;row={**case.row,'Bookings.loaded_at':checkpoint}
    async def scenario():
        adapter,_=source(case,clock,rows=[row])
        async with adapter:
            with pytest.raises(GatewayError) as error:await poll(adapter,case,clock)
        assert error.value.code=='BINDING_DRIFT'
    asyncio.run(scenario())


def test_mixed_checkpoints_fail_and_empty_is_unknown(reviewed_case,clock):
    case=reviewed_case
    async def scenario():
        adapter,_=source(case,clock,rows=[case.row,{**case.row,'Bookings.loaded_at':'2024-03-11T00:00:00Z'}])
        async with adapter:
            with pytest.raises(GatewayError): await poll(adapter,case,clock)
        adapter,_=source(case,clock,rows=[])
        async with adapter: done=await poll(adapter,case,clock)
        assert done.source_watermark is None
        view=ExecutionView(execution_id='empty',state='succeeded',accepted_at=clock.now(),completed_at=clock.now())
        with pytest.raises(GatewayError) as error:
            normalize(case.product,done,view,context=NormalizationContext(spec=case.spec,decision=case.decision,
                executed_decision=case.decision,binding=case.binding,clock=clock))
        assert error.value.code=='FRESHNESS_UNKNOWN'
    asyncio.run(scenario())


def test_cached_rows_cannot_be_freshened_by_newer_checkpoint(reviewed_case,clock):
    case=reviewed_case
    async def scenario():
        adapter,seen=source(case,clock,rows=[{**case.row,'Bookings.loaded_at':'2024-03-10T00:00:00Z'}])
        async with adapter:done=await poll(adapter,case,clock)
        view=ExecutionView(execution_id='old',state='succeeded',accepted_at=clock.now(),completed_at=clock.now())
        with pytest.raises(GatewayError) as error:
            normalize(case.product,done,view,context=NormalizationContext(spec=case.spec,decision=case.decision,
                executed_decision=case.decision,binding=case.binding,clock=clock))
        assert error.value.code=='STALE_DATA'
        assert len([r for r in seen if r.url.path.endswith('/load')])==1
    asyncio.run(scenario())


@pytest.mark.parametrize('checkpoint',['2024-03-12T00:00:00','2024-03-12T00:00:00.123456','2024-03-12T00:00:00-04:00'])
def test_inline_checkpoint_never_guesses_timezone(reviewed_case,clock,checkpoint):
    case=reviewed_case
    async def scenario():
        adapter,_=source(case,clock,rows=[{**case.row,'Bookings.loaded_at':checkpoint}])
        async with adapter:
            with pytest.raises(GatewayError):await poll(adapter,case,clock)
    asyncio.run(scenario())


def test_explicit_checkpoint_instant_is_independent_of_dst_query_timezone(reviewed_case,clock):
    case=reviewed_case
    assert case.manifest.timezone=='America/New_York'
    async def scenario():
        adapter,_=source(case,clock,rows=[{**case.row,'Bookings.loaded_at':'2024-03-10T07:00:00.123456Z'}])
        async with adapter: done=await poll(adapter,case,clock)
        assert done.source_watermark.isoformat()=='2024-03-10T07:00:00.123456+00:00'
    asyncio.run(scenario())


def test_metadata_dimension_cannot_replace_reviewed_measure(reviewed_case,clock):
    case=reviewed_case;meta=deepcopy(case.meta)
    meta['cubes'][0]['dimensions'].extend(meta['cubes'][0]['measures']);meta['cubes'][0]['measures']=[]
    async def scenario():
        adapter,seen=source(case,clock,meta=meta)
        async with adapter:
            with pytest.raises(GatewayError):await poll(adapter,case,clock)
        assert len(seen)==1
    asyncio.run(scenario())


def test_changed_current_scope_cannot_replay_old_query(reviewed_case,clock):
    case=reviewed_case;raw=case.decision.model_dump(mode='json');raw['row_constraints'][1]['values']=['north-B']
    from gateway.contracts.identity import Decision
    async def scenario():
        adapter,seen=source(case,clock)
        async with adapter:
            with pytest.raises(GatewayError) as error:
                await adapter.poll(prepared(case),Decision.model_validate(raw),spec=case.spec,executed_decision=case.decision,
                    deadline=clock.now()+timedelta(seconds=120),request_id=case.spec.request_id)
        assert error.value.code=='NOT_FOUND_OR_FORBIDDEN' and seen==[]
    asyncio.run(scenario())


def test_v2_stored_result_rechecks_scope_and_ages_same_checkpoint(reviewed_case,clock):
    from types import SimpleNamespace
    from gateway.execution.results import project_stored_result
    case=reviewed_case
    async def scenario():
        adapter,_=source(case,clock)
        async with adapter:done=await poll(adapter,case,clock)
        view=ExecutionView(execution_id='stored-v2',state='succeeded',accepted_at=clock.now(),started_at=clock.now(),completed_at=clock.now())
        envelope=normalize(case.product,done,view,context=NormalizationContext(spec=case.spec,decision=case.decision,
            executed_decision=case.decision,binding=case.binding,clock=clock))
        execution=SimpleNamespace(id='stored-v2',request_id=case.spec.request_id,principal_id=case.spec.principal_id,
            version_id=case.product.version_id,product_id=case.manifest.product_id,policy_id=case.manifest.policy_id,
            policy_revision=case.spec.policy_revision,canonical_query=case.spec.model_dump(mode='json')['canonical_query'],canonical_hash=case.spec.canonical_hash,
            context='consumer',created_at=clock.now())
        result=project_stored_result(execution,envelope,case.product,case.binding,case.decision,case.decision,now=clock.now())
        assert result.source_watermark==done.source_watermark
        assert not any('loaded_at' in key for row in result.rows for key in row)
        with pytest.raises(GatewayError) as error:
            project_stored_result(execution,envelope,case.product,case.binding,case.decision,case.decision,now=clock.now()+timedelta(days=2))
        assert error.value.code=='STALE_DATA'
        changed=case.decision.model_copy(update={'policy_revision':2})
        with pytest.raises(GatewayError) as error:
            project_stored_result(execution,envelope,case.product,case.binding,case.decision,changed,now=clock.now())
        assert error.value.code=='NOT_FOUND_OR_FORBIDDEN'
    asyncio.run(scenario())


def test_v2_authority_cannot_downgrade_to_legacy_endpoint(reviewed_case,clock):
    from gateway.adapters.bundles import BundleCatalog
    from gateway.contracts.identity import Decision
    from pathlib import Path
    case=reviewed_case
    records=json.loads((Path(__file__).resolve().parents[2]/'cube/bundles.json').read_text())
    records['reviewed']=case.catalog.resolve(case.binding.model_bundle_digest).model_dump(mode='json')
    legacy_query=PreparedQuery(endpoint_id=records['consumer']['endpoint_id'],model_bundle_digest=records['consumer']['bundle_digest'],
        cube_query={'measures':['Orders.revenue'],'dimensions':['Orders.segment'],'timeDimensions':[{'dimension':'Orders.ordered_at','granularity':'day'}],'limit':1001})
    raw=case.decision.model_dump(mode='json')
    raw['row_constraints']=[{'member':'Orders.tenant_id','operator':'eq','values':['north']},{'member':'Orders.access_group','operator':'in','values':['north']}]
    async def scenario():
        adapter,seen=source(case,clock);adapter.catalog=BundleCatalog(records)
        async with adapter:
            with pytest.raises(GatewayError) as error:
                await adapter.poll(legacy_query,Decision.model_validate(raw),spec=case.spec,executed_decision=case.decision,
                    deadline=clock.now()+timedelta(seconds=120),request_id=case.spec.request_id)
        assert error.value.code=='BINDING_DRIFT' and seen==[]
    asyncio.run(scenario())
