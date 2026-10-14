"""Bounded result expiry and separate-role, append-only audit retention."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select, text

from gateway.contracts.canonical import timestamp
from gateway.registry.models import AuditEvent, ExecutionState, Result


@dataclass(frozen=True)
class RetentionCounts:
    deleted_results: int
    deleted_audits: int


def retention_cutoff(now: datetime, database_now: datetime) -> datetime:
    """An operator clock may defer cleanup, but can never advance DB retention."""
    return min(timestamp(now), timestamp(database_now))


class RetentionSweep:
    """Operator-internal. The app registry never receives audit DELETE rights.

    The two stores use independently configured database roles; audit cleanup can
    fail after result cleanup commits and is safe to retry. No credentials are
    accepted in results, queries or manifests. Each run processes a bounded batch.
    """
    BATCH_SIZE = 1000

    def __init__(self, registry, audit_registry):
        if registry is audit_registry or registry.engine.url.username == audit_registry.engine.url.username:
            raise ValueError("Audit retention requires a distinct maintenance role")
        if any(getattr(registry.engine.url, field) != getattr(audit_registry.engine.url, field)
               for field in ("drivername", "host", "port", "database", "query")):
            raise ValueError("Audit retention must target the same registry database")
        self.registry, self.audit_registry = registry, audit_registry

    async def run_once(self, now: datetime) -> RetentionCounts:
        now = timestamp(now)
        deleted_results = 0
        async with self.registry.transaction() as uow:
            cutoff = retention_cutoff(now, await uow.session.scalar(text("SELECT clock_timestamp()")))
            # Result rows cannot be locked under the app's intentionally read-only
            # content privileges. This is the same state lock used by result reads.
            states = (await uow.session.scalars(select(ExecutionState).join(Result,
                Result.execution_id == ExecutionState.execution_id).where(Result.expires_at <= cutoff)
                .order_by(ExecutionState.execution_id).limit(self.BATCH_SIZE)
                .with_for_update(of=ExecutionState, skip_locked=True))).all()
            for state in states:
                removed = await uow.session.execute(delete(Result).where(
                    Result.execution_id == state.execution_id, Result.expires_at <= cutoff).returning(Result.id))
                if removed.scalar_one_or_none() is not None:
                    deleted_results += 1
                    state.state = "expired"
                    state.error_code = None
            await uow.session.flush()
        async with self.audit_registry.transaction() as uow:
            # Verify this is narrowly provisioned maintenance, not the app/owner.
            permitted = (await uow.session.execute(text("SELECT "
                "has_table_privilege(current_user,'gateway.audit_events','DELETE') AS may_delete, "
                "has_table_privilege(current_user,'gateway.audit_events','UPDATE') AS may_update, "
                "has_table_privilege(current_user,'gateway.results','SELECT') AS may_read_results"))).one()
            if not permitted.may_delete or permitted.may_update or permitted.may_read_results:
                raise ValueError("Audit retention requires the dedicated least-privilege role")
            cutoff = retention_cutoff(now, await uow.session.scalar(text("SELECT clock_timestamp()"))) - timedelta(days=30)
            candidates = select(AuditEvent.id).where(AuditEvent.created_at <= cutoff).order_by(
                AuditEvent.created_at, AuditEvent.id).limit(self.BATCH_SIZE)
            removed = await uow.session.execute(delete(AuditEvent).where(AuditEvent.id.in_(candidates)).returning(AuditEvent.id))
            deleted_audits = len(removed.scalars().all())
        return RetentionCounts(deleted_results=deleted_results, deleted_audits=deleted_audits)
