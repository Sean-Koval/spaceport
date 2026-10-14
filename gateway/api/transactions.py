"""Read-only response transactions cannot have client optimistic-write conflicts."""
from contextlib import asynccontextmanager
from gateway.contracts.errors import GatewayError,ErrorCode
from gateway.policy.audit import audit_unavailable


@asynccontextmanager
async def read_transaction(registry):
    try:
        async with registry.transaction() as uow:yield uow
    except GatewayError as error:
        # Registry also maps deferred trigger/serialization failures to CONFLICT.
        # Discovery made no requested write; its mandatory audit must have committed.
        if error.code==ErrorCode.CONFLICT:raise audit_unavailable() from None
        raise
