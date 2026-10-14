from fastapi import APIRouter, Request, Query
from gateway.contracts.canonical import Identifier
from gateway.publishing.service import DraftView, VersionDiff
from .dependencies import services, principal, query, paging
from .models import (DraftPage,DraftWrite,DraftReceipt,TestRunView,FixtureCheckView,PublishRequest,PublicationReceipt,LifecycleRequest,Changed)

router=APIRouter()


def draft_receipt(row):
    return DraftReceipt(id=row.id,product_id=row.product_id,revision=row.revision,digest=row.content_digest)


def test_view(row):
    checks=[FixtureCheckView(**{name:check.get(name) for name in FixtureCheckView.model_fields}) for check in row.evidence['checks']]
    return TestRunView(**{name:getattr(row,name) for name in TestRunView.model_fields if name!='checks'},checks=checks)

@router.get('/v1/drafts',response_model=DraftPage)
async def search_drafts(request: Request,q: str=Query(default="",max_length=256),cursor: str | None=Query(default=None,max_length=4096),limit: int=Query(default=50,ge=1,le=50)):
    cursor,limit=paging(request,{'q'})
    return await services(request).drafts.search(principal(request),query=request.query_params.get('q',''),cursor=cursor,limit=limit)

@router.post('/v1/drafts',response_model=DraftReceipt)
async def save_draft(request: Request,body: DraftWrite):
    query(request,set())
    return draft_receipt(await services(request).publishing.save_draft(principal(request),body.manifest,body.expected_revision))

@router.get('/v1/drafts/{draft_id}',response_model=DraftView)
async def get_draft(request: Request,draft_id: Identifier):
    query(request,set())
    return await services(request).publishing.get_draft(principal(request),draft_id)

@router.put('/v1/drafts/{draft_id}',response_model=DraftReceipt)
async def update_draft(request: Request,draft_id: Identifier,body: DraftWrite):
    query(request,set())
    return draft_receipt(await services(request).publishing.update_draft(principal(request),draft_id,body.manifest,body.expected_revision))

@router.post('/v1/drafts/{draft_id}/tests',response_model=TestRunView)
async def test_draft(request: Request,draft_id: Identifier):
    query(request,set())
    if await request.body():
        from gateway.contracts.errors import invalid_input
        raise invalid_input()
    return test_view(await services(request).publishing.test(principal(request),draft_id))

@router.get('/v1/drafts/{draft_id}/tests/{run_id}',response_model=TestRunView)
async def get_test(request: Request,draft_id: Identifier,run_id: Identifier):
    query(request,set())
    return test_view(await services(request).publishing.get_test(principal(request),draft_id,run_id))

@router.get('/v1/drafts/{draft_id}/diff',response_model=VersionDiff)
async def get_diff(request: Request,draft_id: Identifier):
    query(request,set())
    return await services(request).publishing.diff(principal(request),draft_id)

@router.post('/v1/drafts/{draft_id}/publish',response_model=PublicationReceipt)
async def publish_draft(request: Request,draft_id: Identifier,body: PublishRequest):
    query(request,set())
    target=await services(request).publishing.publish(principal(request),draft_id,body.tested_digest,body.expected_revision,body.reason)
    return PublicationReceipt(product_id=target.manifest.product_id,version=target.manifest.version,
        version_id=target.version_id,digest=target.manifest.content_digest)

@router.post('/v1/products/{product_id}/lifecycle',response_model=Changed)
async def transition(request: Request,product_id: Identifier,body: LifecycleRequest):
    query(request,set())
    await services(request).publishing.transition(principal(request),product_id,body.version,body.action,body.reason)
    return Changed()
