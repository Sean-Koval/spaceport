"""Fixed progress reports follow the actual scenario's operation boundaries."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import pytest

pytestmark = pytest.mark.unit


def test_gateway_journey_reports_fixed_stage_before_each_operation(monkeypatch, tmp_path):
    from tests.integration import test_bigquery_gateway as scenario
    from tests.integration import source_pilot_bigquery_seed as seeding
    from tests.integration import source_pilot_bigquery_child as children
    from gateway.api import catalog
    from gateway.execution import service, worker, results
    from gateway import product_packages
    stages = []; observed = []; completed_worker = False

    def observed_stage(expected):
        assert stages[-1] == expected
        observed.append(expected)

    seed = NS(registry=object(), policy=object(), bindings=object(), lifecycle=object(),
        child_config=object(), origins=object(),
        principals={key: key for key in ('usage-author', 'usage-publisher', 'usage-consumer-all')})

    @asynccontextmanager
    async def seed_context(database, root):
        observed_stage('package_bootstrap')
        yield seed
        observed_stage('teardown')

    counts = dict(inserts=10, metadata=10, results=10, provider_errors=0)
    @asynccontextmanager
    async def child_context(config, origins):
        observed_stage('cube_startup')
        yield {'routes': object(), 'result': {'counts': counts}}
        observed_stage('teardown')

    class Publishing:
        async def save_draft(self, *args):
            observed_stage('fixture_test'); return NS(id='draft')
        async def test(self, *args):
            observed_stage('fixture_test'); return NS(status='passed')
        async def publish(self, *args):
            observed_stage('publish')

    manifest = NS(content_digest='digest', product_id='product', version='version', limits={})
    components = NS(manifest=manifest, package=object(), publishing=Publishing(), factory=lambda role: object())
    class Catalog:
        def __init__(self, **kwargs): pass
        async def describe(self, *args):
            observed_stage('catalog')
            return NS(digest='digest', provenance={}, limits={}, model_dump=lambda **kwargs: {})

    class Execution:
        def __init__(self, **kwargs): self.queue = object()
        async def submit(self, *args):
            observed_stage('query'); return NS(execution_id='execution')

    class Worker:
        def __init__(self, *args, **kwargs): pass
        async def run_once(self):
            nonlocal completed_worker
            observed_stage('worker'); completed_worker = True; return True

    class Result:
        def __init__(self, *args, **kwargs): pass
        async def get(self, *args):
            observed_stage('result'); return NS(rows=[], freshness=NS(status='unknown'), source_watermark=None)

    def scalar(statement):
        observed_stage('settlement')
        if 'fixture_source_reservations' in statement or 'held_source_capacity' in statement or 'source_reservations' in statement:
            return 0
        if 'source_sessions' in statement: return 5 if completed_worker else 4
        if 'source_operation_groups' in statement: return 2 if completed_worker else 1
        if 'bigquery_source_jobs' in statement: return 10 if completed_worker else 8
        raise AssertionError('Unexpected synthetic statement')

    monkeypatch.setattr(seeding, 'seed_bigquery_pilot', seed_context)
    monkeypatch.setattr(children, 'bigquery_child', child_context)
    monkeypatch.setattr(scenario, 'components', lambda *args: components)
    monkeypatch.setattr(catalog, 'CatalogService', Catalog)
    monkeypatch.setattr(service, 'ExecutionService', Execution)
    monkeypatch.setattr(worker, 'Worker', Worker)
    monkeypatch.setattr(results, 'ResultService', Result)
    monkeypatch.setattr(product_packages, 'build_source_check_plan', lambda package: NS(
        expected_product_digest='digest', expected_provenance={}, expected_rows=[], request={}))
    assert asyncio.run(scenario.gateway_journey(NS(scalar=scalar), tmp_path, stage=stages.append)) == counts
    assert stages == ['package_bootstrap', 'cube_startup', 'fixture_test', 'publish', 'settlement',
        'catalog', 'query', 'worker', 'result', 'settlement', 'teardown']
    assert set(observed) == set(stages)
