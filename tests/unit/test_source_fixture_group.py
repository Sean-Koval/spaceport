"""Run-level source capacity has one durable owner across all fixed cases."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import pytest
from gateway.publishing.test_runner import FixtureRunner,_DraftAuthority

pytestmark=pytest.mark.unit


@pytest.mark.parametrize('fails',[False,True])
def test_fixture_run_opens_one_group_and_always_closes_it(fails):
    # Ordering only: this probe cannot mint an AccessReceipt or SQL owner.
    from gateway.contracts.canonical import canonical_bytes
    from uuid import UUID
    calls=[]
    actor=object()
    invocation=object()
    catalog_wire={'fixture_binding':{'adapter_revision':'domo-named-v2'}}
    class Access:
        async def accept_fixture_invocation(self,uow,**values):
            assert values['actor'] is actor and values['draft_id']=='draft'
            assert values['catalog_canonical']==canonical_bytes(catalog_wire).decode()
            UUID(values['run_id']); UUID(values['request_id'])
            calls.append(('accept',values['run_id']))
            return invocation
    class Registry:
        @asynccontextmanager
        async def transaction(self): yield object()
    class Lifecycle:
        @staticmethod
        def required(binding): return True
        async def begin_fixture_run(self,authority,run_id):
            assert authority.source_invocation is invocation
            calls.append(('begin',run_id));return SimpleNamespace(group_id=run_id,deadline=None)
        async def close_group(self,group_id,reason): calls.append(('close',group_id,reason))
    runner=FixtureRunner(registry=Registry(),policy=SimpleNamespace(requires_source_approval=lambda product:True,
        source_access=Access()),bindings=None,bundles=None,catalog_provider=None,
        adapter_factory=None,source_lifecycle=Lifecycle())
    authority=_DraftAuthority(runner._issuer,actor,SimpleNamespace(id='draft'),None,None,None,1,(),
        SimpleNamespace(fixture_binding=SimpleNamespace(adapter_revision="domo-named-v2"),
            model_dump=lambda **kwargs:catalog_wire))
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
    assert [c[0] for c in calls]==['accept','begin','cases','close']
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


@pytest.mark.parametrize('expire_after_final',[False,True])
def test_reviewed_fixture_group_uses_protected_sorted_roster_before_final_clock(valid_manifest,expire_after_final):
    from datetime import datetime,timedelta,timezone
    from uuid import uuid4
    from gateway.contracts.execution import PinnedProduct
    from gateway.contracts.manifest import validate_manifest
    from gateway.source_lifecycle import SourceLifecycle,SourceGroupContext
    from tests.unit.test_source_approval_contracts import principal,decision
    now=datetime(2026,10,15,tzinfo=timezone.utc);run_id=str(uuid4());calls=[]
    manifest=validate_manifest(valid_manifest)
    product=PinnedProduct(manifest=manifest,binding_digest='a'*64,version_id='draft')
    actor=principal();author=decision();binding=object()
    invocation=SimpleNamespace(invocation_id=run_id,actor=actor,product=product,draft_id='draft',
        draft_digest=manifest.content_digest,catalog_digest='c'*64,effective_deadline_at=now+timedelta(seconds=30))
    authority=SimpleNamespace(actor=actor,product=product,draft=SimpleNamespace(id='draft'),author_decision=author,
        source_invocation=invocation,catalog=SimpleNamespace(digest='c'*64,fixture_binding=binding,
            cases=[SimpleNamespace(fixture=SimpleNamespace(id=name)) for name in ('z-positive','a-positive')]))
    class Session:
        async def scalar(self,stmt):
            calls.append('clock')
            return now+timedelta(seconds=40) if expire_after_final and 'final' in calls else now
    class Registry:
        @asynccontextmanager
        async def transaction(self): yield SimpleNamespace(session=Session())
    class Access:
        async def resolve_owner(self,*args,**kwargs):calls.append('owner');return object()
        async def finalize(self,uow,requests):calls.append('final')
    lifecycle=SourceLifecycle(Registry(),SimpleNamespace(source_access=Access()),None,deployment_epoch='synthetic')
    async def original(*args):return invocation
    async def lock(*args,**kwargs):calls.append('prefix')
    async def snapshot(*args,**kwargs):
        assert kwargs=={'owner_kind':'fixture_invocation','owner_id':run_id}
        return {'draft':{'digest':manifest.content_digest},'source_access':{'positive_roster':['a-positive','z-positive']}},'d'*64
    async def insert(*args,**kwargs):
        calls.append('group');assert kwargs['roster']==['a-positive','z-positive']
        assert kwargs['owner_id']==run_id
        return SourceGroupContext(str(uuid4()),kwargs['deadline'])
    lifecycle._fixture_invocation=original;lifecycle._lock_authority=lock;lifecycle._snapshot=snapshot
    lifecycle._decision=lambda *args,**kwargs:author;lifecycle.source=lambda value:object();lifecycle._insert_group=insert
    async def scenario():
        if expire_after_final:
            with pytest.raises(ValueError,match='deadline elapsed'):await lifecycle.begin_fixture_run(authority,run_id)
        else:
            assert (await lifecycle.begin_fixture_run(authority,run_id)).deadline==invocation.effective_deadline_at
    asyncio.run(scenario())
    assert calls==['prefix','clock','group','owner','final','clock']
