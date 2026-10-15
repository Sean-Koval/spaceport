"""Fictional weekly account-usage contract shared by the two reviewed samples."""
from gateway.contracts.manifest import validate_manifest,Limits
from gateway.contracts.identity import Decision,RowConstraint

LIMITS=Limits(max_rows=100,max_bytes=65536,page_size=100,per_principal_concurrency=1,
    deployment_concurrency=2,max_date_range_days=7,sync_wait_seconds=0,execution_timeout_seconds=30)
WINDOW={'start':'2026-10-05','end':'2026-10-12','account':None}
CORE_ROWS=({'day':'2026-10-05','account':'acct-a','usage_events':'120'},
    {'day':'2026-10-06','account':'acct-a','usage_events':'180'})
ALL_ROWS=(*CORE_ROWS,{'day':'2026-10-05','account':'acct-b','usage_events':'50'})
CASE_PRINCIPALS={'north-all':'fixture-usage-all','north-core':'fixture-usage-core',
    'account-a':'fixture-usage-all','core-account-b':'fixture-usage-core',
    'denied':'fixture-usage-denied','south-denied':'fixture-usage-south','bad-window':'fixture-usage-all'}


def weekly_manifest(*,adapter_revision,binding_id,model_bundle_digest):
    def case(name,inputs,rows=(),error=None):
        return dict(id=name,version='1',inputs=inputs,expected_rows=list(rows),expected_error=error,tolerance='0',source_watermark=None)
    return validate_manifest(dict(schema_version='1.0',product_id='weekly-account-usage',version='1.0.0',
        name='Weekly account usage',description='Daily accepted, deduplicated usage events by account over an approved seven-day UTC window.',
        examples=[{'description':'Seven-day north account usage','inputs':dict(WINDOW)}],owner='product-analytics',domain='customer-success',links=[],
        inputs={'start':{'type':'date'},'end':{'type':'date'},'account':{'type':'string','required':False,'nullable':True,
            'enum':['acct-a','acct-b'],'default':{'value':None}}},
        outputs={'day':{'type':'date'},'account':{'type':'string'},'usage_events':{'type':'decimal','precision':38,'scale':0}},
        filters={},sort={'fields':[],'max_fields':0},grain='daily',units={'usage_events':{'unit':'events'}},timezone='UTC',
        date_boundaries={'start_input':'start','end_input':'end','interval':'[start,end)'},
        aggregation='Sum of producer-deduplicated accepted usage event counts by UTC day and account.',
        freshness={'required':False,'max_age_seconds':86400,'meaning':'source_load'},binding_id=binding_id,binding_revision=1,
        adapter_revision=adapter_revision,policy_id='weekly-usage-access',classification='internal',
        redaction={'start':'digest','end':'digest','account':'omit'},model_bundle_digest=model_bundle_digest,limits=LIMITS.model_dump(mode='json'),
        fixtures=[case('north-all',dict(WINDOW),ALL_ROWS),case('north-core',dict(WINDOW),CORE_ROWS),
            case('account-a',{**WINDOW,'account':'acct-a'},CORE_ROWS),case('core-account-b',{**WINDOW,'account':'acct-b'}),
            case('denied',dict(WINDOW),error='NOT_FOUND_OR_FORBIDDEN'),case('south-denied',dict(WINDOW),error='NOT_FOUND_OR_FORBIDDEN'),
            case('bad-window',{**WINDOW,'end':WINDOW['start']},error='INVALID_INPUT')]))


def fixed_decisions(context):
    if context not in {'consumer','fixture_test'}:raise ValueError('Unknown sample context')
    prefix='usage-consumer-' if context=='consumer' else 'fixture-usage-'
    def decision(group):
        return Decision(principal_id=prefix+group,policy_revision=1,tenant_id='north',
            row_constraints=(RowConstraint(member='Usage.tenant_id',operator='eq',values=('north',)),
                RowConstraint(member='Usage.access_group',operator='in',values=('core','special') if group=='all' else ('core',))),
            visible_fields=('day','account','usage_events'),limits=LIMITS)
    return (decision('all'),decision('core'),decision('all'),decision('core'))
