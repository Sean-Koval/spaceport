"""Bounded saved-head discovery under the existing author-or-publisher gate."""
from sqlalchemy import select

from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.policy.service import _scope
from gateway.publishing.service import review_capabilities
from gateway.registry.models import DraftRevision, ProductRow, ProductVersion
from gateway.registry.repository import not_found
from .catalog import MAX_SCAN, bounded_rows, page_arguments, scope_fingerprint
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

    async def _projection(self, principal, draft_id, uow):
        head, draft, target = await self.publishing._draft(uow, draft_id)
        # Ownership and consumer discovery rights are deliberately not consulted.
        decision, capabilities = await self.publishing._review(principal, target, uow)
        if (head.latest_draft_id != draft.id or head.draft_revision != draft.revision
            or await uow.session.scalar(select(ProductVersion.id).where(ProductVersion.draft_id == draft.id))):
            raise not_found()
        return DraftSummary(id=draft.id, product_id=target.manifest.product_id,
            version=target.manifest.version, name=target.manifest.name, revision=draft.revision,
            **review_capabilities(target.manifest, decision, capabilities)), head.current_version_id

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
        checked = []
        async with read_transaction(self.registry) as uow:
            # Follow publication's product -> policy lock order, including all heads
            # before the fingerprint locks any policy. Keep heads locked to commit.
            for item, _ in matches:
                await uow.lock_product(item.product_id)
            if await scope_fingerprint(self.policy, principal, uow) != initial:
                raise not_found()
            for previous, base_id in matches:
                item, current_base = await self._projection(principal, previous.id, uow)
                if item != previous or current_base != base_id:
                    raise not_found()
                if publish[item.id]:
                    head, _, target = await self.publishing._draft(uow, item.id)
                    targets = await self.publishing._publication_targets(uow, head, target)
                    await self.publishing._scopes(principal, targets, 'publish', uow)
                checked.append(item.model_copy(update={'can_publish': publish[item.id]}))
            # A false base hint remains valid only under the same base and full
            # grants/policy fingerprint as its independently audited probe.
            final = await scope_fingerprint(self.policy, principal, uow)
            if final != initial:
                raise not_found()
        items = checked[:limit]
        token = self.cursors.issue(purpose='drafts', identity=identity, scope=final,
            query=query_hash, last=items[-1].product_id) if len(checked) > limit else None
        return DraftPage(items=items, next_cursor=token)
