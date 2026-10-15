"""The protected publication receipt suffix cannot reacquire earlier locks."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from gateway.contracts.errors import GatewayError
from gateway.contracts.manifest import validate_manifest
from gateway.registry.models import AuditEvent, DraftRevision, ProductRow, ProductVersion, TestRun as EvidenceRecord, VersionState
from gateway.registry.repository import UnitOfWork


def suffix(valid_manifest, *, missing=False, mismatch=False):
    manifest=validate_manifest(valid_manifest); operation=str(uuid4())
    product=SimpleNamespace(id=manifest.product_id,publication_revision=1,source_operation_id=operation,
        current_version_id=None,draft_revision=1,latest_draft_id='draft')
    draft=SimpleNamespace(id='draft',revision=1,manifest=manifest.model_dump(mode='json'),content_digest=manifest.content_digest)
    policy=SimpleNamespace(policy_id=manifest.policy_id,revision=1)
    binding=SimpleNamespace(id='binding',content_digest='b'*64,model_bundle_digest=manifest.model_bundle_digest,adapter_revision=manifest.adapter_revision)
    test=SimpleNamespace(id='test',status='passed',product_id=product.id,draft_id='draft',draft_digest=draft.content_digest,
        binding_id='binding',binding_digest=binding.content_digest,policy_id=policy.policy_id,policy_revision=1)
    receipt=None if missing else dict(action='publish',product_id=product.id,version_id='wrong' if mismatch else 'version',draft_id='draft',test_run_id='test',
        actor_snapshot={'id':'actor'},expected_draft_revision=1,next_product_revision=1,previous_version_id=None)
    class Session:
        def __init__(self): self.values=iter((product.id,policy,binding));self.added=[];self.sql=[]
        async def scalar(self,statement):
            assert 'FOR UPDATE' not in str(statement)
            return next(self.values)
        async def execute(self,statement,params):
            self.sql.append(str(statement));assert 'FOR UPDATE' not in str(statement)
            assert params=={'id':operation}
            return SimpleNamespace(mappings=lambda:SimpleNamespace(one_or_none=lambda:receipt))
        async def get(self,model,key,**kwargs):
            return {ProductRow:product,DraftRevision:draft,EvidenceRecord:test}.get(model,SimpleNamespace(id=policy.policy_id,current_revision=1))
        async def flush(self): pass
        def add(self,row): self.added.append(row)
    session=Session();uow=UnitOfWork(session)
    async def forbidden(*args,**kwargs): raise AssertionError('Late publication lock')
    uow.lock_product=uow.lock_draft=uow.lock_policy=forbidden
    return uow,session,product,operation


def test_reviewed_version_suffix_uses_receipt_without_locks_or_duplicate_audit(valid_manifest):
    uow,session,product,operation=suffix(valid_manifest)
    row=asyncio.run(uow.insert_version(draft_id='draft',expected_revision=1,test_run_id='test',actor_id='actor',version_id='version',source_operation_id=operation))
    assert row.source_operation_id==operation
    assert product.current_version_id=='version' and product.source_operation_id==operation
    assert sum(isinstance(row,ProductVersion) for row in session.added)==1
    assert sum(isinstance(row,VersionState) and row.source_operation_id==operation for row in session.added)==1
    assert not any(isinstance(row,AuditEvent) for row in session.added)
    assert 'transaction_id=pg_current_xact_id()' in session.sql[0]


@pytest.mark.parametrize('changes',[{'missing':True},{'mismatch':True}])
def test_reviewed_version_suffix_rejects_missing_or_foreign_receipt(valid_manifest,changes):
    uow,session,product,operation=suffix(valid_manifest,**changes)
    with pytest.raises(GatewayError):
        asyncio.run(uow.insert_version(draft_id='draft',expected_revision=1,test_run_id='test',actor_id='actor',version_id='version',source_operation_id=operation))
    assert session.added==[] and product.current_version_id is None


def test_publication_revision_is_server_derived_and_operation_links_are_private():
    assert ProductRow.__table__.c.publication_revision.server_default is not None
    for model in (ProductRow,ProductVersion,VersionState):
        assert str(model.__table__.c.source_operation_id.type.compile(dialect=postgresql.dialect()))=='UUID'
