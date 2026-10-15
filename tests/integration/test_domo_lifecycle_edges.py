"""Domo durable send ambiguity and late proofs on the real PostgreSQL boundary."""
import asyncio
from types import SimpleNamespace
import pytest
from gateway.contracts.canonical import canonical_bytes,canonical_digest
from gateway.contracts.errors import ErrorCode,GatewayError
from gateway.contracts.query import QueryRequest
from tests.integration.source_pilot_domo_seed import seed_domo_pilot
from tests.integration.test_domo_gateway import components,publish

pytestmark=pytest.mark.postgres


async def admit(seed,c,principal_id='usage-consumer-all'):
    from gateway.execution.service import ExecutionService
    service=ExecutionService(registry=seed.registry,policy=seed.policy,bindings=seed.bindings,
        source_lifecycle=seed.lifecycles['consumer'])
    request=c.package.consumer_check.request
    accepted=await service.submit(seed.principals[principal_id],request)
    claim=await service.queue.claim('original-worker')
    assert claim and claim.execution_id==accepted.execution_id
    adapter=c.factory('consumer')()
    prepared=await adapter.prepare(claim.spec.pinned_product,QueryRequest.model_validate(claim.spec.canonical_query),
        claim.executed_decision,context='consumer',deadline=claim.deadline,request_id=claim.spec.request_id)
    current=await service.queue.gate(claim)
    plan=await adapter.source_plan(claim.spec,prepared,claim.executed_decision)
    context=await service.queue.source_attempt(claim,prepared,current,plan)
    return SimpleNamespace(service=service,claim=claim,adapter=adapter,prepared=prepared,context=context,
        lifecycle=seed.lifecycles['consumer'])


def intent(prepared):
    return dict(statement_id=prepared.statement_id,statement_digest=prepared.statement_digest,dataset_id=prepared.dataset_id,
        api_origin_digest=canonical_digest('https://api.domo.com'),response_profile_digest=canonical_digest('domo-query-positional-v1'))


async def send_without_observation(seed,c,a):
    from gateway.adapters.domo_http import DomoHttpClient
    source=next(s for s in seed.assets.sources.sources if s.context=='consumer')
    # Reuse only the actual HTTPX boundary fake from the full gateway test.
    transport=a.adapter._adapters['domo-named-v2']._transport
    client=DomoHttpClient(source=source,secret_resolver=seed.secret_resolver('consumer'),transport=transport)
    token=await client.acquire_token(deadline=a.context.deadline)
    permit=await a.lifecycle.domo_arm_send(a.context,a.prepared.digest,intent(a.prepared))
    statement=seed.assets.statements[a.prepared.model_bundle_digest].resolve(
        a.prepared.statement_id,a.prepared.statement_revision,a.prepared.statement_digest)
    response=await client.query(plan=a.prepared,statement=statement,token=token,permit=permit)
    await client.close()
    return client,permit,response


def test_one_domo_post_survives_reclaim_and_late_exact_observation(registry_database,tmp_path):
    db=registry_database
    async def run():
        async with seed_domo_pilot(db,tmp_path/'assets') as seed:
            c=components(seed);await publish(seed,c);a=await admit(seed,c)
            client,permit,response=await send_without_observation(seed,c,a)
            assert c.calls.count(('consumer','query'))==1
            assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==1
            db.execute_owner("UPDATE gateway.execution_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE execution_id=%s",(a.claim.execution_id,))
            successor=await a.service.queue.claim('successor')
            assert successor.generation==2 and successor.reservation_id==a.claim.reservation_id
            assert successor.prepared==a.prepared
            current=await a.service.queue.gate(successor)
            fresh=await a.service.queue.source_attempt(successor,successor.prepared,current,a.context.spec)
            assert fresh.session_id==a.context.session_id
            other=c.factory('consumer')()
            with pytest.raises(GatewayError):
                await other.poll(successor.prepared,current,spec=successor.spec,executed_decision=successor.executed_decision,
                    source_context=fresh,context='consumer',deadline=successor.deadline,request_id=successor.spec.request_id)
            assert c.calls.count(('consumer','query'))==1
            assert not await a.service.queue.fail(a.claim,ErrorCode.UPSTREAM_UNAVAILABLE,source_completed=True)
            assert await a.service.queue.fail(successor,ErrorCode.UPSTREAM_UNAVAILABLE)
            assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==1
            proof=client.success_evidence(response,permit)
            status=await a.lifecycle.domo_observe_success(permit,proof)
            assert status.group_settled and status.can_complete_session
            assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
            assert db.scalar('SELECT count(*) FROM gateway.results')==0
            retained=db.scalar('SELECT terminal_evidence FROM gateway.domo_source_sends WHERE session_id=%s',(fresh.session_id,))
            assert retained==proof and len(retained)==8
            assert (await a.lifecycle.domo_observe_success(permit,proof)).group_settled
            for changed in [{**proof,'response_sha256':'0'*64},{**proof,'status_code':True},
                    {**proof,'row_count':1002},{**proof,'extra':False},
                    {k:v for k,v in proof.items() if k!='response_bytes'}]:
                with pytest.raises(GatewayError):await a.lifecycle.domo_observe_success(permit,changed)
            from gateway.source_lifecycle import DomoSendPermit
            forged=DomoSendPermit._from_committed(a.context,a.prepared.digest,'wrong-nonce')
            forged.start_io(a.prepared.digest)
            with pytest.raises(GatewayError):await a.lifecycle.domo_observe_success(forged,proof)
            assert db.scalar('SELECT terminal_evidence FROM gateway.domo_source_sends WHERE session_id=%s',(fresh.session_id,))==proof
            await other.close();await a.adapter.close()
    asyncio.run(run())


@pytest.mark.parametrize('started',[False,True])
def test_revocation_cannot_turn_started_send_into_no_io_proof(registry_database,tmp_path,started):
    db=registry_database
    async def run():
        async with seed_domo_pilot(db,tmp_path/'assets') as seed:
            c=components(seed);await publish(seed,c);a=await admit(seed,c)
            permit=await a.lifecycle.domo_arm_send(a.context,a.prepared.digest,intent(a.prepared))
            if started:permit.start_io(a.prepared.digest)
            db.execute_owner("UPDATE gateway.principals SET active=false WHERE id='usage-consumer-all'")
            await a.service.queue.fail(a.claim,ErrorCode.NOT_FOUND_OR_FORBIDDEN)
            if started:
                with pytest.raises(ValueError):await a.lifecycle.domo_observe_not_sent(permit)
                assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==1
            else:
                status=await a.lifecycle.domo_observe_not_sent(permit)
                assert status.group_settled and status.can_complete_session
                assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
            assert c.calls.count(('consumer','query'))==0
            assert db.scalar('SELECT count(*) FROM gateway.results')==0
            await a.adapter.close()
    asyncio.run(run())


def test_domo_preparation_is_owner_bound_and_atomic_with_session(registry_database,tmp_path):
    from gateway.adapters.source_preparations import load_source_prepared,save_source_prepared
    db=registry_database
    async def run():
        async with seed_domo_pilot(db,tmp_path/'assets') as seed:
            c=components(seed);await publish(seed,c);a=await admit(seed,c)
            async with seed.registry.transaction() as uow:
                loaded=await load_source_prepared(uow,'consumer',a.claim.execution_id,a.claim.binding)
                assert loaded==a.prepared
            retained=db.scalar('SELECT prepared_canonical FROM gateway.source_preparations WHERE owner_id=%s',(a.claim.execution_id,))
            assert retained==canonical_bytes(a.prepared).decode()
            with pytest.raises(GatewayError):
                async with seed.registry.transaction() as uow:
                    await save_source_prepared(uow,'consumer','wrong-owner',a.context.session_id,a.prepared,a.claim.binding)
            assert db.scalar('SELECT count(*) FROM gateway.source_preparations')==5
            assert db.scalar('SELECT count(*) FROM gateway.source_sessions')==5
            # A new consumer owner rolls back session plus preparation on caller failure.
            other=await a.service.submit(seed.principals['usage-consumer-core'],c.package.consumer_check.request)
            claim=await a.service.queue.claim('second-owner')
            assert claim.execution_id==other.execution_id
            adapter=c.factory('consumer')()
            prepared=await adapter.prepare(claim.spec.pinned_product,QueryRequest.model_validate(claim.spec.canonical_query),
                claim.executed_decision,context='consumer',deadline=claim.deadline,request_id=claim.spec.request_id)
            current=await a.service.queue.gate(claim)
            plan=await adapter.source_plan(claim.spec,prepared,claim.executed_decision)
            bad=prepared.model_copy(update={'statement_id':'unreviewed'})
            with pytest.raises(GatewayError):await a.service.queue.source_attempt(claim,bad,current,plan)
            assert db.scalar('SELECT count(*) FROM gateway.source_sessions')==5
            assert db.scalar('SELECT count(*) FROM gateway.source_preparations')==5
            from unittest.mock import patch
            async def fail_after_real_save(*args,**kwargs):
                await save_source_prepared(*args,**kwargs)
                raise ValueError('Synthetic failure after both SQL inserts')
            with patch('gateway.adapters.source_preparations.save_source_prepared',side_effect=fail_after_real_save):
                with pytest.raises(GatewayError):await a.service.queue.source_attempt(claim,prepared,current,plan)
            assert db.scalar('SELECT count(*) FROM gateway.source_sessions')==5
            assert db.scalar('SELECT count(*) FROM gateway.source_preparations')==5
            await a.service.queue.fail(a.claim,ErrorCode.UPSTREAM_UNAVAILABLE)
            await a.service.queue.fail(claim,ErrorCode.UPSTREAM_UNAVAILABLE)
            assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
            await adapter.close();await a.adapter.close()
    asyncio.run(run())


def test_unused_original_permit_can_prove_no_io_after_actual_deadline(registry_database,tmp_path):
    from datetime import datetime,timezone
    db=registry_database
    async def run():
        async with seed_domo_pilot(db,tmp_path/'assets') as seed:
            c=components(seed);await publish(seed,c);a=await admit(seed,c)
            permit=await a.lifecycle.domo_arm_send(a.context,a.prepared.digest,intent(a.prepared))
            # Actual DB-issued deadline, not a mutated timestamp or fake clock.
            await asyncio.sleep(max(0,(a.context.deadline-datetime.now(timezone.utc)).total_seconds())+.05)
            with pytest.raises(GatewayError):await a.lifecycle.check_attempt(a.context)
            await a.service.queue.fail(a.claim,ErrorCode.EXECUTION_TIMEOUT)
            status=await a.lifecycle.domo_observe_not_sent(permit)
            assert status.group_settled and status.can_complete_session
            assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
            assert c.calls.count(('consumer','query'))==0
            await a.adapter.close()
    asyncio.run(run())


def test_domo_arm_honors_lower_retained_bigquery_global_cap(registry_database,tmp_path):
    import secrets,psycopg
    from psycopg import sql
    from sqlalchemy import create_engine
    from gateway.adapters.bigquery_runtime import BigQuerySource
    from gateway.source_lifecycle import provision_sources
    from tests.integration.conftest import connection_kwargs
    from tests.unit.test_bigquery_serving_source import source_value
    db=registry_database
    role='gwcap_'+secrets.token_hex(8)
    async def run():
        async with seed_domo_pilot(db,tmp_path/'assets') as seed:
            c=components(seed);await publish(seed,c)
            # An actual retained cross-provider profile tightens the deployment cap.
            with psycopg.connect(**connection_kwargs(db.admin_url),autocommit=True) as admin:
                admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}').format(
                    sql.Identifier(role),sql.Literal(secrets.token_urlsafe(32))))
            try:
                bq=BigQuerySource.model_validate(source_value(max_active_jobs=1,max_global_active_jobs=1))
                engine=create_engine(db.urls['owner'])
                try:
                    with engine.begin() as connection:
                        provision_sources(connection,[bq],[dict(login_name=role,source_id=bq.source_id,revision=1,
                            context='consumer',role_kind='runtime')],deployment_epoch=seed.config.deployment_epoch)
                finally:engine.dispose()
                first=await admit(seed,c);second=await admit(seed,c,'usage-consumer-core')
                permit=await first.lifecycle.domo_arm_send(first.context,first.prepared.digest,intent(first.prepared))
                with pytest.raises(GatewayError):
                    await second.lifecycle.domo_arm_send(second.context,second.prepared.digest,intent(second.prepared))
                assert db.scalar("SELECT count(*) FROM gateway.domo_source_sends d JOIN gateway.source_sessions s ON s.id=d.session_id WHERE s.context='consumer'")==1
                assert c.calls.count(('consumer','query'))==0
                await second.service.queue.fail(second.claim,ErrorCode.RESOURCE_LIMIT)
                await first.service.queue.fail(first.claim,ErrorCode.UPSTREAM_UNAVAILABLE)
                assert (await first.lifecycle.domo_observe_not_sent(permit)).group_settled
                assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity')==0
                await first.adapter.close();await second.adapter.close()
            finally:
                with psycopg.connect(**connection_kwargs(db.admin_url.set(database=db.urls['owner'].database)),autocommit=True) as admin:
                    admin.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                with psycopg.connect(**connection_kwargs(db.admin_url),autocommit=True) as admin:
                    admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
    asyncio.run(run())
