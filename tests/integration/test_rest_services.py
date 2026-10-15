"""Required real PostgreSQL authority/transport tests; no in-memory DB substitute."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from psycopg.types.json import Jsonb

from gateway.api.app import create_app
from gateway.api.transport import TransportSettings
from gateway.api.catalog import CatalogService
from gateway.api.choices import ChoiceService
from gateway.api.audit import AuditReadService
from gateway.api.cursors import PageCursorCodec
from gateway.api.readiness import ReadinessService
from gateway.adapters.base import StaticBindingResolver
from gateway.contracts.errors import GatewayError
from gateway.registry.models import TestRun as RegistryTestRun
from tests.integration.test_audit import seed,service as policy_service,principal
from tests.integration.test_publishing import setup
from tests.contract.test_cube_adapter import synthetic
from test_cube_compatibility import cube_stack

pytestmark=pytest.mark.postgres


async def composition(database,clock):
    publishing,manifest=await setup(database,roles=('consumer','author','publisher','policy_admin','audit_view'))
    draft=await publishing.save_draft(principal(),manifest,0)
    async with database.registry.transaction() as uow:
        uow.session.add(RegistryTestRun(id='run-1',product_id=manifest.product_id,draft_id=draft.id,
            draft_digest=draft.content_digest,binding_id='binding-1',binding_digest=synthetic()[0].digest,
            policy_id=manifest.policy_id,policy_revision=1,fixture_version='1',evidence={'synthetic':'passed'},status='passed',actor_id=principal().id))
    async with database.registry.transaction() as uow:
        await uow.insert_version(draft_id=draft.id,expected_revision=1,test_run_id='run-1',actor_id=principal().id)
    registry=database.registry;policy=publishing.policy
    cursors=PageCursorCodec(b'k'*32,clock)
    catalog=CatalogService(registry=registry,policy=policy,bindings=publishing.bindings,cursors=cursors)
    choices=ChoiceService(registry=registry,policy=policy,bindings=publishing.bindings,bundles=publishing.bundles,cursors=cursors,templates=(manifest,))
    audit=AuditReadService(registry=registry,policy=policy,cursors=cursors,clock=clock)
    return SimpleNamespace(authenticator=SimpleNamespace(authenticate=AsyncMock(return_value=principal())),
        registry=registry,policy=policy,bindings=publishing.bindings,bundles=publishing.bundles,execution=None,results=None,
        publishing=publishing,clock=clock,catalog=catalog,choices=choices,audit_reader=audit,readiness=None),draft


@pytest.mark.parametrize('role,action',[('author','author'),('policy_admin','policy_admin'),('audit_view','audit_view')])
def test_non_product_scope_fresh_authorization_and_durable_audit(registry_database,role,action):
    seed(registry_database,roles=(role,))
    registry_database.execute_owner("UPDATE gateway.principal_grants SET entitlements='{}'")
    async def scenario():
        async with registry_database.registry.transaction() as uow:
            value=await policy_service(registry_database).authorize_scope(principal(),'revenue-access',action,uow)
        assert value.action==action and value.tenant_id=='north' and not hasattr(value,'row_constraints')
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action=%s AND decision='allow' AND policy_id='revenue-access'",(action,))==1
        registry_database.execute_owner('UPDATE gateway.principal_grants SET active=false')
        with pytest.raises(GatewayError) as caught:
            async with registry_database.registry.transaction() as uow:
                await policy_service(registry_database).authorize_scope(principal(),'revenue-access',action,uow)
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action=%s AND decision='deny'",(action,))==1
    asyncio.run(scenario())


@pytest.mark.parametrize('role,action',[('publisher','author'),('author','policy_admin'),('operator','audit_view')])
def test_non_product_scope_does_not_union_or_infer_roles(registry_database,role,action):
    seed(registry_database,roles=(role,))
    async def scenario():
        with pytest.raises(GatewayError) as caught:
            async with registry_database.registry.transaction() as uow:
                await policy_service(registry_database).authorize_scope(principal(),'revenue-access',action,uow)
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
    asyncio.run(scenario())


def test_real_catalog_rest_matches_service_and_revocation_is_immediate(registry_database,clock):
    async def scenario():
        services,_=await composition(registry_database,clock)
        direct=await services.catalog.describe(principal(),'revenue')
        app=create_app(services,settings=TransportSettings(allowed_hosts=('testserver',)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver') as client:
            response=await client.get('/v1/products/revenue')
            assert response.status_code==200 and response.json()==direct.model_dump(mode='json')
            registry_database.execute_owner("UPDATE gateway.principal_grants SET roles='[\"author\"]'")
            hidden=await client.get('/v1/products/revenue')
            missing=await client.get('/v1/products/missing')
            assert hidden.status_code==missing.status_code==404
            assert {k:v for k,v in hidden.json().items() if k!='request_id'}=={k:v for k,v in missing.json().items() if k!='request_id'}
            assert (await client.get('/v1/products')).json()=={'items':[],'next_cursor':None}
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='describe' AND decision='allow'")>=2
    asyncio.run(scenario())


def test_real_catalog_partial_metadata_and_example_search_are_filtered(registry_database,clock):
    from tests.unit.test_policy import rules
    async def scenario():
        services,_=await composition(registry_database,clock)
        await services.policy.change_policy(principal(),'revenue-access',rules(visible_fields=['day','revenue']),'Tighten visible fields')
        view=await services.catalog.describe(principal(),'revenue')
        assert set(view.outputs)=={'day','revenue'} and view.description is None and view.examples==()
        assert view.links==() and 'segment' not in view.inputs
        page=await services.catalog.search(principal(),query='unassigned')
        assert page.items==()
    asyncio.run(scenario())


def test_real_choices_audit_and_mutation_use_independent_scopes(registry_database,clock):
    async def scenario():
        services,_=await composition(registry_database,clock)
        assert len((await services.choices.bindings(principal())).items)==1
        assert len((await services.choices.policies(principal())).items)==1
        page=await services.audit_reader.list(principal(),limit=1)
        assert page.items[0].policy_id=='revenue-access'
        if page.next_cursor:
            following=await services.audit_reader.list(principal(),cursor=page.next_cursor,limit=1)
            assert following.items and following.items[0].id!=page.items[0].id
            with pytest.raises(GatewayError):await services.catalog.search(principal(),cursor=page.next_cursor,limit=1)
        registry_database.execute_owner("UPDATE gateway.principal_grants SET roles='[\"publisher\"]'")
        assert (await services.choices.bindings(principal())).items==()
        assert (await services.choices.policies(principal())).items==()
        with pytest.raises(GatewayError):await services.audit_reader.list(principal())
    asyncio.run(scenario())


def test_real_audit_release_requires_successful_commit(registry_database,clock):
    async def scenario():
        services,_=await composition(registry_database,clock)
        # Trigger fails at COMMIT, after the returned rows would otherwise exist.
        registry_database.execute_owner("CREATE FUNCTION gateway.fail_audit_view() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action='audit_view' THEN RAISE EXCEPTION 'synthetic deferred audit outage'; END IF; RETURN NEW; END $$")
        registry_database.execute_owner("CREATE CONSTRAINT TRIGGER fail_audit_view AFTER INSERT ON gateway.audit_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.fail_audit_view()")
        with pytest.raises(GatewayError):await services.audit_reader.list(principal())
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='audit_view'")==0
    asyncio.run(scenario())


def test_real_draft_url_identity_and_current_authorization(registry_database,clock):
    async def scenario():
        services,draft=await composition(registry_database,clock)
        app=create_app(services,settings=TransportSettings(allowed_hosts=('testserver',)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://testserver') as client:
            assert (await client.get('/v1/drafts/'+draft.id)).status_code==200
            manifest=synthetic()[1].manifest.model_dump(mode='json')
            response=await client.put('/v1/drafts/missing',json={'manifest':manifest,'expected_revision':1})
            assert response.status_code==404
            registry_database.execute_owner("UPDATE gateway.principal_grants SET roles='[\"consumer\"]'")
            assert (await client.get('/v1/drafts/'+draft.id)).status_code==404
    asyncio.run(scenario())


def test_real_readiness_uses_exact_deployed_head_fresh_policy_and_metadata(registry_database,clock):
    async def scenario():
        services,_=await composition(registry_database,clock)
        adapter=SimpleNamespace(validate_binding=AsyncMock())
        readiness=ReadinessService(registry=services.registry,policy=services.policy,adapter=adapter,
            bindings=services.bindings,bundles=services.bundles,clock=clock,probe_principal_id='north-user',probe_product_id='revenue')
        assert (await readiness.check()).ready is True
        assert adapter.validate_binding.await_count==1
        registry_database.execute_owner('UPDATE gateway.principal_grants SET active=false')
        assert (await readiness.check()).ready is False
        assert adapter.validate_binding.await_count==1
    asyncio.run(scenario())


@pytest.mark.cube
def test_actual_pinned_cube_readiness_with_real_registry_and_fresh_revocation(registry_database,cube_stack):
    """Both mandatory backends, actual metadata response, no mocked readiness."""
    from gateway.contracts.clock import SystemClock
    from tests.integration.test_cube_adapter_runtime import configured
    async def scenario():
        clock=SystemClock()
        services,_=await composition(registry_database,clock)
        adapter,target,bindings,_,_=configured(cube_stack)
        registered=await services.registry.resolve('revenue')
        assert registered.manifest.content_digest==target.manifest.content_digest
        readiness=ReadinessService(registry=services.registry,policy=services.policy,adapter=adapter,
            bindings=services.bindings,bundles=adapter.catalog,clock=clock,
            probe_principal_id=principal().id,probe_product_id='revenue')
        paths=[]
        async def observe(request):paths.append((request.method,request.url.path))
        adapter._client.event_hooks['request'].append(observe)
        async with adapter:
            assert (await readiness.check()).ready is True
            registry_database.execute_owner('UPDATE gateway.principal_grants SET active=false')
            assert (await readiness.check()).ready is False
        assert paths==[('GET','/cubejs-api/v1/meta')]
        cube_stack.capture('rest-readiness.json',{'ready_before_revocation':True,'ready_after_revocation':False,
            'migration_head':registry_database.migration_head,'product_digest':registered.manifest.content_digest,
            'probe':'actual-private-metadata-only'})
    asyncio.run(scenario())
