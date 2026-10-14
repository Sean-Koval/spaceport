"""Inbox-to-review security regressions requiring actual disposable PostgreSQL.

These do not claim a browser journey or real Cube fixture execution. In particular,
capability hints are checked without inventing a successful publication receipt.
"""
import asyncio
import importlib

import pytest
from psycopg.types.json import Jsonb

from gateway.api.cursors import PageCursorCodec
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.identity import Principal
from gateway.contracts.manifest import validate_manifest
from gateway.execution.cursors import cursor_digest
from tests.integration.conftest import registry_database
from tests.integration.test_audit import principal
from tests.unit.test_policy import rules

pytestmark = pytest.mark.postgres


async def inbox_setup(database, clock):
    from tests.integration.test_publishing import setup
    publishing, manifest = await setup(database, roles=('author', 'publisher', 'policy_admin'))
    drafts = []
    for product_id in ('a-inbox', 'b-inbox'):
        document = manifest.model_dump(mode='json', exclude={'content_digest'})
        document.update(product_id=product_id, name=f'Saved {product_id}')
        drafts.append(await publishing.save_draft(principal(), validate_manifest(document), 0))
    discovery = importlib.import_module('gateway.api.drafts').DraftDiscoveryService(
        registry=database.registry, policy=publishing.policy, publishing=publishing,
        cursors=PageCursorCodec(b'c' * 32, clock),
    )
    return publishing, discovery, drafts


async def denied(operation):
    with pytest.raises(GatewayError) as caught:
        await operation
    assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'


def test_inbox_capabilities_do_not_certify_evidence_or_grant_edit_authority(registry_database, clock):
    async def scenario():
        publishing, inbox, drafts = await inbox_setup(registry_database, clock)
        registry_database.execute_owner(
            "UPDATE gateway.principal_grants SET roles=%s WHERE principal_id='north-user'",
            (Jsonb(['publisher']),),
        )
        page = await inbox.search(principal())
        assert len(page.items) == 2
        item = page.items[0]
        assert item.id == drafts[0].id and item.can_publish
        assert not item.can_test and not item.editable_full_definition
        assert set(item.model_dump()) == {
            'id', 'product_id', 'version', 'name', 'revision',
            'editable_full_definition', 'can_test', 'can_publish',
        }
        view = await publishing.get_draft(principal(), item.id)
        assert not view.editable_full_definition and not view.can_test
        assert view.can_publish
        await denied(publishing.update_draft(
            principal(), item.id, validate_manifest(drafts[0].manifest), item.revision,
        ))
        await denied(publishing.test(principal(), item.id))
        with pytest.raises(GatewayError) as caught:
            await publishing.publish(
                principal(), item.id, drafts[0].content_digest, item.revision,
                'Synthetic inbox capability is not test evidence',
            )
        assert caught.value.code == 'CONFLICT'
        assert registry_database.scalar('SELECT count(*) FROM gateway.product_versions') == 0
        assert registry_database.scalar('SELECT count(*) FROM gateway.test_runs') == 0
        assert registry_database.scalar('SELECT count(*) FROM gateway.draft_revisions') == 2
    asyncio.run(scenario())


def test_draft_owner_loses_inbox_and_direct_review_when_reduced_to_consumer(registry_database, clock):
    async def scenario():
        publishing, inbox, drafts = await inbox_setup(registry_database, clock)
        page = await inbox.search(principal(), limit=1)
        assert page.next_cursor and page.items[0].id == drafts[0].id
        registry_database.execute_owner(
            "UPDATE gateway.principal_grants SET roles=%s WHERE principal_id='north-user'",
            (Jsonb(['consumer']),),
        )
        for cursor in (None, page.next_cursor):
            try:
                remaining = await inbox.search(principal(), limit=1, cursor=cursor)
            except GatewayError as error:
                assert error.code == 'NOT_FOUND_OR_FORBIDDEN'
            else:
                assert remaining.model_dump(mode='json') == {'items': [], 'next_cursor': None}
        for draft in drafts:
            await denied(publishing.get_draft(principal(), draft.id))
        assert registry_database.scalar(
            "SELECT count(*) FROM gateway.draft_revisions WHERE actor_id='north-user'",
        ) == 2
    asyncio.run(scenario())


def test_inbox_cursor_cannot_be_replayed_by_an_equally_authorized_principal(registry_database, clock):
    async def scenario():
        _, inbox, _ = await inbox_setup(registry_database, clock)
        page = await inbox.search(principal(), limit=1)
        assert page.next_cursor
        registry_database.execute_owner(
            "INSERT INTO gateway.principals(id,subject,kind) VALUES ('reviewer-two','synthetic-reviewer-two','demo')",
        )
        registry_database.execute_owner(
            "INSERT INTO gateway.principal_grants(principal_id,policy_id,tenant_id,roles,entitlements) "
            "VALUES ('reviewer-two','revenue-access','north',%s,%s)",
            (Jsonb(['author', 'publisher', 'policy_admin']), Jsonb({'groups': ['north']})),
        )
        other = Principal(id='reviewer-two', subject='synthetic-reviewer-two', kind='demo', service_client_id=None)
        assert len((await inbox.search(other)).items) == 2
        await denied(inbox.search(other, limit=1, cursor=page.next_cursor))
    asyncio.run(scenario())


@pytest.mark.parametrize('foreign_purpose', ['catalog', 'choices-bindings', 'choices-policies', 'audit'])
def test_other_discovery_cursor_purpose_cannot_enter_inbox(registry_database, clock, foreign_purpose):
    async def scenario():
        _, inbox, _ = await inbox_setup(registry_database, clock)
        page = await inbox.search(principal(), limit=1)
        assert page.next_cursor
        # Preserve every other authenticated claim so only purpose distinguishes
        # this token. This uses the service's real codec, not a fabricated string.
        from gateway.api.catalog import scope_fingerprint
        from gateway.api.transactions import read_transaction
        async with read_transaction(inbox.registry) as uow:
            scope = await scope_fingerprint(inbox.policy, principal(), uow)
        token = inbox.cursors.issue(
            purpose=foreign_purpose, identity=canonical_digest(principal()), scope=scope,
            query=canonical_digest({'query': '', 'limit': 1}), last=page.items[0].product_id,
        )
        await denied(inbox.search(principal(), limit=1, cursor=token))
    asyncio.run(scenario())


def test_inbox_cursor_cannot_be_used_as_a_consumer_result_cursor(registry_database, clock):
    async def scenario():
        _, inbox, _ = await inbox_setup(registry_database, clock)
        page = await inbox.search(principal(), limit=1)
        assert page.next_cursor
        with pytest.raises(GatewayError) as caught:
            cursor_digest(page.next_cursor)
        assert caught.value.code == 'NOT_FOUND_OR_FORBIDDEN'
    asyncio.run(scenario())


def test_inbox_link_retains_revision_identity_after_another_author_edit(registry_database, clock):
    async def scenario():
        publishing, inbox, drafts = await inbox_setup(registry_database, clock)
        item = (await inbox.search(principal())).items[0]
        original = drafts[0]
        document = validate_manifest(original.manifest).model_dump(mode='json', exclude={'content_digest'})
        document.update(version='1.1.0', name='Revised saved definition')
        changed = await publishing.update_draft(principal(), item.id, validate_manifest(document), item.revision)
        old_view = await publishing.get_draft(principal(), item.id)
        assert old_view.id == original.id and old_view.revision == 1
        assert not old_view.is_current_revision and old_view.current_draft_id == changed.id
        latest = (await inbox.search(principal())).items[0]
        assert latest.id == changed.id and latest.revision == 2 and latest.version == '1.1.0'
        assert latest.name == 'Revised saved definition'
        with pytest.raises(GatewayError) as caught:
            await publishing.update_draft(principal(), item.id, validate_manifest(document), item.revision)
        assert caught.value.code == 'CONFLICT'
    asyncio.run(scenario())


def test_policy_visibility_change_invalidates_cursor_and_changes_link_capabilities(registry_database, clock):
    async def scenario():
        publishing, inbox, _ = await inbox_setup(registry_database, clock)
        page = await inbox.search(principal(), limit=1)
        assert page.next_cursor and page.items[0].editable_full_definition
        await publishing.policy.change_policy(
            principal(), 'revenue-access', rules(visible_fields=['day', 'segment']),
            'Synthetic crossflow visibility reduction',
        )
        await denied(inbox.search(principal(), limit=1, cursor=page.next_cursor))
        latest = await inbox.search(principal())
        assert all(not item.editable_full_definition for item in latest.items)
        for item in latest.items:
            view = await publishing.get_draft(principal(), item.id)
            assert not view.editable_full_definition
            assert 'revenue' not in view.definition['outputs']
    asyncio.run(scenario())
