"""Missing fixture review is configuration failure, never policy evidence."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.contracts.errors import GatewayError
from gateway.contracts.manifest import Fixture
from gateway.publishing.test_runner import ApprovedFixture, FixtureRunner
from tests.unit.test_source_approval_policy import setup

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('adapter', ['cube-bigquery-v1', 'domo-named-v2'])
@pytest.mark.parametrize('action', ['execute', 'result'])
@pytest.mark.parametrize('kind,expected_error', [
    ('known_answer', None),
    ('input_negative', 'INVALID_INPUT'),
    ('permission_negative', 'NOT_FOUND_OR_FORBIDDEN'),
])
def test_unapproved_fixed_case_never_reaches_policy_or_value_audit(
    north_principal, valid_manifest, adapter, action, kind, expected_error,
):
    async def scenario():
        valid_manifest['adapter_revision'] = adapter
        policy, target, uow, audit = setup(north_principal, valid_manifest, allowed=False)
        policy.principals.resolve_principal = AsyncMock(return_value=north_principal)
        processed = []
        summarize = audit.parameter_summary

        def observed(parameters, **kwargs):
            processed.append(dict(parameters))
            return summarize(parameters, **kwargs)

        audit.parameter_summary = observed
        runner = FixtureRunner(registry=policy.registry, policy=policy, bindings=None, bundles=None,
            catalog_provider=None, adapter_factory=None)
        case = ApprovedFixture(principal_id=north_principal.id, kind=kind,
            fixture=Fixture(id='unapproved-case', version='1',
                inputs={'start': 'person@example.invalid', 'end': '2024-03-12'},
                expected_error=expected_error))

        with pytest.raises(GatewayError) as caught:
            await runner._decision_for_case(uow, target, 1, case, action=action)

        assert caught.value.code == 'BINDING_DRIFT'
        assert 'person@example.invalid' not in str(caught.value)
        policy.principals.resolve_principal.assert_not_awaited()
        policy.principals.get_current.assert_not_awaited()
        assert processed == []
        assert audit.events == []

    asyncio.run(scenario())
