"""Run-level source capacity has one durable owner across all fixed cases."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import pytest
from gateway.publishing.test_runner import FixtureRunner,_DraftAuthority

pytestmark=pytest.mark.unit


@pytest.mark.parametrize('fails',[False,True])
def test_fixture_run_opens_one_group_and_always_closes_it(fails):
    calls=[]
    class Registry:
        @asynccontextmanager
        async def transaction(self): yield object()
    class Lifecycle:
        @staticmethod
        def required(binding): return True
        async def begin_fixture_run(self,authority,run_id):
            calls.append(('begin',run_id));return SimpleNamespace(group_id=run_id,deadline=None)
        async def close_group(self,group_id,reason): calls.append(('close',group_id,reason))
    runner=FixtureRunner(registry=Registry(),policy=None,bindings=None,bundles=None,catalog_provider=None,
        adapter_factory=None,source_lifecycle=Lifecycle())
    actor=object()
    authority=_DraftAuthority(runner._issuer,actor,SimpleNamespace(id='draft'),None,None,None,1,(),
        SimpleNamespace(fixture_binding=SimpleNamespace(adapter_revision="domo-named-v2")))
    async def resolve(*args,**kwargs): return authority
    async def execute(current,run_id,*args):
        assert current.source_group.group_id==run_id
        calls.append(('cases',run_id))
        if fails: raise RuntimeError('synthetic')
        return 'result'
    runner._authority=resolve;runner._run_cases=execute
    async def run():
        if fails:
            with pytest.raises(RuntimeError): await runner.run(actor,'draft')
        else: assert await runner.run(actor,'draft')=='result'
    asyncio.run(run())
    assert [c[0] for c in calls]==['begin','cases','close']
    assert len({c[1] for c in calls})==1
    assert calls[-1][2]==('failure' if fails else 'completed')


def test_domo_fixture_candidate_must_match_its_exact_committed_session(valid_manifest,clock):
    from dataclasses import replace
    from tests.unit.test_domo_serving import arrangement,prepare
    from gateway.contracts.errors import GatewayError
    from gateway.publishing import test_runner
    adapter,s,b,p,q,d,spec,lifecycle,events=arrangement(valid_manifest,clock)
    async def run():
        plan,context=await prepare(adapter,p,q,d,spec,clock)
        result=await adapter.poll(plan,d,spec=spec,executed_decision=d,source_context=context,
            context='consumer',deadline=context.deadline,request_id=spec.request_id)
        test_runner.validate_fixture_source_completed(result,context)
        for changed in [replace(context,session_id='00000000-0000-0000-0000-000000000012'),
                replace(context,generation=2),replace(context,group_id='00000000-0000-0000-0000-000000000013')]:
            with pytest.raises(GatewayError):test_runner.validate_fixture_source_completed(result,changed)
        await adapter.close()
    asyncio.run(run())
