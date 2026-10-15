"""One genuine gateway → Cube HTTP/compiler/SDK → PostgreSQL serving journey.

Only the final Google request boundary is synthetic. Exact loopback routing is
confined to the test and does not qualify TLS, GKE identity or live Google data.
"""
import asyncio
import os
from types import SimpleNamespace
import pytest
from gateway.contracts.canonical import canonical_bytes
from gateway.contracts.clock import SystemClock

pytestmark=[pytest.mark.postgres,pytest.mark.cube]


def components(seed,routes):
    from gateway.adapters.bigquery_serving import BigQueryServingAdapter
    from gateway.adapters.dispatch import SourceDispatchAdapter
    from gateway.publishing.test_runner import FixtureRunner
    from gateway.publishing.service import PublishingService
    from tests.integration.source_pilot_bigquery_child import JourneyTransport
    retained=seed.assets.packages.packages[0];manifest=retained.package.manifest
    responses=[]
    class ObservedTransport(JourneyTransport):
        async def handle_async_request(self,request):
            response=await super().handle_async_request(request)
            responses.append(response.status_code)
            return response
    def factory(context):
        def make():
            adapter=BigQueryServingAdapter(bindings=seed.bindings,catalog=seed.assets.bundles,sources=seed.assets.sources,
                secret_resolver=seed.secret_resolver(context),transport=ObservedTransport(routes))
            return SourceDispatchAdapter(bindings=seed.bindings,bigquery_adapter=adapter)
        return make
    runner=FixtureRunner(registry=seed.registry,policy=seed.policy,bindings=seed.bindings,bundles=seed.assets.bundles,
        catalog_provider=seed.assets.fixtures,adapter_factory=factory('fixture_test'),source_lifecycle=seed.lifecycle)
    publishing=PublishingService(registry=seed.registry,policy=seed.policy,bindings=seed.bindings,
        bundles=seed.assets.bundles,fixture_runner=runner)
    return SimpleNamespace(manifest=manifest,package=retained.package,factory=factory,publishing=publishing,responses=responses)


async def gateway_journey(db,root):
    from tests.integration.source_pilot_bigquery_seed import seed_bigquery_pilot
    from tests.integration.source_pilot_bigquery_child import bigquery_child
    from gateway.api.catalog import CatalogService
    from gateway.execution.service import ExecutionService
    from gateway.execution.worker import Worker
    from gateway.execution.results import ResultService
    from gateway.product_packages import build_source_check_plan
    async with seed_bigquery_pilot(db,root) as seed:
        async with bigquery_child(seed.child_config,seed.origins) as child:
            c=components(seed,child['routes'])
            draft=await c.publishing.save_draft(seed.principals['usage-author'],c.manifest,0)
            tested=await c.publishing.test(seed.principals['usage-author'],draft.id)
            assert tested.status=='passed',(db.scalar('SELECT count(*) FROM gateway.source_sessions'),
                db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs'),
                db.scalar("SELECT count(*) FROM gateway.bigquery_source_jobs WHERE state='done'"),
                c.responses,[(v['fixture_id'],v['actual_error']) for v in tested.evidence['checks']])
            await c.publishing.publish(seed.principals['usage-publisher'],draft.id,c.manifest.content_digest,1,
                'Reviewed isolated synthetic Cube and PostgreSQL journey')
            assert db.scalar('SELECT count(*) FROM gateway.fixture_source_reservations')==0
            assert db.scalar('SELECT count(*) FROM gateway.source_sessions')==4
            assert db.scalar('SELECT count(*) FROM gateway.source_operation_groups WHERE closed AND settled_at IS NOT NULL')==1
            assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
            assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs')==8
            consumer=seed.principals['usage-consumer-all']
            description=await CatalogService(registry=seed.registry,policy=seed.policy,bindings=seed.bindings,cursors=None).describe(
                consumer,c.manifest.product_id,c.manifest.version)
            plan=build_source_check_plan(c.package)
            assert description.digest==plan.expected_product_digest
            assert canonical_bytes(description.provenance)==canonical_bytes(plan.expected_provenance)
            assert description.limits==c.manifest.limits
            assert not any(value in str(description.model_dump(mode='json')) for value in ('/run/secrets/','billing_project','iam.gserviceaccount.com'))
            execution=ExecutionService(registry=seed.registry,policy=seed.policy,bindings=seed.bindings,source_lifecycle=seed.lifecycle)
            accepted=await execution.submit(consumer,plan.request)
            worker=Worker(execution.queue,adapter_factory=c.factory('consumer'),worker_id='synthetic-bq-worker')
            assert await worker.run_once()
            result=await ResultService(seed.registry,seed.policy,seed.bindings,SystemClock()).get(consumer,accepted.execution_id)
            assert canonical_bytes(result.rows)==canonical_bytes(plan.expected_rows)
            assert result.freshness.status=='unknown' and result.source_watermark is None
            assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs WHERE state=\'done\'')==10
            assert db.scalar('SELECT count(*) FROM gateway.source_sessions WHERE sealed')==5
            assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
            assert db.scalar('SELECT count(*) FROM gateway.source_reservations')==0
            assert db.scalar('SELECT count(*) FROM gateway.source_operation_groups WHERE closed AND settled_at IS NOT NULL')==2
        assert child['result']['counts']['inserts']==10
        assert 10<=child['result']['counts']['metadata']<=10000
        assert 5<=child['result']['counts']['results']<=16
        assert child['result']['counts']['provider_errors']==0
    return child['result']['counts']


def test_real_bigquery_author_fixture_publish_describe_worker_result(registry_database,tmp_path):
    if any(name.startswith('GATEWAY_SOURCE_JOURNEY_') for name in os.environ):
        from tests.integration.source_journey_host import run_hosted_journey
        run_hosted_journey(registry_database)
    else:
        asyncio.run(gateway_journey(registry_database,tmp_path/'pilot'))
