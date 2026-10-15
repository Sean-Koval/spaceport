"""Inert fixture caller probes; no owner acceptance, receipt, or source qualification."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.auth.principals import PrincipalGrants
from gateway.contracts.errors import GatewayError
from gateway.contracts.identity import Principal
from gateway.policy.service import PolicyRules
from gateway.publishing.test_runner import FixtureRunner
from gateway.registry.models import CapacityLock
from tests.unit.test_policy import grant, rules
from tests.unit.test_publication_definitions import make_catalog_evidence

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('permitted', [True, False])
def test_fixed_verdict_uses_protected_identity_and_never_audits_unfinalized_values(permitted):
    """The pure policy verdict must not invoke the aborting audited gate."""
    _, product, catalog, _, _ = make_catalog_evidence()
    case = next(item for item in catalog.cases if item.kind == ('known_answer' if permitted else 'permission_negative'))
    fixed = Principal(id=case.principal_id, subject='registered-fixed', kind='user', service_client_id='fixed-client')
    trace = []
    async def review(uow, **kwargs):
        trace.append('structural')
        assert kwargs['invocation_id'] == 'original-invocation'
        return SimpleNamespace(fixture_id=case.fixture.id, kind=case.kind, case=case.model_dump(mode='python'), principal=fixed)
    async def identity(actor, uow):
        trace.append('identity')
        assert actor == fixed
    async def current_grants(actor_id, **kwargs):
        trace.append('grants')
        return PrincipalGrants(principal_id=actor_id, grants=[grant()] if permitted else [])
    async def current_policy(policy_id, uow):
        trace.append('policy')
        return SimpleNamespace(revision=1), SimpleNamespace(rules=PolicyRules.model_validate(rules()))
    policy = SimpleNamespace(requires_source_approval=lambda product:True,
        source_access=SimpleNamespace(load_fixture_case_review=review), _identity=identity, _current=current_policy,
        principals=SimpleNamespace(get_current=current_grants, resolve_principal=AsyncMock()),
        audit=SimpleNamespace(append=AsyncMock()), evaluate=AsyncMock())
    runner = FixtureRunner(registry=None, policy=policy, bindings=None, bundles=None,
        catalog_provider=lambda:catalog, adapter_factory=lambda:None)
    actor, decision = asyncio.run(runner._decision_for_case(SimpleNamespace(session=None), product, 1, case,
        invocation_id='original-invocation'))
    assert actor == fixed and (decision is not None) is permitted
    assert trace == ['structural','identity','grants','policy']
    policy.audit.append.assert_not_called()
    policy.evaluate.assert_not_called()
    policy.principals.resolve_principal.assert_not_called()


def test_missing_structural_invocation_is_not_a_permission_negative_verdict():
    _, product, catalog, _, _ = make_catalog_evidence()
    case = next(item for item in catalog.cases if item.kind == 'permission_negative')
    access = SimpleNamespace(load_fixture_case_review=AsyncMock())
    policy = SimpleNamespace(requires_source_approval=lambda product:True,source_access=access)
    runner = FixtureRunner(registry=None, policy=policy, bindings=None, bundles=None,
        catalog_provider=lambda:catalog, adapter_factory=lambda:None)
    with pytest.raises(GatewayError) as caught:
        asyncio.run(runner._decision_for_case(None, product, 1, case))
    assert caught.value.code == 'BINDING_DRIFT'
    access.load_fixture_case_review.assert_not_called()


@pytest.mark.parametrize('token', ['retained-token', 'wrong-token'])
@pytest.mark.parametrize('initial_state', ['running','succeeded','failed','timed_out'])
def test_failure_cleanup_keeps_exact_immutable_attempt_fence(token,initial_state):
    from gateway.contracts.canonical import canonical_digest
    from gateway.publishing.test_runner import FixtureCheck
    state = SimpleNamespace(state=initial_state,error_code=None,source_completed=initial_state=='succeeded',check_digest=None)
    check = FixtureCheck(fixture_id='case-1',kind='known_answer',passed=False,attempt_id='attempt-1',
        actual_error='UPSTREAM_UNAVAILABLE')
    class Session:
        async def scalar(self, statement):
            if statement.column_descriptions[0]['entity'] is CapacityLock:
                return SimpleNamespace(scope_id='deployment')
            values = statement.compile().params.values()
            assert 'attempt-1' in values and token in values
            return state if token == 'retained-token' else None
        async def flush(self): pass
    class Registry:
        @asynccontextmanager
        async def transaction(self):
            yield SimpleNamespace(session=Session())
    runner = FixtureRunner(registry=Registry(), policy=None, bindings=None, bundles=None,
        catalog_provider=lambda:None, adapter_factory=lambda:None)
    asyncio.run(runner._finish_failed(SimpleNamespace(id='attempt-1',runner_token=token),'UPSTREAM_UNAVAILABLE',check))
    changed=token=='retained-token' and initial_state=='running'
    assert state.state == ('failed' if changed else initial_state)
    assert state.check_digest == (canonical_digest(check) if changed else None)
    assert state.source_completed is (initial_state=='succeeded')


@pytest.mark.parametrize('expired', ['attempt', 'group', 'invocation'])
def test_fixture_budget_is_rechecked_after_final_approval(expired, clock):
    """Clock sequencing probe only; no SQL receipt or provider authority is minted."""
    from datetime import timedelta
    trace = []
    async def finalize(uow, requests):
        trace.append('approval')
        clock.instant += timedelta(seconds=2)
    class Session:
        async def scalar(self, statement):
            trace.append('clock')
            return clock.now()
    deadlines = {kind:clock.now()+timedelta(seconds=1 if kind == expired else 60)
        for kind in ('attempt','group','invocation')}
    authority = SimpleNamespace(source_group=SimpleNamespace(deadline=deadlines['group']),
        source_invocation=SimpleNamespace(effective_deadline_at=deadlines['invocation']))
    attempt = SimpleNamespace(deadline_at=deadlines['attempt'],request_id='attempt-request')
    runner = FixtureRunner(registry=None, policy=SimpleNamespace(source_access=SimpleNamespace(finalize=finalize)),
        bindings=None,bundles=None,catalog_provider=lambda:None,adapter_factory=lambda:None)
    request = SimpleNamespace(accepted=SimpleNamespace(effective_deadline_at=deadlines['attempt']))
    with pytest.raises(GatewayError) as caught:
        asyncio.run(runner._finalize_attempt_gate(SimpleNamespace(session=Session()),(request,),authority,attempt))
    assert caught.value.code == 'EXECUTION_TIMEOUT' and trace == ['approval','clock']


def test_fixture_attempt_and_evidence_persist_exact_python_canonical_witnesses(monkeypatch,clock):
    """Serialization only: this inert session cannot establish owner provenance."""
    from datetime import timedelta
    from gateway.contracts.canonical import canonical_bytes
    from gateway.publishing.models import FixtureAttempt
    from gateway.publishing import test_runner as module
    from gateway.registry.models import TestRun
    binding,product,catalog,_,decisions=make_catalog_evidence()
    actor=Principal(id='author',subject='author',kind='user',service_client_id='author-client')
    authority=SimpleNamespace(product=product,catalog=catalog,actor=actor,policy_revision=1,
        review_visible_fields=('day','segment','revenue'),draft=SimpleNamespace(id='draft-1',content_digest=product.manifest.content_digest),
        binding_row=SimpleNamespace(id='consumer-binding',content_digest=binding.digest),
        fixture_binding_row=SimpleNamespace(id='fixture-binding',content_digest=catalog.fixture_binding.digest))
    rows=[]
    class Session:
        def add(self,row): rows.append(row)
        async def flush(self): pass
        async def refresh(self,row):
            row.created_at=clock.now(); row.deadline_at=clock.now()+timedelta(seconds=120)
    class Registry:
        @asynccontextmanager
        async def transaction(self): yield SimpleNamespace(session=Session())
    async def capacity(*args): return 0,0
    monkeypatch.setattr(module,'lock_execution_capacity',AsyncMock())
    monkeypatch.setattr(module,'source_capacity_counts',capacity)
    class Runner(FixtureRunner):
        async def _authority(self,*args,**kwargs): return authority
        async def _data_decision(self,uow,authority,case,**kwargs):
            return Principal(id=case.principal_id,subject=case.principal_id,kind='demo',service_client_id=None),decisions[case.principal_id]
        async def _dispatch(self,*args): raise GatewayError('UPSTREAM_UNAVAILABLE','synthetic-request','Unavailable')
    runner=Runner(registry=Registry(),policy=SimpleNamespace(requires_source_approval=lambda product:False),
        bindings=None,bundles=None,catalog_provider=lambda:catalog,adapter_factory=lambda:None,clock=clock)
    case=next(case for case in catalog.cases if case.kind=='known_answer')
    attempt,_,decision=asyncio.run(runner._case_start(authority,case))
    assert type(attempt) is FixtureAttempt
    manifest=catalog.execution_manifest(product.manifest)
    assert attempt.fixture_manifest_canonical==canonical_bytes(manifest.model_dump(mode='python',exclude={'content_digest'})).decode()
    assert attempt.query_canonical==canonical_bytes(attempt.canonical_query).decode()
    assert attempt.decision_canonical==canonical_bytes(decision.model_dump(mode='python')).decode()
    asyncio.run(runner._run_cases(authority,'run-1',actor,'draft-1'))
    test=next(row for row in rows if type(row) is TestRun)
    assert test.evidence_canonical==canonical_bytes(test.evidence).decode()
    expected={case.fixture.id:canonical_bytes(decisions[case.principal_id].model_dump(mode='python')).decode()
        for case in catalog.cases if case.kind=='input_negative'}
    assert test.source_decision_canonicals==expected
    assert all(FixtureAttempt.__table__.c[name].nullable for name in
        ('fixture_manifest_canonical','query_canonical','decision_canonical'))


@pytest.mark.parametrize('change', [dict(passed=True),dict(actual=[{'secret':'value'}]),
    dict(attempt_id='another-attempt'),dict(actual_error='EXECUTION_TIMEOUT')])
def test_failure_metadata_rejects_rows_success_or_mismatched_check_before_transaction(change):
    from gateway.publishing.test_runner import FixtureCheck
    check=FixtureCheck(fixture_id='case-1',kind='known_answer',passed=False,attempt_id='attempt-1',
        actual_error='UPSTREAM_UNAVAILABLE')
    registry=SimpleNamespace(transaction=lambda:(_ for _ in ()).throw(AssertionError('No transaction before check validation')))
    runner=FixtureRunner(registry=registry,policy=None,bindings=None,bundles=None,
        catalog_provider=lambda:None,adapter_factory=lambda:None)
    with pytest.raises(GatewayError) as caught:
        asyncio.run(runner._finish_failed(SimpleNamespace(id='attempt-1',runner_token='retained-token'),
            'UPSTREAM_UNAVAILABLE',check.model_copy(update=change)))
    assert caught.value.code=='CONFLICT'
