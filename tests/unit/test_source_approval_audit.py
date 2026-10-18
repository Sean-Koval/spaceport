"""Approval audit pins are bounded metadata, never an approval document."""
import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest

from gateway.contracts.errors import GatewayError
from gateway.policy.audit import AuditEventInput, AuditWriter

pytestmark = pytest.mark.unit
REF = dict(source_approval_id='be6ea27b-6404-4a9c-a027-3af676dfd5d0',
    source_approval_revision=1, source_approval_digest='a' * 64,
    source_approval_state_revision=1, source_access_reason_code='APPROVED')


def writer():
    return AuditWriter(b'synthetic-audit-key-with-32-bytes!!')


def test_allow_event_retains_only_exact_bounded_approval_reference(north_principal):
    event = writer().event(north_principal, 'result', decision='allow', **REF)
    assert {name: getattr(event, name) for name in REF} == REF
    assert not {'content', 'audiences', 'evidence', 'input_domains'} & set(type(event).model_fields)


@pytest.mark.parametrize('field,value', [
    ('source_approval_id', 'not-a-uuid'), ('source_approval_revision', True),
    ('source_approval_revision', 0), ('source_approval_digest', 'synthetic-sensitive-value'),
    ('source_approval_state_revision', 0), ('source_access_reason_code', 'synthetic-sensitive-value'),
])
def test_unbounded_or_malformed_approval_metadata_is_safe(north_principal, field, value):
    with pytest.raises(GatewayError) as caught:
        writer().event(north_principal, 'result', decision='allow', **{**REF, field: value})
    assert caught.value.code == 'INVALID_INPUT'
    assert 'synthetic-sensitive-value' not in str(caught.value)


@pytest.mark.parametrize('field', ['source_approval_id', 'source_approval_revision', 'source_approval_digest',
                                  'source_approval_state_revision'])
def test_partial_approval_reference_is_rejected(north_principal, field):
    with pytest.raises(GatewayError):
        writer().event(north_principal, 'result', decision='allow', **{**REF, field: None})


def test_missing_approval_deny_can_record_reason_without_selecting_a_record(north_principal):
    event = writer().event(north_principal, 'result', decision='deny', source_access_reason_code='MISSING')
    assert event.source_approval_id is None and event.source_access_reason_code == 'MISSING'


def test_append_returns_the_flushed_audit_identifier(north_principal):
    class Session:
        def __init__(self): self.rows = []
        def in_transaction(self): return True
        def add(self, row): self.rows.append(row)
        async def flush(self): self.rows[-1].id = '7d405722-d4b5-4131-b589-81b1c8c69398'
        async def rollback(self): raise AssertionError('Unexpected rollback')
    async def scenario():
        session = Session()
        identifier = await writer().append(SimpleNamespace(session=session),
            writer().event(north_principal, 'describe', decision='allow'))
        assert identifier == session.rows[0].id
        assert str(UUID(identifier)) == identifier
    asyncio.run(scenario())


@pytest.mark.parametrize('changes', [
    {'source_access_reason_code': 'MISSING'},
    {'source_access_reason_code': 'REVOKED'},
    {'source_approval_state_revision': 2},
])
def test_allow_cannot_claim_a_denial_reason_or_revoked_revision(north_principal, changes):
    with pytest.raises(GatewayError):
        writer().event(north_principal, 'result', decision='allow', **{**REF, **changes})
