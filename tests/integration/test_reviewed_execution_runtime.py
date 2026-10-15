"""Actual reviewed services, publication, worker and durable result lifecycle.

Requires both guarded disposable backends; there is no simulated Cube response,
fixture-result insertion or arbitrary endpoint/credentials fallback.
"""
import asyncio
import base64
from dataclasses import replace
import json
from pathlib import Path

import pytest

from test_cube_reviewed_runtime import reviewed_stack, cube_stack, SERVICES, assert_exact_rows
from gateway.adapters.bundles import BundleCatalog
from gateway.auth.principals import PrincipalStore
from gateway.auth.settings import Settings
from gateway.auth.tokens import Authenticator
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import PreparedQuery
from gateway.contracts.query import QueryRequest
from gateway.execution.worker import Worker
from gateway.registry.models import BindingRevision, PolicyRow, PolicyRevision, PrincipalRow, PrincipalGrant
from gateway.registry.repository import Registry
from gateway.reviewed_runtime import load_assets, build_runtime

pytestmark = [pytest.mark.postgres, pytest.mark.cube]
ROOT = Path(__file__).resolve().parents[2]


async def seed_reviewed_registry(database, assets):
    """Only operator assets/identities; real fixture execution must earn publication."""
    owner = Registry(database.urls['owner'].render_as_string(hide_password=False))
    try:
        async with owner.transaction() as uow:
            uow.session.add(PolicyRow(id=assets.policy_id, current_revision=assets.policy_revision))
            await uow.session.flush()
            uow.session.add(PolicyRevision(id='reviewed-policy-1', policy_id=assets.policy_id,
                revision=assets.policy_revision, content=assets.policy_content,
                content_digest=canonical_digest(assets.policy_content), actor_id='synthetic-operator'))
            for binding in assets.bindings:
                uow.session.add(BindingRevision(id='reviewed-' + binding.binding_id,
                    binding_id=binding.binding_id, revision=binding.revision,
                    content=binding.model_dump(mode='json'), content_digest=binding.digest,
                    model_bundle_digest=binding.model_bundle_digest,
                    adapter_revision=binding.adapter_revision, actor_id='synthetic-operator'))
            for account in assets.accounts:
                uow.session.add(PrincipalRow(id=account['id'], subject='synthetic-' + account['id'], kind='demo'))
            await uow.session.flush()
            for account in assets.accounts:
                if account['roles']:
                    uow.session.add(PrincipalGrant(principal_id=account['id'], policy_id=assets.policy_id,
                        tenant_id=account['tenant_id'], roles=account['roles'], entitlements=account['entitlements']))
    finally:
        await owner.close()


def actual_assets(stack):
    assets = load_assets()
    records = json.loads((ROOT / 'cube/reviewed-postgres/bundles.json').read_text())
    # The fixture already independently checked Docker IDs, labels, exact names,
    # private bridge addresses, health and no published source/Cube ports.
    for context, record in records.items():
        record['url'] = f'http://{stack.hosts[SERVICES[context]]}:4000'
    return replace(assets, bundles=BundleCatalog(records))


def test_actual_reviewed_publication_worker_replay_and_reauthorized_stored_result(registry_database, reviewed_stack):
    async def scenario():
        assets = actual_assets(reviewed_stack)
        await seed_reviewed_registry(registry_database, assets)
        principals = PrincipalStore(registry_database.registry.sessions)
        authenticator = Authenticator(Settings(), principals)
        # Reuse only already-mounted disposable keys; no new credential/account.
        def key(name):
            return base64.urlsafe_b64decode((reviewed_stack.secrets / name).read_text().strip() + '=')
        runtime = build_runtime(registry=registry_database.registry, authenticator=authenticator,
            secret_resolver=lambda ref: (reviewed_stack.secrets / Path(ref).name).read_text().strip(),
            audit_key=key('cube_signing_key'), cursor_key=key('cube_test_signing_key'), assets=assets)
        try:
            services = runtime.services
            author = await principals.resolve_principal('demo-author')
            publisher = await principals.resolve_principal('demo-publisher')
            north = await principals.resolve_principal('demo-north')
            south = await principals.resolve_principal('demo-south')
            draft = await services.publishing.save_draft(author, assets.manifest, 0)
            run = await services.publishing.test(author, draft.id)
            assert run.status == 'passed'
            checks = run.evidence['checks']
            assert checks and all(check['passed'] for check in checks)
            assert {check['kind'] for check in checks} >= {'known_answer', 'input_negative', 'permission_negative'}
            assert any(check['observed_source_load_watermark'] for check in checks if check['kind'] == 'known_answer')
            pinned = await services.publishing.publish(publisher, draft.id, draft.content_digest,
                draft.revision, 'Verify the disposable reviewed-v2 fixture evidence')
            assert pinned.manifest.adapter_revision == 'cube-rest-v2'
            request = QueryRequest(product_id=assets.manifest.product_id, version=assets.manifest.version,
                inputs={'start': '2024-03-09', 'end': '2024-03-12'}, idempotency_key='reviewed-replay')
            first = await services.execution.submit(north, request)
            replay = await services.execution.submit(north, request)
            assert replay.execution_id == first.execution_id
            worker = Worker(services.execution.queue, adapter_factory=runtime.adapter_factory,
                worker_id='reviewed-runtime-worker', clock=services.clock)
            assert await worker.run_once() is True
            assert (await services.execution.queue.view(first.execution_id)).state == 'succeeded'
            stored = await services.results.get(north, first.execution_id)
            assert_exact_rows(stored, 'north')
            assert registry_database.scalar('SELECT count(*) FROM gateway.results WHERE execution_id=%s', (first.execution_id,)) == 1
            assert registry_database.scalar("SELECT prepared_query->'cube_query'->'dimensions' ? 'Bookings.loaded_at' FROM gateway.execution_preparations WHERE execution_id=%s", (first.execution_id,)) is True
            assert registry_database.scalar('SELECT count(*) FROM gateway.source_reservations WHERE released_at IS NULL') == 0
            assert (await services.execution.submit(north, request)).execution_id == first.execution_id
            with pytest.raises(GatewayError) as denied:
                await services.results.get(south, first.execution_id)
            assert denied.value.code == 'NOT_FOUND_OR_FORBIDDEN'
            await assert_changed_durable_preparation_rejected(services, runtime, registry_database, north, request)
            registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false WHERE principal_id='demo-north'")
            for action in (lambda: services.results.get(north, first.execution_id),
                           lambda: services.execution.submit(north, request)):
                with pytest.raises(GatewayError) as denied:
                    await action()
                assert denied.value.code == 'NOT_FOUND_OR_FORBIDDEN'
            # Release denial never rewrites the previously earned immutable result.
            assert registry_database.scalar('SELECT count(*) FROM gateway.results WHERE execution_id=%s', (first.execution_id,)) == 1
        finally:
            await runtime.close()
    asyncio.run(scenario())


async def assert_changed_durable_preparation_rejected(services, runtime, database, principal, original_request):
    request = original_request.model_copy(update={'idempotency_key': 'reviewed-changed-preparation'})
    accepted = await services.execution.submit(principal, request)
    queue = services.execution.queue
    claim = await queue.claim('reviewed-first-worker')
    assert claim is not None and claim.execution_id == accepted.execution_id
    async with runtime.adapter_factory() as adapter:
        prepared = await adapter.prepare(claim.spec.pinned_product,
            QueryRequest.model_validate(claim.spec.canonical_query), claim.executed_decision,
            context=claim.spec.context, deadline=claim.deadline, request_id=claim.spec.request_id)
    raw = prepared.model_dump(mode='json')
    raw['cube_query']['limit'] = 2
    await queue.save_prepared(claim, PreparedQuery.model_validate(raw))
    # Simulate only the disposable worker dying. The immutable accepted spec
    # remains intact, while replay receives a well-shaped mismatching query.
    database.execute_owner("UPDATE gateway.execution_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE execution_id=%s", (accepted.execution_id,))
    replacement = Worker(queue, adapter_factory=runtime.adapter_factory,
        worker_id='reviewed-replacement-worker', clock=services.clock)
    assert await replacement.run_once() is True
    failed = await queue.view(accepted.execution_id)
    assert failed.state == 'failed' and failed.error.code == 'BINDING_DRIFT'
    assert database.scalar('SELECT count(*) FROM gateway.results WHERE execution_id=%s', (accepted.execution_id,)) == 0
