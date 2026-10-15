"""Bounded saved-head discovery under the existing author-or-publisher gate."""
from sqlalchemy import select

from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.execution.queue import lock_execution_capacity
from gateway.policy.service import _scope
from gateway.publishing.service import review_capabilities
from gateway.registry.models import DraftRevision, ProductRow, ProductVersion
from gateway.registry.repository import not_found
from .catalog import MAX_SCAN, bounded_rows, page_arguments, scope_fingerprint, scope_policy_ids
from .models import DraftPage, DraftSummary
from .transactions import read_transaction


class DraftDiscoveryService:
    def __init__(self, *, registry, policy, publishing, cursors):
        self.registry, self.policy, self.publishing, self.cursors = registry, policy, publishing, cursors

    async def _eligible_policies(self, principal, uow):
        """Only a coarse work bound, never the row's authorization decision.

        Filter scopes before scanning so unrelated/consumer-only principals do
        not learn the global draft population from a resource-limit response.
        """
        grants = await self.policy.principals.get_current(principal.id, session=uow.session)
        eligible = []
        for policy_id in sorted({grant.policy_id for grant in grants.grants
                                 if {'author', 'publish'} & set(grant.capabilities)}):
            _, content = await self.policy._current(policy_id, uow)
            for action in ('author', 'publish'):
                try:
                    _scope(grants, policy_id, action, content.rules)
                    eligible.append(policy_id)
                    break
                except GatewayError as error:
                    if error.code != ErrorCode.NOT_FOUND_OR_FORBIDDEN:
                        raise
        return eligible

    async def _prepare_projection(self, principal, draft_id, uow, *, loaded=None):
        head, draft, target = loaded or await self.publishing._draft(uow, draft_id)
        # Ownership and consumer discovery rights are deliberately not consulted.
        decision, capabilities, requests = await self.publishing._prepare_review(principal, target, uow)
        if (head.latest_draft_id != draft.id or head.draft_revision != draft.revision
            or await uow.session.scalar(select(ProductVersion.id).where(ProductVersion.draft_id == draft.id))):
            raise not_found()
        return DraftSummary(id=draft.id, product_id=target.manifest.product_id,
            version=target.manifest.version, name=target.manifest.name, revision=draft.revision,
            **review_capabilities(target.manifest, decision, capabilities)), head.current_version_id, requests

    async def _projection(self, principal, draft_id, uow):
        loaded = await self.publishing._draft(uow, draft_id)
        await self.publishing._prelock_reviews((loaded[2],), uow, draft_ids=(draft_id,))
        item, base_id, requests = await self._prepare_projection(principal, draft_id, uow, loaded=loaded)
        await self.publishing._finalize_reviews(requests, uow)
        return item, base_id

    async def _final_page(self, principal, matches, publish, initial, uow):
        """Load the whole page's prefix, then perform exactly one final gate."""
        await lock_execution_capacity(uow)
        for product_id in sorted({item.product_id for item, _ in matches}):
            await uow.lock_product(product_id)
        for draft_id in sorted({item.id for item, _ in matches}):
            await uow.lock_draft(draft_id)
        loaded, probes, targets, version_ids = {}, {}, [], []
        for item, _ in matches:
            current = await self.publishing._draft(uow, item.id)
            loaded[item.id] = current
            targets.append(current[2])
            if publish[item.id]:
                probes[item.id] = await self.publishing._publication_targets(uow, current[0], current[2])
                for target in probes[item.id][1:]:
                    targets.append(target)
                    version_ids.append(target.version_id)
        policy_ids = await scope_policy_ids(self.policy, principal, uow)
        await self.publishing._prelock_reviews(targets, uow,
            draft_ids=tuple(loaded), version_ids=tuple(version_ids), policy_ids=policy_ids)
        if await scope_fingerprint(self.policy, principal, uow, policy_ids=policy_ids) != initial:
            raise not_found()
        checked, requests = [], []
        for previous, base_id in matches:
            item, current_base, pending = await self._prepare_projection(principal, previous.id, uow,
                loaded=loaded[previous.id])
            if item != previous or current_base != base_id:
                raise not_found()
            requests.extend(pending)
            if publish[item.id]:
                # This target's complete publish request is already in pending
                # review proof. Only independently required base scopes remain;
                # repeating the target would exceed the bounded final batch.
                _, pending = await self.publishing._prepare_scopes(principal, probes[item.id][1:], 'publish', uow)
                requests.extend(pending)
            checked.append(item.model_copy(update={'can_publish': publish[item.id]}))
        final = await scope_fingerprint(self.policy, principal, uow, policy_ids=policy_ids)
        if final != initial:
            raise not_found()
        await self.publishing._finalize_reviews(requests, uow)
        return checked, final

    async def search(self, principal, query='', cursor=None, limit=50):
        page_arguments(query, cursor, limit)
        normalized = query.casefold()
        identity = canonical_digest(principal)
        query_hash = canonical_digest({'query': normalized, 'limit': limit})
        async with read_transaction(self.registry) as uow:
            initial = await scope_fingerprint(self.policy, principal, uow)
            policies = await self._eligible_policies(principal, uow)
            rows = bounded_rows((await uow.session.execute(select(DraftRevision.product_id, DraftRevision.id)
                .join(ProductRow, ProductRow.latest_draft_id == DraftRevision.id)
                .where(DraftRevision.manifest['policy_id'].astext.in_(policies),
                    ~select(ProductVersion.id).where(ProductVersion.draft_id == DraftRevision.id).exists())
                .order_by(DraftRevision.product_id.collate('C'), DraftRevision.id.collate('C'))
                .limit(MAX_SCAN + 1))).all()) if policies else []
        last = ''
        if cursor:
            last = self.cursors.read(cursor, purpose='drafts', identity=identity,
                scope=initial, query=query_hash).last
        matches = []
        for product_id, draft_id in rows:
            if product_id <= last:
                continue
            try:
                # A denial aborts only its own transaction, never a later row's.
                async with read_transaction(self.registry) as uow:
                    item, base_id = await self._projection(principal, draft_id, uow)
                if item.product_id != product_id:
                    raise not_found()
                if normalized in ' '.join((item.product_id, item.name, item.version)).casefold():
                    matches.append((item, base_id))
                if len(matches) > limit:
                    break
            except GatewayError as error:
                if error.code != ErrorCode.NOT_FOUND_OR_FORBIDDEN:
                    raise
        publish = {}
        for item, _ in matches:
            # Base denial has its own audited UoW; never recover inside a denied UoW.
            publish[item.id] = item.can_publish and await self.publishing._publish_capability(principal, item.id)
        async with read_transaction(self.registry) as uow:
            checked, final = await self._final_page(principal, matches, publish, initial, uow)
        items = checked[:limit]
        token = self.cursors.issue(purpose='drafts', identity=identity, scope=final,
            query=query_hash, last=items[-1].product_id) if len(checked) > limit else None
        return DraftPage(items=items, next_cursor=token)
