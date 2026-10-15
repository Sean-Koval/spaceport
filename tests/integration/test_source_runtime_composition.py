"""Installed process composition on isolated PostgreSQL; startup has no source I/O."""
import asyncio
import pytest

pytestmark=pytest.mark.postgres


def test_real_api_worker_composition_ready_without_credentials_or_provider_requests(registry_database,tmp_path,monkeypatch):
    import httpx
    from gateway.source_pilot_config import SourcePilotConfig
    from tests.integration.source_pilot_domo_seed import seed_domo_pilot
    import gateway.source_pilot_runtime as runtime
    async def scenario():
        async with seed_domo_pilot(registry_database,tmp_path/'package') as seed:
            urls={role:registry_database.urls[role].render_as_string(hide_password=False) for role in ('app','owner','audit_retention')}
            urls.update(domo_consumer=seed.provider_registries['consumer'].engine.url.render_as_string(hide_password=False),
                domo_fixture=seed.provider_registries['fixture_test'].engine.url.render_as_string(hide_password=False))
            monkeypatch.setattr(SourcePilotConfig,'validate_files',lambda self,**kwargs:None)
            monkeypatch.setattr(SourcePilotConfig,'database_url',lambda self,role:urls[role])
            monkeypatch.setattr(runtime,'secret_key',lambda name:b'x'*32)
            monkeypatch.setattr(runtime,'_read_file',lambda *a:pytest.fail('Source credential read during startup'))
            async def forbidden(*a,**k):pytest.fail('Provider/IdP request during startup or readiness')
            monkeypatch.setattr(httpx.AsyncClient,'send',forbidden)
            for role,context in (('api','fixture_test'),('worker','consumer')):
                built=await runtime.build_runtime(seed.config,root=tmp_path/'package',role=role)
                try:
                    assert (await built.services.readiness.check()).ready is True
                    assert built.adapter._adapters['domo-named-v2'].context==context
                    assert built.services.execution.queue.source_lifecycle is built.source_lifecycle
                    assert registry_database.scalar('SELECT count(*) FROM gateway.products')==0
                finally:await built.close()
    asyncio.run(scenario())


def test_installed_asgi_rest_author_publish_mcp_query_and_result(registry_database,tmp_path,monkeypatch):
    import httpx,json
    from gateway.source_pilot_config import SourcePilotConfig
    from tests.integration.source_pilot_domo_seed import seed_domo_pilot
    from tests.integration.test_domo_gateway import components
    from tests.unit.test_pilot_runtime import lifespan
    from tests.unit.test_identity import signed_token,jwk
    from tests.contract.test_mcp import body,PROTOCOL
    from gateway.contracts.clock import SystemClock
    from gateway.execution.worker import Worker
    import gateway.source_pilot_runtime as runtime
    import gateway.adapters.domo_serving as domo
    async def scenario():
        async with seed_domo_pilot(registry_database,tmp_path/'package') as seed:
            c=components(seed)
            transports={context:c.factory(context)()._adapters['domo-named-v2']._transport for context in ('consumer','fixture_test')}
            urls={role:registry_database.urls[role].render_as_string(hide_password=False) for role in ('app','owner','audit_retention')}
            urls.update(domo_consumer=seed.provider_registries['consumer'].engine.url.render_as_string(hide_password=False),
                domo_fixture=seed.provider_registries['fixture_test'].engine.url.render_as_string(hide_password=False))
            monkeypatch.setattr(SourcePilotConfig,'validate_files',lambda self,**kwargs:None)
            monkeypatch.setattr(SourcePilotConfig,'database_url',lambda self,role:urls[role])
            monkeypatch.setattr(runtime,'secret_key',lambda name:b'x'*32)
            monkeypatch.setattr(runtime,'source_secret_resolver',lambda sources,context:seed.secret_resolver(context))
            actual_domo=domo.DomoRuntimeAdapter
            def adapter(**kwargs):return actual_domo(**kwargs,http_transport=transports[kwargs['context']])
            monkeypatch.setattr(domo,'DomoRuntimeAdapter',adapter)
            actual_auth=runtime.Authenticator;identity_calls=[]
            def auth(settings,store,**kwargs):
                def handler(request):
                    assert str(request.url)==seed.config.oidc.jwks_url
                    identity_calls.append(str(request.url))
                    return httpx.Response(200,headers={'content-type':'application/json'},stream=httpx.ByteStream(json.dumps({'keys':[jwk()]}).encode()))
                result=actual_auth(settings,store,**kwargs,http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
                result._owns_client=True
                return result
            monkeypatch.setattr(runtime,'Authenticator',auth)
            monkeypatch.setenv('GATEWAY_SOURCE_PILOT_CONFIG_FILE',str(tmp_path/'package'/'pilot.json'))
            def headers(principal):
                subject=next(p.subject for p in seed.assets.identities.principals if p.id==principal)
                token=signed_token(SystemClock(),iss=seed.config.oidc.issuer,aud=seed.config.mcp_resource_url,
                    client_id=seed.config.oidc.client_ids[0],sub=subject)
                return {'Authorization':'Bearer '+token}
            application=runtime.app_factory()
            async with lifespan(application),httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url=seed.config.public_origin) as client:
                assert (await client.get('/readyz')).status_code==200
                assert identity_calls==[] and c.calls==[]
                response=await client.post('/v1/drafts',headers=headers('usage-author'),json={'manifest':c.manifest.model_dump(mode='json'),'expected_revision':0})
                assert response.status_code==200,response.text
                draft=response.json();draft_id=draft['id']
                tested=await client.post('/v1/drafts/'+draft_id+'/tests',headers=headers('usage-author'))
                assert tested.status_code==200,tested.text
                assert tested.json()['status']=='passed'
                published=await client.post('/v1/drafts/'+draft_id+'/publish',headers=headers('usage-publisher'),
                    json={'tested_digest':c.manifest.content_digest,'expected_revision':1,'reason':'Synthetic ASGI reviewed fixture proof'})
                assert published.status_code==200,published.text
                description=await client.get('/v1/products/'+c.manifest.product_id,params={'version':c.manifest.version},headers=headers('usage-consumer-all'))
                assert description.status_code==200,description.text
                assert description.json()['digest']==c.manifest.content_digest
                mcp_headers={**headers('usage-consumer-all'),'MCP-Protocol-Version':PROTOCOL,'MCP-Method':'tools/call',
                    'MCP-Name':'query_product','Accept':'application/json, text/event-stream'}
                response=await client.post('/mcp',headers=mcp_headers,json=body(name='query_product',arguments={'request':c.package.consumer_check.request.model_dump(mode='json')}))
                assert response.status_code==200,response.text
                accepted=response.json()['result']['structuredContent'];assert accepted['state']=='accepted'
                worker_runtime=await runtime.build_runtime(seed.config,root=tmp_path/'package',role='worker')
                try:
                    worker=Worker(worker_runtime.services.execution.queue,adapter_factory=worker_runtime.adapter_factory,worker_id='installed-runtime-worker')
                    assert await worker.run_once()
                finally:await worker_runtime.close()
                result=await client.get('/v1/executions/'+accepted['execution_id'],headers=headers('usage-consumer-all'))
                assert result.status_code==200,result.text
                assert result.json()['rows']==list(c.package.consumer_check.expected_rows)
                mcp_headers['MCP-Name']='get_result'
                response=await client.post('/mcp',headers=mcp_headers,json=body(name='get_result',arguments={'execution_id':accepted['execution_id']}))
                assert response.status_code==200,response.text
                assert response.json()['result']['structuredContent']['rows']==result.json()['rows']
                assert c.calls.count(('fixture_test','query'))==4 and c.calls.count(('consumer','query'))==1
                assert registry_database.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
    asyncio.run(scenario())
