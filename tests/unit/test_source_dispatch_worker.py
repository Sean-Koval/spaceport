"""Source-aware worker routing with explicit unit lifecycle/queue doubles."""
import asyncio
from datetime import timedelta
import importlib
from types import SimpleNamespace
import pytest
from gateway.contracts.errors import GatewayError
from gateway.source_lifecycle import SourceAttemptContext
from tests.unit.test_domo_serving import arrangement

pytestmark=pytest.mark.unit


def api():
    spec=importlib.util.find_spec('gateway.adapters.dispatch')
    assert spec is not None,'Source dispatcher is missing'
    return importlib.import_module(spec.name)


def test_dispatcher_requires_an_explicit_provider_and_closes_configured_client(valid_manifest,clock):
    with pytest.raises(ValueError): api().SourceDispatchAdapter(bindings=None)
    adapter,s,b,p,q,d,spec,lifecycle,events=arrangement(valid_manifest,clock)
    dispatch=api().SourceDispatchAdapter(bindings=adapter.bindings,domo_adapter=adapter)
    async def run():
        plan=await dispatch.prepare(p,q,d,context='consumer',deadline=clock.now()+timedelta(seconds=30),request_id=spec.request_id)
        session=await dispatch.source_plan(spec,plan,d)
        assert session.provider=='domo' and events==[]
        await dispatch.close()
    asyncio.run(run())


def test_worker_admits_stable_source_context_before_the_single_domo_post(valid_manifest,clock):
    from gateway.execution.worker import Worker
    adapter,s,b,p,q,d,spec,lifecycle,events=arrangement(valid_manifest,clock)
    dispatch=api().SourceDispatchAdapter(bindings=adapter.bindings,domo_adapter=adapter)
    claim=SimpleNamespace(spec=spec,executed_decision=d,binding=b,prepared=None,
        deadline=clock.now()+timedelta(seconds=30),accepted_at=clock.now(),execution_id='consumer-execution')
    class Queue:
        def __init__(self): self.result=None;self.failure=None;self.admitted=False
        async def claim(self,worker_id): return claim
        async def gate(self,claimed): events.append('queue_gate');return d
        async def save_prepared(self,*args): raise AssertionError('Domo must use typed source preparation')
        async def source_attempt(self,claimed,prepared,current,session_spec):
            assert current==d and not self.admitted
            self.admitted=True;events.append('source_attempt')
            return SourceAttemptContext('00000000-0000-0000-0000-000000000010',
                '00000000-0000-0000-0000-000000000011',1,'d'*64,'e'*64,claim.deadline,session_spec)
        async def complete(self,claimed,result): self.result=result;events.append('queue_complete')
        async def fail(self,claimed,code,**kwargs): self.failure=code
        async def renew(self,claimed): return True
    queue=Queue()
    asyncio.run(Worker(queue,adapter_factory=lambda:dispatch,worker_id='worker',clock=clock).run_once())
    assert queue.failure is None and queue.result is not None and queue.admitted
    assert events.index('source_attempt')<events.index('token')<events.index('query')<events.index('queue_complete')
    assert events.count('query')==1


@pytest.mark.parametrize('endpoint_revision',['cube-rest-v1','cube-rest-v2'])
def test_dispatch_preserves_accepted_spec_for_swapped_cube_endpoint(clock,endpoint_revision):
    from gateway.adapters.base import StaticBindingResolver
    from gateway.adapters.bundles import BundleCatalog
    from gateway.contracts.canonical import canonical_digest
    from gateway.contracts.execution import ExecutionSpec,PreparedQuery
    from tests.contract.test_cube_adapter import synthetic,adapter,upstream_response,meta
    binding,product,decision,request=synthetic()
    from gateway.contracts.query import validate_query
    request=validate_query(product.manifest,request)
    query=request.model_dump(mode='json',exclude={'idempotency_key'})
    spec=ExecutionSpec(principal_id=decision.principal_id,pinned_product=product,canonical_query=query,
        canonical_hash=canonical_digest(query),policy_revision=1,context='consumer',request_id='accepted')
    calls=[]
    def handler(req):
        calls.append(req)
        return upstream_response(200,json=meta())
    catalog=BundleCatalog({'swapped':dict(endpoint_id='swapped',adapter_revision=endpoint_revision,
        url='https://cube.invalid',bundle_digest='b'*64,secret_ref='/run/secrets/cube',context='consumer')})
    cube=adapter(handler,clock,binding=binding,catalog=catalog)
    dispatch=api().SourceDispatchAdapter(bindings=StaticBindingResolver([binding]),cube_adapter=cube)
    prepared=PreparedQuery(endpoint_id='swapped',model_bundle_digest='b'*64,cube_query={
        'measures':['Orders.revenue'],'dimensions':['Orders.segment'],
        'timeDimensions':[{'dimension':'Orders.ordered_at','granularity':'day'}],
        'filters':[],'order':[],'timezone':'America/New_York','limit':10})
    async def run():
        with pytest.raises(GatewayError):
            await dispatch.poll(prepared,decision,spec=spec,executed_decision=decision,
                deadline=clock.now()+timedelta(seconds=30),request_id='accepted')
        await dispatch.close()
    asyncio.run(run())
    assert calls==[]
