"""Typed immutable Domo preparations linked to one admitted logical source owner.

The migration supplies the actual database boundary. These helpers never mint
source authority, accept SQL, create a new session or change a retained record.
"""
from uuid import UUID
from pydantic import TypeAdapter
from sqlalchemy import text

from gateway.contracts.canonical import Identifier, canonical_bytes
from gateway.source_lifecycle import SourceSessionSpec
from .domo_http import source_error
from .domo_runtime import PreparedDomoQuery,parse_domo_binding


def _owner(owner_kind,owner_id):
    if owner_kind not in {'consumer','fixture_test'}:
        raise ValueError
    TypeAdapter(Identifier).validate_python(owner_id,strict=True)


def _session_id(value):
    if not isinstance(value,(str,UUID)) or str(UUID(str(value)))!=str(value):
        raise ValueError
    return str(value)


def _binding_plan(prepared,binding):
    p=PreparedDomoQuery.model_validate(prepared.model_dump(mode='python'))
    b=parse_domo_binding(binding)
    if ((p.context,p.binding_id,p.binding_revision,p.binding_digest,p.model_bundle_digest,
         p.source_id,p.source_revision,p.source_digest,p.dataset_id,p.definition_digest)
        != (b.context,b.binding_id,b.revision,b.digest,b.model_bundle_digest,
            b.source_id,b.source_revision,b.source_digest,b.dataset_id,b.authoritative_definition_digest)
        or not any((p.query_digest,p.scope_digest,p.decision_digest,p.statement_id,p.statement_revision,p.statement_digest)
            ==(v.query_digest,v.scope_digest,v.decision_digest,v.statement_id,v.statement_revision,v.statement_digest)
            for v in b.variants)):
        raise ValueError
    return p


def validate_source_preparation_record(record,owner_kind,owner_id,binding):
    try:
        _owner(owner_kind,owner_id)
        _session_id(record['session_id'])
        p=_binding_plan(PreparedDomoQuery.model_validate(record['prepared_query']),binding)
        session=SourceSessionSpec(**record['session_plan'])
        if ((record['owner_kind'],record['owner_id'],record['session_context'],record['session_owner_id'])
            !=(owner_kind,owner_id,owner_kind,owner_id) or p.context!=owner_kind
            or record['prepared_digest']!=p.digest
            or record['prepared_canonical']!=canonical_bytes(p).decode('utf-8')
            or (session.provider,session.context,session.source_id,session.source_revision,session.definition_digest,
                session.prepared_digest,session.scope_digest,session.decision_digest,
                session.binding_digest,session.model_bundle_digest)
               !=('domo',p.context,p.source_id,p.source_revision,p.source_digest,p.digest,
                  p.scope_digest,p.decision_digest,p.binding_digest,p.model_bundle_digest)):
            raise ValueError
        return p
    except (ValueError,TypeError,AttributeError,KeyError):
        raise source_error() from None


async def _read(uow,owner_kind,owner_id):
    rows=await uow.session.execute(text('''SELECT p.owner_kind,p.owner_id,p.session_id,
        p.prepared_query,p.prepared_canonical,p.prepared_digest,
        s.owner_id AS session_owner_id,s.context AS session_context,s.plan AS session_plan
        FROM gateway.source_preparations p JOIN gateway.source_sessions s ON s.id=p.session_id
        WHERE p.owner_kind=:owner_kind AND p.owner_id=:owner_id'''),
        {'owner_kind':owner_kind,'owner_id':owner_id})
    return rows.mappings().one_or_none()


async def load_source_prepared(uow,owner_kind,owner_id,binding):
    try:
        _owner(owner_kind,owner_id)
        record=await _read(uow,owner_kind,owner_id)
        return None if record is None else validate_source_preparation_record(record,owner_kind,owner_id,binding)
    except (ValueError,TypeError,AttributeError):
        raise source_error() from None


async def save_source_prepared(uow,owner_kind,owner_id,session_id,prepared,binding):
    try:
        _owner(owner_kind,owner_id)
        session_id=_session_id(session_id)
        prepared=_binding_plan(prepared,binding)
        if prepared.context!=owner_kind:
            raise ValueError
        canonical=canonical_bytes(prepared).decode('utf-8')
        if len(canonical.encode('utf-8'))>8192:
            raise ValueError
        record=await _read(uow,owner_kind,owner_id)
        if record is None:
            await uow.session.execute(text('''INSERT INTO gateway.source_preparations
                (owner_kind,owner_id,session_id,prepared_query,prepared_canonical,prepared_digest)
                VALUES (:owner_kind,:owner_id,CAST(:session_id AS uuid),CAST(:prepared_json AS jsonb),CAST(:canonical AS text),:digest)
                ON CONFLICT (owner_kind,owner_id) DO NOTHING'''),
                {'owner_kind':owner_kind,'owner_id':owner_id,'session_id':session_id,
                 'prepared_json':canonical,'canonical':canonical,'digest':prepared.digest})
            record=await _read(uow,owner_kind,owner_id)
        if (record is None or _session_id(record['session_id'])!=session_id
            or validate_source_preparation_record(record,owner_kind,owner_id,binding)!=prepared):
            raise ValueError
        return prepared
    except (ValueError,TypeError,AttributeError,KeyError):
        raise source_error() from None
