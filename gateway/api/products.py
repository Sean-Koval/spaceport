from fastapi import APIRouter, Request, Query
from gateway.contracts.canonical import Identifier, Version
from gateway.contracts.errors import invalid_input
from .models import CatalogPage, ProductDescription
from .dependencies import services, principal, paging, query

router=APIRouter()

@router.get('/v1/products',response_model=CatalogPage)
async def search_products(request: Request,q: str=Query(default="",max_length=256),cursor: str | None=Query(default=None,max_length=4096),limit: int=Query(default=50,ge=1,le=50)):
    cursor,limit=paging(request,{'q'})
    text=request.query_params.get('q','')
    if len(text)>256:raise invalid_input()
    return await services(request).catalog.search(principal(request),query=text,cursor=cursor,limit=limit)

@router.get('/v1/products/{product_id}',response_model=ProductDescription)
async def describe_product(request: Request,product_id: Identifier,version: Version | None=None):
    query(request,{'version'})
    return await services(request).catalog.describe(principal(request),product_id,version=version)
