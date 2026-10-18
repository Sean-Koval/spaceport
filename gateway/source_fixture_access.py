"""Private inert fixture-owner snapshots and checked publication entrypoints.

A loaded snapshot is never a permit. Current Decisions and one final SQL batch
are required before dispatch or release. Successful invocation audits correlate
to the caller-generated, SQL-validated unique invocation ID; denial audits retain the caller request ID.
"""
import json
from typing import Literal

from pydantic import Field, TypeAdapter
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from gateway.contracts.canonical import Array, AwareTimestamp, Digest, Identifier, canonical_digest
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.errors import GatewayError
from gateway.contracts.identity import Decision, Principal
from gateway.registry.repository import not_found
from gateway.source_access_contracts import ApprovalModel, ReviewedPackagePins, RuntimeApprovalProjection, _fresh, _plain


class FixtureCaseReview(ApprovalModel):
    invocation_id: str
    fixture_id: Identifier
    kind: Literal['known_answer', 'input_negative', 'permission_negative']
    fixture_version: Identifier
    case_digest: Digest
    principal: Principal
    case: dict
    catalog_digest: Digest


class FixtureInvocationSnapshot(ApprovalModel):
    invocation_id: str
    product: PinnedProduct
    draft_id: Identifier
    draft_digest: Digest
    catalog_digest: Digest
    execution_contract_digest: Digest
    consumer_binding_digest: Digest
    fixture_binding_digest: Digest
    actor: Principal
    accepted: dict
    effective_deadline_at: AwareTimestamp
    cases: Array[FixtureCaseReview] = Field(min_length=2, max_length=8)


class FixtureAttemptSnapshot(ApprovalModel):
    attempt_id: Identifier
    invocation_id: str
    fixture_id: Identifier
    product: PinnedProduct
    principal: Principal
    actor: Principal
    admitted_decision: Decision
    accepted: dict
    effective_deadline_at: AwareTimestamp


class FixtureTestSnapshot(ApprovalModel):
    invocation: FixtureInvocationSnapshot
    attempts: Array[FixtureAttemptSnapshot] = Field(max_length=8)


def _identifier(value):
    return TypeAdapter(Identifier).validate_python(value, strict=True)


def _snapshot(value, model):
    from gateway.source_access import AcceptedApprovalRef, _uuid
    result = model.model_validate(_plain(value))
    if model is FixtureInvocationSnapshot:
        _uuid(result.invocation_id)
        accepted = AcceptedApprovalRef.model_validate(result.accepted)
        if accepted.principal != result.actor or accepted.effective_deadline_at != result.effective_deadline_at:
            raise ValueError('Original fixture actor differs')
        if len({case.fixture_id for case in result.cases}) != len(result.cases):
            raise ValueError('Duplicate fixture identity')
        from gateway.publishing.test_runner import ApprovedFixture
        for case in result.cases:
            parsed = ApprovedFixture.model_validate(case.case)
            if (case.invocation_id != result.invocation_id or case.catalog_digest != result.catalog_digest
                or parsed.fixture.id != case.fixture_id or parsed.kind != case.kind
                or parsed.fixture.version != case.fixture_version or parsed.principal_id != case.principal.id
                or canonical_digest(parsed) != case.case_digest):
                raise ValueError('Fixture case identity differs')
    elif model is FixtureAttemptSnapshot:
        _uuid(result.invocation_id)
        accepted = AcceptedApprovalRef.model_validate(result.accepted)
        if accepted.principal != result.principal or accepted.effective_deadline_at != result.effective_deadline_at:
            raise ValueError('Original fixture principal differs')
    return result


async def accept_fixture_invocation(service, uow, *, draft_id, actor, catalog_canonical, run_id, request_id):
    from gateway.source_access import _uuid
    try:
        draft_id = _identifier(draft_id)
        actor = _fresh(actor, Principal)
        _uuid(run_id); _uuid(request_id)
        if type(catalog_canonical) is not str or not 1 <= len(catalog_canonical.encode()) <= 65536:
            raise ValueError('Invalid fixture catalog')
    except (ValueError, TypeError, AttributeError, GatewayError):
        raise not_found() from None
    try:
        await uow.session.flush()
        value = await uow.session.scalar(text('SELECT gateway.source_accept_fixture_invocation(:draft,CAST(:actor AS jsonb),:catalog,CAST(:run AS uuid))'),
            dict(draft=draft_id, actor=json.dumps(actor.model_dump(mode='json')), catalog=catalog_canonical, run=run_id))
        result = _snapshot(value, FixtureInvocationSnapshot)
        if result.actor != actor or result.invocation_id != run_id or result.draft_id != draft_id:
            raise ValueError('Fixture acceptance differs')
        return result
    except SQLAlchemyError:
        await service._deny(uow, actor, 'author', request_id, 'UNAVAILABLE')
    except (ValueError, TypeError, AttributeError, GatewayError):
        await service._deny(uow, actor, 'author', request_id, 'AUDIENCE_DENIED')


async def load_fixture_invocation_access(uow, invocation_id):
    from gateway.source_access import _uuid
    try:
        _uuid(invocation_id)
        raw = await uow.session.scalar(text('SELECT gateway.source_read_fixture_invocation(CAST(:id AS uuid))'), {'id': invocation_id})
        return _snapshot(raw, FixtureInvocationSnapshot)
    except (ValueError, TypeError, AttributeError, GatewayError, SQLAlchemyError):
        raise not_found() from None


async def load_fixture_attempt_access(uow, attempt_id):
    try:
        _identifier(attempt_id)
        raw = await uow.session.scalar(text('SELECT gateway.source_read_fixture_attempt(:id)'), {'id': attempt_id})
        return _snapshot(raw, FixtureAttemptSnapshot)
    except (ValueError, TypeError, AttributeError, GatewayError, SQLAlchemyError):
        raise not_found() from None


async def load_fixture_test_access(uow, test_run_id):
    try:
        _identifier(test_run_id)
        raw = _plain(await uow.session.scalar(text('SELECT gateway.source_read_fixture_test(:id)'), {'id': test_run_id}))
        if type(raw) is not dict or set(raw) != {'invocation', 'attempts'} or type(raw['attempts']) is not list:
            raise ValueError('Invalid fixture evidence owner')
        return FixtureTestSnapshot(invocation=_snapshot(raw['invocation'], FixtureInvocationSnapshot),
            attempts=tuple(_snapshot(item, FixtureAttemptSnapshot) for item in raw['attempts']))
    except (ValueError, TypeError, AttributeError, GatewayError, SQLAlchemyError):
        raise not_found() from None


async def load_fixture_case_review(uow, *, invocation_id, fixture_id, request_id):
    from gateway.source_access import _uuid
    try:
        _uuid(invocation_id); _uuid(request_id); _identifier(fixture_id)
        raw = await uow.session.scalar(text('SELECT gateway.source_read_fixture_case(CAST(:id AS uuid),:case)'), {'id': invocation_id, 'case': fixture_id})
        review = FixtureCaseReview.model_validate(_plain(raw))
        if review.invocation_id != invocation_id or review.fixture_id != fixture_id:
            raise ValueError('Fixture review differs')
        return review
    except (ValueError, TypeError, AttributeError, GatewayError, SQLAlchemyError):
        raise not_found() from None


def fixture_owner_request(value, owner, principal, decision, action, request_id):
    from gateway.source_access import AccessRequest, AcceptedApprovalRef
    value = _plain(value)
    keys = {'accepted', 'projection', 'decision', 'audience_entry_id', 'admitted_decision', 'admitted_decision_digest',
        'manifest', 'binding_digest', 'version_id', 'fixture_case_id', 'invoking_actor', 'invoking_actor_decision', 'invoking_actor_entry_id'}
    if type(value) is not dict or set(value) != keys:
        raise ValueError('Invalid fixture owner authority')
    accepted = AcceptedApprovalRef.model_validate(value['accepted'])
    current = Decision.model_validate(value['decision'])
    if accepted.principal != principal or current != decision:
        raise ValueError('Original fixture requester or current decision differs')
    projection = RuntimeApprovalProjection.model_validate(value['projection'])
    pins = ReviewedPackagePins.model_validate(projection._package_content() | {'manifest': value['manifest'], 'approval_digest': projection.approval_digest})
    product = PinnedProduct.model_validate({key: value[key] for key in ('manifest', 'binding_digest', 'version_id')})
    values = dict.fromkeys(AccessRequest.model_fields)
    values.update(request_kind='owner', owner=owner, product=product, principal=principal, decision=current, action=action,
        context='fixture_test', request_id=request_id, pins=pins, projection=projection, audience_entry_id=value['audience_entry_id'],
        accepted=accepted, admitted_decision=value['admitted_decision'], admitted_decision_digest=value['admitted_decision_digest'],
        fixture_case_id=value['fixture_case_id'], invoking_actor=value['invoking_actor'], invoking_actor_decision=value['invoking_actor_decision'],
        invoking_actor_entry_id=value['invoking_actor_entry_id'])
    return AccessRequest.model_validate(values)


async def record_publication_operation(service, uow, *, actor, action, product_id, version_id, draft_id,
        test_run_id, previous_version_id, expected_product_revision, expected_draft_revision, allow_audit_id, request_id):
    from gateway.source_access import _uuid
    try:
        actor = _fresh(actor, Principal)
        _uuid(request_id)
        if type(action) is not str or action not in ('publish', 'deprecate', 'withdraw', 'rollback'):
            raise ValueError('Invalid publication action')
        for value in (product_id, version_id, allow_audit_id): _identifier(value)
        for value in (draft_id, test_run_id, previous_version_id):
            if value is not None: _identifier(value)
        if type(expected_product_revision) is not int or not 0 <= expected_product_revision < 2**31-1:
            raise ValueError('Invalid publication revision')
        if expected_draft_revision is not None and (type(expected_draft_revision) is not int or not 1 <= expected_draft_revision < 2**31):
            raise ValueError('Invalid draft revision')
    except (ValueError, TypeError, AttributeError, GatewayError):
        raise not_found() from None
    try:
        await uow.session.flush()
        result = await uow.session.scalar(text('''SELECT gateway.source_record_publication_operation(CAST(:actor AS jsonb),:action,
            :product_id,:version_id,:draft_id,:test_run_id,:previous_version_id,:expected_product_revision,:expected_draft_revision,:allow_audit_id,:request_id)'''),
            dict(actor=json.dumps(actor.model_dump(mode='json')), action=action, product_id=product_id, version_id=version_id,
                draft_id=draft_id, test_run_id=test_run_id, previous_version_id=previous_version_id,
                expected_product_revision=expected_product_revision, expected_draft_revision=expected_draft_revision,
                allow_audit_id=allow_audit_id, request_id=request_id))
        return _uuid(str(result))
    except SQLAlchemyError:
        await service._deny(uow, actor, 'publish', request_id, 'UNAVAILABLE')
    except (ValueError, TypeError, AttributeError, GatewayError):
        await service._deny(uow, actor, 'publish', request_id, 'AUDIENCE_DENIED')
