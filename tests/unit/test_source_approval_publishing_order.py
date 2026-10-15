"""The publishing prefix must enter the existing capacity lock before P/D/Q.

These transaction spies prove only application lock ordering. They do not
substitute for registry approvals or the real PostgreSQL race qualification.
"""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from gateway.contracts.manifest import validate_manifest
from gateway.publishing.service import PublishingService
from gateway.publishing.test_runner import FixtureRunner
from gateway.registry.models import CapacityLock

pytestmark = pytest.mark.unit


class ReachedProduct(Exception):
    """Stop after observing the first product-lock boundary."""


def transaction_spy():
    events = []

    async def scalar(statement):
        descriptions = getattr(statement, 'column_descriptions', ())
        if descriptions and descriptions[0].get('entity') is CapacityLock:
            events.append('capacity')
            assert statement._for_update_arg is not None
            return SimpleNamespace(scope_id='deployment')
        return 'revenue'

    async def execute(statement):
        events.append('product-insert')

    async def lock_product(product_id):
        events.append('product')
        raise ReachedProduct

    uow = SimpleNamespace(session=SimpleNamespace(scalar=scalar, execute=execute),
        lock_product=lock_product)

    @asynccontextmanager
    async def transaction():
        yield uow

    return SimpleNamespace(transaction=transaction), uow, events


@pytest.mark.parametrize('operation', ['save', 'update', 'draft', 'transition'])
def test_publishing_enters_capacity_before_product_lock(north_principal, valid_manifest, operation):
    async def scenario():
        registry, uow, events = transaction_spy()
        service = PublishingService(registry=registry, policy=None, bindings=None, bundles=None)
        manifest = validate_manifest(valid_manifest)
        with pytest.raises(ReachedProduct):
            if operation == 'save':
                await service.save_draft(north_principal, manifest, 0)
            elif operation == 'update':
                await service.update_draft(north_principal, 'draft-1', manifest, 1)
            elif operation == 'draft':
                await service._draft(uow, 'draft-1')
            else:
                await service.transition(north_principal, 'revenue', '1.0.0', 'withdraw', 'Review')
        assert events == (['capacity', 'product-insert', 'product'] if operation == 'save'
                          else ['capacity', 'product'])
    asyncio.run(scenario())


def test_fixture_authority_enters_capacity_before_product_lock(north_principal):
    async def scenario():
        registry, uow, events = transaction_spy()
        runner = FixtureRunner(registry=registry, policy=None, bindings=None, bundles=None,
            catalog_provider=None, adapter_factory=None)
        with pytest.raises(ReachedProduct):
            await runner._authority(uow, north_principal, 'draft-1')
        assert events == ['capacity', 'product']
    asyncio.run(scenario())
