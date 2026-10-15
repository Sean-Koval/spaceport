"""Scheduling-clock bounds only; genuine DB authority is tested separately."""
import asyncio
from datetime import timedelta

import pytest

from gateway.execution.worker import Worker
from tests.unit.test_execution_clock import UnitAdapter, UnitQueue

pytestmark = pytest.mark.unit


def test_database_claim_budget_bounds_worker_when_local_wall_clock_is_behind(clock):
    async def scenario():
        queue = UnitQueue(clock)
        queue.claimed.claimed_at = queue.claimed.deadline - timedelta(seconds=.01)
        queue.claimed.monotonic_issued_at = asyncio.get_running_loop().time()
        adapter = UnitAdapter(queue, blocked=True)
        await asyncio.wait_for(Worker(queue, adapter_factory=lambda: adapter,
            worker_id='rollback-clock-worker', clock=clock).run_once(), timeout=.2)
        assert queue.failures == [('EXECUTION_TIMEOUT', False)]
        assert not queue.completed and adapter.closed
    asyncio.run(scenario())


def test_claim_commit_delay_is_subtracted_from_database_worker_budget(clock):
    async def scenario():
        queue = UnitQueue(clock)
        queue.claimed.claimed_at = queue.claimed.deadline - timedelta(seconds=.01)
        queue.claimed.monotonic_issued_at = asyncio.get_running_loop().time() - .02
        adapter = UnitAdapter(queue, blocked=True)
        await asyncio.wait_for(Worker(queue, adapter_factory=lambda: adapter,
            worker_id='delayed-commit-worker', clock=clock).run_once(), timeout=.2)
        assert queue.failures == [('EXECUTION_TIMEOUT', False)]
        assert adapter.polls == 0
    asyncio.run(scenario())
