"""Real PostgreSQL reviewed queue identity, deadlines and final-lock races.

Synthetic package/operator review only. No provider credentials or live data are
used; recording adapters, when present, cannot substitute for registry effects.
"""
import asyncio
import copy
from datetime import timezone
from types import SimpleNamespace

import pytest

from gateway.adapters.base import StaticBindingResolver
from gateway.auth.principals import PrincipalStore
from gateway.contracts.errors import GatewayError
from gateway.contracts.identity import Principal
from gateway.contracts.query import QueryRequest
from gateway.execution.service import ExecutionService
from gateway.policy.audit import AuditWriter
from gateway.policy.service import PolicyService
from gateway.source_access import SourceAccessService
from gateway.source_lifecycle import SourceLifecycle
from tests.integration.test_source_approval_registry import install

pytestmark = pytest.mark.postgres


async def setup_queue(db, valid_manifest, *, expires=None, clients=('test-client',)):
    """Use a genuine protected fixture/publication before consumer admission."""
    from tests.integration.test_source_approval_transactions import consumer_setup
    from tests.integration.test_source_fixture_owners import isolated_approval_assets
    def assets(db, raw):
        document, manifest, bindings, sources = isolated_approval_assets(db, raw)
        document['audiences'][0]['client_ids'] = list(clients)
        return document, manifest, bindings, sources
    manifest_data = copy.deepcopy(valid_manifest)
    manifest_data['freshness']['required'] = False
    document, manifest, installed, _ = consumer_setup(db, manifest_data, assets_factory=assets,
        expires=expires.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if expires else None,
        deployment_epoch='queue-test')
    bindings, sources = db.publication_seed.bindings, db.publication_seed.sources
    registry = db.registry
    audit = AuditWriter(b'synthetic-reviewed-queue-audit-key-32')
    access = SourceAccessService(registry, audit)
    policy = PolicyService(registry, PrincipalStore(registry.sessions), audit, source_access=access)
    db.execute_owner("UPDATE gateway.source_runtime_settings SET deployment_epoch='queue-test',max_active_operations=10 WHERE singleton")
    resolver = SimpleNamespace(resolve=lambda ref: next(source for source in sources
        if source.source_id == ref['source_id'] and source.context == ref['context']))
    lifecycle = SourceLifecycle(registry, policy, resolver, deployment_epoch='queue-test')
    service = ExecutionService(registry=registry, policy=policy,
        bindings=StaticBindingResolver(bindings), source_lifecycle=lifecycle)
    actor = Principal(id='north-user', subject='synthetic-north', kind='user', service_client_id=clients[0])
    request = QueryRequest(product_id=manifest.product_id, inputs={'start': '2024-03-09', 'end': '2024-03-12'},
        idempotency_key='queue-reviewed-key')
    return SimpleNamespace(service=service, policy=policy, access=access, document=document,
        installed=installed, actor=actor, request=request, binding=bindings[0], sources=sources)


def revoke(db, seeded):
    return db.scalar('SELECT gateway.source_approval_revoke(%s,%s,%s)',
        (seeded.installed['id'], seeded.installed['content_digest'], 'synthetic-queue-revocation'))


def test_same_key_concurrent_clients_never_share_an_execution_handle(registry_database, valid_manifest):
    async def scenario():
        db = registry_database
        seeded = await setup_queue(db, valid_manifest, clients=('test-client', 'other-client'))
        start = asyncio.Event()
        actors = [seeded.actor, seeded.actor.model_copy(update={'service_client_id': 'other-client'})]
        async def submit(actor):
            await start.wait()
            try:
                return await seeded.service.submit(actor, seeded.request)
            except GatewayError as error:
                return error
        tasks = [asyncio.create_task(submit(actor)) for actor in actors]
        start.set()
        results = await asyncio.gather(*tasks)
        accepted = [result for result in results if not isinstance(result, GatewayError)]
        denied = [result for result in results if isinstance(result, GatewayError)]
        assert len(accepted) == 1 and len(denied) == 1
        assert denied[0].code == 'NOT_FOUND_OR_FORBIDDEN'
        assert db.scalar('SELECT count(*) FROM gateway.executions') == 1
        assert db.scalar('SELECT count(*) FROM gateway.execution_source_authorities') == 1
        assert db.scalar('SELECT count(*) FROM gateway.source_operation_groups WHERE context=\'consumer\'') == 0
        winner = actors[results.index(accepted[0])]
        retry = await seeded.service.submit(winner, seeded.request)
        assert retry.execution_id == accepted[0].execution_id
        retained = db.scalar('SELECT principal_snapshot FROM gateway.execution_source_authorities')
        assert retained == winner.model_dump(mode='json')
    asyncio.run(scenario())


def test_replacement_approval_cannot_resurrect_original_idempotency_key(registry_database, valid_manifest):
    async def scenario():
        db = registry_database
        seeded = await setup_queue(db, valid_manifest)
        original = await seeded.service.submit(seeded.actor, seeded.request)
        original_pin = db.scalar('SELECT row_to_json(a) FROM gateway.execution_source_authorities a')
        revoke(db, seeded)
        successor = copy.deepcopy(seeded.document)
        successor.update(revision=2, supersedes_id=seeded.installed['id'])
        fresh = install(db, successor)
        assert fresh['id'] != seeded.installed['id']
        with pytest.raises(GatewayError) as caught:
            await seeded.service.submit(seeded.actor, seeded.request)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert db.scalar('SELECT id FROM gateway.executions') == original.execution_id
        assert db.scalar('SELECT row_to_json(a) FROM gateway.execution_source_authorities a') == original_pin
        assert db.scalar('SELECT count(*) FROM gateway.source_operation_groups WHERE context=\'consumer\'') == 0
    asyncio.run(scenario())


def test_wait_requires_complete_original_requester(registry_database, valid_manifest):
    async def scenario():
        seeded = await setup_queue(registry_database, valid_manifest, clients=('test-client', 'other-client'))
        accepted = await seeded.service.submit(seeded.actor, seeded.request)
        wrong = seeded.actor.model_copy(update={'service_client_id': 'other-client'})
        with pytest.raises(GatewayError) as caught:
            await seeded.service.queue.wait_authorized(accepted.execution_id, requester=wrong)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        authorized = await seeded.service.queue.wait_authorized(accepted.execution_id, requester=seeded.actor)
        assert authorized.view.execution_id == accepted.execution_id
        assert authorized.principal == seeded.actor
    asyncio.run(scenario())


def test_claim_uses_original_approval_expiry_and_revoke_blocks_preparation(registry_database, valid_manifest):
    async def scenario():
        from gateway.adapters.bigquery_runtime import compile_bigquery_query
        from gateway.contracts.execution import PreparedQuery
        db = registry_database
        expires = db.scalar("SELECT clock_timestamp()+interval '30 seconds'")
        seeded = await setup_queue(db, valid_manifest, expires=expires)
        view = await seeded.service.submit(seeded.actor, seeded.request)
        claim = await seeded.service.queue.claim('reviewed-worker')
        assert claim is not None and claim.execution_id == view.execution_id
        assert claim.deadline == expires
        assert db.scalar('SELECT deadline_at-created_at FROM gateway.executions') .total_seconds() == 120
        retained = db.scalar('SELECT row_to_json(a) FROM gateway.execution_source_authorities a')
        prepared = PreparedQuery(endpoint_id='reviewed-queue', model_bundle_digest=seeded.binding.model_bundle_digest,
            cube_query=compile_bigquery_query(seeded.binding, claim.spec.pinned_product.manifest,
                QueryRequest.model_validate(claim.spec.canonical_query), claim.executed_decision))
        revoke(db, seeded)
        with pytest.raises(GatewayError) as caught:
            await seeded.service.queue.save_prepared(claim, prepared)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert db.scalar('SELECT count(*) FROM gateway.execution_preparations') == 0
        assert db.scalar('SELECT row_to_json(a) FROM gateway.execution_source_authorities a') == retained
    asyncio.run(scenario())


def test_expired_owner_never_creates_claim_even_if_local_clock_rolls_back(registry_database, valid_manifest, clock):
    async def scenario():
        db = registry_database
        expires = db.scalar("SELECT clock_timestamp()+interval '3 seconds'")
        seeded = await setup_queue(db, valid_manifest, expires=expires)
        view = await seeded.service.submit(seeded.actor, seeded.request)
        seeded.service.queue.clock = clock  # 2024 fixture time cannot extend DB authority.
        db.scalar("SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))", (expires,))
        assert db.scalar('SELECT clock_timestamp()>%s', (expires,)) is True
        assert await seeded.service.queue.claim('expired-worker') is None
        assert db.scalar('SELECT count(*) FROM gateway.execution_leases') == 0
        assert db.scalar('SELECT count(*) FROM gateway.source_operation_groups WHERE context=\'consumer\'') == 0
        assert db.scalar('SELECT id FROM gateway.executions') == view.execution_id
    asyncio.run(scenario())


def test_admission_final_gate_serializes_revoke_and_later_claim_denies(registry_database, valid_manifest):
    async def scenario():
        import time
        from queue import Queue
        import psycopg
        from sqlalchemy import text
        from tests.integration.conftest import connection_kwargs
        db = registry_database
        seeded = await setup_queue(db, valid_manifest)
        reached = asyncio.Event()
        release = asyncio.Event()
        admission_pid = None
        original = seeded.access.finalize
        async def paused(uow, requests):
            nonlocal admission_pid
            admission_pid = await uow.session.scalar(text('SELECT pg_backend_pid()'))
            reached.set()
            await release.wait()
            return await original(uow, requests)
        seeded.access.finalize = paused
        submission = asyncio.create_task(seeded.service.submit(seeded.actor, seeded.request))
        gate_reached = asyncio.create_task(reached.wait())
        done, _ = await asyncio.wait((submission, gate_reached), timeout=3,
            return_when=asyncio.FIRST_COMPLETED)
        if submission in done:
            gate_reached.cancel()
            await asyncio.gather(gate_reached, return_exceptions=True)
            await submission
        if gate_reached not in done:
            release.set()
            submission.cancel(); gate_reached.cancel()
            await asyncio.gather(submission, gate_reached, return_exceptions=True)
            pytest.fail('Admission did not reach its final approval gate')
        revoker_pid = Queue()
        def concurrent_revoke():
            with psycopg.connect(**connection_kwargs(db.urls['owner'])) as owner:
                owner.execute("SET LOCAL lock_timeout='6s'")
                revoker_pid.put(owner.info.backend_pid)
                return owner.execute('SELECT gateway.source_approval_revoke(%s,%s,%s)',
                    (seeded.installed['id'], seeded.installed['content_digest'], 'synthetic-race')).fetchone()[0]
        revocation = asyncio.create_task(asyncio.to_thread(concurrent_revoke))
        target = await asyncio.to_thread(revoker_pid.get, True, 2)
        try:
            ceiling = time.monotonic()+3
            while not db.scalar('SELECT %s=ANY(pg_blocking_pids(%s))', (admission_pid, target)):
                assert time.monotonic() < ceiling, 'Revocation did not wait on admission capacity'
                await asyncio.sleep(.005)
            release.set()
            accepted = await submission
            assert (await revocation)['state'] == 'revoked'
        finally:
            release.set()
            await asyncio.gather(submission, revocation, return_exceptions=True)
            seeded.access.finalize = original
        assert db.scalar('SELECT id FROM gateway.executions') == accepted.execution_id
        assert await seeded.service.queue.claim('post-revoke-worker') is None
        assert db.scalar('SELECT count(*) FROM gateway.source_operation_groups WHERE context=\'consumer\'') == 0
    asyncio.run(scenario())


async def materialized_claim(seeded):
    from gateway.adapters.base import authorization_scope_digest
    from gateway.adapters.bigquery_runtime import compile_bigquery_query
    from gateway.contracts.canonical import canonical_digest
    from gateway.contracts.execution import PreparedQuery
    from gateway.source_lifecycle import SourceSessionSpec
    queue = seeded.service.queue
    view = await seeded.service.submit(seeded.actor, seeded.request)
    claim = await queue.claim('completion-worker')
    assert claim is not None and claim.execution_id == view.execution_id
    prepared = PreparedQuery(endpoint_id='reviewed-queue', model_bundle_digest=seeded.binding.model_bundle_digest,
        cube_query=compile_bigquery_query(seeded.binding, claim.spec.pinned_product.manifest,
            QueryRequest.model_validate(claim.spec.canonical_query), claim.executed_decision))
    await queue.save_prepared(claim, prepared)
    source = seeded.sources[0]
    plan = SourceSessionSpec(provider='bigquery', source_id=source.source_id, source_revision=source.revision,
        context='consumer', capacity_key=source.capacity_pool_id, definition_digest=source.digest,
        request_digest=claim.spec.canonical_hash, prepared_digest=canonical_digest(prepared.cube_query),
        scope_digest=authorization_scope_digest(claim.executed_decision),
        decision_digest=canonical_digest(claim.executed_decision), binding_digest=seeded.binding.digest,
        model_bundle_digest=seeded.binding.model_bundle_digest, catalog_digest=None,
        max_submissions=source.max_jobs_per_execution, max_bytes_per_submission=source.per_job_maximum_bytes_billed,
        max_total_bytes=source.total_execution_bytes)
    current = await queue.gate(claim)
    context = await queue.source_attempt(claim, prepared, current, plan)
    return claim, prepared, context


def test_completion_closes_source_before_final_approval_without_second_close(registry_database, valid_manifest):
    async def scenario():
        from gateway.contracts.execution import Completed
        db = registry_database
        seeded = await setup_queue(db, valid_manifest)
        claim, _, _ = await materialized_claim(seeded)
        queue = seeded.service.queue
        events = []
        original_fence, original_close, original_finalize = queue._fence, queue._close_source, seeded.access.finalize
        async def fence(*args, **kwargs):
            events.append('fence')
            return await original_fence(*args, **kwargs)
        async def close(*args, **kwargs):
            events.append('close')
            return await original_close(*args, **kwargs)
        async def finalize(*args, **kwargs):
            events.append('finalize')
            return await original_finalize(*args, **kwargs)
        queue._fence, queue._close_source, seeded.access.finalize = fence, close, finalize
        completed = await queue.complete(claim, Completed(raw_rows=[], source_watermark=None, cache_status='unknown'))
        assert completed.state == 'succeeded'
        assert events == ['fence', 'close', 'finalize']
        assert db.scalar('SELECT count(*) FROM gateway.results') == 1
        assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity') == 0
        assert db.scalar('SELECT closed AND settled_at IS NOT NULL FROM gateway.source_operation_groups WHERE context=\'consumer\'') is True
    asyncio.run(scenario())


def test_direct_revoked_completion_discards_rows_and_independently_settles_terminal_source(registry_database, valid_manifest):
    async def scenario():
        from gateway.contracts.execution import Completed
        db = registry_database
        seeded = await setup_queue(db, valid_manifest)
        claim, _, _ = await materialized_claim(seeded)
        revoke(db, seeded)
        with pytest.raises(GatewayError) as caught:
            await seeded.service.queue.complete(claim,
                Completed(raw_rows=[], source_watermark=None, cache_status='unknown'))
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert db.scalar('SELECT count(*) FROM gateway.results') == 0
        assert db.scalar('SELECT closed AND settled_at IS NOT NULL FROM gateway.source_operation_groups WHERE context=\'consumer\'') is True
        assert db.scalar('SELECT state FROM gateway.execution_states') == 'failed'
        assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'') == 0
    asyncio.run(scenario())


def test_completion_audit_failure_rolls_back_rows_but_terminal_settlement_commits(registry_database, valid_manifest):
    async def scenario():
        from gateway.contracts.execution import Completed
        from gateway.policy.audit import audit_unavailable
        db = registry_database
        seeded = await setup_queue(db, valid_manifest)
        claim, _, _ = await materialized_claim(seeded)
        append = seeded.policy.audit.append
        async def unavailable(uow, event):
            if event.action == 'result' and event.decision == 'allow':
                raise audit_unavailable()
            return await append(uow, event)
        seeded.policy.audit.append = unavailable
        with pytest.raises(GatewayError) as caught:
            await seeded.service.queue.complete(claim,
                Completed(raw_rows=[], source_watermark=None, cache_status='unknown'))
        assert caught.value.code in {'UPSTREAM_UNAVAILABLE', 'NOT_FOUND_OR_FORBIDDEN'}
        assert db.scalar('SELECT count(*) FROM gateway.results') == 0
        assert db.scalar('SELECT closed AND settled_at IS NOT NULL FROM gateway.source_operation_groups WHERE context=\'consumer\'') is True
        assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE actor_id='north-user' AND action='result' AND decision='allow'") == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('mutation', ['deadline', 'principal', 'query'])
def test_gate_revalidates_claim_against_immutable_owner_before_dispatch(registry_database, valid_manifest, mutation):
    async def scenario():
        from datetime import timedelta
        from gateway.contracts.canonical import canonical_digest
        db = registry_database
        seeded = await setup_queue(db, valid_manifest)
        await seeded.service.submit(seeded.actor, seeded.request)
        claim = await seeded.service.queue.claim('claim-mutation-worker')
        assert claim is not None
        if mutation == 'deadline':
            object.__setattr__(claim, 'deadline', claim.deadline + timedelta(hours=1))
        elif mutation == 'principal':
            object.__setattr__(claim, 'principal', claim.principal.model_copy(update={'service_client_id': 'other-client'}))
        else:
            query = claim.spec.model_dump(mode='json')['canonical_query']
            query['inputs']['segment'] = 'A'
            object.__setattr__(claim, 'spec', claim.spec.model_copy(update={
                'canonical_query': query, 'canonical_hash': canonical_digest(query)}))
        with pytest.raises(GatewayError):
            await seeded.service.queue.gate(claim)
        assert db.scalar('SELECT count(*) FROM gateway.execution_preparations') == 0
        assert db.scalar('SELECT count(*) FROM gateway.source_sessions WHERE context=\'consumer\'') == 0
    asyncio.run(scenario())


@pytest.fixture
def source_runtime_role(registry_database):
    """One real non-elevated provider login, scoped only to this random DB."""
    import secrets
    import psycopg
    from psycopg import sql
    from sqlalchemy import create_engine
    from tests.integration.conftest import connection_kwargs
    from gateway.source_lifecycle import provision_sources
    db = registry_database
    role = 'gwq_' + secrets.token_hex(8)
    password = secrets.token_urlsafe(32)
    created = False
    try:
        with psycopg.connect(**connection_kwargs(db.admin_url), autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}')
                .format(sql.Identifier(role), sql.Literal(password)))
            created = True
            admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}')
                .format(sql.Identifier(db.urls['owner'].database), sql.Identifier(role)))
        url = db.urls['app'].set(username=role, password=password)
        def provision(sources):
            engine = create_engine(db.urls['owner'])
            try:
                with engine.begin() as connection:
                    source = sources[0]
                    provision_sources(connection, sources, [dict(login_name=role, source_id=source.source_id,
                        revision=source.revision, context='consumer', role_kind='runtime')], deployment_epoch='queue-test')
            finally:
                engine.dispose()
        def call(statement, parameters):
            from psycopg.types.json import Jsonb
            with psycopg.connect(**connection_kwargs(url)) as provider:
                return provider.execute(statement, tuple(Jsonb(value) if isinstance(value, dict) else value
                    for value in parameters)).fetchone()[0]
        yield SimpleNamespace(provision=provision, call=call)
    finally:
        if created:
            with psycopg.connect(**connection_kwargs(db.admin_url.set(database=db.urls['owner'].database)), autocommit=True) as admin:
                admin.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            with psycopg.connect(**connection_kwargs(db.admin_url), autocommit=True) as admin:
                admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@pytest.mark.parametrize('deny_audit_fails', [False, True])
@pytest.mark.parametrize('invalidation', ['revoke', 'expire'])
def test_invalidated_completion_keeps_unknown_job_held_across_restart_until_exact_terminal_proof(
        registry_database, valid_manifest, source_runtime_role, deny_audit_fails, invalidation):
    async def scenario():
        from gateway.contracts.execution import Completed
        from gateway.execution.queue import ExecutionQueue
        from gateway.policy.audit import audit_unavailable
        from tests.integration.test_source_lifecycle import intent
        db = registry_database
        expires = db.scalar("SELECT clock_timestamp()+interval '4 seconds'") if invalidation == 'expire' else None
        seeded = await setup_queue(db, valid_manifest, expires=expires)
        source_runtime_role.provision(seeded.sources)
        claim, _, context = await materialized_claim(seeded)
        admitted = source_runtime_role.call('SELECT gateway.source_bq_admit(%s,%s,%s)',
            (context.wire_context(), 'e'*64, intent(timeout=1000)))
        assert admitted['submit_once'] is True
        job = admitted['job']
        retained = db.scalar('SELECT row_to_json(a) FROM gateway.execution_source_authorities a')
        if invalidation == 'revoke':
            revoke(db, seeded)
        else:
            db.scalar("SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))", (expires,))
        if deny_audit_fails:
            append = seeded.policy.audit.append
            async def failing_denial(uow, event):
                if event.decision == 'deny':
                    raise audit_unavailable()
                return await append(uow, event)
            seeded.policy.audit.append = failing_denial
        with pytest.raises(GatewayError) as caught:
            await seeded.service.queue.complete(claim,
                Completed(raw_rows=[], source_watermark=None, cache_status='unknown'))
        assert caught.value.code == ('UPSTREAM_UNAVAILABLE' if deny_audit_fails else 'NOT_FOUND_OR_FORBIDDEN')
        assert db.scalar('SELECT closed AND settled_at IS NULL FROM gateway.source_operation_groups WHERE context=\'consumer\'') is True
        assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity') == 1
        restarted = ExecutionQueue(db.registry, seeded.policy, seeded.service.bindings,
            source_lifecycle=seeded.service.queue.source_lifecycle)
        await restarted.maintain()
        assert await restarted.claim('restart-worker') is None
        assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity') == 1
        assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'') == 1
        observed = source_runtime_role.call('SELECT gateway.source_bq_observe(%s,%s)',
            (job['job_id'], dict(intent_digest=job['intent_digest'], state='done', terminal_error=False)))
        assert observed['terminal'] is True and observed['group_settled'] is True
        assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity') == 0
        assert db.scalar('SELECT count(*) FROM gateway.results') == 0
        assert db.scalar('SELECT row_to_json(a) FROM gateway.execution_source_authorities a') == retained
        assert await restarted.claim('after-terminal-worker') is None
        assert db.scalar('SELECT count(*) FROM gateway.bigquery_source_jobs j JOIN gateway.source_sessions s ON s.id=j.session_id WHERE s.context=\'consumer\'') == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['admission', 'completion'])
def test_expiry_during_final_gate_rolls_back_data_and_preserves_only_terminal_cleanup(
        registry_database, valid_manifest, stage):
    async def scenario():
        from gateway.contracts.execution import Completed
        from sqlalchemy import text
        db = registry_database
        expires = db.scalar("SELECT clock_timestamp()+interval '4 seconds'")
        seeded = await setup_queue(db, valid_manifest, expires=expires)
        claim = None
        if stage == 'completion':
            claim, _, _ = await materialized_claim(seeded)
        original = seeded.access.finalize
        async def expired_at_final_gate(uow, requests):
            # Earlier owner/state/source writes have completed. A fresh database
            # clock, not process wall time, must still deny the final suffix.
            await uow.session.execute(text("SELECT pg_sleep(greatest(0,extract(epoch FROM "
                "(CAST(:expiry AS timestamptz)-clock_timestamp()))+0.02))"), {'expiry': expires})
            return await original(uow, requests)
        seeded.access.finalize = expired_at_final_gate
        with pytest.raises(GatewayError) as caught:
            if stage == 'admission':
                await seeded.service.submit(seeded.actor, seeded.request)
            else:
                await seeded.service.queue.complete(claim,
                    Completed(raw_rows=[], source_watermark=None, cache_status='unknown'))
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert db.scalar('SELECT count(*) FROM gateway.results') == 0
        assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE decision='deny'") == 1
        if stage == 'admission':
            assert db.scalar('SELECT count(*) FROM gateway.executions') == 0
            assert db.scalar('SELECT count(*) FROM gateway.execution_source_authorities') == 0
            assert db.scalar('SELECT count(*) FROM gateway.idempotency_records') == 0
        else:
            assert db.scalar('SELECT closed AND settled_at IS NOT NULL FROM gateway.source_operation_groups WHERE context=\'consumer\'') is True
            assert db.scalar('SELECT state FROM gateway.execution_states') == 'timed_out'
    asyncio.run(scenario())


@pytest.mark.parametrize('current_policy', [False, True])
def test_completion_budget_or_changed_policy_cannot_outlive_final_allow_audit(
        registry_database, valid_manifest, current_policy):
    async def scenario():
        from datetime import timedelta
        from gateway.contracts.execution import Completed
        from sqlalchemy import text
        from tests.integration.test_execution_queue import install_policy
        db = registry_database
        raw = copy.deepcopy(valid_manifest)
        if not current_policy:
            raw.setdefault('limits', {})['execution_timeout_seconds'] = 2
            raw['limits']['sync_wait_seconds'] = 1
        seeded = await setup_queue(db, raw)
        claim, _, _ = await materialized_claim(seeded)
        if current_policy:
            install_policy(db, {'execution_timeout_seconds': 2, 'sync_wait_seconds': 1})
        deadline = claim.accepted_at + timedelta(seconds=2)
        append = seeded.policy.audit.append
        reached = []
        async def delayed(uow, event):
            await append(uow, event)
            if event.action == 'result' and event.decision == 'allow':
                reached.append(True)
                await uow.session.execute(text("SELECT pg_sleep(greatest(0,extract(epoch FROM "
                    "(CAST(:deadline AS timestamptz)-clock_timestamp()))+0.03))"), {'deadline': deadline})
        seeded.policy.audit.append = delayed
        with pytest.raises(GatewayError) as caught:
            await seeded.service.queue.complete(claim,
                Completed(raw_rows=[], source_watermark=None, cache_status='unknown'))
        assert caught.value.code == ('NOT_FOUND_OR_FORBIDDEN' if current_policy else 'EXECUTION_TIMEOUT')
        assert bool(reached) is not current_policy
        assert db.scalar('SELECT count(*) FROM gateway.results') == 0
        assert db.scalar("SELECT count(*) FROM gateway.audit_events WHERE actor_id='north-user' AND action='result' AND decision='allow'") == 0
        assert db.scalar('SELECT closed AND settled_at IS NOT NULL FROM gateway.source_operation_groups WHERE context=\'consumer\'') is True
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['claim', 'gate', 'prepared', 'completion'])
def test_expired_lease_at_final_audit_returns_no_worker_capability_or_rows(
        registry_database, valid_manifest, stage):
    async def scenario():
        from gateway.adapters.bigquery_runtime import compile_bigquery_query
        from gateway.contracts.execution import Completed, PreparedQuery
        from sqlalchemy import text
        db = registry_database
        seeded = await setup_queue(db, valid_manifest)
        queue = seeded.service.queue
        queue.LEASE_SECONDS = 1
        if stage == 'completion':
            claim, _, _ = await materialized_claim(seeded)
        else:
            await seeded.service.submit(seeded.actor, seeded.request)
            claim = None if stage == 'claim' else await queue.claim('lease-tail-worker')
        reached = []
        append = seeded.policy.audit.append
        async def delayed(uow, event):
            await append(uow, event)
            if event.action == ('result' if stage == 'completion' else 'execute') and event.decision == 'allow':
                reached.append(True)
                await uow.session.execute(text("SELECT pg_sleep(greatest(0,extract(epoch FROM "
                    "((SELECT expires_at FROM gateway.execution_leases)-clock_timestamp()))+0.03))"))
        seeded.policy.audit.append = delayed
        if stage == 'claim':
            assert await queue.claim('late-claim-worker') is None
            assert db.scalar('SELECT count(*) FROM gateway.execution_leases') == 0
            assert db.scalar('SELECT count(*) FROM gateway.source_operation_groups WHERE context=\'consumer\'') == 0
        else:
            with pytest.raises(GatewayError) as caught:
                if stage == 'gate':
                    await queue.gate(claim)
                elif stage == 'prepared':
                    prepared = PreparedQuery(endpoint_id='reviewed-queue', model_bundle_digest=seeded.binding.model_bundle_digest,
                        cube_query=compile_bigquery_query(seeded.binding, claim.spec.pinned_product.manifest,
                            QueryRequest.model_validate(claim.spec.canonical_query), claim.executed_decision))
                    await queue.save_prepared(claim, prepared)
                else:
                    await queue.complete(claim, Completed(raw_rows=[], source_watermark=None, cache_status='unknown'))
            assert caught.value.code == 'CONFLICT'
            if stage == 'prepared':
                assert db.scalar('SELECT count(*) FROM gateway.execution_preparations') == 0
            if stage == 'completion':
                assert db.scalar('SELECT closed AND settled_at IS NOT NULL FROM gateway.source_operation_groups WHERE context=\'consumer\'') is True
        assert reached
        assert db.scalar('SELECT count(*) FROM gateway.results') == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['gate', 'prepared'])
def test_policy_revision_tightening_denies_before_final_audit(
        registry_database, valid_manifest, stage):
    async def scenario():
        from datetime import timedelta
        from sqlalchemy import text
        from tests.integration.test_execution_queue import install_policy
        db = registry_database
        seeded = await setup_queue(db, valid_manifest)
        claim, prepared, _ = await materialized_claim(seeded)
        install_policy(db, {'execution_timeout_seconds': 2, 'sync_wait_seconds': 1})
        deadline = claim.accepted_at + timedelta(seconds=2)
        append = seeded.policy.audit.append
        reached = []
        async def delayed(uow, event):
            await append(uow, event)
            if event.action == 'execute' and event.decision == 'allow':
                reached.append(True)
                await uow.session.execute(text("SELECT pg_sleep(greatest(0,extract(epoch FROM "
                    "(CAST(:deadline AS timestamptz)-clock_timestamp()))+0.03))"), {'deadline': deadline})
        seeded.policy.audit.append = delayed
        with pytest.raises(GatewayError) as caught:
            if stage == 'gate':
                await seeded.service.queue.gate(claim)
            else:
                await seeded.service.queue.save_prepared(claim, prepared)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        assert not reached
        assert db.scalar('SELECT count(*) FROM gateway.results') == 0
        assert db.scalar('SELECT count(*) FROM gateway.held_source_capacity') == 1
    asyncio.run(scenario())
