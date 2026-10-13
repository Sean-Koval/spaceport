"""Real PostgreSQL publishing transactions. No in-memory database substitute."""
import asyncio
from copy import deepcopy
from pathlib import Path
import importlib
import json
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb
from sqlalchemy import select

from gateway.adapters.base import StaticBindingResolver
from gateway.adapters.bundles import BundleCatalog
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.manifest import validate_manifest
from gateway.registry.models import BindingRevision, TestRun as RegistryTestRun
from tests.contract.test_cube_adapter import synthetic
from tests.integration.test_audit import seed as seed_policy, service as policy_service, principal

pytestmark = pytest.mark.postgres


def api():
    module=importlib.import_module('gateway.publishing.service')
    assert hasattr(module,'PublishingService'), 'PublishingService is not implemented'
    return module


async def setup(database, *, roles=('author','publisher')):
    seed_policy(database, roles=roles)
    binding,product,_,_=synthetic()
    async with database.registry.transaction() as uow:
        uow.session.add(BindingRevision(id='binding-1',binding_id=binding.binding_id,revision=binding.revision,
            content=binding.model_dump(mode='json'),content_digest=binding.digest,
            model_bundle_digest=binding.model_bundle_digest,adapter_revision=binding.adapter_revision,actor_id='operator'))
    return api().PublishingService(registry=database.registry,policy=policy_service(database),
        bindings=StaticBindingResolver([binding]),bundles=BundleCatalog.from_file('cube/bundles.json')),product.manifest


def test_save_draft_is_authorized_and_compare_and_swap(registry_database):
    async def scenario():
        service,manifest=await setup(registry_database)
        first=await service.save_draft(principal(),manifest,0)
        assert first.revision == 1
        with pytest.raises(GatewayError) as caught: await service.save_draft(principal(),manifest,0)
        assert caught.value.code == 'CONFLICT'
        assert registry_database.scalar('SELECT count(*) FROM gateway.draft_revisions') == 1
    asyncio.run(scenario())


def test_authorized_diff_is_field_readable_and_missing_evidence_is_explicit(registry_database):
    async def scenario():
        service,manifest=await setup(registry_database)
        draft=await service.save_draft(principal(),manifest,0)
        review=await service.diff(principal(),draft.id)
        assert review.before_digest is None and review.after_digest == manifest.content_digest
        assert any(c.path == '/description' and c.kind == 'added' for c in review.changes)
        assert review.stale_evidence == ('missing',)
    asyncio.run(scenario())


def test_no_publication_without_current_permission_negative_evidence(registry_database):
    async def scenario():
        service,manifest=await setup(registry_database)
        draft=await service.save_draft(principal(),manifest,0)
        async with registry_database.registry.transaction() as uow:
            uow.session.add(RegistryTestRun(product_id='revenue',draft_id=draft.id,draft_digest=draft.content_digest,
                binding_id='binding-1',binding_digest=synthetic()[0].digest,policy_id=manifest.policy_id,
                policy_revision=1,fixture_version='1',evidence={'known_answer':'pass'},status='passed',actor_id=principal().id))
        with pytest.raises(GatewayError) as caught:
            await service.publish(principal(),draft.id,draft.content_digest,1,'Reviewed known answers')
        assert caught.value.code == 'CONFLICT'
        assert registry_database.scalar('SELECT count(*) FROM gateway.product_versions') == 0
    asyncio.run(scenario())


def test_binding_change_is_detected_after_authorization(registry_database):
    async def scenario():
        service,manifest=await setup(registry_database)
        draft=await service.save_draft(principal(),manifest,0)
        service.bindings=StaticBindingResolver([])
        with pytest.raises(GatewayError) as caught:
            await service.publish(principal(),draft.id,draft.content_digest,1,'Reviewed')
        assert caught.value.code == 'CONFLICT'
    asyncio.run(scenario())


async def fixture_setup(database, *, roles=('author','publisher'), manifest_changes=None):
    from gateway.publishing.test_runner import ApprovedFixture, FixtureCatalog, FixtureRunner, execution_contract_digest
    from gateway.contracts.manifest import Fixture
    from gateway.contracts.execution import Completed, PreparedQuery
    from datetime import datetime,timezone
    service,manifest=await setup(database,roles=roles)
    if manifest_changes:
        raw=manifest.model_dump(mode='json',exclude={'content_digest'});raw.update(manifest_changes)
        manifest=validate_manifest(raw)
    binding=synthetic()[0]
    fixture_binding=binding.model_copy(update={'binding_id':'fixture-revenue-orders','context':'fixture_test',
        'model_bundle_digest':service.bundles.resolve('8c3e9a8b030fb495bbdbbec19061df43c17d508ffadaf390a025d19120e43f17').bundle_digest})
    async with database.registry.transaction() as uow:
        uow.session.add(BindingRevision(id='fixture-binding-1',binding_id=fixture_binding.binding_id,
            revision=1,content=fixture_binding.model_dump(mode='json'),content_digest=fixture_binding.digest,
            model_bundle_digest=fixture_binding.model_bundle_digest,adapter_revision=fixture_binding.adapter_revision,actor_id='operator'))
    for identity,tenant in [('fixture-north','north'),('fixture-south','south'),('fixture-denied','north')]:
        database.execute_owner('INSERT INTO gateway.principals(id,subject,kind) VALUES (%s,%s,\'demo\')',(identity,identity))
        if identity != 'fixture-denied':
            database.execute_owner("INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) VALUES (%s,'revenue-access',%s,'[\"consumer\"]',%s)",(identity,tenant,Jsonb({'groups':[tenant]})))
    cases=[ApprovedFixture(fixture=f,principal_id='fixture-south' if f.id=='south-total' else 'fixture-north',
        kind='input_negative' if f.expected_error else 'known_answer') for f in manifest.fixtures]
    cases.append(ApprovedFixture(fixture=Fixture(id='permission-denied',version='1',inputs={'start':'2024-03-09','end':'2024-03-12'},
        expected_error='NOT_FOUND_OR_FORBIDDEN'),principal_id='fixture-denied',kind='permission_negative'))
    catalog=FixtureCatalog(product_id='revenue',consumer_binding=binding,fixture_binding=fixture_binding,
        execution_contract_digest=execution_contract_digest(manifest),cases=cases)
    calls=[]
    # A test-only source-load watermark captured once, before any source call.
    # The real Cube/source-load gate is separate and cannot be claimed here.
    source_load_watermark=database.scalar('SELECT clock_timestamp()')
    class Adapter:
        async def prepare(self,product,request,decision,**kwargs):
            self.request=request;self.decision=decision
            calls.append(('prepare',kwargs['context'],decision.principal_id))
            return PreparedQuery(endpoint_id='synthetic-fixture',model_bundle_digest=fixture_binding.model_bundle_digest,cube_query={})
        async def poll(self,prepared,decision,**kwargs):
            calls.append(('poll',kwargs['context'],decision.principal_id))
            fixture=next(c.fixture for c in cases if c.kind=='known_answer' and c.principal_id==decision.principal_id
                and c.fixture.inputs['start']==self.request.inputs['start'])
            rows=[{fixture_binding.fields[k].response_key:v for k,v in row.items()} for row in fixture.expected_rows]
            return Completed(raw_rows=rows,source_watermark=source_load_watermark)
        async def close(self): pass
    runner=FixtureRunner(registry=database.registry,policy=service.policy,
        bindings=StaticBindingResolver([binding,fixture_binding]),bundles=service.bundles,
        catalog_provider=lambda:catalog,adapter_factory=Adapter)
    service.fixture_runner=runner
    return service,manifest,catalog,calls


def test_isolated_fixture_evidence_publishes_original_digest(registry_database):
    async def scenario():
        service,manifest,catalog,calls=await fixture_setup(registry_database)
        draft=await service.save_draft(principal(),manifest,0)
        run=await service.test(principal(),draft.id)
        assert run.status=='passed' and run.draft_digest==manifest.content_digest
        assert all(call[1]=='fixture_test' for call in calls)
        assert all(call[2] != principal().id for call in calls)
        assert registry_database.scalar('SELECT count(*) FROM gateway.executions')==0
        assert registry_database.scalar('SELECT count(*) FROM gateway.fixture_source_reservations WHERE released_at IS NULL')==0
        for check in run.evidence['checks']:
            if check['kind']=='known_answer':
                assert registry_database.scalar('SELECT check_digest FROM gateway.fixture_attempt_states WHERE attempt_id=%s',(check['attempt_id'],))==canonical_digest(check)
        assert registry_database.constraint_failure("UPDATE gateway.fixture_attempt_states SET check_digest=repeat('a',64) WHERE state='succeeded'")=='23514'
        assert registry_database.denied_as_app('DELETE FROM gateway.fixture_attempt_states')=='permission_denied'
        assert any(c['kind']=='permission_negative' and c['actual_error']=='NOT_FOUND_OR_FORBIDDEN' for c in run.evidence['checks'])
        product=await service.publish(principal(),draft.id,manifest.content_digest,1,'Verified isolated known answers')
        assert product.manifest.content_digest==manifest.content_digest
        assert product.binding_digest==catalog.consumer_binding.digest
        assert registry_database.scalar('SELECT reason FROM gateway.publication_events')=='Verified isolated known answers'
    asyncio.run(scenario())


def test_edit_after_green_evidence_requires_retest(registry_database):
    async def scenario():
        service,manifest,_,_=await fixture_setup(registry_database)
        draft=await service.save_draft(principal(),manifest,0)
        assert (await service.test(principal(),draft.id)).status=='passed'
        raw=manifest.model_dump(mode='json',exclude={'content_digest'});raw['description']='Changed after testing'
        await service.save_draft(principal(),validate_manifest(raw),1)
        with pytest.raises(GatewayError) as caught:
            await service.publish(principal(),draft.id,draft.content_digest,1,'Stale review')
        assert caught.value.code=='CONFLICT'
        assert 'draft' in (await service.diff(principal(),draft.id)).stale_evidence
    asyncio.run(scenario())


def test_two_publishers_race_exact_tested_digest(registry_database):
    async def scenario():
        service,manifest,_,_=await fixture_setup(registry_database)
        draft=await service.save_draft(principal(),manifest,0)
        assert (await service.test(principal(),draft.id)).status=='passed'
        async def publish():
            try:
                await service.publish(principal(),draft.id,draft.content_digest,1,'Reviewed concurrent publish')
                return 'committed'
            except GatewayError as error:return error.code.value
        assert sorted(await asyncio.wait_for(asyncio.gather(publish(),publish()),5))==['CONFLICT','committed']
        assert registry_database.scalar('SELECT count(*) FROM gateway.publication_events')==1
    asyncio.run(scenario())


def test_lifecycle_reasons_are_immutable_and_withdrawal_is_immediate(registry_database):
    async def scenario():
        service,manifest,_,_=await fixture_setup(registry_database)
        draft=await service.save_draft(principal(),manifest,0)
        await service.test(principal(),draft.id)
        product=await service.publish(principal(),draft.id,draft.content_digest,1,'Published after review')
        await service.transition(principal(),'revenue','1.0.0','deprecate','New version recommended')
        assert registry_database.scalar('SELECT status FROM gateway.version_states')=='deprecated'
        await service.transition(principal(),'revenue','1.0.0','withdraw','Incorrect business definition')
        with pytest.raises(GatewayError) as caught: await registry_database.registry.resolve('revenue')
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        assert registry_database.scalar('SELECT count(*) FROM gateway.publication_events')==3
        assert registry_database.denied_as_app("UPDATE gateway.publication_events SET reason='hidden'")=='permission_denied'
        assert registry_database.denied_as_owner("UPDATE gateway.test_runs SET status='failed'")=='permission_denied'
    asyncio.run(scenario())


@pytest.mark.parametrize('changed',['policy','positive_grant','negative_grant','fixture_binding','fixture_version'])
def test_current_publication_evidence_is_invalidated_by_authority_changes(registry_database,changed):
    async def scenario():
        from gateway.adapters.base import StaticBindingResolver
        from tests.unit.test_policy import rules
        service,manifest,catalog,_=await fixture_setup(registry_database,roles=('author','publisher','policy_admin'))
        draft=await service.save_draft(principal(),manifest,0)
        assert (await service.test(principal(),draft.id)).status=='passed'
        if changed=='policy':
            await service.policy.change_policy(principal(),manifest.policy_id,rules(),'Reviewed new policy revision')
        elif changed=='positive_grant':
            registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false WHERE principal_id='fixture-north'")
        elif changed=='negative_grant':
            registry_database.execute_owner("INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) VALUES ('fixture-denied','revenue-access','north','[\"consumer\"]','{\"groups\":[\"north\"]}')")
        elif changed=='fixture_binding':
            service.fixture_runner.bindings=StaticBindingResolver([catalog.consumer_binding])
        else:
            cases=[c.model_copy(update={'fixture':c.fixture.model_copy(update={'version':'2'})}) for c in catalog.cases]
            service.fixture_runner.catalog_provider=lambda:catalog.model_copy(update={'cases':tuple(cases)})
        with pytest.raises(GatewayError) as caught:
            await service.publish(principal(),draft.id,draft.content_digest,1,'Review is stale')
        assert caught.value.code=='CONFLICT'
        assert registry_database.scalar('SELECT count(*) FROM gateway.product_versions')==0
        causes=(await service.diff(principal(),draft.id)).stale_evidence
        assert ('policy' if changed in {'policy','positive_grant','negative_grant'} else 'fixtures') in causes
    asyncio.run(scenario())


def test_failed_fixture_values_are_persisted_before_passing_checks(registry_database):
    async def scenario():
        from gateway.contracts.execution import Completed
        service,manifest,catalog,_=await fixture_setup(registry_database)
        original=service.fixture_runner.adapter_factory
        class WrongAnswer(original):
            async def poll(self,*args,**kwargs):
                completed=await super().poll(*args,**kwargs)
                rows=[dict(row) for row in completed.raw_rows]
                if rows: rows[0]['Orders.revenue']='0.00'
                return completed.model_copy(update={'raw_rows':tuple(rows)})
        service.fixture_runner.adapter_factory=WrongAnswer
        draft=await service.save_draft(principal(),manifest,0)
        run=await service.test(principal(),draft.id)
        assert run.status=='failed'
        checks=run.evidence['checks']
        assert not checks[0]['passed']
        north=next(c for c in checks if c['fixture_id']=='north-total')
        assert north['expected'][0]['revenue']=='10.10' and north['actual'][0]['revenue']=='0.00'
        assert north['absolute_tolerance']=='0.00' and north['relative_tolerance']=='0'
        assert north['reference_watermark']=='2024-03-12T00:00:00Z'
        assert north['observed_source_load_watermark'] is not None
        assert registry_database.scalar('SELECT count(*) FROM gateway.executions')==0
        with pytest.raises(GatewayError) as caught:
            await service.publish(principal(),draft.id,draft.content_digest,1,'Bad answers')
        assert caught.value.code=='CONFLICT'
    asyncio.run(scenario())


@pytest.mark.parametrize('source_error',['UPSTREAM_UNAVAILABLE','STALE_DATA','FRESHNESS_UNKNOWN','BINDING_DRIFT','RESOURCE_LIMIT'])
def test_failed_source_distinguishes_completed_rejection_from_uncertain_capacity(registry_database,source_error):
    async def scenario():
        from gateway.contracts.execution import Completed
        from datetime import timedelta
        service,manifest,catalog,_=await fixture_setup(registry_database)
        original=service.fixture_runner.adapter_factory
        class Unavailable(original):
            async def poll(self,*args,**kwargs):
                if source_error=='UPSTREAM_UNAVAILABLE':
                    raise GatewayError(source_error,'fixture-source','Source unavailable')
                completed=await super().poll(*args,**kwargs)
                if source_error=='RESOURCE_LIMIT':
                    return completed.model_copy(update={'raw_rows':({'Orders.revenue':'0.00'},)*(manifest.limits.max_rows+1)})
                if source_error=='BINDING_DRIFT':
                    return completed.model_copy(update={'raw_rows':({'Orders.revenue':'invalid-decimal'},)})
                watermark=None if source_error=='FRESHNESS_UNKNOWN' else completed.source_watermark-timedelta(days=2)
                return completed.model_copy(update={'source_watermark':watermark})
        service.fixture_runner.adapter_factory=Unavailable
        draft=await service.save_draft(principal(),manifest,0)
        run=await service.test(principal(),draft.id)
        assert run.status=='failed'
        assert any(c['actual_error']==source_error for c in run.evidence['checks'])
        if source_error=='UPSTREAM_UNAVAILABLE':
            assert registry_database.scalar('SELECT count(*) FROM gateway.fixture_source_reservations WHERE released_at IS NULL')==2
            assert registry_database.scalar('SELECT count(*) FROM gateway.fixture_attempt_states WHERE source_completed')==0
        else:
            known=sum(case.kind=='known_answer' for case in catalog.cases)
            assert registry_database.scalar('SELECT count(*) FROM gateway.fixture_source_reservations WHERE released_at IS NULL')==0
            assert registry_database.scalar("SELECT count(*) FROM gateway.fixture_attempt_states WHERE source_completed AND state='failed' AND check_digest IS NULL AND error_code=%s",(source_error,))==known
            rejected=[c for c in run.evidence['checks'] if c['kind']=='known_answer']
            assert len(rejected)==known and all(not c['passed'] and c['actual']==[] and c['attempt_id'] for c in rejected)
            assert registry_database.scalar("SELECT count(*) FROM gateway.fixture_source_reservations r JOIN gateway.fixture_attempt_states s ON s.attempt_id=r.attempt_id WHERE r.released_at IS NOT NULL AND r.release_reason='completed' AND s.source_completed AND s.state='failed'")==known
            with pytest.raises(GatewayError) as caught:
                await service.publish(principal(),draft.id,draft.content_digest,1,'Rejected completed source')
            assert caught.value.code=='CONFLICT'
            service.fixture_runner.adapter_factory=original
            corrected=await service.test(principal(),draft.id)
            assert corrected.status=='passed' and corrected.id!=run.id
            assert registry_database.scalar('SELECT count(*) FROM gateway.fixture_source_reservations WHERE released_at IS NULL')==0
            assert registry_database.scalar('SELECT status FROM gateway.test_runs WHERE id=%s',(run.id,))=='failed'
        assert registry_database.scalar('SELECT count(*) FROM gateway.executions')==0
    asyncio.run(scenario())


def test_policy_change_while_publication_waits_is_rechecked(registry_database):
    async def scenario():
        from gateway.policy.service import PolicyContent
        from gateway.registry.models import PolicyRevision
        from tests.unit.test_policy import rules
        service,manifest,_,_=await fixture_setup(registry_database)
        draft=await service.save_draft(principal(),manifest,0)
        await service.test(principal(),draft.id)
        async with registry_database.registry.transaction() as writer:
            head=await writer.lock_policy_head(manifest.policy_id)
            document=PolicyContent(rules=rules(),reason='Changed during review').model_dump(mode='json')
            writer.session.add(PolicyRevision(id='new-policy',policy_id=manifest.policy_id,revision=2,
                content=document,content_digest=canonical_digest(document),actor_id='operator'))
            await writer.session.flush();head.current_revision=2
            from sqlalchemy import text
            transaction_id=await writer.session.scalar(text('SELECT pg_current_xact_id()::text'))
            publishing=asyncio.create_task(service.publish(principal(),draft.id,draft.content_digest,1,'Concurrent review'))
            # Observe an actual transaction-ID lock wait, not a scheduling delay.
            async with asyncio.timeout(3):
                while not registry_database.scalar("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='transactionid' AND transactionid::text=%s AND NOT granted)",(transaction_id,)):
                    assert not publishing.done()
                    await asyncio.sleep(0.01)
        with pytest.raises(GatewayError) as caught: await asyncio.wait_for(publishing,5)
        assert caught.value.code=='CONFLICT'
        assert registry_database.scalar('SELECT count(*) FROM gateway.product_versions')==0
    asyncio.run(scenario())


def test_rollback_requires_retained_bundle_and_preserves_published_manifests(registry_database):
    async def scenario():
        from gateway.adapters.bundles import BundleCatalog
        service,manifest,_,_=await fixture_setup(registry_database)
        first=await service.save_draft(principal(),manifest,0)
        await service.test(principal(),first.id)
        first_product=await service.publish(principal(),first.id,first.content_digest,1,'First tested release')
        raw=manifest.model_dump(mode='json',exclude={'content_digest'});raw['version']='1.1.0'
        second=await service.save_draft(principal(),validate_manifest(raw),1)
        await service.test(principal(),second.id)
        second_product=await service.publish(principal(),second.id,second.content_digest,2,'Second tested release')
        await service.transition(principal(),'revenue','1.0.0','rollback','Return to compatible release')
        assert (await service.registry.resolve('revenue')).version_id==first_product.version_id
        await service.transition(principal(),'revenue','1.1.0','rollback','Restore latest compatible release')
        assert (await service.registry.resolve('revenue')).version_id==second_product.version_id
        current=service.bundles
        fixture=current.resolve('8c3e9a8b030fb495bbdbbec19061df43c17d508ffadaf390a025d19120e43f17')
        service.bundles=BundleCatalog({'fixture':fixture.model_dump(mode='json')})
        with pytest.raises(GatewayError) as caught:
            await service.transition(principal(),'revenue','1.0.0','rollback','Bundle no longer retained')
        assert caught.value.code=='BINDING_DRIFT'
        assert (await service.registry.resolve('revenue')).version_id==second_product.version_id
        assert registry_database.scalar('SELECT count(*) FROM gateway.publication_events')==4
        assert registry_database.denied_as_owner("UPDATE gateway.product_versions SET manifest='{}'")=='permission_denied'
    asyncio.run(scenario())


def test_fixture_deadline_is_database_owned_and_uses_tighter_current_limit(registry_database):
    async def scenario():
        from tests.unit.test_policy import rules
        service,manifest,catalog,_=await fixture_setup(registry_database,
            roles=('author','publisher','policy_admin'),manifest_changes={'limits':{'execution_timeout_seconds':10}})
        await service.policy.change_policy(principal(),manifest.policy_id,rules(limits={'execution_timeout_seconds':7}),
            'Tighter reviewed execution deadline')
        draft=await service.save_draft(principal(),manifest,0)
        run=await service.test(principal(),draft.id)
        assert run.status=='passed'
        assert registry_database.scalar("SELECT bool_and(deadline_at=created_at+interval '7 seconds') FROM gateway.fixture_attempts")
        # The application cannot extend an immutable attempt after acceptance.
        assert registry_database.denied_as_app("UPDATE gateway.fixture_attempts SET deadline_at=deadline_at+interval '1 hour'")=='permission_denied'
        # A new direct-SQL attempt cannot choose its own acceptance/deadline time.
        columns='product_id,draft_id,draft_digest,consumer_binding_id,consumer_binding_digest,fixture_binding_id,fixture_binding_digest,fixture_manifest,fixture_manifest_digest,execution_contract_digest,policy_id,policy_revision,catalog_digest,fixture_id,fixture_version,actor_id,fixed_principal_id,principal_snapshot,decision,canonical_query,canonical_hash,request_id'
        registry_database.execute_app(f"INSERT INTO gateway.fixture_attempts(id,runner_token,deadline_at,{columns}) SELECT 'direct-extension','direct-token',deadline_at+interval '1 hour',{columns} FROM gateway.fixture_attempts LIMIT 1")
        assert registry_database.scalar("SELECT deadline_at=created_at+interval '7 seconds' FROM gateway.fixture_attempts WHERE id='direct-extension'")
        altered=columns.replace('decision',"jsonb_set(decision,'{limits,execution_timeout_seconds}','120'::jsonb)")
        assert registry_database.constraint_failure(f"INSERT INTO gateway.fixture_attempts(id,runner_token,deadline_at,{columns}) SELECT 'extended-limit','extended-token',deadline_at,{altered} FROM gateway.fixture_attempts LIMIT 1")=='23514'
        # Bad typed decisions are rejected before arithmetic/casts can extend it.
        for value in ('0','121','null','\"7\"','7.5'):
            altered=columns.replace('decision',f"jsonb_set(decision,'{{limits,execution_timeout_seconds}}','{value}'::jsonb)")
            assert registry_database.constraint_failure(f"INSERT INTO gateway.fixture_attempts(id,runner_token,deadline_at,{columns}) SELECT 'bad-timeout','bad-token',deadline_at,{altered} FROM gateway.fixture_attempts LIMIT 1")=='23514'
    asyncio.run(scenario())


def test_tightened_fixture_capacity_is_shared_and_charged_to_author(registry_database):
    async def scenario():
        from tests.unit.test_policy import rules
        service,manifest,_,_=await fixture_setup(registry_database,roles=('author','publisher','policy_admin'))
        await service.policy.change_policy(principal(),manifest.policy_id,rules(limits={'per_principal_concurrency':1}),
            'One source slot per submitting author')
        original=service.fixture_runner.adapter_factory
        class Uncertain(original):
            async def poll(self,*args,**kwargs): raise GatewayError('UPSTREAM_UNAVAILABLE','uncertain','Unavailable')
        service.fixture_runner.adapter_factory=Uncertain
        draft=await service.save_draft(principal(),manifest,0)
        run=await service.test(principal(),draft.id)
        assert run.status=='failed'
        assert registry_database.scalar('SELECT count(*) FROM gateway.fixture_attempts')==1
        assert registry_database.scalar("SELECT count(*) FROM gateway.held_source_capacity WHERE principal_id='north-user'")==1
        assert registry_database.scalar("SELECT count(*) FROM gateway.held_source_capacity WHERE principal_id='fixture-north'")==0
        assert any(c['actual_error']=='RESOURCE_LIMIT' for c in run.evidence['checks'])
    asyncio.run(scenario())


def test_deprecation_warns_and_withdrawal_blocks_new_and_stored_output(registry_database,valid_manifest):
    async def scenario():
        from tests.integration.test_results import seeded_result
        from gateway.execution.service import ExecutionService
        from gateway.contracts.query import QueryRequest
        ctx=await seeded_result(registry_database,valid_manifest,count=1)
        registry_database.execute_owner("UPDATE gateway.principal_grants SET roles='[\"consumer\",\"publisher\",\"policy_admin\"]'::jsonb WHERE principal_id='north-user'")
        service=api().PublishingService(registry=registry_database.registry,policy=ctx.policy,
            bindings=StaticBindingResolver([ctx.binding]),bundles=BundleCatalog.from_file('cube/bundles.json'))
        await service.transition(principal(),'revenue','1.0.0','deprecate','A newer version is recommended')
        result=await ctx.service.get(principal(),ctx.execution.id)
        assert any('deprecated' in warning.lower() for warning in result.warnings)
        assert result.rows==ctx.envelope.rows
        await service.transition(principal(),'revenue','1.0.0','withdraw','Answer is no longer supported')
        with pytest.raises(GatewayError) as caught: await ctx.service.get(principal(),ctx.execution.id)
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        consumer=ExecutionService(registry=registry_database.registry,policy=ctx.policy,bindings=service.bindings)
        with pytest.raises(GatewayError) as caught:
            await consumer.submit(principal(),QueryRequest(product_id='revenue',version='1.0.0',
                inputs={'start':'2024-03-09','end':'2024-03-12'}))
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        assert registry_database.scalar('SELECT count(*) FROM gateway.publication_events')==2
    asyncio.run(scenario())


# Both real-service gates are required for this case. No adapter is mocked.
from test_cube_compatibility import cube_stack


@pytest.mark.cube
def test_real_isolated_cube_fixture_run_publishes_exact_original_digest(registry_database,cube_stack):
    async def scenario():
        from gateway.adapters.cube import CubeAdapter
        from gateway.publishing.test_runner import FixtureCatalog
        from gateway.contracts.clock import SystemClock
        service,manifest,catalog,_=await fixture_setup(registry_database)
        approved=FixtureCatalog.model_validate(json.loads(Path('fixtures/products/revenue.tests.json').read_text()))
        assert catalog.digest==approved.digest
        records=json.loads(Path('cube/bundles.json').read_text())
        if cube_stack.host_mode:
            for record in records.values():
                name='cube-test' if record['context']=='fixture_test' else 'cube'
                record['url']=f'http://{cube_stack.hosts[name]}:4000'
        bundles=BundleCatalog(records)
        resolver=StaticBindingResolver([catalog.consumer_binding,catalog.fixture_binding])
        clock=SystemClock()
        service.bundles=bundles
        runner=service.fixture_runner;runner.bundles=bundles;runner.clock=clock
        runner.adapter_factory=lambda:CubeAdapter(catalog=bundles,bindings=resolver,clock=clock,
            secret_resolver=lambda reference:(cube_stack.secrets/Path(reference).name).read_text().strip())
        draft=await service.save_draft(principal(),manifest,0)
        run=await service.test(principal(),draft.id)
        assert run.status=='passed'
        assert run.draft_digest==manifest.content_digest and run.fixture_version==approved.digest
        assert registry_database.scalar('SELECT count(*) FROM gateway.executions')==0
        assert registry_database.scalar('SELECT count(*) FROM gateway.fixture_source_reservations WHERE released_at IS NULL')==0
        assert all(check['observed_source_load_watermark'] and check['freshness_status']=='fresh'
            for check in run.evidence['checks'] if check['kind']=='known_answer')
        pinned=await service.publish(principal(),draft.id,manifest.content_digest,1,'Actual isolated Cube known answers verified')
        assert pinned.manifest.content_digest==manifest.content_digest
        assert pinned.binding_digest==catalog.consumer_binding.digest
        cube_stack.capture('published-fixture-evidence.json',{'product_digest':pinned.manifest.content_digest,
            'catalog_digest':run.fixture_version,'status':run.status,'checks':run.evidence['checks']})
    asyncio.run(scenario())


@pytest.mark.parametrize('changed',['missing_attempt','other_case_attempt','wrong_actor','altered_observation'])
def test_publication_rejects_plausible_checks_without_exact_attempt_provenance(registry_database,changed):
    async def scenario():
        service,manifest,_,_=await fixture_setup(registry_database)
        draft=await service.save_draft(principal(),manifest,0)
        original=await service.test(principal(),draft.id)
        assert original.status=='passed'
        evidence=deepcopy(original.evidence)
        checks=[c for c in evidence['checks'] if c['kind']=='known_answer']
        if changed=='missing_attempt': checks[0]['attempt_id']='never-executed'
        if changed=='other_case_attempt': checks[0]['attempt_id']=checks[1]['attempt_id']
        if changed=='altered_observation': checks[0]['observed_source_load_watermark']='2024-03-12T00:00:00Z'
        async with registry_database.registry.transaction() as uow:
            uow.session.add(RegistryTestRun(product_id='revenue',draft_id=draft.id,draft_digest=draft.content_digest,
                binding_id=original.binding_id,binding_digest=original.binding_digest,policy_id=manifest.policy_id,
                policy_revision=original.policy_revision,fixture_version=original.fixture_version,evidence=evidence,
                status='passed',actor_id='operator' if changed=='wrong_actor' else principal().id))
        with pytest.raises(GatewayError) as caught:
            await service.publish(principal(),draft.id,draft.content_digest,1,'Forged attempt provenance')
        assert caught.value.code=='CONFLICT'
        assert registry_database.scalar('SELECT count(*) FROM gateway.product_versions')==0
        assert 'fixtures' in (await service.diff(principal(),draft.id)).stale_evidence
    asyncio.run(scenario())


def test_url_bound_draft_update_rejects_id_substitution_and_stale_revision(registry_database):
    async def scenario():
        service,manifest=await setup(registry_database)
        first=await service.save_draft(principal(),manifest,0)
        raw=manifest.model_dump(mode='json',exclude={'content_digest'});raw['description']='A reviewed explanation'
        changed=validate_manifest(raw)
        for identity in ('unknown-draft',):
            with pytest.raises(GatewayError) as caught: await service.update_draft(principal(),identity,changed,1)
            assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        other_raw=deepcopy(raw);other_raw['product_id']='other-revenue'
        other=await service.save_draft(principal(),validate_manifest(other_raw),0)
        with pytest.raises(GatewayError) as caught: await service.update_draft(principal(),other.id,changed,1)
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        second=await service.update_draft(principal(),first.id,changed,1)
        assert second.revision==2 and second.product_id==manifest.product_id
        with pytest.raises(GatewayError) as caught: await service.update_draft(principal(),first.id,changed,2)
        assert caught.value.code=='CONFLICT'
        with pytest.raises(GatewayError) as caught: await service.update_draft(principal(),second.id,changed,1)
        assert caught.value.code=='CONFLICT'
        assert registry_database.scalar("SELECT draft_revision FROM gateway.products WHERE id='revenue'")==2
    asyncio.run(scenario())
