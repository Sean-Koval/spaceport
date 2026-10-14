from fastapi import APIRouter, Request, Query
from gateway.contracts.canonical import Identifier
from .dependencies import services, principal, paging, query
from .models import BindingChoicePage, PolicyChoicePage, PolicyWrite, PolicyReceipt, AuditPage

router=APIRouter()

@router.get('/v1/bindings',response_model=BindingChoicePage)
async def bindings(request: Request,draft_id: Identifier | None=None,cursor: str | None=Query(default=None,max_length=4096),limit: int=Query(default=50,ge=1,le=50)):
    cursor,limit=paging(request,{'draft_id'})
    return await services(request).choices.bindings(principal(request),draft_id=draft_id,cursor=cursor,limit=limit)

@router.get('/v1/policies',response_model=PolicyChoicePage)
async def policies(request: Request,draft_id: Identifier | None=None,cursor: str | None=Query(default=None,max_length=4096),limit: int=Query(default=50,ge=1,le=50)):
    cursor,limit=paging(request,{'draft_id'})
    return await services(request).choices.policies(principal(request),draft_id=draft_id,cursor=cursor,limit=limit)

@router.put('/v1/policies/{policy_id}',response_model=PolicyReceipt)
async def change_policy(request: Request,policy_id: Identifier,body: PolicyWrite):
    query(request,set())
    revision=await services(request).policy.change_policy(principal(request),policy_id,body.rules.model_dump(mode='json'),body.reason)
    return PolicyReceipt(policy_id=policy_id,revision=revision)

@router.get('/v1/audit',response_model=AuditPage)
async def audit(request: Request,cursor: str | None=Query(default=None,max_length=4096),limit: int=Query(default=50,ge=1,le=50)):
    cursor,limit=paging(request)
    return await services(request).audit_reader.list(principal(request),cursor=cursor,limit=limit)
