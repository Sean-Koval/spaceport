"""Real PostgreSQL current-authorization/audit gates around worker calls."""
import asyncio
from datetime import datetime, timezone

import pytest

from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import Completed, Pending
from tests.integration.conftest import registry_database
from tests.integration.test_execution_queue import api, setup, RecordingAdapter
from tests.integration.test_audit import principal

pytestmark=pytest.mark.postgres


@pytest.mark.parametrize('mutation',[
    "UPDATE gateway.principal_grants SET active=false",
    "UPDATE gateway.principals SET active=false",
    "UPDATE gateway.version_states SET status='withdrawn'",
    "UPDATE gateway.principal_grants SET entitlements='{\"groups\":[\"south\"]}'",
    "UPDATE gateway.principal_grants SET tenant_id='south'",
])
def test_revocation_before_claim_prevents_all_upstream(registry_database, mutation):
    async def scenario():
        service,request=await setup(registry_database)
        view=await service.submit(principal(),request)
        registry_database.execute_owner(mutation)
        constructed=[]
        def factory(): constructed.append(True); return RecordingAdapter()
        await api().worker.Worker(service.queue,adapter_factory=factory,worker_id='worker-1').run_once()
        assert not constructed
        assert (await service.queue.view(view.execution_id)).state=='failed'
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='deny'")>=1
    asyncio.run(scenario())


def test_audit_outage_before_worker_prevents_adapter_construction(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        await service.submit(principal(),request)
        role=registry_database.roles['app']
        registry_database.execute_owner('REVOKE INSERT (id) ON gateway.audit_events FROM "'+role+'"')
        constructed=[]
        await api().worker.Worker(service.queue,adapter_factory=lambda:constructed.append(True),worker_id='worker-1').run_once()
        assert not constructed
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations')==0
    asyncio.run(scenario())


def test_pending_grant_change_stops_next_poll_and_keeps_uncertain_reservation(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        view=await service.submit(principal(),request)
        async def poll():
            registry_database.execute_owner("UPDATE gateway.principal_grants SET entitlements='{\"groups\":[\"changed\"]}'")
            return Pending(retry_after_seconds=.25)
        adapter=RecordingAdapter(poll=poll)
        await api().worker.Worker(service.queue,adapter_factory=lambda:adapter,worker_id='worker-1').run_once()
        assert [item[0] for item in adapter.calls].count('poll')==1
        assert (await service.queue.view(view.execution_id)).state=='failed'
        assert registry_database.scalar('SELECT count(*) FROM gateway.results')==0
        assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL')==1
    asyncio.run(scenario())


def test_completion_reauthorizes_before_persisting_any_rows(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        view=await service.submit(principal(),request)
        async def poll():
            registry_database.execute_owner('UPDATE gateway.principal_grants SET active=false')
            return Completed(raw_rows=[],source_watermark=datetime.now(timezone.utc))
        adapter=RecordingAdapter(poll=poll)
        await api().worker.Worker(service.queue,adapter_factory=lambda:adapter,worker_id='worker-1').run_once()
        assert (await service.queue.view(view.execution_id)).state=='failed'
        assert registry_database.scalar('SELECT count(*) FROM gateway.results')==0
    asyncio.run(scenario())


def test_submission_and_worker_audits_are_committed_before_upstream(registry_database):
    async def scenario():
        service,request=await setup(registry_database)
        view=await service.submit(principal(),request)
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='execute' AND decision='allow'")==1
        async def prepare():
            assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='execute' AND decision='allow'")>=3
            assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations')==1
        adapter=RecordingAdapter(prepare=prepare)
        await api().worker.Worker(service.queue,adapter_factory=lambda:adapter,worker_id='worker-1').run_once()
        assert (await service.queue.view(view.execution_id)).state=='succeeded'
    asyncio.run(scenario())


def test_policy_head_change_while_pending_stops_next_poll(registry_database):
    async def scenario():
        from tests.unit.test_policy import rules
        from gateway.contracts.canonical import canonical_digest
        from psycopg.types.json import Jsonb
        service,request=await setup(registry_database)
        view=await service.submit(principal(),request)
        async def poll():
            content={'rules':rules(),'reason':'New independently versioned policy'}
            registry_database.execute_owner("INSERT INTO gateway.policy_revisions(id,policy_id,revision,content,content_digest,actor_id) VALUES ('policy-2','revenue-access',2,%s,%s,'operator')",(Jsonb(content),canonical_digest(content)))
            registry_database.execute_owner("UPDATE gateway.policies SET current_revision=2 WHERE id='revenue-access'")
            return Pending(retry_after_seconds=.25)
        adapter=RecordingAdapter(poll=poll)
        await api().worker.Worker(service.queue,adapter_factory=lambda:adapter,worker_id='worker-1').run_once()
        assert [item[0] for item in adapter.calls].count('poll')==1
        assert (await service.queue.view(view.execution_id)).state=='failed'
        assert registry_database.scalar('SELECT count(*) FROM gateway.results')==0
    asyncio.run(scenario())


def test_scope_denial_during_submission_is_durable_without_execution(registry_database):
    async def scenario():
        from tests.unit.test_policy import rules
        from gateway.contracts.canonical import canonical_digest
        from psycopg.types.json import Jsonb
        service,request=await setup(registry_database)
        content={'rules':rules(visible_fields=['revenue']),'reason':'Hide required grain'}
        registry_database.execute_owner("INSERT INTO gateway.policy_revisions(id,policy_id,revision,content,content_digest,actor_id) VALUES ('policy-2','revenue-access',2,%s,%s,'operator')",(Jsonb(content),canonical_digest(content)))
        registry_database.execute_owner("UPDATE gateway.policies SET current_revision=2 WHERE id='revenue-access'")
        with pytest.raises(GatewayError) as caught: await service.submit(principal(),request)
        assert caught.value.code=='NOT_FOUND_OR_FORBIDDEN'
        assert registry_database.scalar('SELECT count(*) FROM gateway.executions')==0
        assert registry_database.scalar("SELECT count(*) FROM gateway.audit_events WHERE action='execute' AND decision='deny'")==1
    asyncio.run(scenario())
