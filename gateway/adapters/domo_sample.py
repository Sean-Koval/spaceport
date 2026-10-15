"""Offline authoring assets for the one fictional weekly-usage pilot.

SQL is a reviewable template, not measured Domo dialect/type/PDP compatibility.
The operator supplies exact source/dataset/definition identities. The dataset
must already contain producer-deduplicated daily account counts, each account
assigned one access group. Runtime rejects duplicate grain or non-LONG counts.
This module performs no credential lookup, provider call, grant or publication.
"""
from dataclasses import dataclass
import hashlib

from gateway.contracts.canonical import canonical_digest
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import Manifest
from gateway.contracts.query import QueryRequest, validate_query
from .base import authorization_scope_digest
from .domo_runtime import (DomoSource, DomoStatement, DomoStatementCatalog, DomoRuntimeBinding,
                           check_domo_runtime_binding, domo_request_digest)


@dataclass(frozen=True)
class DomoSampleAssets:
    manifest: Manifest
    binding: DomoRuntimeBinding
    statements: DomoStatementCatalog
    statement_records: tuple[DomoStatement, ...]


def build_weekly_domo_assets(*, manifest, source, dataset_id, definition_digest, decisions):
    """Return retained exact bytes for review; a new version needs new assets."""
    m=Manifest.model_validate(manifest.model_dump(mode='python'))
    source=DomoSource.model_validate(source.model_dump(mode='python'))
    decisions=tuple(Decision.model_validate(d.model_dump(mode='python')) for d in decisions)
    case_ids=('north-all','north-core','account-a','core-account-b')
    window={'start':'2026-10-05','end':'2026-10-12','account':None}
    inputs=(window,window,{**window,'account':'acct-a'},{**window,'account':'acct-b'})
    if (m.product_id!='weekly-account-usage' or m.version!='1.0.0' or m.adapter_revision!='domo-named-v2'
        or m.owner!='product-analytics' or m.policy_id!='weekly-usage-access'
        or dataset_id not in source.dataset_ids or len(decisions)!=4
        or tuple(case.id for case in m.fixtures[:4])!=case_ids
        or tuple(dict(case.inputs) for case in m.fixtures[:4])!=inputs
        or m.filters or m.sort.fields or m.timezone!='UTC' or m.grain!='daily'
        or set(m.outputs)!={'day','account','usage_events'} or m.freshness.required):
        raise ValueError('The offline builder supports only the reviewed weekly usage template')
    expected_groups=(('core','special'),('core',),('core','special'),('core',))
    prefix='usage-consumer-' if source.context=='consumer' else 'fixture-usage-'
    statements=[]
    for index,(decision,groups,values) in enumerate(zip(decisions,expected_groups,inputs,strict=True)):
        constraints={c.member:(c.operator,c.values) for c in decision.row_constraints}
        if (decision.principal_id!=prefix+('all' if len(groups)==2 else 'core') or decision.policy_revision!=1
            or decision.tenant_id!='north' or set(decision.visible_fields)!=set(m.outputs)
            or canonical_digest(decision.limits)!=canonical_digest(m.limits)
            or constraints!={'Usage.tenant_id':('eq',('north',)),'Usage.access_group':('in',groups)}):
            raise ValueError('The sample requires its exact fixed data decisions')
        sql=("SELECT day, account, usage_events FROM table WHERE tenant_id = 'north' AND access_group IN ("
             +', '.join("'"+group+"'" for group in groups)
             +") AND day >= '2026-10-05' AND day < '2026-10-12'")
        if values['account'] is not None:
            sql+=" AND account = '"+values['account']+"'"
        sql+=' ORDER BY day, account LIMIT '+str(m.limits.max_rows+1)
        statements.append(DomoStatement(statement_id=source.context.replace('_','-')+'-'+case_ids[index],
            revision=1,text=sql,sha256=hashlib.sha256(sql.encode('utf-8')).hexdigest()))
    catalog=DomoStatementCatalog(statements)
    raw=m.model_dump(mode='python',exclude={'content_digest'})
    raw['model_bundle_digest']=catalog.digest
    m=Manifest.model_validate(raw)
    variants=[]
    for decision,values,statement in zip(decisions,inputs,statements,strict=True):
        request=validate_query(m,QueryRequest(product_id=m.product_id,version=m.version,inputs=values))
        variants.append(dict(query_digest=domo_request_digest(request),scope_digest=authorization_scope_digest(decision),
            decision_digest=canonical_digest(decision),statement_id=statement.statement_id,
            statement_revision=statement.revision,statement_digest=statement.sha256))
    binding=DomoRuntimeBinding(binding_id=m.binding_id,revision=m.binding_revision,context=source.context,
        model_bundle_digest=catalog.digest,source_id=source.source_id,source_revision=source.revision,
        source_digest=source.digest,owner=m.owner,policy_id=m.policy_id,tenant_id='north',dataset_id=dataset_id,
        authoritative_definition_digest=definition_digest,tenant_member='Usage.tenant_id',
        entitlement_member='Usage.access_group',grain_keys=['day','account'],time_field='day',input_fields={'account':'account'},
        columns=[dict(field=name,source_name=name,source_type='STRING',conversion='string') for name in ('day','account')]
            +[dict(field='usage_events',source_name='usage_events',source_type='LONG',conversion='long-decimal0')],
        variants=variants)
    check_domo_runtime_binding(binding,m,source=source)
    return DomoSampleAssets(m,binding,catalog,tuple(statements))
