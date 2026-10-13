"""Separate immutable draft fixtures; consumer execution/result FKs stay strict."""
from datetime import datetime
from sqlalchemy import Boolean, CheckConstraint, DateTime, FetchedValue, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from gateway.registry.models import Base, Record


class FixtureAttempt(Record, Base):
    __tablename__ = 'fixture_attempts'
    product_id: Mapped[str] = mapped_column(String(128))
    draft_id: Mapped[str] = mapped_column(String(128))
    draft_digest: Mapped[str] = mapped_column(String(64))
    consumer_binding_id: Mapped[str] = mapped_column(String(128))
    consumer_binding_digest: Mapped[str] = mapped_column(String(64))
    fixture_binding_id: Mapped[str] = mapped_column(String(128))
    fixture_binding_digest: Mapped[str] = mapped_column(String(64))
    fixture_manifest: Mapped[dict] = mapped_column(JSONB)
    fixture_manifest_digest: Mapped[str] = mapped_column(String(64))
    execution_contract_digest: Mapped[str] = mapped_column(String(64))
    policy_id: Mapped[str] = mapped_column(String(128))
    policy_revision: Mapped[int] = mapped_column(Integer)
    catalog_digest: Mapped[str] = mapped_column(String(64))
    fixture_id: Mapped[str] = mapped_column(String(128))
    fixture_version: Mapped[str] = mapped_column(String(128))
    actor_id: Mapped[str] = mapped_column(ForeignKey('gateway.principals.id'))
    fixed_principal_id: Mapped[str] = mapped_column(ForeignKey('gateway.principals.id'))
    principal_snapshot: Mapped[dict] = mapped_column(JSONB)
    decision: Mapped[dict] = mapped_column(JSONB)
    canonical_query: Mapped[dict] = mapped_column(JSONB)
    canonical_hash: Mapped[str] = mapped_column(String(64))
    runner_token: Mapped[str] = mapped_column(String(128), unique=True)
    request_id: Mapped[str] = mapped_column(String(128))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(['draft_id','product_id','draft_digest'],['gateway.draft_revisions.id','gateway.draft_revisions.product_id','gateway.draft_revisions.content_digest']),
        ForeignKeyConstraint(['consumer_binding_id','consumer_binding_digest'],['gateway.binding_revisions.id','gateway.binding_revisions.content_digest']),
        ForeignKeyConstraint(['fixture_binding_id','fixture_binding_digest'],['gateway.binding_revisions.id','gateway.binding_revisions.content_digest']),
        ForeignKeyConstraint(['policy_id','policy_revision'],['gateway.policy_revisions.policy_id','gateway.policy_revisions.revision']),
        UniqueConstraint('id','actor_id','fixed_principal_id','runner_token',name='uq_fixture_attempt_authority'),
        CheckConstraint("deadline_at > created_at AND deadline_at <= created_at + interval '120 seconds' "
            "AND deadline_at = created_at + make_interval(secs => (decision->'limits'->>'execution_timeout_seconds')::integer)",
            name='bounded_deadline'),
    )


class FixtureAttemptState(Base):
    __tablename__ = 'fixture_attempt_states'
    attempt_id: Mapped[str] = mapped_column(ForeignKey('gateway.fixture_attempts.id'), primary_key=True)
    state: Mapped[str] = mapped_column(String(16))
    source_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=FetchedValue(), server_onupdate=FetchedValue())
    error_code: Mapped[str | None] = mapped_column(String(32))
    check_digest: Mapped[str | None] = mapped_column(String(64), server_default=FetchedValue())
    __table_args__ = (CheckConstraint("state IN ('running','succeeded','failed','timed_out')",name='state_valid'),)


class FixtureSourceReservation(Record, Base):
    __tablename__ = 'fixture_source_reservations'
    attempt_id: Mapped[str] = mapped_column(String(128), unique=True)
    actor_id: Mapped[str] = mapped_column(String(128))
    fixed_principal_id: Mapped[str] = mapped_column(String(128))
    runner_token: Mapped[str] = mapped_column(String(128), unique=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=FetchedValue(), server_onupdate=FetchedValue())
    release_reason: Mapped[str | None] = mapped_column(String(32), server_default=FetchedValue())
    __table_args__ = (ForeignKeyConstraint(['attempt_id','actor_id','fixed_principal_id','runner_token'],
        ['gateway.fixture_attempts.id','gateway.fixture_attempts.actor_id','gateway.fixture_attempts.fixed_principal_id','gateway.fixture_attempts.runner_token']),)


class PublicationEvent(Record, Base):
    __tablename__ = 'publication_events'
    product_id: Mapped[str] = mapped_column(String(128))
    version_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(ForeignKey('gateway.principals.id'))
    reason: Mapped[str] = mapped_column(String(1024))
    request_id: Mapped[str] = mapped_column(String(128))
    __table_args__ = (
        ForeignKeyConstraint(['version_id','product_id'],['gateway.product_versions.id','gateway.product_versions.product_id']),
        CheckConstraint("action IN ('publish','deprecate','withdraw','rollback')",name='action_valid'),
        CheckConstraint("length(btrim(reason)) BETWEEN 1 AND 1024 AND reason !~ '[[:cntrl:]]'",name='reason_bounded'),
    )
