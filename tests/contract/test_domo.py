"""Offline reviewed Domo contracts; no source access or credentials."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import hashlib
import importlib
import json
import socket

import pytest
from pydantic import ValidationError
from gateway.adapters.base import authorization_scope_digest
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.identity import Decision
from gateway.contracts.manifest import Manifest
from gateway.contracts.query import QueryRequest, validate_query

pytestmark = pytest.mark.contract


def api():
    module = importlib.util.find_spec('gateway.adapters.domo')
    assert module is not None, 'The offline Domo named-query reader is not implemented'
    return importlib.import_module(module.name)


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail('Offline Domo contracts must never open sockets')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)


def setup(valid_manifest):
    a = api()
    raw = deepcopy(valid_manifest)
    raw['adapter_revision'] = 'domo-named-v1'
    raw['binding_id'] = 'domo-revenue'
    manifest = Manifest.model_validate(raw)
    decision = Decision(principal_id='north-user', tenant_id='north', policy_revision=1,
        row_constraints=[{'member':'tenant','operator':'eq','values':['north']},
                         {'member':'access_group','operator':'in','values':['north']}],
        visible_fields=list(manifest.outputs), limits=manifest.limits)
    request = validate_query(manifest, QueryRequest(product_id=manifest.product_id,
        version=manifest.version, inputs={'start':'2024-03-09','end':'2024-03-12'}))
    statement = a.ReviewedStatement(statement_id='north-march', revision=1,
        text="SELECT day, segment, revenue FROM reviewed_north_march",
        sha256=hashlib.sha256(b'SELECT day, segment, revenue FROM reviewed_north_march').hexdigest())
    variant = a.DomoQueryVariant(query_digest=a.request_digest(request),
        scope_digest=authorization_scope_digest(decision), statement_id=statement.statement_id,
        statement_revision=statement.revision, statement_digest=statement.sha256)
    binding = a.DomoNamedQueryBinding(binding_id=manifest.binding_id, revision=1,
        model_bundle_digest=manifest.model_bundle_digest, owner=manifest.owner,
        policy_id=manifest.policy_id, tenant_id='north', dataset_id='00000000-0000-0000-0000-000000000001',
        authoritative_definition_digest='b'*64, tenant_member='tenant', entitlement_member='access_group',
        columns=[{'field':name, 'source_name':name, 'source_type':'STRING'} for name in manifest.outputs],
        variants=[variant])
    product = PinnedProduct(manifest=manifest,binding_digest=binding.digest,version_id='domo-product-v1')
    return a,binding,product,request,decision,a.StatementCatalog([statement])


def wire():
    return {'datasource':'00000000-0000-0000-0000-000000000001',
        'columns':['day','segment','revenue'],
        'metadata':[{'type':'STRING','dataSourceId':'00000000-0000-0000-0000-000000000001'}]*3,
        'rows':[['2024-03-09','A','9007199254740993.01']], 'numRows':1,'numColumns':3,'fromcache':True}


def encode(value):
    return json.dumps(value,separators=(',',':')).encode()


def evidence(a,binding,request,decision,body,clock,**updates):
    return a.DomoFreshnessEvidence(binding_digest=binding.digest,
        definition_digest=binding.authoritative_definition_digest,
        query_digest=a.request_digest(request),scope_digest=authorization_scope_digest(decision),
        response_sha256=hashlib.sha256(body).hexdigest(),
        source_watermark=updates.pop('source_watermark',clock.now()-timedelta(hours=1)), **updates)


class Transport:
    def __init__(self,a,body,proof=None,*,status=200,encoding='identity',hook=None):
        self.a,self.body,self.proof=a,body,proof
        self.status,self.encoding,self.hook=status,encoding,hook
        self.calls=[]
        self.closed=False
    async def query(self,plan,*,deadline,max_bytes):
        self.calls.append(plan)
        async def chunks():
            try:
                yield self.body[:10]
                if self.hook: await self.hook()
                yield self.body[10:]
            finally:
                self.closed=True
        return self.a.DomoResponse(self.a.DomoResponseHead(status_code=self.status,
            content_encoding=self.encoding),chunks(),self.proof)


def reader(context,clock,transport,gate):
    a,binding,product,request,decision,statements=context
    return a.DomoReader(binding=binding,statements=statements,transport=transport,
        authorize=gate,clock=clock)


def run_read(context,clock,transport,gate,principal):
    a,binding,product,request,decision,statements=context
    return asyncio.run(reader(context,clock,transport,gate).read(product,request,principal,
        deadline=clock.now()+timedelta(seconds=120),request_id='request-1'))


def assert_error(call,code):
    with pytest.raises(GatewayError) as caught: call()
    assert caught.value.code==code
    return caught.value


def test_named_query_selects_exact_immutable_bytes(valid_manifest):
    a,binding,product,request,decision,statements=setup(valid_manifest)
    plan=a.compile_named_query(binding,product,request,decision,statements)
    assert plan.statement.text=='SELECT day, segment, revenue FROM reviewed_north_march'
    assert plan.query_digest==a.request_digest(request)
    assert plan.dataset_id==binding.dataset_id
    with pytest.raises((TypeError,ValidationError)):
        binding.columns[0].field='other'
    assert 'SELECT' not in repr(plan)


@pytest.mark.parametrize('change',['date','filter','sort','tenant','principal','policy','entitlement','fields'])
def test_unreviewed_request_and_scope_never_compile(valid_manifest,change):
    a,binding,product,request,decision,statements=setup(valid_manifest)
    if change=='date': request=request.model_copy(update={'inputs':{**request.inputs,'end':'2024-03-11'}})
    elif change=='filter': request=QueryRequest(**{**request.model_dump(mode='python'),'filters':[{'field':'segment','operator':'eq','value':'A'}]})
    elif change=='sort': request=QueryRequest(**{**request.model_dump(mode='python'),'sort':[{'field':'day','direction':'desc'}]})
    elif change=='tenant': decision=decision.model_copy(update={'tenant_id':'south'})
    elif change=='principal': decision=decision.model_copy(update={'principal_id':'other'})
    elif change=='policy': decision=decision.model_copy(update={'policy_revision':2})
    elif change=='entitlement': decision=decision.model_copy(update={'row_constraints':decision.row_constraints[:1]})
    else: decision=decision.model_copy(update={'visible_fields':('day',)})
    with pytest.raises(GatewayError): a.compile_named_query(binding,product,request,decision,statements)


def test_idempotency_does_not_change_selection(valid_manifest):
    a,binding,product,request,decision,statements=setup(valid_manifest)
    changed=request.model_copy(update={'idempotency_key':'different'})
    assert a.compile_named_query(binding,product,changed,decision,statements).query_digest==a.request_digest(request)


def test_reader_uses_exact_statement_reauthorizes_and_preserves_decimal(valid_manifest,clock,north_principal):
    c=setup(valid_manifest); a,b,p,q,d,s=c; body=encode(wire()); actions=[]
    async def gate(principal,product,action,request_id):
        actions.append(action); return d
    t=Transport(a,body,evidence(a,b,q,d,body,clock))
    result=run_read(c,clock,t,gate,north_principal)
    assert result.rows[0]['revenue']=='9007199254740993.01'
    assert result.freshness.status=='fresh'
    assert result.cache_status=='hit'
    assert actions==['execute','result']
    assert len(t.calls)==1 and t.closed


@pytest.mark.parametrize('mutation', ['columns','width','count','bool_count','decimal','type','datasource','extra','null','duplicate_key'])
def test_bad_response_fails_without_releasing_rows(valid_manifest,clock,north_principal,mutation):
    c=setup(valid_manifest); a,b,p,q,d,s=c; value=wire()
    if mutation=='columns': value['columns'].reverse()
    elif mutation=='width': value['rows'][0].append('extra')
    elif mutation=='count': value['numRows']=2
    elif mutation=='bool_count': value['numRows']=True
    elif mutation=='decimal': value['rows'][0][2]=1.23
    elif mutation=='type': value['metadata'][2]={'type':'DOUBLE','dataSourceId':b.dataset_id}
    elif mutation=='datasource': value['datasource']='private-other'
    elif mutation=='extra': value['rows'][0][0]='2024-03-09T00:00:00Z'
    elif mutation=='null': value['rows'][0][2]=None
    body=encode(value)
    if mutation=='duplicate_key': body=body.replace(b'"numRows":1',b'"numRows":1,"numRows":1')
    async def gate(*args): return d
    t=Transport(a,body,evidence(a,b,q,d,body,clock))
    error=assert_error(lambda:run_read(c,clock,t,gate,north_principal),'BINDING_DRIFT')
    assert 'private-other' not in str(error)
    assert t.closed


@pytest.mark.parametrize('case',['missing','stale','future','mismatch'])
def test_freshness_is_bound_source_evidence(valid_manifest,clock,north_principal,case):
    c=setup(valid_manifest); a,b,p,q,d,s=c; body=encode(wire())
    proof=evidence(a,b,q,d,body,clock)
    if case=='missing': proof=None
    elif case=='stale': proof=proof.model_copy(update={'source_watermark':clock.now()-timedelta(days=2)})
    elif case=='future': proof=proof.model_copy(update={'source_watermark':clock.now()+timedelta(seconds=1)})
    else: proof=proof.model_copy(update={'response_sha256':'0'*64})
    async def gate(*args): return d
    t=Transport(a,body,proof)
    code='STALE_DATA' if case=='stale' else 'FRESHNESS_UNKNOWN'
    assert_error(lambda:run_read(c,clock,t,gate,north_principal),code)


@pytest.mark.parametrize('change',['tenant','policy','principal','limits'])
def test_revoked_authority_during_read_discards_result(valid_manifest,clock,north_principal,change):
    c=setup(valid_manifest); a,b,p,q,d,s=c; body=encode(wire()); calls=0
    async def gate(*args):
        nonlocal calls
        calls+=1
        if calls==1: return d
        field,value={'tenant':('tenant_id','south'),'policy':('policy_revision',2),
            'principal':('principal_id','other'),'limits':('limits',d.limits.model_copy(update={'max_bytes':100}))}[change]
        return d.model_copy(update={field:value})
    t=Transport(a,body,evidence(a,b,q,d,body,clock))
    assert_error(lambda:run_read(c,clock,t,gate,north_principal),'NOT_FOUND_OR_FORBIDDEN')
    assert len(t.calls)==1 and t.closed


@pytest.mark.parametrize('case',['bytes','rows','normalized'])
def test_complete_result_limits(valid_manifest,clock,north_principal,case):
    if case=='normalized': valid_manifest['limits']={'max_bytes':500}
    c=setup(valid_manifest); a,b,p,q,d,s=c; value=wire()
    if case=='rows': value['rows']*=1001; value['numRows']=1001
    body=b' '*1_048_577 if case=='bytes' else encode(value)
    async def gate(*args): return d
    t=Transport(a,body,evidence(a,b,q,d,body,clock))
    assert_error(lambda:run_read(c,clock,t,gate,north_principal),'RESOURCE_LIMIT')
    assert t.closed


@pytest.mark.parametrize('status',[301,401,403,429,500])
def test_upstream_failures_are_redacted_and_never_retry(valid_manifest,clock,north_principal,status):
    c=setup(valid_manifest); a,b,p,q,d,s=c
    async def gate(*args): return d
    t=Transport(a,b'credential SQL private detail',status=status)
    error=assert_error(lambda:run_read(c,clock,t,gate,north_principal),'UPSTREAM_UNAVAILABLE')
    assert 'SQL' not in str(error) and len(t.calls)==1


def test_mandatory_dependencies_no_default_network(valid_manifest,clock):
    c=setup(valid_manifest)
    with pytest.raises((TypeError,ValueError)):
        reader(c,clock,None,None)


def test_timeout_includes_authorization(valid_manifest,clock,north_principal):
    c=setup(valid_manifest); a,b,p,q,d,s=c
    async def gate(*args): await asyncio.sleep(1); return d
    t=Transport(a,encode(wire()))
    async def call():
        return await reader(c,clock,t,gate).read(p,q,north_principal,
            deadline=clock.now()+timedelta(milliseconds=10),request_id='request-1')
    assert_error(lambda:asyncio.run(call()),'EXECUTION_TIMEOUT')
    assert t.calls==[]


def test_timeout_while_streaming_discards_result(valid_manifest,clock,north_principal):
    c=setup(valid_manifest); a,b,p,q,d,s=c
    async def gate(*args): return d
    async def pause(): await asyncio.sleep(1)
    t=Transport(a,encode(wire()),hook=pause)
    async def call():
        return await reader(c,clock,t,gate).read(p,q,north_principal,
            deadline=clock.now()+timedelta(milliseconds=10),request_id='request-1')
    assert_error(lambda:asyncio.run(call()),'EXECUTION_TIMEOUT')
    assert len(t.calls)==1 and t.closed

@pytest.mark.parametrize('field,value',[('binding_digest','0'*64),('definition_digest','0'*64),
    ('dataset_id','00000000-0000-0000-0000-000000000002'),('max_rows',1001),('max_bytes',2_000_000)])
def test_response_validator_revalidates_compiled_plan(valid_manifest,clock,field,value):
    a,b,p,q,d,s=setup(valid_manifest)
    plan=a.compile_named_query(b,p,q,d,s).model_copy(update={field:value})
    body=encode(wire())
    assert_error(lambda:a.validate_domo_response(b,p,plan,body,evidence(a,b,q,d,body,clock),now=clock.now()),'BINDING_DRIFT')


@pytest.mark.parametrize('code',['UNAUTHENTICATED','NOT_FOUND_OR_FORBIDDEN','UPSTREAM_UNAVAILABLE','CONFLICT'])
def test_gate_errors_are_safe_shared_errors(valid_manifest,clock,north_principal,code):
    c=setup(valid_manifest); a,b,p,q,d,s=c
    async def gate(*args): raise GatewayError(code,'unsafe','private SQL credential text')
    t=Transport(a,encode(wire()))
    error=assert_error(lambda:run_read(c,clock,t,gate,north_principal),code)
    assert str(error)!='private SQL credential text' and error.request_id=='request-1'
    assert t.calls==[]


@pytest.mark.parametrize('encoding',['gzip','br'])
def test_transcoding_rejected(valid_manifest,clock,north_principal,encoding):
    c=setup(valid_manifest); a,b,p,q,d,s=c
    async def gate(*args): return d
    t=Transport(a,encode(wire()),encoding=encoding)
    assert_error(lambda:run_read(c,clock,t,gate,north_principal),'UPSTREAM_UNAVAILABLE')


def test_empty_results_have_exact_schema_and_freshness(valid_manifest,clock,north_principal):
    c=setup(valid_manifest); a,b,p,q,d,s=c; value=wire(); value['rows']=[]; value['numRows']=0
    body=encode(value)
    async def gate(*args): return d
    result=run_read(c,clock,Transport(a,body,evidence(a,b,q,d,body,clock)),gate,north_principal)
    assert result.rows==() and result.freshness.status=='fresh'


def test_unsupported_freeform_input_never_reaches_source(valid_manifest,clock,north_principal):
    c=setup(valid_manifest); a,b,p,q,d,s=c
    c=(a,b,p,q.model_copy(update={'inputs':{**q.inputs,'sql':'SELECT secret'}}),d,s)
    async def gate(*args): return d
    t=Transport(a,encode(wire()))
    assert_error(lambda:run_read(c,clock,t,gate,north_principal),'INVALID_INPUT')
    assert t.calls==[]


def test_statement_digest_drift_is_rejected(valid_manifest):
    a,b,p,q,d,s=setup(valid_manifest)
    with pytest.raises(ValidationError):
        a.ReviewedStatement(statement_id='bad',revision=1,text='SELECT changed',sha256='a'*64)
    changed=b.model_copy(update={'variants':(b.variants[0].model_copy(update={'statement_digest':'0'*64}),)})
    changed_product=p.model_copy(update={'binding_digest':changed.digest})
    assert_error(lambda:a.compile_named_query(changed,changed_product,q,d,s),'BINDING_DRIFT')


def test_optional_freshness_does_not_invent_watermark(valid_manifest,clock,north_principal):
    valid_manifest['freshness']['required']=False
    c=setup(valid_manifest); a,b,p,q,d,s=c
    async def gate(*args): return d
    result=run_read(c,clock,Transport(a,encode(wire())),gate,north_principal)
    assert result.source_watermark is None and result.freshness.status=='unknown'


def test_no_live_source_registration():
    from gateway.adapters.bigquery import select_adapter
    assert_error(lambda:select_adapter('domo'),'INVALID_INPUT')


def test_wall_clock_source_deadline_cannot_be_renewed(valid_manifest,clock,north_principal):
    c=setup(valid_manifest); a,b,p,q,d,s=c
    async def gate(*args): return d
    async def advance(): clock.instant+=timedelta(seconds=121)
    t=Transport(a,encode(wire()),hook=advance)
    assert_error(lambda:run_read(c,clock,t,gate,north_principal),'EXECUTION_TIMEOUT')


def test_checked_in_offline_fixture_is_reproducible():
    from pathlib import Path
    path=Path(__file__).resolve().parents[2]/'fixtures/adapter/domo.json'
    assert path.exists(), 'The offline Domo fixture is missing'
    fixture=json.loads(path.read_text())
    a=api(); b=a.DomoNamedQueryBinding.model_validate(fixture['binding'])
    p=PinnedProduct.model_validate(fixture['product']); q=QueryRequest.model_validate(fixture['query'])
    d=Decision.model_validate(fixture['decision'])
    s=a.StatementCatalog([a.ReviewedStatement.model_validate(fixture['statement'])])
    plan=a.compile_named_query(b,p,q,d,s)
    result=a.validate_domo_response(b,p,plan,encode(fixture['response']),
        a.DomoFreshnessEvidence.model_validate(fixture['freshness_evidence']),now=fixture['assessed_at'])
    assert result.rows[0]['revenue']=='9007199254740993.01'


def test_transport_cannot_replace_selected_plan_with_another_reviewed_variant(valid_manifest,clock,north_principal):
    c=setup(valid_manifest); a,b,p,q,d,s=c
    other=q.model_copy(update={'inputs':{**q.inputs,'end':'2024-03-11'}})
    replacement=b.variants[0].model_copy(update={'query_digest':a.request_digest(other)})
    b=a.DomoNamedQueryBinding.model_validate({**b.model_dump(mode='python'),'variants':[*b.variants,replacement]})
    p=p.model_copy(update={'binding_digest':b.digest}); c=(a,b,p,q,d,s)
    body=encode(wire())
    proof=evidence(a,b,other,d,body,clock)
    class Swap(Transport):
        async def query(self,plan,**kwargs):
            object.__setattr__(plan,'query_digest',replacement.query_digest)
            return await super().query(plan,**kwargs)
    async def gate(*args): return d
    t=Swap(a,body,proof)
    assert_error(lambda:run_read(c,clock,t,gate,north_principal),'FRESHNESS_UNKNOWN')


def test_authorization_callback_cannot_rewrite_pinned_product(valid_manifest,clock,north_principal):
    c=setup(valid_manifest); a,b,p,q,d,s=c
    body=encode(wire()); proof=evidence(a,b,q,d,body,clock)
    calls=0
    async def gate(principal,product,*args):
        nonlocal calls
        calls+=1
        object.__setattr__(product,'binding_digest','0'*64)
        object.__setattr__(principal,'id','someone-else')
        return d
    t=Transport(a,body,proof)
    result=run_read(c,clock,t,gate,north_principal)
    assert result.rows[0]['revenue']=='9007199254740993.01'
    assert p.binding_digest==b.digest and north_principal.id=='north-user'


@pytest.mark.parametrize('flag',['redirected','transcoded'])
def test_response_head_retains_unsafe_transport_history(valid_manifest,clock,north_principal,flag):
    c=setup(valid_manifest); a,b,p,q,d,s=c
    assert flag in a.DomoResponseHead.model_fields, 'Transport history must be reported explicitly'
    class Unsafe(Transport):
        async def query(self,*args,**kwargs):
            response=await super().query(*args,**kwargs)
            return a.DomoResponse(response.head.model_copy(update={flag:True}),response.body,response.freshness)
    async def gate(*args): return d
    t=Unsafe(a,encode(wire()))
    assert_error(lambda:run_read(c,clock,t,gate,north_principal),'UPSTREAM_UNAVAILABLE')


def test_statement_resolver_cannot_replace_the_reviewed_statement_before_io(valid_manifest,clock,north_principal):
    c=setup(valid_manifest); a,b,p,q,d,s=c
    text='SELECT secret FROM other_dataset'
    other=a.ReviewedStatement(statement_id='other',revision=1,text=text,sha256=hashlib.sha256(text.encode()).hexdigest())
    class ForgedResolver:
        def resolve(self,*args): return other
    assert_error(lambda:a.compile_named_query(b,p,q,d,ForgedResolver()),'BINDING_DRIFT')
    with pytest.raises(ValueError):
        a.DomoReader(binding=b,statements=ForgedResolver(),transport=Transport(a,b''),authorize=lambda *args:None)


def test_response_close_is_inside_total_deadline_before_release_gate(valid_manifest,clock,north_principal):
    c=setup(valid_manifest); a,b,p,q,d,s=c; body=encode(wire()); actions=[]
    class SlowClose:
        def __init__(self): self.sent=False
        def __aiter__(self): return self
        async def __anext__(self):
            if self.sent: raise StopAsyncIteration
            self.sent=True; return body
        async def aclose(self): await asyncio.sleep(.06)
    class SlowTransport(Transport):
        async def query(self,plan,**kwargs):
            self.calls.append(plan)
            return a.DomoResponse(a.DomoResponseHead(status_code=200),SlowClose(),evidence(a,b,q,d,body,clock))
    async def gate(principal,product,action,request_id): actions.append(action); return d
    t=SlowTransport(a,body)
    async def read():
        return await reader(c,clock,t,gate).read(p,q,north_principal,
            deadline=clock.now()+timedelta(milliseconds=20),request_id='request-1')
    assert_error(lambda:asyncio.run(read()),'EXECUTION_TIMEOUT')
    assert actions==['execute']


def test_result_repr_does_not_disclose_rows(valid_manifest,clock,north_principal):
    c=setup(valid_manifest); a,b,p,q,d,s=c; body=encode(wire())
    async def gate(*args): return d
    result=run_read(c,clock,Transport(a,body,evidence(a,b,q,d,body,clock)),gate,north_principal)
    assert '9007199254740993.01' not in repr(result)
    assert '9007199254740993.01' not in str(result)
    assert result.rows[0]['revenue']=='9007199254740993.01'
