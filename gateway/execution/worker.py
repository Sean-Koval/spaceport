"""Bounded read-only worker. Expired leases do not release upstream capacity."""
import asyncio
from contextlib import suppress
from datetime import datetime

from gateway.contracts.clock import SystemClock
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.execution import Completed, Pending, is_completed
from gateway.contracts.query import QueryRequest
from .queue import effective_deadline, failure


def backoff_seconds(attempt: int, now: datetime, deadline: datetime) -> float:
    return min((.25,.5,1,2)[min(max(attempt,0),3)],max(0.0,(deadline-now).total_seconds()))


class Worker:
    def __init__(self,queue,*,adapter_factory,worker_id,clock=None):
        self.queue,self.adapter_factory,self.worker_id=queue,adapter_factory,worker_id
        self.clock=clock or SystemClock()

    async def _heartbeat(self,claim):
        # Real scheduling prevents fake-clock tests from advancing a concurrent
        # renewal task beyond their intended execution timeline.
        while True:
            await asyncio.sleep(5)
            if not await self.queue.renew(claim): return

    async def run_once(self) -> bool:
        claim=await self.queue.claim(self.worker_id)
        if claim is None: return False
        adapter=None
        heartbeat=asyncio.create_task(self._heartbeat(claim))
        source_completed=False
        deadline=effective_deadline(claim)
        clock_started=self.clock.now()
        monotonic_started=asyncio.get_running_loop().time()
        budget=max(0.0,(deadline-clock_started).total_seconds())
        # A local wall-clock rollback cannot stretch the fresh DB claim budget.
        # Issuance precedes commit, so subtract commit/scheduling delay too.
        claimed_at=getattr(claim,'claimed_at',None)
        issued=getattr(claim,'monotonic_issued_at',None)
        if claimed_at is not None and issued is not None:
            budget=min(budget,max(0.0,(deadline-claimed_at).total_seconds()-max(0.0,monotonic_started-issued)))
        try:
            if budget<=0: raise failure(ErrorCode.EXECUTION_TIMEOUT,claim.spec.request_id)
            async with asyncio.timeout_at(monotonic_started+budget) as timeout:
                # Claim transaction committed its audit before adapter construction.
                adapter=self.adapter_factory()
                prepared=claim.prepared
                if prepared is None:
                    current=await self.queue.gate(claim)
                    deadline=min(deadline,effective_deadline(claim,current))
                    timeout.reschedule(min(timeout.when(),monotonic_started+(deadline-clock_started).total_seconds()))
                    if self.clock.now()>=deadline:
                        raise failure(ErrorCode.EXECUTION_TIMEOUT,claim.spec.request_id)
                    async with asyncio.timeout(max(0.0,(deadline-self.clock.now()).total_seconds())):
                        prepared=await adapter.prepare(claim.spec.pinned_product,
                            QueryRequest.model_validate(claim.spec.canonical_query),
                            claim.executed_decision,context=claim.spec.context,deadline=deadline,request_id=claim.spec.request_id)
                    if claim.spec.pinned_product.manifest.adapter_revision!='domo-named-v2':
                        await self.queue.save_prepared(claim,prepared)
                attempt=0
                while True:
                    if self.clock.now()>=deadline:
                        raise failure(ErrorCode.EXECUTION_TIMEOUT,claim.spec.request_id)
                    current=await self.queue.gate(claim)
                    deadline=min(deadline,effective_deadline(claim,current))
                    timeout.reschedule(min(timeout.when(),monotonic_started+(deadline-clock_started).total_seconds()))
                    if self.clock.now()>=deadline:
                        raise failure(ErrorCode.EXECUTION_TIMEOUT,claim.spec.request_id)
                    async with asyncio.timeout(max(0.0,(deadline-self.clock.now()).total_seconds())):
                        revision=claim.spec.pinned_product.manifest.adapter_revision
                        authority={"spec":claim.spec,"executed_decision":claim.executed_decision} if revision in {
                            "cube-rest-v2","cube-bigquery-v1","domo-named-v2"} else {}
                        if revision in {"cube-bigquery-v1","domo-named-v2"}:
                            session_spec=await adapter.source_plan(claim.spec,prepared,claim.executed_decision)
                            authority['source_context']=await self.queue.source_attempt(claim,prepared,current,session_spec)
                        polled=await adapter.poll(prepared,current,**authority,context=claim.spec.context,
                            deadline=deadline,request_id=claim.spec.request_id)
                    if is_completed(polled):
                        source_completed=True
                        if self.clock.now()>=deadline:
                            raise failure(ErrorCode.EXECUTION_TIMEOUT,claim.spec.request_id)
                        await self.queue.complete(claim,polled)
                        return True
                    if not isinstance(polled,Pending):
                        raise failure(ErrorCode.UPSTREAM_UNAVAILABLE,claim.spec.request_id)
                    duration=backoff_seconds(attempt,self.clock.now(),deadline)
                    if duration<=0: raise failure(ErrorCode.EXECUTION_TIMEOUT,claim.spec.request_id)
                    await self.clock.sleep(duration)
                    attempt+=1
        except TimeoutError:
            await self.queue.fail(claim,ErrorCode.EXECUTION_TIMEOUT,source_completed=source_completed)
            return True
        except GatewayError as error:
            await self.queue.fail(claim,error.code,source_completed=source_completed)
            return True
        except Exception:
            await self.queue.fail(claim,ErrorCode.UPSTREAM_UNAVAILABLE,source_completed=source_completed)
            return True
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError,Exception): await heartbeat
            if adapter is not None:
                with suppress(Exception):
                    async with asyncio.timeout(1): await adapter.close()
