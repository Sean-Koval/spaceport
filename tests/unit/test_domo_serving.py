"""Orchestration unit checks; shared PostgreSQL acceptance is tested separately."""
import asyncio
from datetime import timedelta
import importlib
import json
from types import MappingProxyType
import httpx
import pytest
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import ErrorCode,GatewayError
from gateway.contracts.execution import ExecutionSpec
from gateway.source_lifecycle import SourceAttemptContext,SourceStatus,DomoSendPermit
from gateway.adapters.base import StaticBindingResolver
from tests.contract.test_domo_runtime import setup
from tests.contract.test_domo_http import wire,token_wire

pytestmark=pytest.mark.unit


def api():
    spec=importlib.util.find_spec('gateway.adapters.domo_serving')
    assert spec is not None,'Domo gateway orchestration adapter is missing'
    return importlib.import_module(spec.name)


class Lifecycle:
    """Explicit unit double; never evidence for database authorization/settlement."""
    def __init__(self,events,deny_check=0):
        self.events=events;self.checks=0;self.deny_check=deny_check;self.permit=None;self.evidence=None
    async def check_attempt(self,context):
        self.events.append('check');self.checks+=1
        if self.checks==self.deny_check:
            raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN,'unit','Access changed.')
    async def domo_arm_send(self,context,fingerprint,intent):
        self.events.append('arm')
        self.permit=DomoSendPermit._from_committed(context,fingerprint,'unit-nonce')
        return self.permit
    async def domo_observe_success(self,permit,evidence):
        self.events.append('observe');self.evidence=evidence;permit._success_proof()
    async def domo_observe_not_sent(self,permit):
        permit._claim_not_sent();self.events.append('not_sent')
    async def seal_source(self,context,reason):
        self.events.append('seal:'+reason)
        terminal=self.permit is None or self.permit._state in {'observed','not_sent'}
        return SourceStatus(context.session_id,True,terminal,False,False,int(self.permit is not None),int(not terminal))


def arrangement(valid_manifest,clock,*,status=200,deny_check=0):
    a,s,b,p,q,d,statements=setup(valid_manifest)
    events=[];lifecycle=Lifecycle(events,deny_check)
    class Sources:
        def resolve(self,ref):
            assert ref==dict(provider='domo',source_id=s.source_id,revision=s.revision,
                descriptor_digest=s.digest,context=s.context)
            return s
    async def handler(request):
        is_token=request.url.path=='/oauth/token'
        events.append('token' if is_token else 'query')
        return httpx.Response(200 if is_token else status,headers={'content-type':'application/json'},
            stream=httpx.ByteStream(json.dumps(token_wire() if is_token else wire()).encode()))
    def resolver(ref):
        events.append('secret')
        return 'client-id' if ref.endswith('/id') else 'private-secret'
    adapter=api().DomoRuntimeAdapter(bindings=StaticBindingResolver([b]),sources=Sources(),
        statements=MappingProxyType({statements.digest:statements}),lifecycle=lifecycle,secret_resolver=resolver,
        context='consumer',clock=clock,http_transport=httpx.MockTransport(handler))
    query=q.model_dump(mode='json',exclude={'idempotency_key'})
    spec=ExecutionSpec(principal_id=d.principal_id,pinned_product=p,canonical_query=query,
        canonical_hash=canonical_digest(query),policy_revision=1,context='consumer',request_id='accepted-source')
    return adapter,s,b,p,q,d,spec,lifecycle,events


async def prepare(adapter,p,q,d,spec,clock):
    deadline=clock.now()+timedelta(seconds=30)
    plan=await adapter.prepare(p,q,d,context='consumer',deadline=deadline,request_id=spec.request_id)
    session=await adapter.source_plan(spec,plan,d)
    ctx=SourceAttemptContext('00000000-0000-0000-0000-000000000010',
        '00000000-0000-0000-0000-000000000011',1,'d'*64,'e'*64,deadline,session)
    return plan,ctx


def test_gateway_domo_sequence_is_pure_prepare_then_one_observed_send(valid_manifest,clock):
    adapter,s,b,p,q,d,spec,lifecycle,events=arrangement(valid_manifest,clock)
    async def run():
        plan,context=await prepare(adapter,p,q,d,spec,clock)
        assert events==[] and context.spec.definition_digest==s.digest
        assert context.spec.request_digest==spec.canonical_hash
        result=await adapter.poll(plan,d,spec=spec,executed_decision=d,source_context=context,
            context='consumer',deadline=context.deadline,request_id=spec.request_id)
        assert result.evidence()==lifecycle.evidence
        assert lifecycle.evidence['plan_fingerprint']==plan.digest
        assert events==['check','secret','secret','token','check','arm','check','query','observe','check','seal:completed']
        with pytest.raises(GatewayError):
            await adapter.poll(plan,d,spec=spec,executed_decision=d,source_context=context,
                context='consumer',deadline=context.deadline,request_id=spec.request_id)
        assert events.count('query')==1
        await adapter.close()
    asyncio.run(run())


@pytest.mark.parametrize('case',['spec','decision','missing_context','prepared'])
def test_domo_reconstructs_trusted_acceptance_before_credentials(valid_manifest,clock,case):
    adapter,s,b,p,q,d,spec,lifecycle,events=arrangement(valid_manifest,clock)
    async def run():
        plan,context=await prepare(adapter,p,q,d,spec,clock)
        accepted=spec;current=d
        if case=='spec': accepted=spec.model_copy(update={'request_id':'wrong'})
        elif case=='decision': current=d.model_copy(update={'limits':d.limits.model_copy(update={'max_bytes':1000})})
        elif case=='missing_context': context=None
        elif case=='prepared': plan=plan.model_copy(update={'max_rows':1})
        with pytest.raises(GatewayError):
            await adapter.poll(plan,current,spec=accepted,executed_decision=d,source_context=context,
                context='consumer',deadline=clock.now()+timedelta(seconds=30),request_id=spec.request_id)
        assert not {'secret','token','query','arm'}&set(events)
        await adapter.close()
    asyncio.run(run())


@pytest.mark.parametrize('deny_check,expected',[(3,'not_sent'),(4,'observe')])
def test_revocation_does_not_confuse_source_completion_with_row_authority(valid_manifest,clock,deny_check,expected):
    adapter,s,b,p,q,d,spec,lifecycle,events=arrangement(valid_manifest,clock,deny_check=deny_check)
    async def run():
        plan,context=await prepare(adapter,p,q,d,spec,clock)
        with pytest.raises(GatewayError) as caught:
            await adapter.poll(plan,d,spec=spec,executed_decision=d,source_context=context,
                context='consumer',deadline=context.deadline,request_id=spec.request_id)
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        assert expected in events
        assert events.count('query')==(1 if deny_check==4 else 0)
        await adapter.close()
    asyncio.run(run())


def test_unknown_post_remains_unobserved_and_is_never_retried(valid_manifest,clock):
    adapter,s,b,p,q,d,spec,lifecycle,events=arrangement(valid_manifest,clock,status=401)
    async def run():
        plan,context=await prepare(adapter,p,q,d,spec,clock)
        with pytest.raises(GatewayError):
            await adapter.poll(plan,d,spec=spec,executed_decision=d,source_context=context,
                context='consumer',deadline=context.deadline,request_id=spec.request_id)
        assert lifecycle.permit._state=='started' and lifecycle.evidence is None
        assert events.count('query')==1 and events.count('token')==1 and 'not_sent' not in events
        assert 'seal:runtime_error' in events
        await adapter.close()
    asyncio.run(run())
