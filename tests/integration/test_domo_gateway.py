"""Actual registry/policy/fixture/worker path; only final Domo HTTP is simulated.

These tests qualify gateway integration and strict transport behavior, not Domo
SQL dialect, account shares, PDP, producer semantics or live source compatibility.
"""
import asyncio
import json
from types import SimpleNamespace
import httpx
import pytest
from gateway.contracts.canonical import canonical_bytes
from gateway.contracts.clock import SystemClock
from gateway.contracts.errors import GatewayError
from gateway.contracts.query import QueryRequest

pytestmark=pytest.mark.postgres


def components(seed,*,query_status=200,on_query=None):
    from gateway.adapters.domo_serving import DomoRuntimeAdapter
    from gateway.adapters.dispatch import SourceDispatchAdapter
    from gateway.publishing.test_runner import FixtureRunner
    from gateway.publishing.service import PublishingService
    retained=seed.assets.packages.packages[0]
    manifest=retained.package.manifest
    binding=next(b for b in seed.assets.bindings if b.context=='consumer')
    catalog=seed.assets.fixtures.resolve(manifest,binding)
    calls=[]
    def factory(context):
        source=next(s for s in seed.assets.sources.sources if s.context==context)
        selected=next(b for b in seed.assets.bindings if b.context==context)
        statement_catalog=seed.assets.statements[selected.model_bundle_digest]
        answers={}
        for variant in selected.variants:
            statement=statement_catalog.resolve(variant.statement_id,variant.statement_revision,variant.statement_digest)
            case_id=statement.statement_id.removeprefix(context.replace('_','-')+'-')
            case=next(c for c in catalog.cases if c.fixture.id==case_id)
            answers[statement.text]=case.fixture.expected_rows
        async def handler(request):
            if request.url.path=='/oauth/token':
                calls.append((context,'token'))
                body=dict(access_token='synthetic-private-token',customer=source.auth.expected_customer,
                    userId=source.auth.expected_user_id,expires_in=3599,token_type='bearer',scope='data',role='Reader',jti='synthetic')
                return httpx.Response(200,headers={'content-type':'application/json'},stream=httpx.ByteStream(json.dumps(body).encode()))
            assert request.url.path=='/v1/datasets/query/execute/'+selected.dataset_id
            data=json.loads(request.content);assert set(data)=={'sql'} and data['sql'] in answers
            calls.append((context,'query'))
            if on_query: await on_query(context)
            rows=[[r['day'],r['account'],int(r['usage_events'])] for r in answers[data['sql']]]
            body=dict(datasource=selected.dataset_id,columns=['day','account','usage_events'],
                metadata=[dict(type=t,dataSourceId=selected.dataset_id,maxLength=-1,minLength=-1,periodIndex=0)
                    for t in ['STRING','STRING','LONG']],rows=rows,numRows=len(rows),numColumns=3,fromcache=False)
            return httpx.Response(query_status if context=='consumer' else 200,headers={'content-type':'application/json'},stream=httpx.ByteStream(json.dumps(body).encode()))
        def make():
            domo=DomoRuntimeAdapter(bindings=seed.bindings,sources=seed.assets.sources,statements=seed.assets.statements,
                lifecycle=seed.lifecycles[context],secret_resolver=seed.secret_resolver(context),context=context,
                http_transport=httpx.MockTransport(handler))
            return SourceDispatchAdapter(bindings=seed.bindings,domo_adapter=domo)
        return make
    runner=FixtureRunner(registry=seed.registry,policy=seed.policy,bindings=seed.bindings,bundles=seed.assets.bundles,
        catalog_provider=seed.assets.fixtures,adapter_factory=factory('fixture_test'),source_lifecycle=seed.lifecycles['fixture_test'])
    publishing=PublishingService(registry=seed.registry,policy=seed.policy,bindings=seed.bindings,
        bundles=seed.assets.bundles,fixture_runner=runner)
    return SimpleNamespace(manifest=manifest,package=retained.package,catalog=catalog,calls=calls,
        factory=factory,runner=runner,publishing=publishing)


async def publish(seed,c):
    draft=await c.publishing.save_draft(seed.principals['usage-author'],c.manifest,0)
    run=await c.publishing.test(seed.principals['usage-author'],draft.id)
    assert run.status=='passed',[(v['fixture_id'],v['actual_error']) for v in run.evidence['checks']]
    product=await c.publishing.publish(seed.principals['usage-publisher'],draft.id,c.manifest.content_digest,1,
        'Reviewed isolated synthetic gateway fixtures')
    return draft,run,product


def test_real_domo_author_fixture_publish_describe_worker_result(registry_database,tmp_path):
    from tests.integration.source_pilot_domo_seed import seed_domo_pilot
    from gateway.api.catalog import CatalogService
    from gateway.execution.service import ExecutionService
    from gateway.execution.worker import Worker
    from gateway.execution.results import ResultService
    from gateway.product_packages import build_source_check_plan
    db=registry_database
    async def run():
        async with seed_domo_pilot(db,tmp_path/'assets') as seed:
            c=components(seed);draft,test,product=await publish(seed,c)
            assert c.calls.count(('fixture_test','query'))==4
            assert db.scalar('SELECT count(*) FROM gateway.fixture_source_reservations')==0
            assert db.scalar('SELECT count(*) FROM gateway.source_preparations')==4
            assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
            assert db.scalar('SELECT count(*) FROM gateway.source_operation_groups WHERE closed AND settled_at IS NOT NULL')==1
            consumer=seed.principals['usage-consumer-all']
            description=await CatalogService(registry=seed.registry,policy=seed.policy,bindings=seed.bindings,cursors=None).describe(
                consumer,c.manifest.product_id,c.manifest.version)
            plan=build_source_check_plan(c.package)
            assert description.digest==plan.expected_product_digest
            assert canonical_bytes(description.provenance)==canonical_bytes(plan.expected_provenance)
            assert description.examples and description.links==() and description.provenance.links==()
            assert description.limits==c.manifest.limits
            assert not any(word in str(description.model_dump(mode='json')) for word in ('/run/secrets/','dataset_id','client_secret'))
            execution=ExecutionService(registry=seed.registry,policy=seed.policy,bindings=seed.bindings,
                source_lifecycle=seed.lifecycles['consumer'])
            accepted=await execution.submit(consumer,plan.request)
            worker=Worker(execution.queue,adapter_factory=c.factory('consumer'),worker_id='synthetic-worker')
            assert await worker.run_once()
            result=await ResultService(seed.registry,seed.policy,seed.bindings,SystemClock()).get(consumer,accepted.execution_id)
            assert canonical_bytes(result.rows)==canonical_bytes(plan.expected_rows)
            assert result.freshness.status=='unknown' and result.source_watermark is None
            assert c.calls.count(('consumer','query'))==1
            assert db.scalar('SELECT count(*) FROM gateway.source_preparations')==5
            assert db.scalar('SELECT count(*) FROM gateway.domo_source_sends WHERE state=\'terminal_success\'')==5
            assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
            assert db.scalar('SELECT count(*) FROM gateway.source_reservations')==0
    asyncio.run(run())


@pytest.mark.parametrize('mode',['unknown','revoked_after_read'])
def test_real_domo_unknown_and_revoked_reads_do_not_release_rows(registry_database,tmp_path,mode):
    from tests.integration.source_pilot_domo_seed import seed_domo_pilot
    from gateway.execution.service import ExecutionService
    from gateway.execution.worker import Worker
    from gateway.execution.results import ResultService
    db=registry_database
    async def on_query(context):
        if context=='consumer' and mode=='revoked_after_read':
            db.execute_owner("UPDATE gateway.principals SET active=false WHERE id='usage-consumer-all'")
    async def run():
        async with seed_domo_pilot(db,tmp_path/'assets') as seed:
            c=components(seed,query_status=401 if mode=='unknown' else 200,on_query=on_query)
            await publish(seed,c)
            consumer=seed.principals['usage-consumer-all']
            execution=ExecutionService(registry=seed.registry,policy=seed.policy,bindings=seed.bindings,
                source_lifecycle=seed.lifecycles['consumer'])
            accepted=await execution.submit(consumer,QueryRequest.model_validate(c.package.consumer_check.request))
            worker=Worker(execution.queue,adapter_factory=c.factory('consumer'),worker_id='synthetic-worker')
            await worker.run_once()
            assert c.calls.count(('consumer','query'))==1
            assert db.scalar('SELECT count(*) FROM gateway.results')==0
            held=db.scalar('SELECT count(*) FROM gateway.held_source_capacity')
            assert held==(1 if mode=='unknown' else 0)
            state=db.scalar("SELECT d.state FROM gateway.domo_source_sends d JOIN gateway.source_sessions s ON s.id=d.session_id WHERE s.context='consumer'")
            assert state==('intent' if mode=='unknown' else 'terminal_success')
            if mode=='revoked_after_read':
                with pytest.raises(GatewayError):
                    await ResultService(seed.registry,seed.policy,seed.bindings,SystemClock()).get(consumer,accepted.execution_id)
            assert not await worker.run_once()
            assert c.calls.count(('consumer','query'))==1
    asyncio.run(run())
