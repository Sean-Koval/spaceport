from fastapi import APIRouter, Request
from gateway.contracts.canonical import Identifier
from gateway.contracts.execution import ExecutionView, ResultEnvelope
from gateway.contracts.query import QueryRequest
from gateway.contracts.errors import invalid_input
from .dependencies import services, principal, query

router=APIRouter()

@router.post('/v1/executions',response_model=ExecutionView)
async def submit_execution(request: Request,body: QueryRequest):
    query(request,set())
    return await services(request).execution.submit_and_wait(principal(request),body,seconds=5)

@router.get('/v1/executions/{execution_id}',response_model=ResultEnvelope | ExecutionView)
async def get_execution(request: Request,execution_id: Identifier,cursor: str | None=None):
    query(request,{'cursor'})
    if cursor is not None and not 1<=len(cursor)<=4096:raise invalid_input()
    return await services(request).results.get(principal(request),execution_id,cursor)
