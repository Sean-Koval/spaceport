"""Internal source authority is frozen, bounded and never public query input."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


def plan(**overrides):
    from gateway.source_lifecycle import SourceSessionSpec
    values = dict(provider="bigquery", source_id="own-usage", source_revision=1,
        context="consumer", capacity_key="bigquery:billing:US",
        definition_digest="a" * 64, request_digest="b" * 64,
        prepared_digest="c" * 64, scope_digest="d" * 64,
        decision_digest="e" * 64, binding_digest="f" * 64,
        model_bundle_digest="1" * 64, catalog_digest=None,
        max_submissions=2, max_bytes_per_submission=1000, max_total_bytes=2000)
    return SourceSessionSpec(**(values | overrides))


def test_source_plan_is_frozen_and_hashable():
    value = plan()
    assert hash(value)
    with pytest.raises(FrozenInstanceError):
        value.source_id = "alias"


@pytest.mark.parametrize("overrides", [
    {"provider": "http"}, {"source_revision": True}, {"max_submissions": True},
    {"max_submissions": 9}, {"prepared_digest": "C" * 64},
    {"context": "public"}, {"max_total_bytes": 999},
    {"max_bytes_per_submission": None},
    {"capacity_key": "bigquery:billing:US\n"},
])
def test_source_plan_rejects_unbounded_or_ambiguous_values(overrides):
    with pytest.raises(ValueError):
        plan(**overrides)


def test_domo_plan_has_no_fictional_byte_budget():
    value = plan(provider="domo", capacity_key="domo:customer", max_submissions=1,
        max_bytes_per_submission=None, max_total_bytes=None)
    assert value.provider == "domo"
    with pytest.raises(ValueError):
        plan(provider="domo", max_submissions=2)


def test_attempt_context_is_exact_and_separate_from_preparation():
    from gateway.source_lifecycle import SourceAttemptContext
    value = SourceAttemptContext(group_id=str(uuid4()), session_id=str(uuid4()),
        generation=1, authority_stamp="2" * 64, context_digest="3" * 64,
        deadline=datetime(2026, 10, 15, tzinfo=timezone.utc), spec=plan())
    assert value.request_id == value.session_id + ".g1"
    assert value.wire_context()["prepared_digest"] == "c" * 64
    assert "lease_token" not in value.wire_context()
    with pytest.raises(FrozenInstanceError):
        value.generation = 2


def test_source_status_distinguishes_case_terminal_and_group_settled():
    from gateway.source_lifecycle import SourceStatus
    value = SourceStatus(session_id=str(uuid4()), sealed=True, terminal=True,
        group_closed=False, group_settled=False, admitted_count=1, unresolved_count=0)
    assert value.can_complete_session
    assert not value.group_settled


def test_domo_permit_has_one_exact_start_and_redacted_representation():
    from gateway.source_lifecycle import DomoSendPermit
    permit = DomoSendPermit._from_committed(object(), "a" * 64, "secret-proof")
    assert "secret-proof" not in repr(permit)
    with pytest.raises(ValueError):
        permit.start_io("b" * 64)
    permit.start_io("a" * 64)
    with pytest.raises(ValueError):
        permit.start_io("a" * 64)
    with pytest.raises(ValueError):
        permit._claim_not_sent()


def test_unused_domo_permit_can_only_claim_no_io_once():
    from gateway.source_lifecycle import DomoSendPermit
    permit = DomoSendPermit._from_committed(object(), "a" * 64, "secret-proof")
    assert permit._claim_not_sent() == "secret-proof"
    with pytest.raises(ValueError):
        permit._claim_not_sent()
    with pytest.raises(ValueError):
        permit.start_io("a" * 64)


def test_wire_context_revalidates_bypassed_frozen_objects():
    from gateway.source_lifecycle import SourceAttemptContext
    value = SourceAttemptContext(group_id=str(uuid4()), session_id=str(uuid4()),
        generation=1, authority_stamp="2" * 64, context_digest="3" * 64,
        deadline=datetime(2026, 10, 15, tzinfo=timezone.utc), spec=plan())
    object.__setattr__(value.spec, "max_submissions", True)
    with pytest.raises(ValueError):
        value.wire_context()


@pytest.mark.parametrize("sealed,terminal", [(False, True), (True, False), (False, False)])
def test_group_settlement_cannot_contradict_own_session(sealed, terminal):
    from gateway.source_lifecycle import SourceStatus
    with pytest.raises(ValueError):
        SourceStatus(session_id=str(uuid4()), sealed=sealed, terminal=terminal,
            group_closed=True, group_settled=True, admitted_count=0, unresolved_count=0)


def test_domo_plan_uses_binding_bundle_without_inventing_descriptor_field(valid_manifest,clock):
    import asyncio
    from gateway.source_lifecycle import SourceLifecycle
    from tests.unit.test_domo_serving import arrangement,prepare
    adapter,source,binding,product,request,decision,spec,_,_=arrangement(valid_manifest,clock)
    async def scenario():
        prepared,context=await prepare(adapter,product,request,decision,spec,clock)
        lifecycle=SourceLifecycle(None,None,adapter.sources,deployment_epoch='synthetic')
        assert not hasattr(source,'model_bundle_digest')
        assert lifecycle._validate_plan(spec,prepared,decision,binding,context.spec)==context.spec
        await adapter.close()
    asyncio.run(scenario())
