"""Mandatory transaction-local, strictly redacted audit writes.

Only an operator supplies the HMAC key. Raw parameters never enter an event or
ORM record. Application-role append-only privileges are not tamper-proof storage
against a database owner; the owner can change privileges/triggers and storage.
"""
import hashlib
import hmac
import re
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, ValidationError, field_validator, model_validator

from gateway.auth.principals import Capability
from gateway.contracts.canonical import Array, Digest, Identifier, Scalar, StrictModel, bounded_json, canonical_bytes
from gateway.contracts.errors import ErrorCode, GatewayError, invalid_input
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.identity import Principal
from gateway.contracts.manifest import reject_secret
from gateway.registry.models import AuditEvent
from gateway.registry.repository import UnitOfWork

AuditAction = Capability | Literal["change_policy"]
SourceAccessReason = Literal["APPROVED", "MISSING", "REVOKED", "EXPIRED", "PACKAGE_MISMATCH",
    "AUDIENCE_DENIED", "AMBIGUOUS", "UNSUPPORTED_SOURCE", "INVALID_INPUT", "UNAVAILABLE"]
ParameterMap = Annotated[dict[Identifier, Scalar], Field(max_length=50)]
_PARAMETERS = TypeAdapter(ParameterMap)


def reject_recognized_credential(value: str) -> None:
    """Reject known embedded formats, not every possible unknown secret.

    All raw audit strings must also be bounded server-owned identifiers. Pattern
    checks cannot prove arbitrary text is secret-free; transports generate IDs.
    """
    reject_secret(value)
    # Strong bearer/JWT shapes are recognized anywhere. Ambiguous short key
    # prefixes need a token boundary, preserving words such as task/risk/desk.
    if re.search(r"(?:dpg_(?:svc|demo)_[A-Za-z0-9_-]{43}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|secret://|env:|postgres(?:ql)?://|(?<![A-Za-z0-9_])(?:sk-|ghp_|github_pat_|AKIA)[A-Za-z0-9_-]+)", value):
        raise ValueError("Recognized credentials do not belong in audit text")


def audit_unavailable() -> GatewayError:
    return GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE, str(uuid4()), "Required audit recording is unavailable.")


class AuditEventInput(StrictModel):
    """Internal only. No field can contain parameters, rows, SQL or credentials."""
    actor_id: Identifier
    service_client_id: Identifier | None = None
    action: AuditAction
    product_id: Identifier | None = None
    version_id: Identifier | None = None
    policy_id: Identifier | None = None
    policy_revision: int | None = Field(default=None, ge=1)
    decision: Literal["allow", "deny"]
    parameter_names: Array[Identifier] = Field(default=(), max_length=50)
    parameter_digest: Digest | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=2_147_483_647)
    row_count: int | None = Field(default=None, ge=0, le=2_147_483_647)
    adapter: Identifier | None = None
    correlation_id: Identifier
    source_approval_id: Identifier | None = None
    source_approval_revision: int | None = Field(default=None, ge=1, le=2_147_483_647)
    source_approval_digest: Digest | None = None
    source_approval_state_revision: int | None = Field(default=None, ge=1, le=2)
    source_access_reason_code: SourceAccessReason | None = None

    @field_validator("source_approval_id")
    @classmethod
    def canonical_approval_id(cls, value):
        if value is not None and str(UUID(value)) != value:
            raise ValueError("Approval identity must be a canonical UUID")
        return value

    @model_validator(mode="after")
    def complete_approval_reference(self):
        reference = (self.source_approval_id, self.source_approval_revision,
            self.source_approval_digest, self.source_approval_state_revision)
        if any(value is not None for value in reference) and not all(value is not None for value in reference):
            raise ValueError("Approval audit references must be complete")
        if self.source_approval_id is not None and self.source_access_reason_code is None:
            raise ValueError("Approval references require a bounded decision reason")
        if self.source_access_reason_code == "APPROVED" and (self.source_approval_id is None or self.decision != "allow"):
            raise ValueError("Approval allows require an exact reference")
        if self.decision == "allow" and self.source_access_reason_code is not None and (
            self.source_access_reason_code != "APPROVED" or self.source_approval_state_revision != 1):
            raise ValueError("Approval allows require the active initial state")
        return self

    @field_validator("actor_id", "service_client_id", "product_id", "version_id", "policy_id", "adapter", "correlation_id")
    @classmethod
    def credential_free_identifiers(cls, value):
        if value is not None:
            reject_recognized_credential(value)
        return value

    @model_validator(mode="after")
    def distinct_names(self):
        for name in self.parameter_names:
            self.credential_free_identifiers(name)
        if len(set(self.parameter_names)) != len(self.parameter_names):
            raise ValueError("Parameter names must be distinct")
        if self.parameter_names and self.parameter_digest is None:
            raise ValueError("Parameter summaries require a keyed digest")
        return self


class AuditWriter:
    def __init__(self, hmac_key: bytes):
        if type(hmac_key) is not bytes or not 32 <= len(hmac_key) <= 1024:
            raise ValueError("An explicit operator-owned audit key of at least 32 bytes is required")
        self._hmac_key = hmac_key

    def parameter_summary(self, parameters: dict, *, redaction: dict | None = None) -> tuple[tuple[str, ...], str]:
        """Names include omitted inputs; their values never enter even the HMAC.

        redaction is the pinned manifest's allowlist. Unknown inputs are rejected
        safely rather than accidentally hashing identity/SQL/credential arguments.
        The no-product variant is only for internal named administrative fields.
        """
        try:
            parameters = _PARAMETERS.validate_python(parameters, strict=True)
            bounded_json(parameters)
            for name in parameters:
                reject_recognized_credential(name)
            if redaction is not None:
                if (not isinstance(redaction, dict) or set(parameters) - set(redaction)
                    or any(mode not in {"digest", "omit"} for mode in redaction.values())):
                    raise ValueError("Invalid parameter redaction")
                digest_values = {name: value for name, value in parameters.items() if redaction[name] == "digest"}
            else:
                digest_values = parameters
            digest = hmac.new(self._hmac_key, b"dpg-audit-parameters-v1\0" + canonical_bytes(digest_values), hashlib.sha256).hexdigest()
            return tuple(sorted(parameters)), digest
        except (ValidationError, ValueError, TypeError, GatewayError):
            raise invalid_input("The audit parameter summary is invalid.") from None

    def event(self, actor: Principal, action: AuditAction, *, decision: Literal["allow", "deny"],
              product: PinnedProduct | None = None, policy_id: str | None = None, policy_revision: int | None = None,
              parameters: dict | None = None, duration_ms: int | None = None,
              row_count: int | None = None, correlation_id: str | None = None,
              source_approval_id: str | None = None, source_approval_revision: int | None = None,
              source_approval_digest: str | None = None, source_approval_state_revision: int | None = None,
              source_access_reason_code: SourceAccessReason | None = None) -> AuditEventInput:
        try:
            actor = Principal.model_validate(actor.model_dump(mode="python", warnings=False))
            if product is not None and policy_id is not None and policy_id != product.manifest.policy_id:
                raise ValueError("Audit policy identity conflicts with the pinned product")
            names, digest = self.parameter_summary({} if parameters is None else parameters,
                redaction=product.manifest.redaction if product is not None else None)
            return AuditEventInput(actor_id=actor.id, service_client_id=actor.service_client_id,
                action=action, product_id=product.manifest.product_id if product is not None else None,
                version_id=product.version_id if product is not None else None,
                policy_id=product.manifest.policy_id if product is not None else policy_id,
                policy_revision=policy_revision, decision=decision, parameter_names=names, parameter_digest=digest,
                duration_ms=duration_ms, row_count=row_count,
                adapter=product.manifest.adapter_revision if product is not None else None,
                correlation_id=str(uuid4()) if correlation_id is None else correlation_id,
                source_approval_id=source_approval_id, source_approval_revision=source_approval_revision,
                source_approval_digest=source_approval_digest, source_approval_state_revision=source_approval_state_revision,
                source_access_reason_code=source_access_reason_code)
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise invalid_input("The audit event is invalid.") from None

    async def append(self, uow: UnitOfWork, event: AuditEventInput) -> str:
        """Flush before dispatch, result release or publication; never commits.

        Every failure rolls back all caller changes, even if a caller catches the
        safe error. No required audit write can be suppressed to commit a mutation.
        """
        try:
            event = AuditEventInput.model_validate(event.model_dump(mode="python", warnings=False))
            if not uow.session.in_transaction():
                raise ValueError("An audit write requires a caller transaction")
            record = AuditEvent(**event.model_dump(mode="json", warnings=False, exclude_none=True))
            uow.session.add(record)
            await uow.session.flush()
            return record.id
        except Exception:
            try:
                await uow.session.rollback()
            except Exception:
                pass
            raise audit_unavailable() from None
