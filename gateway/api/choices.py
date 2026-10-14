"""Attachable approved choices, never an unscoped connector inventory."""
from sqlalchemy import select
from pydantic import TypeAdapter
from gateway.contracts.canonical import canonical_digest,Identifier
from gateway.contracts.errors import ErrorCode,GatewayError,invalid_input
from gateway.contracts.manifest import validate_manifest
from gateway.adapters.base import check_binding
from gateway.registry.models import DraftRevision,ProductRow,ProductVersion,VersionState,BindingRevision
from gateway.registry.repository import not_found
from .catalog import MAX_SCAN,page_arguments,bounded_rows,scope_fingerprint
from .transactions import read_transaction
from .models import BindingChoice,BindingChoicePage,PolicyChoice,PolicyChoicePage


class ChoiceService:
    def __init__(self,*,registry,policy,bindings,bundles,cursors,templates=()):
        self.registry,self.policy,self.binding_resolver,self.bundles,self.cursors=registry,policy,bindings,bundles,cursors
        self.templates=tuple(validate_manifest(item.model_dump(mode='json')) for item in templates)
        bounded_rows(self.templates)

    async def _context(self,principal,draft_id,uow):
        if draft_id is None:return None
        draft=await uow.session.get(DraftRevision,draft_id)
        if draft is None:raise not_found()
        manifest=validate_manifest(draft.manifest)
        await self.policy.authorize_scope(principal,manifest.policy_id,'author',uow)
        return manifest

    async def _authorize(self,principal,policy_id,context,uow):
        scopes=sorted({policy_id}|({context.policy_id} if context is not None else set()))
        authorization=None
        for scope in scopes:
            value=await self.policy.authorize_scope(principal,scope,'author',uow)
            if scope==policy_id:authorization=value
        if context is not None and context.policy_id!=policy_id:
            for scope in scopes:await self.policy.authorize_scope(principal,scope,'policy_admin',uow)
        return authorization

    async def _witnesses(self,uow):
        drafts=(await uow.session.execute(select(DraftRevision.manifest).join(ProductRow,
            ProductRow.latest_draft_id==DraftRevision.id).order_by(DraftRevision.id).limit(MAX_SCAN+1))).scalars().all()
        versions=(await uow.session.execute(select(ProductVersion.manifest).join(ProductRow,
            ProductRow.current_version_id==ProductVersion.id).join(VersionState,VersionState.version_id==ProductVersion.id)
            .where(VersionState.status.in_(['published','deprecated'])).order_by(ProductVersion.id).limit(MAX_SCAN+1))).scalars().all()
        return [*self.templates,*(validate_manifest(raw) for raw in bounded_rows([*drafts,*versions]))]

    async def _binding(self,manifest,uow):
        row=await uow.session.scalar(select(BindingRevision).where(BindingRevision.binding_id==manifest.binding_id,
            BindingRevision.revision==manifest.binding_revision))
        if row is None:raise not_found()
        binding=await self.binding_resolver.resolve(manifest.binding_id,manifest.binding_revision)
        check_binding(binding,manifest,digest=row.content_digest)
        endpoint=self.bundles.resolve(manifest.model_bundle_digest)
        if (binding.context!='consumer' or endpoint.context!='consumer' or row.adapter_revision!=manifest.adapter_revision
            or row.model_bundle_digest!=manifest.model_bundle_digest):raise not_found()
        return BindingChoice(binding_id=binding.binding_id,revision=binding.revision,digest=binding.digest,
            adapter_revision=binding.adapter_revision,model_bundle_digest=binding.model_bundle_digest)

    async def _page(self,principal,*,kind,draft_id,cursor,limit):
        page_arguments(cursor=cursor,limit=limit)
        if draft_id is not None:
            try:TypeAdapter(Identifier).validate_python(draft_id,strict=True)
            except ValueError:raise invalid_input() from None
        identity=canonical_digest(principal)
        query_hash=canonical_digest({'draft_id':draft_id,'limit':limit})
        purpose='choices-'+kind
        async with read_transaction(self.registry) as uow:
            initial=await scope_fingerprint(self.policy,principal,uow)
            context=await self._context(principal,draft_id,uow)
            if kind=='bindings':candidates=bounded_rows(await self._witnesses(uow))
            else:
                grants=await self.policy.principals.get_current(principal.id,session=uow.session)
                candidates=sorted({grant.policy_id for grant in grants.grants})
        last=''
        if cursor:last=self.cursors.read(cursor,purpose=purpose,identity=identity,scope=initial,query=query_hash).last
        allowed={}
        for candidate in candidates:
            policy_id=candidate.policy_id if kind=='bindings' else candidate
            try:
                async with read_transaction(self.registry) as uow:
                    current_context=await self._context(principal,draft_id,uow)
                    authorization=await self._authorize(principal,policy_id,current_context,uow)
                    value=await self._binding(candidate,uow) if kind=='bindings' else PolicyChoice(policy_id=policy_id,revision=authorization.policy_revision)
                key=(value.binding_id+':'+f'{value.revision:020d}') if kind=='bindings' else value.policy_id
                if key>last:allowed[key]=(candidate,value)
            except GatewayError as error:
                if error.code not in {ErrorCode.NOT_FOUND_OR_FORBIDDEN,ErrorCode.BINDING_DRIFT}:raise
                # An inaccessible or incompatible witness never becomes an inventory row.
        chosen=sorted(allowed)[:limit+1]
        async with read_transaction(self.registry) as uow:
            final=await scope_fingerprint(self.policy,principal,uow)
            if initial!=final:raise not_found()
            context=await self._context(principal,draft_id,uow)
            values=[]
            for key in chosen:
                candidate,value=allowed[key]
                authorization=await self._authorize(principal,candidate.policy_id if kind=='bindings' else candidate,context,uow)
                value=await self._binding(candidate,uow) if kind=='bindings' else PolicyChoice(policy_id=candidate,revision=authorization.policy_revision)
                values.append(value)
        token=self.cursors.issue(purpose=purpose,identity=identity,scope=final,query=query_hash,last=chosen[limit-1]) if len(chosen)>limit else None
        cls=BindingChoicePage if kind=='bindings' else PolicyChoicePage
        return cls(items=values[:limit],next_cursor=token)

    async def bindings(self,principal,draft_id=None,cursor=None,limit=50):
        return await self._page(principal,kind='bindings',draft_id=draft_id,cursor=cursor,limit=limit)

    async def policies(self,principal,draft_id=None,cursor=None,limit=50):
        return await self._page(principal,kind='policies',draft_id=draft_id,cursor=cursor,limit=limit)
