"""Submit/wait orchestration; private queue/access doubles are not SQL evidence."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import ExecutionView
from gateway.contracts.query import QueryRequest
from gateway.execution.service import ExecutionService
from gateway.registry.repository import not_found
from tests.unit.test_source_approval_results import ReviewedResultProbe

pytestmark=pytest.mark.unit


def submission(clock, *, existing=False, wrong_client=False, denied_owner=False):
    probe=ReviewedResultProbe(clock)
    probe.resolve=AsyncMock(return_value=probe.product)
    service=ExecutionService(registry=probe,policy=probe.policy,bindings=probe.bindings,clock=clock)
    calls=[]
    class Queue:
        async def maintain(self): calls.append('maintain')
        async def lock_capacity(self,uow): calls.append('capacity')
        async def find_existing(self,uow,principal,product,idempotency_key):
            calls.append('existing')
            if wrong_client: raise not_found()
            return 'execution-1' if existing else None
        async def admit(self,uow,principal,spec,decision,idempotency_key,*,access_request=None):
            calls.append('admit')
            assert access_request is probe.access_request
            assert spec.canonical_query['inputs']['segment'] is None
            return SimpleNamespace(view=ExecutionView(execution_id='execution-1',state='accepted',accepted_at=clock.now()))
    async def resolve_owner(uow,owner,principal,decision,*,action,request_id):
        assert owner=={'kind':'execution','id':'execution-1'} and action=='execute'
        calls.append('owner')
        if denied_owner: raise not_found()
        return probe.access_request
    async def resolve_admission(uow,product,principal,decision,*,request_id):
        calls.append('admission')
        return probe.access_request
    probe.resolve_owner=resolve_owner
    probe.resolve_admission=resolve_admission
    service.queue=Queue()
    request=QueryRequest(product_id=probe.product.manifest.product_id,inputs={'start':'2024-03-09','end':'2024-03-12'},
        idempotency_key='same-key')
    return service,probe,request,calls


@pytest.mark.parametrize('existing',[False,True])
def test_submission_selects_retained_owner_before_new_approval_and_domain_validation(clock,monkeypatch,existing):
    service,probe,request,calls=submission(clock,existing=existing)
    from gateway.execution import service as module
    validate=module.validate_query
    def observed(manifest,request):
        calls.append('validate')
        return validate(manifest,request)
    monkeypatch.setattr(module,'validate_query',observed)
    result=asyncio.run(service.submit(probe.actor,request))
    assert result.execution_id=='execution-1'
    assert calls==['maintain','capacity','existing','validate','owner' if existing else 'admission','admit']


def test_different_client_idempotency_denies_before_normalizing_or_auditing(clock,monkeypatch):
    service,probe,request,calls=submission(clock,existing=True,wrong_client=True)
    from gateway.execution import service as module
    monkeypatch.setattr(module,'validate_query',lambda *args:pytest.fail('Untrusted retry reached normalization'))
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.submit(probe.actor,request))
    assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
    assert calls==['maintain','capacity','existing'] and probe.events==[]


def test_revoked_original_idempotency_never_selects_replacement_approval(clock):
    service,probe,request,calls=submission(clock,existing=True,denied_owner=True)
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.submit(probe.actor,request))
    assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
    assert 'owner' in calls and 'admission' not in calls and 'admit' not in calls


def test_submission_rejects_unreviewed_domain_before_any_audit_or_admission(clock):
    service,probe,request,calls=submission(clock)
    request=request.model_copy(update={'inputs':{'start':'2031-01-01','end':'2031-01-02'}})
    processed=[]
    original=probe.audit.parameter_summary
    def summarize(parameters,**kwargs):
        processed.append(dict(parameters))
        return original(parameters,**kwargs)
    probe.audit.parameter_summary=summarize
    with pytest.raises(GatewayError):
        asyncio.run(service.submit(probe.actor,request))
    assert 'admit' not in calls and all('2031-01-01' not in p.values() for p in processed)


def test_internal_wait_passes_original_complete_requester(clock):
    from tests.unit.test_execution_clock import waiting_service
    service,authorized=waiting_service(clock,saved=1)
    from gateway.contracts.identity import Principal
    original=Principal(id='north-user',subject='synthetic-north',kind='user',service_client_id='original-client')
    authorized.principal=original
    calls=[]
    async def wait_authorized(execution_id,*,requester):
        calls.append(requester)
        return SimpleNamespace(**{**vars(authorized),'view':authorized.view.model_copy(update={
            'state':'running','started_at':clock.now()})})
    service.queue.wait_authorized=wait_authorized
    asyncio.run(service.submit_and_wait(None,None))
    assert calls and all(requester==original for requester in calls)


def test_submission_rejected_reviewed_value_records_safe_source_denial(clock):
    service, probe, request, calls = submission(clock)
    request = request.model_copy(update={'inputs': {'start': '2031-01-01', 'end': '2031-01-02'}})
    with pytest.raises(GatewayError) as caught:
        asyncio.run(service.submit(probe.actor, request))
    assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
    denied = [event for event in probe.committed_events if event.decision == 'deny']
    assert len(denied) == 1
    assert denied[0].action == 'execute' and denied[0].source_access_reason_code == 'INVALID_INPUT'
    assert denied[0].parameter_names == []
    assert denied[0].parameter_digest == probe.audit.parameter_summary({})[1]
    assert denied[0].product_id is None and denied[0].version_id is None
    assert '2031-01-01' not in str(vars(denied[0]))
    assert 'admit' not in calls
