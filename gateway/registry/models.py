"""PostgreSQL records. Content rows are append-only; heads/state are separate.

These are internal persistence types, never client-submitted identity/policy types.
Database privileges and triggers, not ORM setters, enforce immutability.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, MetaData, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(schema="gateway", naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s", "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })


class Record:
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("clock_timestamp()"))


class ProductRow(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    draft_revision: Mapped[int] = mapped_column(Integer, server_default="0")
    latest_draft_id: Mapped[str | None] = mapped_column(String(128))
    current_version_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("clock_timestamp()"))
    __table_args__ = (
        CheckConstraint("draft_revision >= 0", name="draft_revision_nonnegative"),
        CheckConstraint("(draft_revision = 0 AND latest_draft_id IS NULL) OR (draft_revision > 0 AND latest_draft_id IS NOT NULL)", name="draft_head_coherent"),
        ForeignKeyConstraint(["latest_draft_id", "id", "draft_revision"], ["gateway.draft_revisions.id", "gateway.draft_revisions.product_id", "gateway.draft_revisions.revision"], name="fk_product_latest_draft", use_alter=True, deferrable=True, initially="DEFERRED"),
        ForeignKeyConstraint(["current_version_id", "id"], ["gateway.product_versions.id", "gateway.product_versions.product_id"], name="fk_product_current_version", use_alter=True),
    )


class DraftRevision(Record, Base):
    __tablename__ = "draft_revisions"
    product_id: Mapped[str] = mapped_column(ForeignKey("gateway.products.id"))
    revision: Mapped[int] = mapped_column(Integer)
    manifest: Mapped[dict] = mapped_column(JSONB)
    content_digest: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(128))
    __table_args__ = (UniqueConstraint("product_id", "revision"), UniqueConstraint("id", "product_id", "revision", name="uq_draft_id_product_revision"),
        UniqueConstraint("id", "product_id", "content_digest", name="uq_draft_id_product_digest"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("content_digest ~ '^[0-9a-f]{64}$'", name="digest_format"),
        CheckConstraint("(jsonb_typeof(manifest) = 'object' AND manifest->>'product_id' = product_id AND manifest->>'content_digest' = content_digest) IS TRUE", name="manifest_identity"))


class BindingRevision(Record, Base):
    __tablename__ = "binding_revisions"
    binding_id: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSONB)
    content_digest: Mapped[str] = mapped_column(String(64))
    model_bundle_digest: Mapped[str] = mapped_column(String(64))
    adapter_revision: Mapped[str] = mapped_column(String(128))
    actor_id: Mapped[str] = mapped_column(String(128))
    __table_args__ = (UniqueConstraint("binding_id", "revision"), UniqueConstraint("id", "content_digest"),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("content_digest ~ '^[0-9a-f]{64}$' AND model_bundle_digest ~ '^[0-9a-f]{64}$'", name="digest_format"),
        CheckConstraint("jsonb_typeof(content) = 'object'", name="content_object"))


class PolicyRow(Base):
    __tablename__ = "policies"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    current_revision: Mapped[int] = mapped_column(Integer)
    __table_args__ = (CheckConstraint("current_revision > 0", name="revision_positive"),
        ForeignKeyConstraint(["id", "current_revision"], ["gateway.policy_revisions.policy_id", "gateway.policy_revisions.revision"], name="fk_policy_current_revision", use_alter=True, deferrable=True, initially="DEFERRED"))


class PolicyRevision(Record, Base):
    __tablename__ = "policy_revisions"
    policy_id: Mapped[str] = mapped_column(ForeignKey("gateway.policies.id"))
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSONB)
    content_digest: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(128))
    __table_args__ = (UniqueConstraint("policy_id", "revision"), CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("content_digest ~ '^[0-9a-f]{64}$'", name="digest_format"),
        CheckConstraint("jsonb_typeof(content) = 'object'", name="content_object"))


class TestRun(Record, Base):
    __tablename__ = "test_runs"
    product_id: Mapped[str] = mapped_column(ForeignKey("gateway.products.id"))
    draft_id: Mapped[str] = mapped_column(ForeignKey("gateway.draft_revisions.id"))
    draft_digest: Mapped[str] = mapped_column(String(64))
    binding_id: Mapped[str] = mapped_column(ForeignKey("gateway.binding_revisions.id"))
    binding_digest: Mapped[str] = mapped_column(String(64))
    policy_id: Mapped[str] = mapped_column(String(128))
    policy_revision: Mapped[int] = mapped_column(Integer)
    fixture_version: Mapped[str] = mapped_column(String(128))
    evidence: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(128))
    __table_args__ = (ForeignKeyConstraint(["policy_id", "policy_revision"], ["gateway.policy_revisions.policy_id", "gateway.policy_revisions.revision"]),
        CheckConstraint("status IN ('passed', 'failed')", name="status_valid"),
        CheckConstraint("draft_digest ~ '^[0-9a-f]{64}$' AND binding_digest ~ '^[0-9a-f]{64}$'", name="digest_format"),
        CheckConstraint("jsonb_typeof(evidence) = 'object'", name="evidence_object"))


class ProductVersion(Record, Base):
    __tablename__ = "product_versions"
    product_id: Mapped[str] = mapped_column(ForeignKey("gateway.products.id"))
    version: Mapped[str] = mapped_column(String(128))
    manifest: Mapped[dict] = mapped_column(JSONB)
    content_digest: Mapped[str] = mapped_column(String(64))
    binding_id: Mapped[str] = mapped_column(String(128))
    binding_digest: Mapped[str] = mapped_column(String(64))
    policy_id: Mapped[str] = mapped_column(String(128))
    policy_revision: Mapped[int] = mapped_column(Integer)
    test_run_id: Mapped[str] = mapped_column(ForeignKey("gateway.test_runs.id"), unique=True)
    draft_id: Mapped[str] = mapped_column(String(128), unique=True)
    actor_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256))
    domain: Mapped[str] = mapped_column(String(128))
    classification: Mapped[str] = mapped_column(String(16))
    __table_args__ = (UniqueConstraint("product_id", "version"), UniqueConstraint("id", "product_id"),
        ForeignKeyConstraint(["draft_id", "product_id", "content_digest"], ["gateway.draft_revisions.id", "gateway.draft_revisions.product_id", "gateway.draft_revisions.content_digest"]),
        ForeignKeyConstraint(["binding_id", "binding_digest"], ["gateway.binding_revisions.id", "gateway.binding_revisions.content_digest"]),
        ForeignKeyConstraint(["policy_id", "policy_revision"], ["gateway.policy_revisions.policy_id", "gateway.policy_revisions.revision"]),
        CheckConstraint("content_digest ~ '^[0-9a-f]{64}$' AND binding_digest ~ '^[0-9a-f]{64}$'", name="digest_format"),
        CheckConstraint("(jsonb_typeof(manifest) = 'object' AND manifest->>'product_id' = product_id AND manifest->>'version' = version AND manifest->>'content_digest' = content_digest) IS TRUE", name="manifest_identity"),
        Index("ix_product_versions_catalog", "domain", "classification", "name"))


class VersionState(Base):
    __tablename__ = "version_states"
    version_id: Mapped[str] = mapped_column(ForeignKey("gateway.product_versions.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), server_default="published")
    actor_id: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(1024))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("clock_timestamp()"))
    __table_args__ = (CheckConstraint("status IN ('published', 'deprecated', 'withdrawn')", name="status_valid"),)


class PrincipalRow(Base):
    __tablename__ = "principals"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    subject: Mapped[str] = mapped_column(String(512), unique=True)
    service_client_id: Mapped[str | None] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    __table_args__ = (CheckConstraint("kind IN ('user', 'service', 'demo')", name="kind_valid"),)


class PrincipalGrant(Base):
    __tablename__ = "principal_grants"
    principal_id: Mapped[str] = mapped_column(ForeignKey("gateway.principals.id"), primary_key=True)
    policy_id: Mapped[str] = mapped_column(ForeignKey("gateway.policies.id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    roles: Mapped[list] = mapped_column(JSONB)
    entitlements: Mapped[dict] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    __table_args__ = (CheckConstraint("jsonb_typeof(roles) = 'array' AND jsonb_typeof(entitlements) = 'object'", name="grants_shape"),)


class Execution(Record, Base):
    __tablename__ = "executions"
    principal_id: Mapped[str] = mapped_column(ForeignKey("gateway.principals.id"))
    product_id: Mapped[str] = mapped_column(String(128))
    version_id: Mapped[str] = mapped_column(String(128))
    policy_id: Mapped[str] = mapped_column(String(128))
    policy_revision: Mapped[int] = mapped_column(Integer)
    canonical_query: Mapped[dict] = mapped_column(JSONB)
    canonical_hash: Mapped[str] = mapped_column(String(64))
    context: Mapped[str] = mapped_column(String(16))
    request_id: Mapped[str] = mapped_column(String(128))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (ForeignKeyConstraint(["version_id", "product_id"], ["gateway.product_versions.id", "gateway.product_versions.product_id"]),
        ForeignKeyConstraint(["policy_id", "policy_revision"], ["gateway.policy_revisions.policy_id", "gateway.policy_revisions.revision"]),
        UniqueConstraint("id", "principal_id", "version_id", "policy_revision", "canonical_hash", name="uq_execution_result_scope"),
        UniqueConstraint("id", "principal_id", "version_id", "canonical_hash", name="uq_execution_idempotency_scope"),
        CheckConstraint("context IN ('consumer', 'fixture_test')", name="context_valid"),
        CheckConstraint("canonical_hash ~ '^[0-9a-f]{64}$'", name="digest_format"),
        CheckConstraint("jsonb_typeof(canonical_query) = 'object'", name="query_object"),
        CheckConstraint("deadline_at > created_at AND deadline_at <= created_at + interval '120 seconds'", name="bounded_deadline"))


class ExecutionState(Base):
    __tablename__ = "execution_states"
    execution_id: Mapped[str] = mapped_column(ForeignKey("gateway.executions.id"), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), server_default="accepted")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(32))
    __table_args__ = (CheckConstraint("state IN ('accepted', 'running', 'succeeded', 'failed', 'timed_out', 'expired')", name="state_valid"),
        Index("ix_execution_states_queue", "state"))


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    principal_id: Mapped[str] = mapped_column(ForeignKey("gateway.principals.id"), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    key_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    canonical_hash: Mapped[str] = mapped_column(String(64))
    execution_id: Mapped[str] = mapped_column(String(128))
    __table_args__ = (ForeignKeyConstraint(["execution_id", "principal_id", "version_id", "canonical_hash"], ["gateway.executions.id", "gateway.executions.principal_id", "gateway.executions.version_id", "gateway.executions.canonical_hash"]), ForeignKeyConstraint(["version_id", "product_id"], ["gateway.product_versions.id", "gateway.product_versions.product_id"]),
        CheckConstraint("key_digest ~ '^[0-9a-f]{64}$' AND canonical_hash ~ '^[0-9a-f]{64}$'", name="digest_format"))


class CapacityLock(Base):
    __tablename__ = "capacity_locks"
    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)


class ExecutionLease(Base):
    __tablename__ = "execution_leases"
    execution_id: Mapped[str] = mapped_column(ForeignKey("gateway.executions.id"), primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(128))
    lease_token: Mapped[str] = mapped_column(String(128), unique=True)
    generation: Mapped[int] = mapped_column(Integer, server_default="1")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_reserved_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("generation > 0", name="generation_positive"),)


class Result(Record, Base):
    __tablename__ = "results"
    execution_id: Mapped[str] = mapped_column(String(128), unique=True)
    principal_id: Mapped[str] = mapped_column(String(128))
    version_id: Mapped[str] = mapped_column(String(128))
    policy_revision: Mapped[int] = mapped_column(Integer)
    canonical_hash: Mapped[str] = mapped_column(String(64))
    envelope: Mapped[dict] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (ForeignKeyConstraint(["execution_id", "principal_id", "version_id", "policy_revision", "canonical_hash"], ["gateway.executions.id", "gateway.executions.principal_id", "gateway.executions.version_id", "gateway.executions.policy_revision", "gateway.executions.canonical_hash"]),
        CheckConstraint("jsonb_typeof(envelope) = 'object'", name="envelope_object"),
        CheckConstraint("expires_at > created_at AND expires_at <= created_at + interval '24 hours'", name="bounded_retention"),
        UniqueConstraint("id", "principal_id", "version_id", "policy_revision"),
        Index("ix_results_retention", "expires_at"))


class ResultCursor(Record, Base):
    __tablename__ = "result_cursors"
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    result_id: Mapped[str] = mapped_column(String(128))
    principal_id: Mapped[str] = mapped_column(String(128))
    version_id: Mapped[str] = mapped_column(String(128))
    policy_revision: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (ForeignKeyConstraint(["result_id", "principal_id", "version_id", "policy_revision"], ["gateway.results.id", "gateway.results.principal_id", "gateway.results.version_id", "gateway.results.policy_revision"], ondelete="CASCADE"),
        CheckConstraint("position >= 0", name="position_nonnegative"),
        CheckConstraint("token_digest ~ '^[0-9a-f]{64}$'", name="digest_format"))


class AuditEvent(Record, Base):
    __tablename__ = "audit_events"
    actor_id: Mapped[str] = mapped_column(String(128))
    service_client_id: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    product_id: Mapped[str | None] = mapped_column(String(128))
    version_id: Mapped[str | None] = mapped_column(String(128))
    policy_id: Mapped[str | None] = mapped_column(String(128))
    policy_revision: Mapped[int | None] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(32))
    parameter_names: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    parameter_digest: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    row_count: Mapped[int | None] = mapped_column(Integer)
    adapter: Mapped[str | None] = mapped_column(String(128))
    correlation_id: Mapped[str] = mapped_column(String(128))
    __table_args__ = (CheckConstraint("jsonb_typeof(parameter_names) = 'array'", name="parameter_names_array"),
        CheckConstraint("policy_id IS NULL OR policy_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'", name="policy_id_format"),
        CheckConstraint("parameter_digest IS NULL OR parameter_digest ~ '^[0-9a-f]{64}$'", name="digest_format"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration_nonnegative"),
        CheckConstraint("row_count IS NULL OR row_count >= 0", name="row_count_nonnegative"),
        Index("ix_audit_events_retention", "created_at"))
