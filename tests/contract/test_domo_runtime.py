"""Finite operator v2 plan selection, independent of live source authority."""
from copy import deepcopy
import hashlib
import importlib
import pytest
from gateway.adapters.base import authorization_scope_digest
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import Manifest
from gateway.contracts.query import QueryRequest, validate_query

pytestmark = pytest.mark.contract
DATASET = '00000000-0000-0000-0000-000000000001'


def api():
    module = importlib.import_module('gateway.adapters.domo_runtime')
    assert hasattr(module, 'DomoRuntimeBinding'), 'Domo v2 binding is missing'
    return module


def setup(valid_manifest):
    a = api()
    source = a.DomoSource(source_id='consumer',revision=1,context='consumer',
        auth=dict(mode='oauth-client-credentials',client_id_ref='/run/secrets/id',
            client_secret_ref='/run/secrets/secret',client_id_sha256=hashlib.sha256(b'client-id').hexdigest(),
            expected_customer='acme',expected_user_id=123),dataset_ids=[DATASET],
        source_policy_digest='a'*64,max_dispatches=10,max_active_sends=2)
    text = "SELECT day, account, usage_events FROM table WHERE tenant_id = 'north' AND access_group = 'core'"
    statement = a.DomoStatement(statement_id='core-week',revision=1,text=text,
        sha256=hashlib.sha256(text.encode()).hexdigest())
    catalog = a.DomoStatementCatalog([statement])
    raw = deepcopy(valid_manifest)
    raw.update(product_id='weekly-account-usage',binding_id='usage-domo',adapter_revision='domo-named-v2',
        model_bundle_digest=catalog.digest,timezone='UTC',
        inputs={'start':{'type':'date'},'end':{'type':'date'},'account':{'type':'string','nullable':True,
            'required':False,'enum':['acct-a','acct-b'],'default':{'value':None}}},
        outputs={'day':{'type':'date'},'account':{'type':'string'},
                 'usage_events':{'type':'decimal','precision':38,'scale':0}},
        filters={},sort={'fields':[],'max_fields':0},units={'usage_events':{'unit':'events'}},
        redaction={'start':'digest','end':'digest','account':'omit'},
        freshness={'required':False,'meaning':'source_load','max_age_seconds':86400},
        fixtures=[{'id':'core-week','version':'1','inputs':{'start':'2026-10-05','end':'2026-10-12'},
                   'expected_rows':[{'day':'2026-10-05','account':'acct-a','usage_events':'120'}]}],
        examples=[{'description':'One reviewed week','inputs':{'start':'2026-10-05','end':'2026-10-12'}}])
    manifest = Manifest.model_validate(raw)
    decision = Decision(principal_id='north-user',tenant_id='north',policy_revision=1,
        visible_fields=list(manifest.outputs), limits=manifest.limits,
        row_constraints=[dict(member='tenant_id',operator='eq',values=['north']),
                         dict(member='access_group',operator='in',values=['core'])])
    request = validate_query(manifest, QueryRequest(product_id=manifest.product_id,version=manifest.version,
        inputs={'start':'2026-10-05','end':'2026-10-12'}))
    variant = dict(query_digest=a.domo_request_digest(request),scope_digest=authorization_scope_digest(decision),
        decision_digest=canonical_digest(decision),
        statement_id=statement.statement_id,statement_revision=1,statement_digest=statement.sha256)
    binding = a.DomoRuntimeBinding(binding_id=manifest.binding_id,revision=1,model_bundle_digest=catalog.digest,
        owner=manifest.owner,policy_id=manifest.policy_id,tenant_id='north',context='consumer',
        source_id=source.source_id,source_revision=source.revision,source_digest=source.digest,
        dataset_id=DATASET,authoritative_definition_digest='b'*64,
        tenant_member='tenant_id',entitlement_member='access_group',
        grain_keys=['day','account'],time_field='day',input_fields={'account':'account'},
        columns=[dict(field='day',source_name='day',source_type='STRING',conversion='string'),
                 dict(field='account',source_name='account',source_type='STRING',conversion='string'),
                 dict(field='usage_events',source_name='usage_events',source_type='LONG',conversion='long-decimal0')],
        variants=[variant])
    product = PinnedProduct(manifest=manifest,binding_digest=binding.digest,version_id='published-usage')
    return a,source,binding,product,request,decision,catalog


def test_exact_v2_preparation_has_no_sql_or_session(valid_manifest):
    a,s,b,p,q,d,c = setup(valid_manifest)
    plan = a.compile_domo_query(b,p,q,d,c,source=s,request_id='accepted-1')
    assert plan.statement_id == 'core-week'
    assert plan.source_digest == s.digest
    assert plan.query_digest == a.domo_request_digest(q)
    assert plan.decision_digest == canonical_digest(d)
    assert plan.model_bundle_digest == c.digest
    serialized = plan.model_dump(mode='json')
    assert not {'sql','text','session_id','generation','token'} & set(serialized)
    assert 'SELECT' not in str(serialized)
    assert a.parse_domo_binding(b.model_dump(mode='python')) == b


@pytest.mark.parametrize('change', ['principal','policy','scope','tenant','fields','date','version',
    'account','source_identity','source_dataset','binding_digest','statement'])
def test_unreviewed_plan_never_compiles(valid_manifest, change):
    a,s,b,p,q,d,c = setup(valid_manifest)
    if change == 'principal': d=d.model_copy(update={'principal_id':'different'})
    elif change == 'policy': d=d.model_copy(update={'policy_revision':2})
    elif change == 'scope': d=d.model_copy(update={'row_constraints':d.row_constraints[:1]})
    elif change == 'tenant': d=d.model_copy(update={'tenant_id':'south'})
    elif change == 'fields': d=d.model_copy(update={'visible_fields':('day','account')})
    elif change == 'date': q=q.model_copy(update={'inputs':{**q.inputs,'end':'2026-10-11'}})
    elif change == 'version': q=q.model_copy(update={'version':'2.0.0'})
    elif change == 'account': q=q.model_copy(update={'inputs':{**q.inputs,'account':'acct-b'}})
    elif change == 'source_identity': s=s.model_copy(update={'revision':2})
    elif change == 'source_dataset': s=s.model_copy(update={'dataset_ids':('00000000-0000-0000-0000-000000000002',)})
    elif change == 'binding_digest': p=p.model_copy(update={'binding_digest':'0'*64})
    elif change == 'statement':
        text='SELECT other FROM table'
        c=a.DomoStatementCatalog([a.DomoStatement(statement_id='core-week',revision=1,text=text,
            sha256=hashlib.sha256(text.encode()).hexdigest())])
    with pytest.raises(GatewayError): a.compile_domo_query(b,p,q,d,c,source=s)


def test_idempotency_is_not_source_statement_identity(valid_manifest):
    a,s,b,p,q,d,c=setup(valid_manifest)
    changed=q.model_copy(update={'idempotency_key':'other'})
    assert a.compile_domo_query(b,p,q,d,c,source=s) == a.compile_domo_query(b,p,changed,d,c,source=s)


@pytest.mark.parametrize('limit,value', [('max_rows',999),('max_bytes',1000),('page_size',1),
    ('max_date_range_days',7),('execution_timeout_seconds',119),('sync_wait_seconds',0)])
def test_changed_effective_limits_require_an_exact_reviewed_variant(valid_manifest,limit,value):
    a,s,b,p,q,d,c=setup(valid_manifest)
    assert getattr(d.limits,limit)!=value
    d=d.model_copy(update={'limits':d.limits.model_copy(update={limit:value})})
    with pytest.raises(GatewayError) as caught:
        a.compile_domo_query(b,p,q,d,c,source=s)
    assert caught.value.code=='INVALID_INPUT'
