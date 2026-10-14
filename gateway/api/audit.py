"""Scoped redacted audit access with authorization before selection and release."""
from datetime import datetime
import json
from sqlalchemy import select,tuple_,func
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import ErrorCode,GatewayError
from gateway.registry.models import AuditEvent
from gateway.registry.repository import not_found
from .catalog import page_arguments,scope_fingerprint
from .transactions import read_transaction
from .models import AuditEntry,AuditPage


class AuditReadService:
    def __init__(self,*,registry,policy,cursors,clock):
        self.registry,self.policy,self.cursors,self.clock=registry,policy,cursors,clock

    async def list(self,principal,cursor=None,limit=50):
        page_arguments(cursor=cursor,limit=limit)
        identity=canonical_digest(principal);query_hash=canonical_digest({'limit':limit})
        async with read_transaction(self.registry) as uow:
            initial=await scope_fingerprint(self.policy,principal,uow)
            grants=await self.policy.principals.get_current(principal.id,session=uow.session)
            first_upper=await uow.session.scalar(select(func.clock_timestamp()))
        scopes=[]
        # Every denied scope aborts only its own transaction. No failed UoW reuse.
        for policy_id in sorted({grant.policy_id for grant in grants.grants}):
            try:
                async with read_transaction(self.registry) as uow:
                    await self.policy.authorize_scope(principal,policy_id,'audit_view',uow)
                scopes.append(policy_id)
            except GatewayError as error:
                if error.code!=ErrorCode.NOT_FOUND_OR_FORBIDDEN:raise
        if not scopes:raise not_found()
        position=self.cursors.read(cursor,purpose='audit',identity=identity,scope=initial,query=query_hash) if cursor else None
        async with read_transaction(self.registry) as uow:
            # Capture DB clock before the view's own final allow audit. A continuation
            # keeps this upper bound, so viewing cannot extend its own page stream.
            upper=datetime.fromisoformat(position.upper) if position and position.upper else first_upper
            final=await scope_fingerprint(self.policy,principal,uow)
            if initial!=final:raise not_found()
            for policy_id in scopes:await self.policy.authorize_scope(principal,policy_id,'audit_view',uow)
            statement=select(AuditEvent).where(AuditEvent.policy_id.in_(scopes),AuditEvent.created_at<=upper)
            if position:
                try:
                    created,identifier=json.loads(position.last)
                    created=datetime.fromisoformat(created)
                except (ValueError,TypeError):raise not_found() from None
                statement=statement.where(tuple_(AuditEvent.created_at,AuditEvent.id)>(created,identifier))
            rows=(await uow.session.execute(statement.order_by(AuditEvent.created_at,AuditEvent.id).limit(limit+1))).scalars().all()
            items=[AuditEntry(**{field:getattr(row,field) for field in AuditEntry.model_fields}) for row in rows[:limit]]
        token=None
        if len(rows)>limit:
            tail=rows[limit-1]
            token=self.cursors.issue(purpose='audit',identity=identity,scope=final,query=query_hash,
                last=json.dumps([tail.created_at.isoformat(),tail.id],separators=(',',':')),upper=upper.isoformat())
        return AuditPage(items=items,next_cursor=token)
