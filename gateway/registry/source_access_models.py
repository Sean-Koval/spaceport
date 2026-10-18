"""Protected source approval records; writes belong to checked SQL entrypoints."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class SourceAccessApproval(Base):
    __tablename__ = 'source_access_approvals'
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    supersedes_id: Mapped[UUID | None] = mapped_column(ForeignKey('gateway.source_access_approvals.id'))
    content: Mapped[dict] = mapped_column(JSONB)
    canonical_content: Mapped[str] = mapped_column(Text)
    content_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    product_id: Mapped[str] = mapped_column(String(128))
    product_version: Mapped[str] = mapped_column(String(64))
    manifest_digest: Mapped[str] = mapped_column(String(64))
    policy_id: Mapped[str] = mapped_column(ForeignKey('gateway.policies.id'))
    consumer_binding_id: Mapped[str] = mapped_column(String(128))
    consumer_binding_digest: Mapped[str] = mapped_column(String(64))
    fixture_binding_id: Mapped[str] = mapped_column(String(128))
    fixture_binding_digest: Mapped[str] = mapped_column(String(64))
    fixture_catalog_digest: Mapped[str] = mapped_column(String(64))
    package_pins_digest: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint('id', 'revision', 'content_digest'),
        ForeignKeyConstraint(['consumer_binding_id','consumer_binding_digest'], ['gateway.binding_revisions.id','gateway.binding_revisions.content_digest']),
        ForeignKeyConstraint(['fixture_binding_id','fixture_binding_digest'], ['gateway.binding_revisions.id','gateway.binding_revisions.content_digest']),
    )


class SourceAccessApprovalState(Base):
    __tablename__ = 'source_access_approval_states'
    approval_id: Mapped[UUID] = mapped_column(ForeignKey('gateway.source_access_approvals.id'), primary_key=True)
    state: Mapped[str] = mapped_column(String(16))
    state_revision: Mapped[int] = mapped_column(Integer)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revocation_reason_code: Mapped[str | None] = mapped_column(String(128))


class SourceAccessAdminEvent(Base):
    __tablename__ = 'source_access_admin_events'
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    approval_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    approval_revision: Mapped[int] = mapped_column(Integer)
    approval_digest: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))
    operator_role: Mapped[str] = mapped_column(String(63))
    review_id: Mapped[str] = mapped_column(String(128))
    evidence_ref: Mapped[str] = mapped_column(String(128))
    evidence_digest: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    reason_code: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (ForeignKeyConstraint(['approval_id','approval_revision','approval_digest'], ['gateway.source_access_approvals.id','gateway.source_access_approvals.revision','gateway.source_access_approvals.content_digest']),)


class ExecutionSourceAuthority(Base):
    __tablename__ = 'execution_source_authorities'
    execution_id: Mapped[str] = mapped_column(ForeignKey('gateway.execution_authorizations.execution_id'),primary_key=True)
    approval_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    approval_revision: Mapped[int] = mapped_column(Integer)
    approval_digest: Mapped[str] = mapped_column(String(64))
    audience_entry_id: Mapped[str] = mapped_column(String(128))
    principal_snapshot: Mapped[dict] = mapped_column(JSONB)
    principal_digest: Mapped[str] = mapped_column(String(64))
    package_pins_digest: Mapped[str] = mapped_column(String(64))
    accepted_approval_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (ForeignKeyConstraint(['approval_id','approval_revision','approval_digest'],
        ['gateway.source_access_approvals.id','gateway.source_access_approvals.revision','gateway.source_access_approvals.content_digest']),)
