"""Current exact-scope policy decisions and independently versioned changes.

Allowed decisions and mutations are audited in the caller transaction. A denial
rolls that transaction back first, then durably appends just its redacted denied
record in an independent transaction, and raises the safe shared error. Callers
must not resume an aborted UoW. There is no positive authorization cache.

An execute gate must be committed before an upstream request. Every worker start,
result/cursor/cache fetch and publishing operation must use a fresh gate. This
module supplies that gate; it does not dispatch queries or release results.
"""
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from gateway.auth.principals import PolicyGrant, PrincipalGrants, PrincipalStore
from gateway.contracts.canonical import Array, Identifier, StrictModel, canonical_digest
from gateway.contracts.errors import ErrorCode, GatewayError, invalid_input
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.identity import Decision, Principal, RowConstraint
from gateway.contracts.manifest import Limits
from gateway.registry.models import PolicyRevision, PrincipalRow, VersionState
from gateway.registry.repository import Registry, UnitOfWork, not_found
from .audit import AuditWriter, audit_unavailable, reject_recognized_credential


class TenantConstraint(StrictModel):
    member: Identifier
    operator: Literal["eq"]
    source: Literal["grant.tenant_id"]


class EntitlementConstraint(StrictModel):
    member: Identifier
    operator: Literal["in"]
    source: Annotated[str, Field(pattern=r"^grant\.entitlements\.[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]


class PolicyRules(StrictModel):
    """One bounded inert V1 policy shape, not client-provided executable rules."""
    tenant_ids: Array[Identifier] = Field(max_length=100)
    tenant_constraint: TenantConstraint
    entitlement_constraint: EntitlementConstraint
    visible_fields: Array[Identifier] = Field(min_length=1, max_length=50)
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="after")
    def coherent_scopes(self):
        if len(set(self.tenant_ids)) != len(self.tenant_ids) or len(set(self.visible_fields)) != len(self.visible_fields):
            raise ValueError("Policy tenants and visible fields must be distinct")
        if self.tenant_constraint.member == self.entitlement_constraint.member:
            raise ValueError("Tenant and entitlement constraints must be independent")
        return self


class PolicyContent(StrictModel):
    rules: PolicyRules
    reason: Annotated[str, Field(min_length=1, max_length=1024)]

    @field_validator("reason")
    @classmethod
    def safe_reason(cls, value):
        if not value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("A bounded explanation is required")
        reject_recognized_credential(value)
        return value


def policy_unavailable() -> GatewayError:
    return GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE, str(uuid4()), "Current access policy is unavailable.")


def _scope(grants: PrincipalGrants, policy_id: str, action: str, rules: PolicyRules) -> PolicyGrant:
    # Select exact policy and tenant before checking capabilities or entitlements.
    # An action in another scope can never supply the missing part of this scope.
    scopes = [grant for grant in grants.grants if grant.policy_id == policy_id and grant.tenant_id in rules.tenant_ids]
    if len(scopes) != 1 or action not in scopes[0].capabilities:
        raise not_found()
    return scopes[0]


def derive_decision(grants: PrincipalGrants, action: str, product: PinnedProduct,
                    rules: PolicyRules, revision: int) -> Decision:
    """Pure internal derivation. External endpoints must never accept these types."""
    try:
        grants = PrincipalGrants.model_validate(grants.model_dump(mode="python", warnings=False))
        rules = PolicyRules.model_validate(rules.model_dump(mode="python", warnings=False))
        product = PinnedProduct.model_validate(product.model_dump(mode="python", warnings=False))
        if type(revision) is not int or revision < 1:
            raise ValueError("Invalid policy revision")
        grant = _scope(grants, product.manifest.policy_id, action, rules)
        entitlement = rules.entitlement_constraint.source.removeprefix("grant.entitlements.")
        values = grant.entitlements.get(entitlement)
        if not values and action in {"discover", "describe", "execute", "result"}:
            raise not_found()
        if set(rules.visible_fields) - set(product.manifest.outputs):
            raise ValueError("Policy fields are not in the pinned product")
        limits = Limits(**{name: min(getattr(rules.limits, name), getattr(product.manifest.limits, name)) for name in Limits.model_fields})
        constraints = [RowConstraint(member=rules.tenant_constraint.member, operator="eq", values=[grant.tenant_id])]
        if values:
            constraints.append(RowConstraint(member=rules.entitlement_constraint.member, operator="in", values=values))
        return Decision(principal_id=grants.principal_id, policy_revision=revision, tenant_id=grant.tenant_id,
            row_constraints=constraints, visible_fields=rules.visible_fields, limits=limits)
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise policy_unavailable() from None


class ScopedAuthorization(StrictModel):
    """Internal metadata scope proof, never executable authority or public input."""
    principal_id: Identifier
    policy_id: Identifier
    policy_revision: int = Field(ge=1)
    tenant_id: Identifier
    action: Literal["author", "policy_admin", "audit_view"]


class PolicyService:
    def __init__(self, registry: Registry, principals: PrincipalStore, audit: AuditWriter):
        self.registry, self.principals, self.audit = registry, principals, audit

    async def _identity(self, principal: Principal, uow: UnitOfWork) -> None:
        identity = (await uow.session.execute(select(PrincipalRow.kind, PrincipalRow.service_client_id).where(
            PrincipalRow.id == principal.id, PrincipalRow.active.is_(True)))).one_or_none()
        if identity is None or identity.kind != principal.kind or (principal.kind == "service" and identity.service_client_id != principal.service_client_id):
            raise not_found()

    async def _current(self, policy_id: str, uow: UnitOfWork) -> tuple[PolicyRevision, PolicyContent]:
        revision = await uow.lock_policy(policy_id)
        try:
            content = PolicyContent.model_validate(revision.content)
            if canonical_digest(revision.content) != revision.content_digest:
                raise ValueError("Policy digest mismatch")
            return revision, content
        except (ValidationError, ValueError, TypeError, GatewayError):
            raise policy_unavailable() from None

    async def _deny(self, uow: UnitOfWork, actor: Principal, action: str, *, product: PinnedProduct | None,
                    policy_revision: int | None, correlation_id: str, policy_id: str | None = None, parameters: dict | None = None) -> None:
        # Audit has no principal/policy/version foreign keys, so the independent
        # insert cannot wait on the policy lock or flush/commit caller mutations.
        # Rollback first also avoids holding any app-owned lock during this write.
        try:
            await uow.session.rollback()
            event = self.audit.event(actor, action, product=product, policy_id=policy_id, policy_revision=policy_revision,
                decision="deny", parameters=parameters, correlation_id=correlation_id)
            async with self.registry.transaction() as denied_uow:
                await self.audit.append(denied_uow, event)
        except Exception:
            raise audit_unavailable() from None

    async def authorize(self, principal: Principal, action: str, product: PinnedProduct, uow: UnitOfWork,
                        *, parameters: dict | None = None, correlation_id: str | None = None,
                        duration_ms: int | None = None, row_count: int | None = None) -> Decision:
        """Fresh grant/policy read and a mandatory pre-operation audit flush.

        No tenant, roles or entitlements are accepted from request arguments.
        Denial always aborts the caller UoW and records a durable denied decision.
        Parameters are optional internal audit context, checked against redaction.
        """
        request_id = str(uuid4())
        revision = None
        try:
            principal = Principal.model_validate(principal.model_dump(mode="python", warnings=False))
            request_id = self.audit.event(principal, action, decision="deny", correlation_id=correlation_id).correlation_id
            product = PinnedProduct.model_validate(product.model_dump(mode="python", warnings=False))
            if action not in {"discover", "describe", "execute", "result", "author", "publish", "policy_admin", "operate", "audit_view"}:
                raise invalid_input("The authorization action is invalid.")
            await self._identity(principal, uow)
            grants = await self.principals.get_current(principal.id, session=uow.session)
            current, content = await self._current(product.manifest.policy_id, uow)
            revision = current.revision
            decision = derive_decision(grants, action, product, content.rules, revision)
            if action in {"discover", "describe", "execute", "result"}:
                state = await uow.session.scalar(select(VersionState.status).where(VersionState.version_id == product.version_id))
                if state not in {"published", "deprecated"}:
                    raise not_found()
            await self.audit.append(uow, self.audit.event(principal, action, product=product, policy_revision=revision,
                decision="allow", parameters=parameters, correlation_id=request_id, duration_ms=duration_ms, row_count=row_count))
            return decision
        except GatewayError as error:
            if error.code == ErrorCode.NOT_FOUND_OR_FORBIDDEN:
                await self._deny(uow, principal, action, product=product, policy_revision=revision,
                    correlation_id=request_id, parameters=parameters)
                raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN, request_id, "The requested product is unavailable.") from None
            if error.code == ErrorCode.UPSTREAM_UNAVAILABLE:
                await self._deny(uow, principal, action, product=product, policy_revision=revision,
                    correlation_id=request_id, parameters=parameters)
            else:
                await uow.session.rollback()
            raise GatewayError(error.code, request_id, error.safe_message) from None
        except SQLAlchemyError:
            await self._deny(uow, principal, action, product=product, policy_revision=revision,
                correlation_id=request_id, parameters=parameters)
            raise GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE, request_id, "Current access policy is unavailable.") from None
        except (ValidationError, ValueError, TypeError, AttributeError):
            await uow.session.rollback()
            raise GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE, request_id, "Current access policy is unavailable.") from None

    async def authorize_scope(self, principal: Principal, policy_id: str, action: str,
                              uow: UnitOfWork, *, correlation_id: str | None = None) -> ScopedAuthorization:
        """Fresh exact-scope administrative permission with mandatory allow/deny audit.

        No product, row constraints, or consumer authority is constructed. Denial
        aborts the caller transaction, exactly as product authorization does.
        """
        if action not in {"author", "policy_admin", "audit_view"}:
            raise invalid_input("The authorization action is invalid.")
        request_id = str(uuid4())
        revision = None
        try:
            principal = Principal.model_validate(principal.model_dump(mode="python", warnings=False))
            request_id = self.audit.event(principal, action, policy_id=policy_id,
                decision="deny", correlation_id=correlation_id).correlation_id
            await self._identity(principal, uow)
            grants = await self.principals.get_current(principal.id, session=uow.session)
            current, content = await self._current(policy_id, uow)
            revision = current.revision
            grant = _scope(grants, policy_id, action, content.rules)
            authorization = ScopedAuthorization(principal_id=principal.id, policy_id=policy_id,
                policy_revision=revision, tenant_id=grant.tenant_id, action=action)
            await self.audit.append(uow, self.audit.event(principal, action, policy_id=policy_id,
                policy_revision=revision, decision="allow", correlation_id=request_id))
            return authorization
        except GatewayError as error:
            if error.code in {ErrorCode.NOT_FOUND_OR_FORBIDDEN, ErrorCode.UPSTREAM_UNAVAILABLE}:
                await self._deny(uow, principal, action, product=None, policy_id=policy_id,
                    policy_revision=revision, correlation_id=request_id)
            else:
                await uow.session.rollback()
            message = "The requested product is unavailable." if error.code == ErrorCode.NOT_FOUND_OR_FORBIDDEN else error.safe_message
            raise GatewayError(error.code, request_id, message) from None
        except SQLAlchemyError:
            await self._deny(uow, principal, action, product=None, policy_id=policy_id,
                policy_revision=revision, correlation_id=request_id)
            raise policy_unavailable() from None
        except (ValidationError, ValueError, TypeError, AttributeError):
            await uow.session.rollback()
            raise policy_unavailable() from None

    async def change_policy(self, actor: Principal, policy_id: str, rules: dict, reason: str) -> int:
        """Separate policy-admin permission; append revision, head and audit atomically."""
        request_id = str(uuid4())
        revision = None
        try:
            try:
                actor = Principal.model_validate(actor.model_dump(mode="python", warnings=False))
                # Validate bounded actor/target context before opening a UoW or
                # writing a denial. The failed request's rules/reason are omitted.
                request_id = self.audit.event(actor, "change_policy", policy_id=policy_id,
                    decision="deny", correlation_id=request_id).correlation_id
            except (ValidationError, ValueError, TypeError, AttributeError):
                raise invalid_input("The policy administration context is invalid.") from None
            async with self.registry.transaction() as uow:
                try:
                    await self._identity(actor, uow)
                    grants = await self.principals.get_current(actor.id, session=uow.session)
                    current, content = await self._current(policy_id, uow)
                    revision = current.revision
                    _scope(grants, policy_id, "policy_admin", content.rules)
                except GatewayError as error:
                    await self._deny(uow, actor, "change_policy", product=None,
                        policy_revision=revision, correlation_id=request_id, policy_id=policy_id)
                    message = "The requested product is unavailable." if error.code == ErrorCode.NOT_FOUND_OR_FORBIDDEN else error.safe_message
                    raise GatewayError(error.code, request_id, message) from None
                except SQLAlchemyError:
                    await self._deny(uow, actor, "change_policy", product=None,
                        policy_revision=revision, correlation_id=request_id, policy_id=policy_id)
                    raise GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE, request_id,
                        "Current access policy is unavailable.") from None
                try:
                    changed = PolicyContent.model_validate({"rules": rules, "reason": reason})
                except (ValidationError, ValueError, TypeError):
                    raise invalid_input("The policy rules or reason are invalid.") from None
                revision += 1
                document = changed.model_dump(mode="json", warnings=False)
                uow.session.add(PolicyRevision(policy_id=policy_id, revision=revision, content=document,
                    content_digest=canonical_digest(document), actor_id=actor.id))
                # Immediate revision FK points to the existing policy; insert the
                # immutable revision before advancing its deferred head reference.
                await uow.session.flush()
                head = await uow.lock_policy_head(policy_id)
                head.current_revision = revision
                await self.audit.append(uow, self.audit.event(actor, "change_policy", policy_id=policy_id, policy_revision=revision,
                    decision="allow", parameters={"reason": reason}, correlation_id=request_id))
                return revision
        except GatewayError:
            raise
        except (ValidationError, ValueError, TypeError, AttributeError, SQLAlchemyError):
            raise policy_unavailable() from None
