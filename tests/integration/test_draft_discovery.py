"""Authorized draft discovery against disposable real PostgreSQL only.

The publication seed uses the registry transaction seam, as the REST integration
suite does. Its synthetic test record is not evidence of source correctness or
of a successful publishing fixture run.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb
import pytest

from gateway.api.app import create_app
from gateway.api.cursors import PageCursorCodec
from gateway.api.transport import TransportSettings
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.identity import Principal
from gateway.contracts.manifest import validate_manifest
from gateway.registry.models import TestRun as RegistryTestRun
from tests.contract.test_cube_adapter import synthetic
from tests.integration.conftest import connection_kwargs
from tests.integration.test_audit import principal
from tests.integration.test_publishing import setup
from tests.unit.test_policy import rules

pytestmark = pytest.mark.postgres

SUMMARY_FIELDS = {
    'id', 'product_id', 'version', 'name', 'revision',
    'editable_full_definition', 'can_test', 'can_publish',
}


def api():
    module = importlib.import_module('gateway.api.drafts')
    assert hasattr(module, 'DraftDiscoveryService'), 'DraftDiscoveryService is not implemented'
    return module


async def composition(database, clock):
    publishing, manifest = await setup(database, roles=('author', 'publisher', 'policy_admin'))
    discovery = api().DraftDiscoveryService(
        registry=database.registry, policy=publishing.policy, publishing=publishing,
        cursors=PageCursorCodec(b'd' * 32, clock),
    )
    return discovery, publishing, manifest


def changed(manifest, **changes):
    raw = manifest.model_dump(mode='json', exclude={'content_digest'})
    raw.update(changes)
    return validate_manifest(raw)


def set_roles(database, roles, *, policy_id='revenue-access', tenant_id='north'):
    database.execute_owner(
        'UPDATE gateway.principal_grants SET roles=%s,tenant_id=%s '
        'WHERE principal_id=%s AND policy_id=%s',
        (Jsonb(list(roles)), tenant_id, principal().id, policy_id),
    )


def seed_other_policy(database, *, policy_id='other-access', roles=('author', 'publisher', 'policy_admin')):
    content = {'rules': rules(), 'reason': 'Independent synthetic review policy'}
    with psycopg.connect(**connection_kwargs(database.urls['owner'])) as connection:
        connection.execute(
            'INSERT INTO gateway.policies(id,current_revision) VALUES (%s,1)', (policy_id,),
        )
        connection.execute(
            'INSERT INTO gateway.policy_revisions(id,policy_id,revision,content,content_digest,actor_id) '
            'VALUES (%s,%s,1,%s,%s,%s)',
            (str(uuid4()), policy_id, Jsonb(content), canonical_digest(content), principal().id),
        )
        connection.execute(
            'INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) '
            "VALUES (%s,%s,'north',%s,'{}')",
            (principal().id, policy_id, Jsonb(list(roles))),
        )


async def seed_publication(database, draft, manifest):
    """Persist a real immutable version without claiming fixture-run readiness."""
    test_id = str(uuid4())
    async with database.registry.transaction() as uow:
        uow.session.add(RegistryTestRun(
            id=test_id, product_id=manifest.product_id, draft_id=draft.id,
            draft_digest=draft.content_digest, binding_id='binding-1',
            binding_digest=synthetic()[0].digest, policy_id=manifest.policy_id,
            policy_revision=1, fixture_version='1', evidence={'synthetic_registry_seed': True},
            status='passed', actor_id=principal().id,
        ))
    async with database.registry.transaction() as uow:
        return await uow.insert_version(
            draft_id=draft.id, expected_revision=draft.revision,
            test_run_id=test_id, actor_id=principal().id,
        )


async def seed_pair(publishing, manifest):
    first = await publishing.save_draft(principal(), changed(manifest, product_id='alpha'), 0)
    second = await publishing.save_draft(principal(), changed(manifest, product_id='bravo'), 0)
    return first, second


def test_draft_summary_releases_only_review_metadata(registry_database, clock):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        draft = await publishing.save_draft(principal(), manifest, 0)
        page = await discovery.search(principal())
        assert page.model_dump(mode='json') == {
            'items': [{
                'id': draft.id, 'product_id': manifest.product_id, 'version': manifest.version,
                'name': manifest.name, 'revision': 1, 'editable_full_definition': True,
                'can_test': True, 'can_publish': True,
            }],
            'next_cursor': None,
        }
        assert set(page.items[0].model_dump()) == SUMMARY_FIELDS
        assert registry_database.scalar('SELECT count(*) FROM gateway.executions') == 0
        assert registry_database.scalar('SELECT count(*) FROM gateway.test_runs') == 0
    asyncio.run(scenario())


def test_creator_and_non_review_roles_cannot_discover_draft_metadata(registry_database, clock):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        draft = await publishing.save_draft(
            principal(), changed(manifest, name='Confidential planning draft'), 0,
        )
        for roles in ((), ('consumer',), ('operator',), ('policy_admin',), ('audit_view',)):
            set_roles(registry_database, roles)
            for query in ('', draft.product_id, 'Confidential', manifest.version):
                assert (await discovery.search(principal(), query=query)).model_dump(mode='json') == {
                    'items': [], 'next_cursor': None,
                }, (roles, query)
    asyncio.run(scenario())


def test_author_role_in_an_unrelated_tenant_cannot_discover_drafts(registry_database, clock):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        await publishing.save_draft(principal(), manifest, 0)
        set_roles(registry_database, ('author', 'publisher'), tenant_id='unrelated-tenant')
        assert (await discovery.search(principal())).model_dump(mode='json') == {
            'items': [], 'next_cursor': None,
        }
    asyncio.run(scenario())


def test_unrelated_policy_roles_do_not_supply_target_review_permission(registry_database, clock):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        seed_other_policy(registry_database)
        hidden = await publishing.save_draft(
            principal(), changed(manifest, product_id='hidden', name='Restricted planning', policy_id='other-access'), 0,
        )
        visible = await publishing.save_draft(principal(), manifest, 0)
        set_roles(registry_database, ('consumer',), policy_id='other-access')
        page = await discovery.search(principal())
        assert [item.id for item in page.items] == [visible.id]
        for query in ('hidden', 'Restricted'):
            assert (await discovery.search(principal(), query=query)).items == ()
        assert hidden.id not in page.model_dump_json()
    asyncio.run(scenario())


def test_capabilities_keep_author_visibility_and_publisher_roles_separate(registry_database, clock):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        await publishing.save_draft(principal(), manifest, 0)
        for partial in (False, True):
            if partial:
                set_roles(registry_database, ('author', 'publisher', 'policy_admin'))
                await publishing.policy.change_policy(
                    principal(), manifest.policy_id, rules(visible_fields=['day', 'revenue']),
                    'Limit definition visibility for synthetic review',
                )
            for roles in (('author',), ('publisher',), ('author', 'publisher')):
                set_roles(registry_database, roles)
                # Metadata review does not require consumer data entitlements.
                registry_database.execute_owner("UPDATE gateway.principal_grants SET entitlements='{}'")
                summary, = (await discovery.search(principal())).items
                expected = (
                    'author' in roles and not partial, 'author' in roles, 'publisher' in roles,
                )
                assert (summary.editable_full_definition, summary.can_test, summary.can_publish) == expected, (roles, partial)
                assert set(summary.model_dump()) == SUMMARY_FIELDS
    asyncio.run(scenario())


def test_publish_capability_requires_independent_base_and_target_policy_authority(registry_database, clock):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        seed_other_policy(registry_database)
        base = await publishing.save_draft(principal(), manifest, 0)
        await seed_publication(registry_database, base, manifest)
        next_manifest = changed(manifest, version='1.1.0', policy_id='other-access')
        target = await publishing.save_draft(principal(), next_manifest, 1)
        cases = [
            (('consumer',), ('publisher',), False),
            (('publisher',), ('author',), False),
            (('publisher',), ('publisher',), True),
        ]
        for base_roles, target_roles, can_publish in cases:
            set_roles(registry_database, base_roles)
            set_roles(registry_database, target_roles, policy_id='other-access')
            summary, = (await discovery.search(principal())).items
            assert summary.id == target.id and summary.can_publish is can_publish, (base_roles, target_roles)
            assert summary.can_test is ('author' in target_roles)
            assert summary.editable_full_definition is ('author' in target_roles)
    asyncio.run(scenario())


def test_only_latest_draft_head_is_discoverable(registry_database, clock):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        old = await publishing.save_draft(principal(), changed(manifest, name='Obsolete draft'), 0)
        current_manifest = changed(manifest, name='Current draft', version='2.0.0')
        current = await publishing.save_draft(principal(), current_manifest, 1)
        summary, = (await discovery.search(principal())).items
        assert (summary.id, summary.revision, summary.version, summary.name) == (
            current.id, 2, '2.0.0', 'Current draft',
        )
        assert summary.id != old.id
        assert (await discovery.search(principal(), query='Obsolete')).items == ()
    asyncio.run(scenario())


def test_published_draft_head_stays_excluded_after_lifecycle_changes(registry_database, clock):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        draft = await publishing.save_draft(principal(), manifest, 0)
        version = await seed_publication(registry_database, draft, manifest)
        for status, action in (('published', None), ('deprecated', 'deprecate'), ('withdrawn', 'withdraw')):
            if action:
                await publishing.transition(
                    principal(), manifest.product_id, manifest.version, action, 'Synthetic lifecycle change',
                )
            assert registry_database.scalar(
                'SELECT status FROM gateway.version_states WHERE version_id=%s', (version.id,),
            ) == status
            assert registry_database.scalar(
                'SELECT draft_id FROM gateway.product_versions WHERE id=%s', (version.id,),
            ) == draft.id
            assert (await discovery.search(principal())).model_dump(mode='json') == {
                'items': [], 'next_cursor': None,
            }, status
        replacement = await publishing.save_draft(principal(), changed(manifest, version='1.1.0'), 1)
        assert [item.id for item in (await discovery.search(principal())).items] == [replacement.id]
    asyncio.run(scenario())


def test_search_is_case_insensitive_on_authorized_id_name_and_version_only(registry_database, clock):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        draft = await publishing.save_draft(principal(), changed(
            manifest, product_id='Roadmap-revenue', name='Quarterly Forecast', version='7.8.9',
            description='Description-only secret needle', owner='ownership-needle', domain='domain-needle',
        ), 0)
        for query in ('ROADMAP', 'forecast', '8.9'):
            assert [item.id for item in (await discovery.search(principal(), query=query)).items] == [draft.id]
        for query in ('Description-only', 'ownership-needle', 'domain-needle', 'not-present'):
            assert (await discovery.search(principal(), query=query)).items == ()
    asyncio.run(scenario())


def test_paging_uses_product_id_c_order_without_duplicates(registry_database, clock):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        product_ids = ['zulu', 'alpha', 'Aardvark', 'a-1', 'Zebra', 'a_1', 'a.1']
        for product_id in product_ids:
            await publishing.save_draft(principal(), changed(manifest, product_id=product_id), 0)
        pages = []
        cursor = None
        for _ in range(len(product_ids)):
            page = await discovery.search(principal(), cursor=cursor, limit=2)
            assert 1 <= len(page.items) <= 2
            pages.extend(item.product_id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert cursor is None
        assert pages == sorted(product_ids)
        assert len(set(pages)) == len(product_ids)
    asyncio.run(scenario())


def test_case_normalized_search_can_continue_with_the_same_cursor(registry_database, clock):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        first, second = await seed_pair(publishing, manifest)
        page = await discovery.search(principal(), query='DAILY', limit=1)
        assert [item.id for item in page.items] == [first.id] and page.next_cursor
        following = await discovery.search(principal(), query='daily', cursor=page.next_cursor, limit=1)
        assert [item.id for item in following.items] == [second.id]
        assert following.next_cursor is None
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['grant', 'policy', 'account'])
def test_cursor_fails_closed_after_current_authority_changes(registry_database, clock, change):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        await seed_pair(publishing, manifest)
        page = await discovery.search(principal(), limit=1)
        assert page.next_cursor
        if change == 'grant':
            registry_database.execute_owner('UPDATE gateway.principal_grants SET active=false')
        elif change == 'policy':
            await publishing.policy.change_policy(
                principal(), manifest.policy_id, rules(), 'Refresh synthetic policy revision',
            )
        else:
            registry_database.execute_owner("UPDATE gateway.principals SET active=false WHERE id='north-user'")
        with pytest.raises(GatewayError) as caught:
            await discovery.search(principal(), cursor=page.next_cursor, limit=1)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
    asyncio.run(scenario())


def test_cursor_is_bound_to_principal_normalized_request_and_purpose(registry_database, clock):
    async def scenario():
        from gateway.api.catalog import scope_fingerprint
        discovery, publishing, manifest = await composition(registry_database, clock)
        await seed_pair(publishing, manifest)
        page = await discovery.search(principal(), limit=1)
        assert page.next_cursor
        token = page.next_cursor
        registry_database.execute_owner(
            "INSERT INTO gateway.principals(id,subject,kind) VALUES ('another-user','another-subject','demo')",
        )
        registry_database.execute_owner(
            'INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) '
            "SELECT 'another-user',policy_id,tenant_id,roles,entitlements FROM gateway.principal_grants "
            "WHERE principal_id='north-user'",
        )
        other = Principal(id='another-user', subject='another-subject', service_client_id=None, kind='demo')
        async with registry_database.registry.transaction() as uow:
            scope = await scope_fingerprint(publishing.policy, principal(), uow)
        other_purpose = discovery.cursors.issue(
            purpose='catalog', identity=canonical_digest(principal()), scope=scope,
            query=canonical_digest({'query': '', 'limit': 1}), last='alpha',
        )
        tampered = ('A' if token[0] != 'A' else 'B') + token[1:]
        cases = [
            (other, token, {'limit': 1}),
            (principal(), token, {'limit': 1, 'query': 'different'}),
            (principal(), token, {'limit': 2}),
            (principal(), other_purpose, {'limit': 1}),
            (principal(), tampered, {'limit': 1}),
        ]
        for actor, candidate, kwargs in cases:
            with pytest.raises(GatewayError) as caught:
                await discovery.search(actor, cursor=candidate, **kwargs)
            assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
        # Rejected replays have not consumed or changed the valid continuation.
        following = await discovery.search(principal(), cursor=token, limit=1)
        assert [item.product_id for item in following.items] == ['bravo']
    asyncio.run(scenario())


def test_cursor_expires_at_exactly_five_minutes(registry_database, clock):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        await seed_pair(publishing, manifest)
        page = await discovery.search(principal(), limit=1)
        assert page.next_cursor
        clock.instant += timedelta(seconds=299)
        assert [item.product_id for item in (await discovery.search(
            principal(), cursor=page.next_cursor, limit=1,
        )).items] == ['bravo']
        clock.instant += timedelta(seconds=1)
        with pytest.raises(GatewayError) as caught:
            await discovery.search(principal(), cursor=page.next_cursor, limit=1)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
    asyncio.run(scenario())


def test_discovery_rejects_unbounded_or_wrong_type_arguments(registry_database, clock):
    async def scenario():
        discovery, _, _ = await composition(registry_database, clock)
        cases = [
            {'limit': 0}, {'limit': 51}, {'limit': True}, {'limit': '1'},
            {'query': 'x' * 257}, {'query': None}, {'cursor': ''}, {'cursor': 'x' * 4097},
        ]
        for kwargs in cases:
            with pytest.raises(GatewayError) as caught:
                await discovery.search(principal(), **kwargs)
            assert caught.value.code == 'INVALID_INPUT', kwargs
    asyncio.run(scenario())


def test_candidate_scan_is_bounded_even_when_query_would_match_nothing(registry_database, clock):
    async def scenario():
        discovery, _, manifest = await composition(registry_database, clock)
        records = []
        for index in range(1001):
            product_id = f'candidate-{index:04d}'
            draft_id = f'draft-{index:04d}'
            value = changed(manifest, product_id=product_id)
            records.append((product_id, draft_id, value))
        # Bulk fixture data still goes through real PostgreSQL identity/head
        # constraints; no fake registry or in-memory discovery result exists.
        with psycopg.connect(**connection_kwargs(registry_database.urls['owner'])) as connection:
            cursor = connection.cursor()
            cursor.executemany(
                'INSERT INTO gateway.products(id) VALUES (%s)',
                [(product_id,) for product_id, _, _ in records],
            )
            cursor.executemany(
                'INSERT INTO gateway.draft_revisions(id,product_id,revision,manifest,content_digest,actor_id) '
                'VALUES (%s,%s,1,%s,%s,%s)',
                [(draft_id, product_id, Jsonb(value.model_dump(mode='json')), value.content_digest, principal().id)
                 for product_id, draft_id, value in records],
            )
            cursor.executemany(
                'UPDATE gateway.products SET draft_revision=1,latest_draft_id=%s WHERE id=%s',
                [(draft_id, product_id) for product_id, draft_id, _ in records],
            )
        with pytest.raises(GatewayError) as caught:
            await discovery.search(principal(), query='no-matching-name', limit=1)
        assert caught.value.code == 'RESOURCE_LIMIT'
        # A consumer cannot infer a hidden population through a 1001-row limit.
        set_roles(registry_database, ('consumer',))
        assert (await discovery.search(principal())).model_dump(mode='json') == {
            'items': [], 'next_cursor': None,
        }
        set_roles(registry_database, ('author', 'publisher'), tenant_id='unrelated-tenant')
        assert (await discovery.search(principal())).model_dump(mode='json') == {
            'items': [], 'next_cursor': None,
        }
        # An eligible role for another policy still cannot observe this bound.
        seed_other_policy(registry_database)
        assert (await discovery.search(principal())).model_dump(mode='json') == {
            'items': [], 'next_cursor': None,
        }
    asyncio.run(scenario())


def test_no_summary_escapes_a_deferred_authorization_audit_commit_failure(registry_database, clock):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        await publishing.save_draft(principal(), manifest, 0)
        before = registry_database.scalar('SELECT count(*) FROM gateway.audit_events')
        registry_database.execute_owner(
            'CREATE FUNCTION gateway.fail_draft_review_audit() RETURNS trigger LANGUAGE plpgsql AS $$ '
            "BEGIN IF NEW.action IN ('author','publish') THEN "
            "RAISE EXCEPTION 'synthetic deferred review audit outage'; END IF; RETURN NEW; END $$",
        )
        registry_database.execute_owner(
            'CREATE CONSTRAINT TRIGGER fail_draft_review_audit AFTER INSERT ON gateway.audit_events '
            'DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION gateway.fail_draft_review_audit()',
        )
        with pytest.raises(GatewayError) as caught:
            await discovery.search(principal())
        assert caught.value.code == 'UPSTREAM_UNAVAILABLE'
        assert registry_database.scalar('SELECT count(*) FROM gateway.audit_events') == before
    asyncio.run(scenario())


@pytest.mark.parametrize('mutation', ['head', 'publication', 'grant', 'policy', 'account'])
def test_final_release_rechecks_state_after_initial_review(
    registry_database, clock, monkeypatch, mutation,
):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        draft = await publishing.save_draft(principal(), manifest, 0)
        module = api()
        real_transaction = module.read_transaction
        real_projection = discovery._projection
        reviewed = False
        mutated = False

        async def projection(*args, **kwargs):
            nonlocal reviewed
            result = await real_projection(*args, **kwargs)
            reviewed = True
            return result

        @asynccontextmanager
        async def transaction(registry):
            nonlocal mutated
            async with real_transaction(registry) as uow:
                yield uow
            # Run only after the real preliminary authorization transaction has
            # committed and released its locks. The final response must observe
            # this real persisted change, rather than a mocked review result.
            if reviewed and not mutated:
                mutated = True
                if mutation == 'head':
                    await publishing.save_draft(
                        principal(), changed(manifest, name='Replacement after initial review'), 1,
                    )
                elif mutation == 'publication':
                    await seed_publication(registry_database, draft, manifest)
                elif mutation == 'grant':
                    registry_database.execute_owner('UPDATE gateway.principal_grants SET active=false')
                elif mutation == 'policy':
                    await publishing.policy.change_policy(
                        principal(), manifest.policy_id, rules(visible_fields=['day', 'revenue']),
                        'Change visibility before draft response release',
                    )
                else:
                    registry_database.execute_owner(
                        "UPDATE gateway.principals SET active=false WHERE id='north-user'",
                    )

        monkeypatch.setattr(discovery, '_projection', projection)
        monkeypatch.setattr(module, 'read_transaction', transaction)
        with pytest.raises(GatewayError) as caught:
            await discovery.search(principal())
        assert reviewed and mutated, 'The mutation must occur after a real preliminary review'
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
    asyncio.run(scenario())


def test_real_rest_draft_list_matches_service_and_revokes_without_cached_metadata(registry_database, clock):
    async def scenario():
        discovery, publishing, manifest = await composition(registry_database, clock)
        draft = await publishing.save_draft(principal(), manifest, 0)
        services = SimpleNamespace(
            authenticator=SimpleNamespace(authenticate=AsyncMock(return_value=principal())),
            drafts=discovery, publishing=publishing,
        )
        app = create_app(services, settings=TransportSettings(allowed_hosts=('testserver',)))
        direct = await discovery.search(principal(), query='DAILY', limit=1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver') as client:
            response = await client.get('/v1/drafts', params={'q': 'DAILY', 'limit': 1})
            assert response.status_code == 200
            assert response.json() == direct.model_dump(mode='json')
            assert response.headers['cache-control'] == 'no-store'
            assert response.json()['items'][0]['id'] == draft.id
            set_roles(registry_database, ('consumer',))
            hidden = await client.get('/v1/drafts', params={'q': manifest.name})
            assert hidden.status_code == 200
            assert hidden.json() == {'items': [], 'next_cursor': None}
            assert hidden.headers['cache-control'] == 'no-store'
            assert (await client.get('/v1/drafts/' + draft.id)).status_code == 404
    asyncio.run(scenario())
