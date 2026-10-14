"""No authority is reconstructed from request arguments."""
from fastapi import Request
from gateway.contracts.errors import invalid_input


def principal(request: Request):return request.state.principal

def services(request: Request):return request.app.state.services


def query(request: Request, allowed):
    pairs=request.query_params.multi_items()
    if len({key for key,_ in pairs})!=len(pairs) or any(key not in allowed for key,_ in pairs):
        raise invalid_input()


def paging(request: Request, allowed=()):
    query(request,{'cursor','limit',*allowed})
    raw=request.query_params.get('limit','50')
    if not raw.isascii() or not raw.isdecimal() or len(raw)>2 or not 1<=int(raw)<=50:raise invalid_input()
    cursor=request.query_params.get('cursor')
    if cursor is not None and not 1<=len(cursor)<=4096:raise invalid_input()
    return cursor,int(raw)
