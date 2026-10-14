"""Permission-filtered discovery shared directly by REST and MCP."""
from uuid import uuid4
from pydantic import TypeAdapter
from sqlalchemy import select
from gateway.contracts.canonical import Identifier, Version, canonical_digest
from gateway.contracts.errors import ErrorCode, GatewayError, invalid_input
from gateway.contracts.identity import Principal
from gateway.adapters.base import check_binding,validate_query_visibility
from gateway.contracts.query import QueryRequest,validate_query
from gateway.contracts.manifest import SortDefinition,iso_date
from gateway.contracts.execution import Provenance
from gateway.registry.models import ProductRow, ProductVersion, VersionState, PolicyRow
from gateway.registry.repository import not_found
from .transactions import read_transaction
from .models import ProductDescription, CatalogPage

MAX_SCAN=1000


def page_arguments(query='',cursor=None,limit=50):
    if (type(query) is not str or len(query)>256 or type(limit) is not int or not 1<=limit<=50
        or cursor is not None and (type(cursor) is not str or not 1<=len(cursor)<=4096)):
        raise invalid_input()


def bounded_rows(rows):
    if len(rows)>MAX_SCAN:raise GatewayError(ErrorCode.RESOURCE_LIMIT,str(uuid4()),'The discovery work limit was exceeded.')
    return rows


async def scope_fingerprint(policy,principal,uow):
    """Read fresh verified identity, grants and all relevant digest-valid policy heads."""
    principal=Principal.model_validate(principal.model_dump(mode='python'))
    await policy._identity(principal,uow)
    grants=await policy.principals.get_current(principal.id,session=uow.session)
    current=[]
    for policy_id in sorted({grant.policy_id for grant in grants.grants}):
        head=await uow.session.get(PolicyRow,policy_id,populate_existing=True)
        if head is None:
            current.append([policy_id,None]);continue
        revision,_=await policy._current(policy_id,uow)
        current.append([policy_id,revision.revision,revision.content_digest])
    return canonical_digest({'principal':principal,'grants':grants,'policies':current})


def visible_examples(manifest,decision,binding):
    examples=[]
    for example in manifest.examples:
        try:
            request=validate_query(manifest,QueryRequest(product_id=manifest.product_id,version=manifest.version,inputs=example.inputs))
            validate_query_visibility(binding,request,decision,'catalog-example')
        except GatewayError:
            continue
        bounds=manifest.date_boundaries
        if bounds and (iso_date(request.inputs[bounds.end_input])-iso_date(request.inputs[bounds.start_input])).days>decision.limits.max_date_range_days:
            continue
        permitted=True
        for name,field in binding.input_fields.items():
            if name not in request.inputs or request.inputs[name] is None:continue
            member=binding.fields[field].member
            for constraint in decision.row_constraints:
                if constraint.member==member and request.inputs[name] not in constraint.values:permitted=False
        if permitted:examples.append(example)
    return examples


def project_product(target,decision,binding,status):
    manifest=target.manifest
    check_binding(binding,manifest,digest=target.binding_digest)
    visible=set(decision.visible_fields)
    if binding.context!='consumer' or not visible<=set(manifest.outputs):raise not_found()
    full=visible==set(manifest.outputs)
    allowed_inputs={name for name,field in binding.input_fields.items() if field in visible}
    times={name for name,value in binding.fields.items() if value.kind=='time'}
    boundaries=manifest.date_boundaries if times<=visible else None
    if boundaries:allowed_inputs|={boundaries.start_input,boundaries.end_input}
    return ProductDescription(product_id=manifest.product_id,version=manifest.version,digest=manifest.content_digest,
        name=manifest.name,status=status,description=manifest.description if full else None,
        owner=manifest.owner if full else None,domain=manifest.domain if full else None,
        examples=visible_examples(manifest,decision,binding) if full else (),links=(),
        inputs={k:v for k,v in manifest.inputs.items() if k in allowed_inputs},
        outputs={k:v for k,v in manifest.outputs.items() if k in visible},
        filters={k:v for k,v in manifest.filters.items() if k in visible},
        sort=SortDefinition(fields=[name for name in manifest.sort.fields if name in visible],max_fields=manifest.sort.max_fields),
        units={k:v for k,v in manifest.units.items() if k in visible},grain=manifest.grain if full else None,
        aggregation=manifest.aggregation if full else None,timezone=manifest.timezone,date_boundaries=boundaries,
        freshness=manifest.freshness,provenance=Provenance(binding_id=manifest.binding_id,
            binding_revision=manifest.binding_revision,adapter_revision=manifest.adapter_revision,
            model_bundle_digest=manifest.model_bundle_digest,links=()),partial=not full,limits=decision.limits)


class CatalogService:
    def __init__(self,*,registry,policy,bindings,cursors):
        self.registry,self.policy,self.bindings,self.cursors=registry,policy,bindings,cursors

    async def _projection(self,principal,target,uow,*,discovery=False):
        if discovery:await self.policy.authorize(principal,'discover',target,uow)
        decision=await self.policy.authorize(principal,'describe',target,uow)
        state=await uow.session.scalar(select(VersionState.status).where(VersionState.version_id==target.version_id))
        if state not in {'published','deprecated'}:raise not_found()
        binding=await self.bindings.resolve(target.manifest.binding_id,target.manifest.binding_revision)
        return project_product(target,decision,binding,state)

    async def describe(self,principal,product_id,version=None):
        try:
            TypeAdapter(Identifier).validate_python(product_id,strict=True)
            if version is not None:TypeAdapter(Version).validate_python(version,strict=True)
        except ValueError:raise invalid_input() from None
        target=await self.registry.resolve(product_id,version)
        async with read_transaction(self.registry) as uow:
            result=await self._projection(principal,target,uow)
        return result

    async def search(self,principal,query='',cursor=None,limit=50):
        page_arguments(query,cursor,limit)
        normalized=query.casefold()
        identity=canonical_digest(principal)
        query_hash=canonical_digest({'query':normalized,'limit':limit})
        async with read_transaction(self.registry) as uow:
            initial=await scope_fingerprint(self.policy,principal,uow)
            rows=bounded_rows((await uow.session.execute(select(ProductVersion.product_id,ProductVersion.version,
                ProductVersion.id).join(ProductRow,ProductRow.current_version_id==ProductVersion.id)
                .join(VersionState,VersionState.version_id==ProductVersion.id)
                .where(VersionState.status.in_(['published','deprecated'])).order_by(ProductVersion.product_id.collate("C"),ProductVersion.id.collate("C"))
                .limit(MAX_SCAN+1))).all())
        last=''
        if cursor:last=self.cursors.read(cursor,purpose='catalog',identity=identity,scope=initial,query=query_hash).last
        matches=[]
        for product_id,version,version_id in rows:
            if product_id<=last:continue
            try:
                target=await self.registry.resolve(product_id,version)
                if target.version_id!=version_id:raise not_found()
                async with read_transaction(self.registry) as uow:
                    projection=await self._projection(principal,target,uow,discovery=True)
                haystack=' '.join(value for value in [projection.product_id,projection.name,projection.description,projection.domain] if value)
                if normalized in haystack.casefold():matches.append(target)
                if len(matches)>limit:break
            except GatewayError as error:
                if error.code!=ErrorCode.NOT_FOUND_OR_FORBIDDEN:raise
                # Denial aborted its own transaction, never reuse that UoW.
        async with read_transaction(self.registry) as uow:
            final=await scope_fingerprint(self.policy,principal,uow)
            if final!=initial:raise not_found()
            checked=[await self._projection(principal,target,uow,discovery=True) for target in matches]
        items=checked[:limit]
        token=self.cursors.issue(purpose='catalog',identity=identity,scope=final,query=query_hash,last=items[-1].product_id) if len(checked)>limit else None
        return CatalogPage(items=items,next_cursor=token)
