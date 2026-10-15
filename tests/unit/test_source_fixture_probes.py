"""Strict in-memory owner sequencing; no SQL acceptance or access receipts."""
import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest

from gateway.contracts.errors import GatewayError
from gateway.contracts.identity import Principal
from gateway.source_access import AccessRequest
from tests.unit.test_policy import grant
from tests.unit.test_publication_definitions import review_service

pytestmark = pytest.mark.unit


def test_retained_fixture_probe_uses_real_request_contracts_and_distinct_original_actors():
    context=review_service(grant_kind='author')
    view=asyncio.run(context.service.get_test(context.actor,context.draft.id,context.stored.id))
    batch=context.access.owner_batches[-1]
    assert view.id==context.stored.id
    assert all(type(item) is AccessRequest for item in batch)
    assert {item.principal.id for item in batch}=={context.actor.id,context.original_actor.id,'fixture-north'}
    assert {(item.owner.kind,item.action) for item in batch if item.owner}=={
        ('fixture_invocation','author'),('fixture_attempt','result')}
    assert all(item.invoking_actor==context.original_actor for item in batch if item.fixture_case_id)


@pytest.mark.parametrize('change',['original_author','result_permission','fixed_client','missing_attempt','evidence','negative_allowed'])
def test_retained_fixture_probe_does_not_lend_authority_to_mutated_evidence(change):
    context=review_service(grant_kind='author')
    if change=='original_author': context.fixed_grants[context.original_actor.id]=[]
    elif change=='result_permission': context.fixed_grants['fixture-north']=[grant(capabilities=('execute',))]
    elif change=='fixed_client':
        case=next(case for case in context.access.cases.values() if case.kind=='known_answer')
        case.principal=Principal(id=case.principal.id,subject=case.principal.id,kind='user',service_client_id='borrowed-client')
    elif change=='missing_attempt': context.access.attempts.pop(next(iter(context.access.attempts)))
    elif change=='evidence': context.stored.evidence['checks'][0]['actual']=[]
    else: context.fixed_grants['fixture-denied']=[grant()]
    with pytest.raises(GatewayError) as caught:
        asyncio.run(context.service.get_test(context.actor,context.draft.id,context.stored.id))
    assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
    assert not context.access.owner_batches and not any(event.decision=='allow' for event in context.records)


def test_retained_fixture_probe_rechecks_policy_at_final_batch_and_does_not_issue_receipts():
    async def scenario():
        context=review_service(grant_kind='author')
        async with context.service.registry.transaction() as uow:
            requests=await context.service.fixture_runner.prepare_evidence_access(uow,context.stored)
            context.fixed_grants['fixture-north']=[grant(capabilities=('execute',))]
            with pytest.raises(GatewayError):
                await context.access.finalize(uow,requests)
        assert not context.access.owner_batches and not any(event.decision=='allow' for event in context.records)
        valid=review_service(grant_kind='author')
        async with valid.service.registry.transaction() as uow:
            requests=await valid.service.fixture_runner.prepare_evidence_access(uow,valid.stored)
            assert await valid.access.finalize(uow,requests)==()
    asyncio.run(scenario())


def test_retained_fixture_probe_requires_the_complete_positive_owner_roster():
    async def scenario():
        context=review_service(grant_kind='author')
        async with context.service.registry.transaction() as uow:
            requests=await context.service.fixture_runner.prepare_evidence_access(uow,context.stored)
            assert len(requests)>1
            with pytest.raises(GatewayError):
                await context.access.finalize(uow,requests[:1])
        assert not context.access.owner_batches and not any(event.decision=='allow' for event in context.records)
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['catalog_subset','catalog_duplicate','case_subset','invocation_subset','invocation_duplicate'])
def test_retained_fixture_probe_rejects_replaced_or_ambiguous_structural_rosters(change):
    context=review_service(grant_kind='author')
    access=context.access
    if change=='catalog_subset': access.catalog=access.catalog.model_copy(update={'cases':access.catalog.cases[:1]})
    elif change=='catalog_duplicate': access.catalog=access.catalog.model_copy(update={'cases':access.catalog.cases+(access.catalog.cases[0],)})
    elif change=='case_subset': access.cases.pop(next(iter(access.cases)))
    elif change=='invocation_subset': access.invocation.cases=access.invocation.cases[:1]
    else: access.invocation.cases=access.invocation.cases+(access.invocation.cases[0],)
    with pytest.raises(GatewayError): access._check_roster()
    with pytest.raises(GatewayError):
        asyncio.run(context.service.get_test(context.actor,context.draft.id,context.stored.id))
    assert not access.owner_batches and not context.records
