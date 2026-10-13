"""Complete immutable results, current authorization and transaction-audited release."""
from datetime import datetime, timedelta
from time import perf_counter_ns

from gateway.adapters.base import (CubeBinding, authorization_scope_digest, check_binding,
    validate_data_decision, validate_query_visibility)
from gateway.contracts.canonical import canonical_bytes, canonical_digest, timestamp
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.execution import ExecutionSpec, FreshnessAssessment, PinnedProduct, ResultEnvelope
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import iso_date
from gateway.contracts.query import QueryRequest
from gateway.registry.models import Execution, Result


def _error(execution, code=ErrorCode.BINDING_DRIFT):
    message = {ErrorCode.BINDING_DRIFT: "The stored execution context is inconsistent.",
        ErrorCode.NOT_FOUND_OR_FORBIDDEN: "The requested product is unavailable.",
        ErrorCode.RESOURCE_LIMIT: "The complete result exceeds the current limits."}[code]
    return GatewayError(code, execution.request_id, message)


def _complete(execution: Execution, envelope: ResultEnvelope, executed: Decision) -> ResultEnvelope:
    envelope = ResultEnvelope.model_validate(envelope.model_dump(mode="python", warnings=False))
    executed = Decision.model_validate(executed.model_dump(mode="python", warnings=False))
    if (execution.context != "consumer" or envelope.state != "succeeded" or envelope.next_cursor is not None
        or envelope.row_count != len(envelope.rows)
        or envelope.freshness.assessed_at < envelope.completed_at
        or envelope.execution_id != execution.id or envelope.product_id != execution.product_id
        or envelope.version != execution.canonical_query.get("version")
        or envelope.accepted_at != timestamp(execution.created_at)
        or executed.principal_id != execution.principal_id or executed.policy_revision != execution.policy_revision
        or not set(envelope.typed_schema) <= set(executed.visible_fields)
        or canonical_digest(execution.canonical_query) != execution.canonical_hash
        or envelope.row_count > executed.limits.max_rows
        or len(canonical_bytes(envelope)) > executed.limits.max_bytes):
        raise ValueError("Incomplete or inconsistent execution")
    return envelope


def prepare_stored_result(execution: Execution, envelope: ResultEnvelope,
                          executed_decision: Decision, now: datetime) -> Result:
    """Pure completion seam. Caller supplies DB time and inserts under its lease fence.

    The application deliberately cannot set created_at. Its database default occurs
    after this DB timestamp, so expiry is never more than 24 hours after creation.
    No rows or terminal state are committed by this function.
    """
    try:
        now = timestamp(now)
        envelope = _complete(execution, envelope, executed_decision)
        if envelope.completed_at > now:
            raise ValueError("Future completion")
        return Result(execution_id=execution.id, principal_id=execution.principal_id,
            version_id=execution.version_id, policy_revision=execution.policy_revision,
            canonical_hash=execution.canonical_hash, envelope=envelope.model_dump(mode="json"),
            expires_at=now + timedelta(hours=24))
    except (ValueError, TypeError, KeyError, AttributeError, GatewayError):
        raise _error(execution) from None


def validate_result_scope(execution: Execution, product: PinnedProduct, binding: CubeBinding,
                          executed: Decision, current: Decision, *, enforce_date_limit: bool = True) -> ExecutionSpec:
    """Shared authority gate, with date limits deferred only for cursor resolution."""
    try:
        product = PinnedProduct.model_validate(product.model_dump(mode="python"))
        executed = validate_data_decision(executed, execution.request_id, binding.constraint_members)
        current = validate_data_decision(current, execution.request_id, binding.constraint_members)
        spec = ExecutionSpec(principal_id=execution.principal_id, pinned_product=product,
            canonical_query=execution.canonical_query, canonical_hash=execution.canonical_hash,
            policy_revision=execution.policy_revision, context=execution.context, request_id=execution.request_id)
        if (execution.context != "consumer" or binding.context != "consumer"
            or product.version_id != execution.version_id or product.manifest.product_id != execution.product_id
            or product.manifest.policy_id != execution.policy_id):
            raise ValueError("Mismatched pinned execution")
        check_binding(binding, product.manifest, digest=product.binding_digest)
        if (executed.principal_id != execution.principal_id or executed.policy_revision != execution.policy_revision
            or authorization_scope_digest(current) != authorization_scope_digest(executed)
            or not set(current.visible_fields) <= set(executed.visible_fields)):
            raise _error(execution, ErrorCode.NOT_FOUND_OR_FORBIDDEN)
        if not set(executed.visible_fields) <= set(product.manifest.outputs):
            raise ValueError("Unknown executed fields")
        grain = {name for name, item in binding.fields.items() if item.kind != "measure"}
        if not grain <= set(current.visible_fields) or not grain <= set(executed.visible_fields):
            raise _error(execution, ErrorCode.NOT_FOUND_OR_FORBIDDEN)
        request = QueryRequest.model_validate(spec.canonical_query)
        validate_query_visibility(binding, request, current, execution.request_id)
        bounds = product.manifest.date_boundaries
        if enforce_date_limit and bounds is not None and (iso_date(request.inputs[bounds.end_input]) - iso_date(request.inputs[bounds.start_input])).days > current.limits.max_date_range_days:
            raise _error(execution, ErrorCode.RESOURCE_LIMIT)
        return spec
    except GatewayError as error:
        if error.code in {ErrorCode.NOT_FOUND_OR_FORBIDDEN, ErrorCode.RESOURCE_LIMIT}:
            raise
        raise _error(execution) from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _error(execution) from None


def project_stored_result(execution: Execution, envelope: ResultEnvelope, product: PinnedProduct,
                          binding: CubeBinding, executed: Decision, current: Decision, *, now: datetime) -> ResultEnvelope:
    """Validate the whole stored answer before safe field projection or pagination.

    Reassess age against the existing source watermark at explicit current time.
    Neither source-load time, completion time nor immutable storage is changed.
    """
    validate_result_scope(execution, product, binding, executed, current)
    try:
        envelope = _complete(execution, envelope, executed)
        manifest = product.manifest
        schema = envelope.typed_schema
        if not set(current.visible_fields) <= set(schema):
            raise _error(execution, ErrorCode.NOT_FOUND_OR_FORBIDDEN)
        bounds = manifest.date_boundaries
        expected_range = None if bounds is None else {"start": execution.canonical_query["inputs"][bounds.start_input],
            "end": execution.canonical_query["inputs"][bounds.end_input], "interval": bounds.interval}
        expected_provenance = {"binding_id":manifest.binding_id,"binding_revision":manifest.binding_revision,
            "adapter_revision":manifest.adapter_revision,"model_bundle_digest":manifest.model_bundle_digest,"links":[]}
        if (envelope.product_digest != manifest.content_digest or envelope.grain != manifest.grain
            or envelope.timezone != manifest.timezone or canonical_digest(envelope.effective_range) != canonical_digest(expected_range)
            or canonical_digest(envelope.provenance) != canonical_digest(expected_provenance)
            or canonical_digest(schema) != canonical_digest({name:manifest.outputs[name] for name in schema})
            or canonical_digest(envelope.units) != canonical_digest({name:unit for name,unit in manifest.units.items() if name in schema})
            or envelope.freshness.max_age_seconds != manifest.freshness.max_age_seconds
            or envelope.freshness.assessed_at < envelope.completed_at
            or manifest.freshness.required and envelope.freshness.status != "fresh"
            or envelope.warnings != (() if envelope.freshness.status == "fresh" else ("Source-load freshness is "+envelope.freshness.status+".",))):
            raise ValueError("Stored metadata differs from pinned context")
        grain_names = [name for name,item in binding.fields.items() if item.kind != "measure"]
        grains = set()
        for row in envelope.rows:
            grain = tuple(row[name] for name in grain_names)
            if grain in grains:
                raise ValueError("Duplicate stored grain")
            grains.add(grain)
            for name,item in binding.fields.items():
                if item.kind == "time" and not expected_range["start"] <= row[name] < expected_range["end"]:
                    raise ValueError("Stored bucket outside execution range")
        now = timestamp(now)
        age = (now - envelope.source_watermark).total_seconds() if envelope.source_watermark is not None else None
        known = age is not None and age >= 0
        status = "unknown" if not known else "fresh" if age <= manifest.freshness.max_age_seconds else "stale"
        if manifest.freshness.required and status != "fresh":
            raise GatewayError(ErrorCode.FRESHNESS_UNKNOWN if status == "unknown" else ErrorCode.STALE_DATA,
                execution.request_id, "The recorded source-load freshness cannot currently be established."
                if status == "unknown" else "The recorded source-load watermark is now too old.")
        freshness = FreshnessAssessment(status=status, assessed_at=now, age_seconds=age if known else None,
            max_age_seconds=manifest.freshness.max_age_seconds)
        fields = set(current.visible_fields)
        data = envelope.model_dump(mode="python")
        data.update(freshness=freshness, warnings=[] if status == "fresh" else ["Source-load freshness is "+status+"."],
            typed_schema={name:value for name,value in schema.items() if name in fields},
            units={name:value for name,value in envelope.units.items() if name in fields},
            rows=[{name:value for name,value in row.items() if name in fields} for row in envelope.rows])
        if (envelope.row_count > min(manifest.limits.max_rows,current.limits.max_rows)
            or len(canonical_bytes(data)) > min(manifest.limits.max_bytes,current.limits.max_bytes)):
            raise _error(execution, ErrorCode.RESOURCE_LIMIT)
        return ResultEnvelope.model_validate(data)
    except GatewayError as error:
        if error.code in {ErrorCode.NOT_FOUND_OR_FORBIDDEN, ErrorCode.RESOURCE_LIMIT, ErrorCode.FRESHNESS_UNKNOWN, ErrorCode.STALE_DATA}:
            raise
        raise _error(execution) from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _error(execution) from None


class ResultService:
    """Fetch a current-authorized view; only return after required audits commit.

    Append an outcome after the fresh authorization event, never UPDATE it.
    row_count is the validated complete answer total, including on cursor pages;
    it is unknown for ExecutionView. duration_ms is monotonic request handling
    through response construction, excluding the final audit flush, commit and
    network delivery. Both required records and any cursor commit together.
    """
    def __init__(self, registry, policy, bindings, clock):
        self.registry, self.policy, self.bindings, self.clock = registry, policy, bindings, clock

    async def _denied(self, principal, request_id):
        from sqlalchemy.exc import SQLAlchemyError
        # Deliberately no target/row metadata for guessed IDs and foreign cursors.
        try:
            async with self.registry.transaction() as uow:
                await self.policy.audit.append(uow, self.policy.audit.event(principal, "result",
                    decision="deny", correlation_id=request_id))
        except GatewayError as error:
            if error.code != ErrorCode.CONFLICT:
                raise
            raise GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE, request_id, "The result store is unavailable.") from None
        except SQLAlchemyError:
            raise GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE, request_id, "The result store is unavailable.") from None

    async def get(self, principal, execution_id: str, cursor: str | None = None):
        from uuid import uuid4
        from pydantic import TypeAdapter
        from sqlalchemy import select, text
        from sqlalchemy.exc import SQLAlchemyError
        from gateway.contracts.canonical import Identifier
        from gateway.contracts.execution import ErrorView, ExecutionView
        from gateway.contracts.identity import Principal
        from gateway.contracts.manifest import validate_manifest
        from gateway.registry.models import (BindingRevision, ExecutionAuthorization, ExecutionState,
            ProductVersion, VersionState)
        from gateway.registry.repository import not_found
        from .cursors import CursorStore, cursor_digest, result_page

        started_ns = perf_counter_ns()
        request_id = str(uuid4())
        policy_denied = False
        try:
            principal = Principal.model_validate(principal.model_dump(mode="python"))
        except (ValueError, TypeError, AttributeError):
            raise GatewayError(ErrorCode.UNAUTHENTICATED, request_id, "A valid identity is required.") from None
        try:
            # Same outward denial for malformed, guessed and foreign object references.
            try:
                TypeAdapter(Identifier).validate_python(execution_id, strict=True)
                if cursor is not None: cursor_digest(cursor)
            except (ValueError, TypeError):
                raise not_found() from None
            async with self.registry.transaction() as uow:
                execution = await uow.session.scalar(select(Execution).where(
                    Execution.id == execution_id, Execution.principal_id == principal.id,
                    Execution.context == "consumer"))
                if execution is None: raise not_found()
                await uow.lock_product(execution.product_id)
                record = (await uow.session.execute(select(ProductVersion, BindingRevision).join(
                    BindingRevision, BindingRevision.id == ProductVersion.binding_id).where(
                    ProductVersion.id == execution.version_id, ProductVersion.product_id == execution.product_id))).one_or_none()
                if record is None: raise not_found()
                version, binding_record = record
                product = PinnedProduct(manifest=validate_manifest(version.manifest),
                    binding_digest=version.binding_digest, version_id=version.id)
                if product.manifest.content_digest != version.content_digest or binding_record.content_digest != version.binding_digest:
                    raise _error(execution)
                try:
                    current = await self.policy.authorize(principal, "result", product, uow,
                        correlation_id=request_id, parameters=execution.canonical_query.get("inputs"))
                except GatewayError:
                    # PolicyService persists its own denied event after rollback.
                    policy_denied = True
                    raise
                # Hold the actual mutable lifecycle row through audit commit, so a
                # concurrent withdrawal serializes with this release.
                lifecycle = await uow.session.scalar(select(VersionState).where(
                    VersionState.version_id == version.id).with_for_update().execution_options(populate_existing=True))
                if lifecycle is None or lifecycle.status not in {"published", "deprecated"}: raise not_found()
                authorization = await uow.session.get(ExecutionAuthorization, execution.id)
                if authorization is None: raise _error(execution)
                executed = Decision.model_validate(authorization.decision)
                admitted_principal = Principal.model_validate(authorization.principal_snapshot)
                if (canonical_digest(executed) != authorization.full_decision_digest
                    or authorization_scope_digest(executed) != authorization.scope_digest):
                    raise _error(execution)
                if admitted_principal != principal: raise not_found()
                binding = await self.bindings.resolve(product.manifest.binding_id, product.manifest.binding_revision)
                validate_result_scope(execution, product, binding, executed, current, enforce_date_limit=False)
                state = await uow.session.scalar(select(ExecutionState).where(
                    ExecutionState.execution_id == execution.id).with_for_update().execution_options(populate_existing=True))
                if state is None: raise _error(execution)
                error = None
                if state.state in {"failed", "timed_out"}:
                    error = ErrorView(code=state.error_code, request_id=execution.request_id,
                        safe_message="The execution did not succeed.")
                view = ExecutionView(execution_id=execution.id, state=state.state, accepted_at=execution.created_at,
                    started_at=state.started_at, completed_at=state.completed_at, error=error)
                if state.state != "succeeded":
                    if cursor is not None: raise not_found()
                    validate_result_scope(execution, product, binding, executed, current)
                    response = view
                else:
                    result = await uow.session.scalar(select(Result).where(Result.execution_id == execution.id,
                        Result.principal_id == principal.id, Result.version_id == execution.version_id,
                        Result.policy_revision == execution.policy_revision, Result.canonical_hash == execution.canonical_hash))
                    if result is None: raise _error(execution)
                    # The real DB clock prevents a slow or incorrectly backdated
                    # process clock extending result/cursor lifetime.
                    now = max(timestamp(self.clock.now()), timestamp(await uow.session.scalar(text("SELECT clock_timestamp()"))))
                    if result.expires_at <= now:
                        if cursor is not None: raise not_found()
                        validate_result_scope(execution, product, binding, executed, current)
                        response = ExecutionView(**{**view.model_dump(mode="python"), "state":"expired"})
                    else:
                        stored = ResultEnvelope.model_validate(result.envelope)
                        if (stored.started_at != state.started_at or stored.completed_at != state.completed_at):
                            raise _error(execution)
                        # Resolve only after current authority, identity and expiry,
                        # before freshness or limit projection can change its error.
                        cursors = CursorStore(uow, now)
                        offset = 0 if cursor is None else await cursors.resolve(cursor, result)
                        complete = project_stored_result(execution, stored, product, binding, executed, current, now=now)
                        if lifecycle.status == "deprecated":
                            # Current server-owned lifecycle, never a mutable reason
                            # or stored warning. Stored evidence has been validated.
                            data = complete.model_dump(mode="python")
                            data["warnings"] = (*complete.warnings, "This product version is deprecated.")
                            if len(canonical_bytes(data)) > min(product.manifest.limits.max_bytes, current.limits.max_bytes):
                                raise _error(execution, ErrorCode.RESOURCE_LIMIT)
                            complete = ResultEnvelope.model_validate(data)
                        page_size = min(product.manifest.limits.page_size, current.limits.page_size)
                        token = None
                        if offset + page_size < complete.row_count:
                            token = await cursors.issue(principal.id, execution.id, current.policy_revision,
                                offset + page_size, result.expires_at)
                        response = result_page(complete, offset=offset, page_size=page_size, next_cursor=token,
                            max_bytes=min(product.manifest.limits.max_bytes, current.limits.max_bytes))
                duration_ms = min(2_147_483_647, max(0, (perf_counter_ns() - started_ns) // 1_000_000))
                await self.policy.audit.append(uow, self.policy.audit.event(principal, "result",
                    product=product, policy_revision=current.policy_revision, decision="allow",
                    parameters=execution.canonical_query.get("inputs"), correlation_id=request_id,
                    row_count=response.row_count if isinstance(response, ResultEnvelope) else None,
                    duration_ms=duration_ms))
            # Exiting Registry.transaction commits the audit AND cursor before bytes
            # leave this service. A flush-only success is deliberately insufficient.
            return response
        except GatewayError as error:
            # Result GET has no publication/idempotency optimistic-write conflict.
            # Registry may already have translated a DB commit/trigger failure;
            # classify it at this boundary without changing other callers' codes.
            if error.code == ErrorCode.CONFLICT:
                raise GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE, request_id, "The result store is unavailable.") from None
            if error.code == ErrorCode.NOT_FOUND_OR_FORBIDDEN:
                if not policy_denied: await self._denied(principal, request_id)
                raise GatewayError(ErrorCode.NOT_FOUND_OR_FORBIDDEN, request_id, "The requested product is unavailable.") from None
            raise GatewayError(error.code, request_id, error.safe_message) from None
        except SQLAlchemyError:
            raise GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE, request_id, "The result store is unavailable.") from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise GatewayError(ErrorCode.BINDING_DRIFT, request_id, "The stored execution context is inconsistent.") from None
