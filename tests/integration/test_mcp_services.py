"""Actual app-role PostgreSQL services through the MCP socket, not policy doubles."""
import asyncio
import json
import secrets
from types import SimpleNamespace

import httpx
import pytest

from gateway.adapters.base import StaticBindingResolver
from gateway.api.catalog import CatalogService
from gateway.api.cursors import PageCursorCodec
from gateway.auth.principals import PrincipalStore,credential_digest
from gateway.auth.settings import Settings
from gateway.auth.tokens import Authenticator
from gateway.execution.service import ExecutionService
from tests.contract.test_mcp import running,body,post
from tests.contract.result_parity import assert_result_page_parity
from tests.integration.test_results import seeded_result

pytestmark=pytest.mark.postgres


def test_real_mcp_rest_catalog_result_pagination_revocation_and_identity(registry_database,valid_manifest,tmp_path):
    async def scenario():
        ctx=await seeded_result(registry_database,valid_manifest,count=451)
        registry=registry_database.registry;bindings=StaticBindingResolver([ctx.binding])
        token='dpg_demo_'+secrets.token_urlsafe(32)
        credential=tmp_path/'hashes.json'
        credential.write_text(json.dumps({'version':1,'credentials':[{'digest':credential_digest(token),
            'principal_id':'north-user','kind':'demo','active':True}]}));credential.chmod(0o600)
        authenticator=Authenticator(Settings(credential_hash_file=str(credential)),PrincipalStore(registry.sessions),clock=ctx.clock)
        svc=SimpleNamespace(authenticator=authenticator,clock=ctx.clock,
            catalog=CatalogService(registry=registry,policy=ctx.policy,bindings=bindings,cursors=PageCursorCodec(b'k'*32,ctx.clock)),
            execution=ExecutionService(registry=registry,policy=ctx.policy,bindings=bindings,clock=ctx.clock),results=ctx.service)
        try:
            async with running(svc) as (base,_),httpx.AsyncClient(trust_env=False,headers={'Authorization':'Bearer '+token}) as client:
                async def tool(name,args):
                    response=await post(base,body(name=name,arguments=args),headers={'Authorization':'Bearer '+token})
                    assert response.status_code==200
                    return response.json()['result']
                rest=(await client.get(base+'/v1/products/revenue')).json()
                mcp=await tool('describe_product',{'product_id':'revenue'})
                assert not mcp['isError'] and mcp['structuredContent']==rest
                rest=(await client.get(base+'/v1/products')).json()
                assert (await tool('search_products',{}))['structuredContent']==rest
                pages=[];cursor=None
                while True:
                    result=await tool('get_result',{'execution_id':'execution-1','cursor':cursor})
                    assert not result['isError']
                    page=result['structuredContent'];pages.extend(page['rows'])
                    # Reuse the exact MCP-issued cursor on REST: shared authority.
                    rest=(await client.get(base+'/v1/executions/execution-1',params={} if cursor is None else {'cursor':cursor})).json()
                    assert_result_page_parity(page,rest,ctx.envelope.model_dump(mode='json'))
                    cursor=page['next_cursor']
                    if cursor is None:break
                assert len(pages)==451 and pages==ctx.envelope.model_dump(mode='json')['rows']
                registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false WHERE principal_id='north-user'")
                denied=await tool('get_result',{'execution_id':'execution-1'})
                assert denied['structuredContent']['code']=='NOT_FOUND_OR_FORBIDDEN'
                assert (await tool('search_products',{}))['structuredContent']=={'items':[],'next_cursor':None}
                registry_database.execute_owner("UPDATE gateway.principals SET active=false WHERE id='north-user'")
                response=await post(base,body(),headers={'Authorization':'Bearer '+token})
                assert response.status_code==401
        finally:await authenticator.close()
    asyncio.run(scenario())
