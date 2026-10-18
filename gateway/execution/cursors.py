"""High-entropy server-stored cursors, scoped through immutable result foreign keys."""
from datetime import datetime
import hashlib
import re
import secrets

from sqlalchemy import select

from gateway.contracts.canonical import canonical_bytes, timestamp
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.execution import ResultEnvelope
from gateway.registry.models import Execution, ExecutionState, Result, ResultCursor
from gateway.registry.repository import UnitOfWork, not_found


def new_cursor_token() -> str:
    return secrets.token_urlsafe(32)


def cursor_digest(token: str) -> str:
    if type(token) is not str or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise not_found()
    return hashlib.sha256(b"dpg-result-cursor-v1\0" + token.encode("ascii")).hexdigest()


def result_page(envelope: ResultEnvelope, *, offset: int, page_size: int,
                next_cursor: str | None, max_bytes: int) -> ResultEnvelope:
    try:
        envelope = ResultEnvelope.model_validate(envelope.model_dump(mode="python"))
        if (envelope.state != "succeeded" or envelope.row_count != len(envelope.rows) or envelope.next_cursor is not None
            or type(offset) is not int or offset < 0 or offset >= envelope.row_count and not (offset == 0 == envelope.row_count)
            or type(page_size) is not int or not 1 <= page_size <= 200
            or type(max_bytes) is not int or not 1 <= max_bytes <= 1048576
            or (offset + page_size < envelope.row_count) != (next_cursor is not None)):
            raise ValueError("Invalid page")
        if next_cursor is not None:
            cursor_digest(next_cursor)
        data = envelope.model_dump(mode="python")
        data.update(rows=envelope.rows[offset:offset+page_size], next_cursor=next_cursor)
        if len(canonical_bytes(data)) > max_bytes:
            raise GatewayError(ErrorCode.RESOURCE_LIMIT, envelope.execution_id, "The complete page exceeds the current byte limit.")
        return ResultEnvelope.model_validate(data)
    except (ValueError, TypeError, AttributeError):
        raise not_found() from None


class CursorStore:
    """Internal transaction-local store. ResultService owns authorization and commit.

    Results have no UPDATE privilege, so their mutable execution state is the lock
    shared with retention and completion. No cursor includes an ID or offset.
    """
    def __init__(self, uow: UnitOfWork, now: datetime):
        self.uow, self.now = uow, timestamp(now)
        self.release_expires_at: datetime | None = None

    def _retain_expiry(self, expires_at: datetime) -> None:
        """Keep immutable input/output cursor deadlines for the release suffix."""
        expiry = timestamp(expires_at)
        self.release_expires_at = (expiry if self.release_expires_at is None
            else min(self.release_expires_at, expiry))

    async def issue(self, principal_id: str, execution_id: str, policy_revision: int,
                    offset: int, expires_at: datetime) -> str:
        state = await self.uow.session.scalar(select(ExecutionState).join(Execution,
            Execution.id == ExecutionState.execution_id).where(Execution.id == execution_id,
            Execution.principal_id == principal_id, Execution.policy_revision == policy_revision,
            Execution.context == "consumer").with_for_update(of=ExecutionState).execution_options(populate_existing=True))
        result = await self.uow.session.scalar(select(Result).where(Result.execution_id == execution_id,
            Result.principal_id == principal_id, Result.policy_revision == policy_revision))
        if state is None or state.state != "succeeded" or result is None:
            raise not_found()
        try:
            expiry = min(timestamp(expires_at), timestamp(result.expires_at))
            if (expiry <= self.now or type(offset) is not int or not 0 < offset < result.envelope["row_count"]):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise not_found() from None
        token = new_cursor_token()
        self.uow.session.add(ResultCursor(token_digest=cursor_digest(token), result_id=result.id,
            principal_id=result.principal_id, version_id=result.version_id, policy_revision=result.policy_revision,
            position=offset, expires_at=expiry))
        await self.uow.session.flush()
        self._retain_expiry(expiry)
        return token

    async def resolve(self, token: str, result: Result) -> int:
        row = await self.uow.session.scalar(select(ResultCursor).where(
            ResultCursor.token_digest == cursor_digest(token), ResultCursor.result_id == result.id,
            ResultCursor.principal_id == result.principal_id, ResultCursor.version_id == result.version_id,
            ResultCursor.policy_revision == result.policy_revision))
        if (row is None or row.expires_at <= self.now or result.expires_at <= self.now
            or row.expires_at > result.expires_at or not 0 < row.position < result.envelope["row_count"]):
            raise not_found()
        self._retain_expiry(row.expires_at)
        return row.position
