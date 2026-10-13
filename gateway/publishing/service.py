"""Authorized draft review, tested publication, and append-only lifecycle events."""
from typing import Annotated, Literal
import re

from pydantic import BeforeValidator, Field, JsonValue, ValidationError, field_serializer

from gateway.contracts.canonical import Array, Digest, Identifier, StrictModel, canonical_bytes, json_value
from gateway.contracts.errors import invalid_input
from gateway.contracts.manifest import Manifest
from gateway.policy.audit import reject_recognized_credential


class FieldChange(StrictModel):
    path: str
    kind: Literal['added', 'changed', 'removed']
    before: Annotated[JsonValue, BeforeValidator(json_value)] = None
    after: Annotated[JsonValue, BeforeValidator(json_value)] = None

    @field_serializer('before', 'after')
    def serialize_values(self, value):
        return json_value(value)


class VersionDiff(StrictModel):
    draft_id: Identifier
    product_id: Identifier
    revision: int = Field(ge=1)
    latest_test_run_id: Identifier | None
    before_digest: Digest | None
    after_digest: Digest
    changes: Array[FieldChange]
    stale_evidence: Array[Literal['missing', 'draft', 'binding', 'policy', 'fixtures', 'failed']] = ()


class DraftView(StrictModel):
    """Authorized review projection, deliberately not a round-trippable Manifest.

    Only a currently authorized author with all output fields visible may use
    the full-definition replacement operation. A transport must never invent
    fields omitted from this projection or serialize raw draft ORM records.
    """
    id: Identifier
    product_id: Identifier
    revision: int = Field(ge=1)
    digest: Digest
    definition: Annotated[dict[str, JsonValue], BeforeValidator(json_value)]
    editable_full_definition: bool
    current_draft_id: Identifier | None
    is_current_revision: bool
    can_test: bool = Field(description='Current author capability hint only; execution rechecks all authority and pins.')
    can_publish: bool = Field(description='Current base and target publisher capability hint only; not evidence readiness or publication authorization.')

    @field_serializer('definition')
    def serialize_definition(self, value):
        return json_value(value)


def definition_changes(before: dict, after: dict) -> tuple[FieldChange, ...]:
    """Ordered JSON pointers; mappings are unordered, arrays remain meaningful."""
    changes = []
    def visit(old, new, path):
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(set(old) | set(new)):
                child = path + '/' + key.replace('~', '~0').replace('/', '~1')
                if key not in old:
                    changes.append(FieldChange(path=child, kind='added', after=new[key]))
                elif key not in new:
                    changes.append(FieldChange(path=child, kind='removed', before=old[key]))
                else:
                    visit(old[key], new[key], child)
        elif canonical_bytes(old) != canonical_bytes(new):
            changes.append(FieldChange(path=path, kind='changed', before=old, after=new))
    visit(before, after, '')
    return tuple(changes)


def review_projection(manifest: Manifest, visible_fields: tuple[str, ...]) -> dict:
    """Apply each side's current output visibility before computing a diff.

    Author/publisher permission separately gates all document metadata. There are
    no source URLs, SQL, credentials, or inferred upstream-row effects here.
    """
    doc = manifest.model_dump(mode='json', exclude={'content_digest'})
    visible = set(visible_fields)
    for name in ('outputs', 'filters', 'units'):
        doc[name] = {key:value for key,value in doc[name].items() if key in visible}
    doc['sort']['fields'] = [key for key in doc['sort']['fields'] if key in visible]
    for fixture in doc['fixtures']:
        fixture['expected_rows'] = [{key:value for key,value in row.items() if key in visible}
                                    for row in fixture['expected_rows']]
    return doc


def validate_reason(reason: str) -> str:
    try:
        if type(reason) is not str or not 1 <= len(reason) <= 1024 or not reason.strip():
            raise ValueError
        if any(ord(character) < 32 or ord(character) == 127 for character in reason):
            raise ValueError
        if re.search(r"(?i)\b(?:bearer|password|api[_ -]?key|token)\s*[:= ]\s*\S+", reason):
            raise ValueError
        reject_recognized_credential(reason)
        return reason
    except (ValueError, TypeError):
        raise invalid_input('A bounded, credential-free transition reason is required.') from None

from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from gateway.policy.service import derive_decision, policy_unavailable
from sqlalchemy.dialects.postgresql import insert

from gateway.adapters.base import check_binding
from gateway.contracts.errors import ErrorCode, GatewayError
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.identity import Principal
from gateway.contracts.manifest import validate_manifest
from gateway.registry.models import (BindingRevision, DraftRevision, ProductRow,
    ProductVersion, TestRun, VersionState)
from gateway.registry.repository import conflict, not_found


def review_target(manifest, identity, binding_digest=None):
    """Metadata-only authorization target; never a consumer execution identity.

    Authorization needs the exact manifest policy, not a working source. The
    inert fallback digest cannot reach execution: source pins are checked later.
    """
    return PinnedProduct(manifest=manifest, binding_digest=binding_digest or manifest.content_digest,
                         version_id=identity)


class PublishingService:
    def __init__(self, *, registry, policy, bindings, bundles, fixture_runner=None):
        self.registry, self.policy, self.bindings, self.bundles = registry, policy, bindings, bundles
        self.fixture_runner = fixture_runner

    async def _binding(self, manifest, uow):
        row = await uow.session.scalar(select(BindingRevision).where(
            BindingRevision.binding_id == manifest.binding_id, BindingRevision.revision == manifest.binding_revision))
        if row is None:
            raise GatewayError(ErrorCode.BINDING_DRIFT, str(uuid4()), 'The reviewed binding is unavailable.')
        binding = await self.bindings.resolve(manifest.binding_id, manifest.binding_revision)
        check_binding(binding, manifest, digest=row.content_digest)
        if (binding.context != 'consumer' or row.model_bundle_digest != manifest.model_bundle_digest
            or row.adapter_revision != manifest.adapter_revision):
            raise GatewayError(ErrorCode.BINDING_DRIFT, str(uuid4()), 'The reviewed binding is unavailable.')
        return row, binding

    async def _scopes(self, actor, targets, action, uow):
        # Sorted policy heads avoid old/new policy lock inversions across products.
        decisions = {}
        for target in sorted(targets, key=lambda item: (item.manifest.policy_id, item.version_id)):
            decisions[target.version_id] = await self.policy.authorize(actor, action, target, uow)
        return decisions

    async def _review(self, actor, target, uow):
        """One audited author-or-publisher gate using one exact current scope.

        Only pure decision derivation may try another action. A denied audited
        gate aborts its transaction and is never continued for a fallback.
        """
        action, revision, request_id = 'author', None, str(uuid4())
        try:
            actor = Principal.model_validate(actor.model_dump(mode='python'))
            await self.policy._identity(actor, uow)
            grants = await self.policy.principals.get_current(actor.id, session=uow.session)
            current, content = await self.policy._current(target.manifest.policy_id, uow)
            revision = current.revision
            try:
                decision = derive_decision(grants, 'publish', target, content.rules, revision)
                action = 'publish'
            except GatewayError as error:
                if error.code != ErrorCode.NOT_FOUND_OR_FORBIDDEN:
                    raise
                decision = derive_decision(grants, 'author', target, content.rules, revision)
            capabilities = next(grant.capabilities for grant in grants.grants
                if grant.policy_id == target.manifest.policy_id and grant.tenant_id == decision.tenant_id)
            await self.policy.audit.append(uow, self.policy.audit.event(actor, action,
                product=target, policy_revision=revision, decision='allow', correlation_id=request_id))
            return decision, capabilities
        except GatewayError as error:
            if error.code in {ErrorCode.NOT_FOUND_OR_FORBIDDEN, ErrorCode.UPSTREAM_UNAVAILABLE}:
                await self.policy._deny(uow, actor, action, product=target,
                    policy_revision=revision, correlation_id=request_id)
            else:
                await uow.session.rollback()
            message = 'The requested product is unavailable.' if error.code == ErrorCode.NOT_FOUND_OR_FORBIDDEN else error.safe_message
            raise GatewayError(error.code, request_id, message) from None
        except SQLAlchemyError:
            await self.policy._deny(uow, actor, action, product=target,
                policy_revision=revision, correlation_id=request_id)
            raise GatewayError(ErrorCode.UPSTREAM_UNAVAILABLE, request_id, 'Current access policy is unavailable.') from None
        except (ValidationError, ValueError, TypeError, AttributeError):
            await uow.session.rollback()
            raise policy_unavailable() from None

    async def save_draft(self, actor: Principal, manifest: Manifest, expected_revision: int) -> DraftRevision:
        return await self._save_draft(actor, manifest, expected_revision)

    async def update_draft(self, actor: Principal, draft_id: str, manifest: Manifest,
                           expected_revision: int) -> DraftRevision:
        """Replace only this URL-bound current draft in the publication lock domain."""
        return await self._save_draft(actor, manifest, expected_revision, draft_id=draft_id)

    async def _save_draft(self, actor, manifest, expected_revision, *, draft_id=None):
        actor = Principal.model_validate(actor.model_dump(mode='python'))
        manifest = validate_manifest(manifest.model_dump(mode='json'))
        if type(expected_revision) is not int or expected_revision < 0:
            raise invalid_input('A nonnegative expected revision is required.')
        new_id = str(uuid4())
        async with self.registry.transaction() as uow:
            if draft_id is not None:
                url_product = await uow.session.scalar(select(DraftRevision.product_id).where(DraftRevision.id == draft_id))
                if url_product != manifest.product_id:
                    raise not_found()
            else:
                await uow.session.execute(insert(ProductRow).values(id=manifest.product_id).on_conflict_do_nothing())
            head = await uow.lock_product(manifest.product_id)
            old = await uow.session.get(DraftRevision, head.latest_draft_id) if head.latest_draft_id else None
            # A product created through an internal published-version seam still
            # cannot use create/save to bypass hidden-definition replacement.
            previous = old or (await uow.session.get(ProductVersion, head.current_version_id)
                               if head.current_version_id else None)
            targets = [review_target(manifest, new_id)]
            if previous is not None:
                targets.append(review_target(validate_manifest(previous.manifest), previous.id))
            decisions = await self._scopes(actor, targets, 'author', uow)
            if previous is not None:
                for target in targets:
                    if set(target.manifest.outputs) - set(decisions[target.version_id].visible_fields):
                        await self.policy._deny(uow, actor, 'author', product=target,
                            policy_revision=decisions[target.version_id].policy_revision, correlation_id=str(uuid4()))
                        raise not_found()
                if targets[-1].manifest.policy_id != manifest.policy_id:
                    await self._scopes(actor, targets, 'policy_admin', uow)
            if (head.draft_revision != expected_revision
                or draft_id is not None and (old is None or old.id != draft_id or old.revision != expected_revision)):
                raise conflict()
            await self._binding(manifest, uow)
            row = DraftRevision(id=new_id,product_id=manifest.product_id,revision=expected_revision+1,
                manifest=manifest.model_dump(mode='json'),content_digest=manifest.content_digest,actor_id=actor.id)
            uow.session.add(row)
            await uow.session.flush()
            head.draft_revision, head.latest_draft_id = row.revision, row.id
            await uow.session.flush()
            return row

    async def get_draft(self, actor: Principal, draft_id: str) -> DraftView:
        async with self.registry.transaction() as uow:
            head, draft, target = await self._draft(uow, draft_id)
            decision, capabilities = await self._review(actor, target, uow)
            editable = (not set(target.manifest.outputs) - set(decision.visible_fields)
                and 'author' in capabilities)
            is_current = head.latest_draft_id == draft.id and head.draft_revision == draft.revision
            has_base = head.current_version_id is not None
            view = dict(id=draft.id, product_id=target.manifest.product_id, revision=draft.revision,
                digest=draft.content_digest, definition=review_projection(target.manifest,decision.visible_fields),
                editable_full_definition=editable, is_current_revision=is_current,
                can_test='author' in capabilities, can_publish='publish' in capabilities)
        # Optional lookups use their own UoWs. Denying a new head or a required
        # base scope must never resume an authorization-aborted review UoW.
        current_id = draft_id if is_current else await self._current_draft_id(actor, draft_id)
        if view['can_publish'] and has_base:
            view['can_publish'] = await self._publish_capability(actor, draft_id)
        return DraftView(**view, current_draft_id=current_id)

    async def _current_draft_id(self, actor, draft_id):
        try:
            async with self.registry.transaction() as uow:
                head, _, original = await self._draft(uow, draft_id)
                if head.latest_draft_id is None:
                    return None
                _, latest, target = await self._draft(uow, head.latest_draft_id)
                if target.manifest.product_id != original.manifest.product_id:
                    raise not_found()
                await self._review(actor, target, uow)
                return latest.id
        except GatewayError as error:
            if error.code != ErrorCode.NOT_FOUND_OR_FORBIDDEN:
                raise
            return None

    async def _publish_capability(self, actor, draft_id):
        """Audited capability probe only; publish retains its independent gate."""
        try:
            async with self.registry.transaction() as uow:
                head, _, target = await self._draft(uow, draft_id)
                targets = [target]
                if head.current_version_id:
                    old = await uow.session.get(ProductVersion, head.current_version_id)
                    if old is None or old.manifest['product_id'] != target.manifest.product_id:
                        raise not_found()
                    targets.append(review_target(validate_manifest(old.manifest),old.id,old.binding_digest))
                await self._scopes(actor, targets, 'publish', uow)
                return True
        except GatewayError as error:
            if error.code != ErrorCode.NOT_FOUND_OR_FORBIDDEN:
                raise
            return False

    async def _draft(self, uow, draft_id):
        product_id = await uow.session.scalar(select(DraftRevision.product_id).where(DraftRevision.id == draft_id))
        if product_id is None:
            raise not_found()
        head = await uow.lock_product(product_id)
        draft = await uow.lock_draft(draft_id)
        manifest = validate_manifest(draft.manifest)
        return head, draft, review_target(manifest, draft.id)

    async def _latest_test(self, uow, draft):
        return await uow.session.scalar(select(TestRun).where(TestRun.draft_id == draft.id)
            .order_by(TestRun.created_at.desc(), TestRun.id.desc()).limit(1))

    async def _stale(self, uow, head, draft, target, decision, test):
        causes = []
        if head.latest_draft_id != draft.id or head.draft_revision != draft.revision:
            causes.append('draft')
        if test is None:
            return tuple(causes + ['missing'])
        if test.draft_digest != draft.content_digest or test.draft_id != draft.id:
            causes.append('draft')
        try:
            binding_row, binding = await self._binding(target.manifest, uow)
            if test.binding_id != binding_row.id or test.binding_digest != binding_row.content_digest:
                causes.append('binding')
        except GatewayError as error:
            if error.code != ErrorCode.BINDING_DRIFT:
                raise
            causes.append('binding')
            binding = None
        if test.policy_id != target.manifest.policy_id or test.policy_revision != decision.policy_revision:
            causes.append('policy')
        if self.fixture_runner is None or binding is None:
            causes.append('fixtures')
        elif not self.fixture_runner.evidence_current(target.manifest, binding, test):
            causes.append('fixtures')
        else:
            causes.extend(await self.fixture_runner.evidence_stale_causes(target.manifest, binding, test, uow))
        if test.status != 'passed':
            causes.append('failed')
        return tuple(dict.fromkeys(causes))

    async def diff(self, actor: Principal, draft_id: str) -> VersionDiff:
        async with self.registry.transaction() as uow:
            head, draft, target = await self._draft(uow, draft_id)
            old = await uow.session.get(ProductVersion, head.current_version_id) if head.current_version_id else None
            targets = [target]
            if old:
                targets.append(review_target(validate_manifest(old.manifest), old.id, old.binding_digest))
            # Review is accessible independently to authors and publishers, with
            # every target separately authorized using its own current scope.
            decisions = {}
            for item in sorted(targets, key=lambda item: (item.manifest.policy_id,item.version_id)):
                decisions[item.version_id], _ = await self._review(actor, item, uow)
            after = review_projection(target.manifest, decisions[target.version_id].visible_fields)
            before = review_projection(targets[-1].manifest, decisions[old.id].visible_fields) if old else {}
            test = await self._latest_test(uow, draft)
            # Use the same selected immutable run as stale-evidence assessment,
            # but disclose an ID only if explicit get_test would accept its pins.
            run_id = test.id if test is not None and (test.draft_id == draft.id
                and test.draft_digest == draft.content_digest and test.product_id == target.manifest.product_id
                and test.policy_id == target.manifest.policy_id) else None
            return VersionDiff(draft_id=draft.id, product_id=target.manifest.product_id, revision=draft.revision,
                latest_test_run_id=run_id, before_digest=old.content_digest if old else None, after_digest=draft.content_digest,
                changes=definition_changes(before, after), stale_evidence=await self._stale(
                    uow, head, draft, target, decisions[target.version_id], test))

    async def test(self, actor: Principal, draft_id: str) -> TestRun:
        # Author permission is always checked before exposing configuration gaps.
        async with self.registry.transaction() as uow:
            head, draft, target = await self._draft(uow,draft_id)
            await self.policy.authorize(actor,'author',target,uow)
            if head.latest_draft_id != draft.id:
                raise conflict()
            await self._binding(target.manifest,uow)
        if self.fixture_runner is None:
            raise GatewayError(ErrorCode.BINDING_DRIFT,str(uuid4()),'Approved fixture execution is unavailable.')
        completed = await self.fixture_runner.run(actor,draft_id)
        return await self.get_test(actor,draft_id,completed.id)

    async def get_test(self, actor: Principal, draft_id: str, run_id: str) -> TestRun:
        """Current-authorized explicit-ID evidence review for the existing route.

        The returned projection retains the stored run identity and exact tested
        pins. Its filtered payload is not the full immutable evidence payload.
        No history enumeration, mutation, or execution occurs here.
        """
        from .test_runner import filtered_test_run
        async with self.registry.transaction() as uow:
            _, draft, target = await self._draft(uow,draft_id)
            decision, _ = await self._review(actor, target, uow)
            stored = await uow.session.scalar(select(TestRun).where(
                TestRun.id == run_id, TestRun.draft_id == draft.id,
                TestRun.draft_digest == draft.content_digest, TestRun.product_id == target.manifest.product_id,
                TestRun.policy_id == target.manifest.policy_id))
            if stored is None:
                raise not_found()
            return filtered_test_run(stored,decision.visible_fields,target.manifest)

    async def publish(self, actor: Principal, draft_id: str, tested_digest: str,
                      expected_revision: int, reason: str) -> PinnedProduct:
        from .models import PublicationEvent
        async with self.registry.transaction() as uow:
            head, draft, target = await self._draft(uow,draft_id)
            targets = [target]
            if head.current_version_id:
                current = await uow.session.get(ProductVersion,head.current_version_id)
                targets.append(review_target(validate_manifest(current.manifest),current.id,current.binding_digest))
            decisions = await self._scopes(actor,targets,'publish',uow)
            reason = validate_reason(reason)
            evidence = await self._latest_test(uow,draft)
            stale = await self._stale(uow,head,draft,target,decisions[target.version_id],evidence)
            if (type(expected_revision) is not int or draft.revision != expected_revision
                or tested_digest != draft.content_digest or stale):
                raise conflict()
            endpoint = self.bundles.resolve(target.manifest.model_bundle_digest)
            if endpoint.context != 'consumer':
                raise GatewayError(ErrorCode.BINDING_DRIFT,str(uuid4()),'The retained model bundle is unavailable.')
            row = await uow.insert_version(draft_id=draft.id,expected_revision=expected_revision,
                test_run_id=evidence.id,actor_id=actor.id)
            state = await uow.session.get(VersionState,row.id)
            state.reason = reason
            uow.session.add(PublicationEvent(product_id=row.product_id,version_id=row.id,action='publish',
                actor_id=actor.id,reason=reason,request_id=str(uuid4())))
            await uow.session.flush()
            return PinnedProduct(manifest=target.manifest,binding_digest=row.binding_digest,version_id=row.id)

    async def transition(self, actor: Principal, product_id: str, version: str,
                         action: Literal['deprecate','withdraw','rollback'], reason: str) -> None:
        from .models import PublicationEvent
        async with self.registry.transaction() as uow:
            head = await uow.lock_product(product_id)
            row = await uow.session.scalar(select(ProductVersion).where(
                ProductVersion.product_id == product_id, ProductVersion.version == version))
            if row is None:
                raise not_found()
            target = review_target(validate_manifest(row.manifest),row.id,row.binding_digest)
            targets = [target]
            if action == 'rollback' and head.current_version_id and head.current_version_id != row.id:
                current = await uow.session.get(ProductVersion,head.current_version_id)
                targets.append(review_target(validate_manifest(current.manifest),current.id,current.binding_digest))
            await self._scopes(actor,targets,'publish',uow)
            if action not in {'deprecate','withdraw','rollback'}:
                raise invalid_input('The lifecycle transition is invalid.')
            reason = validate_reason(reason)
            state = await uow.session.scalar(select(VersionState).where(VersionState.version_id == row.id).with_for_update())
            if state is None or state.status == 'withdrawn':
                raise not_found()
            if action == 'rollback':
                await self._binding(target.manifest,uow)
                if self.bundles.resolve(target.manifest.model_bundle_digest).context != 'consumer':
                    raise GatewayError(ErrorCode.BINDING_DRIFT,str(uuid4()),'The retained model bundle is unavailable.')
                head.current_version_id = row.id
            else:
                state.status = 'deprecated' if action == 'deprecate' else 'withdrawn'
            state.actor_id, state.reason = actor.id, reason
            from sqlalchemy import func
            state.updated_at = func.clock_timestamp()
            uow.session.add(PublicationEvent(product_id=product_id,version_id=row.id,action=action,
                actor_id=actor.id,reason=reason,request_id=str(uuid4())))
            await uow.session.flush()
