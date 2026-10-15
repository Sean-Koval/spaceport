"""The actual Domo wire boundary; HTTP fake never opens provider sockets."""
import importlib
import json
from copy import deepcopy

import pytest

from gateway.contracts.errors import GatewayError

pytestmark = pytest.mark.contract
DATASET = '00000000-0000-0000-0000-000000000001'


def api():
    spec = importlib.util.find_spec('gateway.adapters.domo_http')
    assert spec is not None, 'Domo runtime HTTP boundary is missing'
    return importlib.import_module(spec.name)


def wire():
    return {'datasource': DATASET, 'columns': ['day', 'account', 'usage_events'],
        'metadata': [{'type': t, 'dataSourceId': DATASET, 'maxLength': -1,
            'minLength': -1, 'periodIndex': 0} for t in ['STRING', 'STRING', 'LONG']],
        'rows': [['2026-10-05', 'acct-a', 9007199254740993]],
        'numRows': 1, 'numColumns': 3, 'fromcache': False}


def parse(value=None, **bounds):
    body = value if isinstance(value, bytes) else json.dumps(wire() if value is None else value).encode()
    return api().parse_domo_success(body, dataset_id=DATASET, max_rows=100,
        max_bytes=bounds.get('max_bytes', 65536))


def test_success_retains_exact_long_and_private_rows():
    result = parse()
    assert result.rows == (('2026-10-05', 'acct-a', 9007199254740993),)
    assert result.source_types == ('STRING', 'STRING', 'LONG')
    assert result.columns == ('day', 'account', 'usage_events')
    assert result.cache_status == 'miss'
    assert 'acct-a' not in repr(result)


@pytest.mark.parametrize('mutation', ['extra', 'missing', 'dataset', 'duplicate_column',
    'metadata_field', 'metadata_null', 'metadata_type', 'metadata_dataset', 'metadata_bool',
    'string_rows', 'object_rows', 'width', 'row_count', 'count_bool', 'cache_int',
    'long_float', 'long_bool', 'long_overflow', 'nested_cell', 'column_overflow'])
def test_invalid_envelopes_never_establish_terminal_success(mutation):
    value = wire()
    if mutation == 'extra': value['error'] = 'private upstream diagnostic'
    elif mutation == 'missing': del value['fromcache']
    elif mutation == 'dataset': value['datasource'] = '00000000-0000-0000-0000-000000000002'
    elif mutation == 'duplicate_column': value['columns'][1] = 'day'
    elif mutation == 'metadata_field': value['metadata'][0]['unknown'] = 'secret'
    elif mutation == 'metadata_null': value['metadata'][0]['maxLength'] = None
    elif mutation == 'metadata_type': value['metadata'][0]['type'] = 'DATE'
    elif mutation == 'metadata_dataset': value['metadata'][0]['dataSourceId'] = 'wrong'
    elif mutation == 'metadata_bool': value['metadata'][0]['periodIndex'] = True
    elif mutation == 'string_rows': value['rows'] = ['encoded row']
    elif mutation == 'object_rows': value['rows'] = [{'day': '2026-10-05'}]
    elif mutation == 'width': value['rows'][0].append('extra')
    elif mutation == 'row_count': value['numRows'] = 2
    elif mutation == 'count_bool': value['numRows'] = True
    elif mutation == 'cache_int': value['fromcache'] = 0
    elif mutation == 'long_float': value['rows'][0][2] = 120.0
    elif mutation == 'long_bool': value['rows'][0][2] = True
    elif mutation == 'long_overflow': value['rows'][0][2] = 2**63
    elif mutation == 'nested_cell': value['rows'][0][0] = {'nested': 'private'}
    elif mutation == 'column_overflow': value['columns'][0] = 'x' * 257
    with pytest.raises(GatewayError) as caught: parse(value)
    assert caught.value.code == 'BINDING_DRIFT'
    assert 'private' not in str(caught.value)


def test_duplicate_keys_and_long_exponent_rejected():
    body = json.dumps(wire()).encode()
    for bad in [body.replace(b'"numRows": 1', b'"numRows": 1, "numRows": 1'),
                body.replace(b'9007199254740993', b'1e2'), b'{"rows":NaN}']:
        with pytest.raises(GatewayError): parse(bad)


def test_exact_sentinel_bound_and_bytes():
    value = wire()
    value['rows'] *= 101
    value['numRows'] = 101
    assert len(parse(value).rows) == 101
    value['rows'].append(deepcopy(value['rows'][0])); value['numRows'] += 1
    with pytest.raises(GatewayError): parse(value)
    with pytest.raises(GatewayError) as caught: parse(max_bytes=10)
    assert caught.value.code == 'RESOURCE_LIMIT'


def test_optional_metadata_and_empty_result_are_recognized():
    value = wire()
    value['rows'] = []; value['numRows'] = 0
    value['metadata'] = [{'type': m['type'], 'dataSourceId': DATASET} for m in value['metadata']]
    result = parse(value)
    assert result.rows == ()
    assert len(result.response_sha256) == 64


@pytest.mark.parametrize('value', [-2**63, 2**63-1])
def test_long_signed_boundaries_are_lossless(value):
    response = wire(); response['rows'][0][2] = value
    assert parse(response).rows[0][2] == value


def source():
    import hashlib
    from gateway.adapters.domo_runtime import DomoSource
    return DomoSource(source_id='consumer', revision=1, context='consumer',
        auth=dict(mode='oauth-client-credentials', client_id_ref='/run/secrets/domo_id',
            client_secret_ref='/run/secrets/domo_secret', client_id_sha256=hashlib.sha256(b'client-id').hexdigest(),
            expected_customer='acme', expected_user_id=123), dataset_ids=[DATASET],
        source_policy_digest='a'*64, max_dispatches=10, max_active_sends=2)


def token_wire():
    return dict(access_token='private-token', customer='acme', userId=123,
        expires_in=3599, token_type='bearer', scope='data', role='Reader', jti='request')


def secrets(ref):
    return 'client-id' if ref.endswith('domo_id') else 'private-secret'


def test_token_uses_actual_httpx_basic_form_and_identity(clock):
    import asyncio
    import httpx
    from datetime import timedelta
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={'content-type': 'application/json'},
            stream=httpx.ByteStream(json.dumps(token_wire()).encode()))
    async def run():
        cls = getattr(api(), 'DomoHttpClient', None)
        assert cls is not None, 'Bounded Domo HTTP client is missing'
        client = cls(source=source(), secret_resolver=secrets,
            transport=httpx.MockTransport(handler), clock=clock)
        assert calls == []
        try:
            token = await client.acquire_token(deadline=clock.now()+timedelta(seconds=30))
            assert token.value == 'private-token'
            assert 'private-token' not in repr(token)
            assert token.source_digest == source().digest
        finally: await client.close()
    asyncio.run(run())
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == 'https://api.domo.com/oauth/token'
    assert request.method == 'POST'
    assert request.content == b'grant_type=client_credentials&scope=data'
    assert request.headers['authorization'].startswith('Basic ')
    assert request.headers['accept-encoding'] == 'identity'


@pytest.mark.parametrize('case', ['customer','user','scope','missing_identity','expiry','bool_expiry',
    'token_type','extra','redirect','error','oversize','compression'])
def test_token_failures_never_query_or_expose_details(clock, case):
    import asyncio
    import httpx
    from datetime import timedelta
    calls = []
    payload = token_wire(); status = 200; headers = {}
    if case == 'customer': payload['customer'] = 'different'
    elif case == 'user': payload['userId'] = 456
    elif case == 'scope': payload['scope'] = 'data user'
    elif case == 'missing_identity': del payload['customer']
    elif case == 'expiry': payload['expires_in'] = 0
    elif case == 'bool_expiry': payload['expires_in'] = True
    elif case == 'token_type': payload['token_type'] = 'other'
    elif case == 'extra': payload['refresh_token'] = 'private-token'
    elif case == 'redirect': status = 302; headers['location'] = 'https://evil.example'
    elif case == 'error': status = 401
    elif case == 'oversize': payload['access_token'] = 'x'*17000
    elif case == 'compression': headers['content-encoding'] = 'gzip'
    async def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'content-type': 'application/json', **headers},
            stream=httpx.ByteStream(json.dumps(payload).encode()))
    async def run():
        cls = getattr(api(), 'DomoHttpClient', None)
        assert cls is not None, 'Bounded Domo HTTP client is missing'
        client = cls(source=source(), secret_resolver=secrets,
            transport=httpx.MockTransport(handler), clock=clock)
        try:
            with pytest.raises(GatewayError) as caught:
                await client.acquire_token(deadline=clock.now()+timedelta(seconds=30))
            assert 'private' not in str(caught.value)
        finally: await client.close()
    asyncio.run(run())
    assert len(calls) == 1 and calls[0].url.path == '/oauth/token'


@pytest.mark.parametrize('where', ['column','cell'])
def test_success_rejects_non_utf8_scalar_strings(where):
    value=wire()
    if where=='column': value['columns'][0]='\ud800'
    else: value['rows'][0][1]='\ud800'
    with pytest.raises(GatewayError): parse(value)


def test_token_deadline_bounds_stalled_stream_cleanup(clock):
    import asyncio
    import httpx
    import time
    from datetime import timedelta
    class Stalled(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b''
        async def aclose(self):
            await asyncio.Event().wait()
    async def handler(request):
        return httpx.Response(200,headers={'content-type':'application/json'},stream=Stalled())
    async def run():
        client=api().DomoHttpClient(source=source(),secret_resolver=secrets,
            transport=httpx.MockTransport(handler),clock=clock)
        client._budget=lambda deadline: .02
        started=time.monotonic()
        try:
            with pytest.raises(GatewayError) as caught:
                await asyncio.wait_for(client.acquire_token(deadline=clock.now()+timedelta(seconds=1)),timeout=.3)
            assert caught.value.code=='EXECUTION_TIMEOUT'
            assert time.monotonic()-started < .15
        finally: await client.close()
    asyncio.run(run())


def query_setup(valid_manifest,clock):
    from datetime import timedelta
    from gateway.source_lifecycle import SourceAttemptContext,SourceSessionSpec,DomoSendPermit
    from tests.contract.test_domo_runtime import setup
    a,s,b,p,q,d,c=setup(valid_manifest)
    plan=a.compile_domo_query(b,p,q,d,c,source=s)
    spec=SourceSessionSpec(provider='domo',source_id=s.source_id,source_revision=s.revision,
        context=s.context,capacity_key=s.capacity_pool_id,definition_digest=s.digest,
        request_digest=plan.query_digest,prepared_digest=plan.digest,scope_digest=plan.scope_digest,
        decision_digest=plan.decision_digest,binding_digest=plan.binding_digest,
        model_bundle_digest=plan.model_bundle_digest,catalog_digest=None,max_submissions=1,
        max_bytes_per_submission=None,max_total_bytes=None)
    context=SourceAttemptContext(group_id='00000000-0000-0000-0000-000000000010',
        session_id='00000000-0000-0000-0000-000000000011',generation=1,authority_stamp='d'*64,
        context_digest='e'*64,deadline=clock.now()+timedelta(seconds=30),spec=spec)
    permit=DomoSendPermit._from_committed(context,plan.digest,'private-nonce')
    statement=c.resolve(plan.statement_id,plan.statement_revision,plan.statement_digest)
    token=api().DomoToken('private-token',s.digest,clock.now()+timedelta(seconds=100))
    return s,plan,statement,token,permit


def test_query_requires_actual_single_use_committed_permit(valid_manifest,clock):
    import asyncio
    import httpx
    s,plan,statement,token,permit=query_setup(valid_manifest,clock)
    calls=[]
    async def handler(request):
        calls.append(request)
        return httpx.Response(200,headers={'content-type':'application/json'},
            stream=httpx.ByteStream(json.dumps(wire()).encode()))
    async def run():
        client=api().DomoHttpClient(source=s,secret_resolver=secrets,
            transport=httpx.MockTransport(handler),clock=clock)
        assert hasattr(client,'query'), 'Exact Domo query transport is missing'
        try:
            response=await client.query(plan=plan,statement=statement,token=token,permit=permit)
            assert response.dataset_id==DATASET
            assert permit._state=='started'  # HTTP alone cannot persist terminal proof.
            with pytest.raises(GatewayError):
                await client.query(plan=plan,statement=statement,token=token,permit=permit)
        finally: await client.close()
    asyncio.run(run())
    assert len(calls)==1
    assert calls[0].method=='POST' and str(calls[0].url)==f'https://api.domo.com/v1/datasets/query/execute/{DATASET}'
    assert json.loads(calls[0].content)=={'sql':statement.text}
    assert calls[0].headers['authorization']=='Bearer private-token'


@pytest.mark.parametrize('case',['permit','plan','statement','token_source','token_expiry','context_source'])
def test_query_preflight_drift_never_sends(valid_manifest,clock,case):
    import asyncio
    from dataclasses import replace
    import httpx
    from datetime import timedelta
    s,plan,statement,token,permit=query_setup(valid_manifest,clock)
    if case=='permit': permit=object()
    elif case=='plan': plan=plan.model_copy(update={'dataset_id':'00000000-0000-0000-0000-000000000002'})
    elif case=='statement': statement=statement.model_copy(update={'text':'SELECT secret'})
    elif case=='token_source': token=replace(token,source_digest='0'*64)
    elif case=='token_expiry': token=replace(token,expires_at=clock.now())
    elif case=='context_source': permit.context=replace(permit.context,spec=replace(permit.context.spec,source_id='other'))
    calls=[]
    async def handler(request): calls.append(request); raise AssertionError('Source I/O forbidden')
    async def run():
        client=api().DomoHttpClient(source=s,secret_resolver=secrets,transport=httpx.MockTransport(handler),clock=clock)
        assert hasattr(client,'query'), 'Exact Domo query transport is missing'
        try:
            with pytest.raises(GatewayError):
                await client.query(plan=plan,statement=statement,token=token,permit=permit)
        finally: await client.close()
    asyncio.run(run())
    assert calls==[]


@pytest.mark.parametrize('case',['401','500','redirect','malformed','wrong_dataset','oversize','connection'])
def test_query_unknown_outcomes_never_retry_or_refresh(valid_manifest,clock,case):
    import asyncio
    import httpx
    s,plan,statement,token,permit=query_setup(valid_manifest,clock)
    calls=[];payload=wire();status=200
    if case in {'401','500'}: status=int(case)
    if case=='redirect': status=302
    if case=='malformed': del payload['numRows']
    if case=='wrong_dataset': payload['datasource']='00000000-0000-0000-0000-000000000002'
    if case=='oversize': payload['rows'][0][1]='x'*plan.max_bytes
    async def handler(request):
        calls.append(request)
        if case=='connection': raise httpx.ConnectError('secret token in diagnostic',request=request)
        return httpx.Response(status,headers={'content-type':'application/json'},
            stream=httpx.ByteStream(json.dumps(payload).encode()))
    async def run():
        client=api().DomoHttpClient(source=s,secret_resolver=secrets,transport=httpx.MockTransport(handler),clock=clock)
        assert hasattr(client,'query'), 'Exact Domo query transport is missing'
        try:
            with pytest.raises(GatewayError) as caught:
                await client.query(plan=plan,statement=statement,token=token,permit=permit)
            assert 'secret' not in str(caught.value) and 'SELECT' not in str(caught.value)
            assert permit._state=='started'
        finally: await client.close()
    asyncio.run(run())
    assert len(calls)==1 and '/query/execute/' in calls[0].url.path


def test_token_origin_cannot_mutate_through_secret_resolver(clock):
    import asyncio
    import httpx
    from datetime import timedelta
    calls=[]
    async def handler(request):
        calls.append(request)
        return httpx.Response(200,headers={'content-type':'application/json'},
            stream=httpx.ByteStream(json.dumps(token_wire()).encode()))
    async def run():
        client=None
        def resolver(ref):
            object.__setattr__(client.source,'api_origin','https://unexpected.invalid')
            return secrets(ref)
        client=api().DomoHttpClient(source=source(),secret_resolver=resolver,
            transport=httpx.MockTransport(handler),clock=clock)
        try:
            await client.acquire_token(deadline=clock.now()+timedelta(seconds=30))
        finally: await client.close()
    asyncio.run(run())
    assert len(calls)==1 and str(calls[0].url)=='https://api.domo.com/oauth/token'


def test_wall_clock_rollback_does_not_refresh_token_to_query_deadline(valid_manifest,clock,monkeypatch):
    import asyncio
    import httpx
    from datetime import timedelta
    ticks=[0.0]
    monkeypatch.setattr(api(),'monotonic',lambda:ticks[0],raising=False)
    s,plan,statement,token,permit=query_setup(valid_manifest,clock)
    calls=[]
    async def handler(request):
        calls.append(request)
        return httpx.Response(200,headers={'content-type':'application/json'},
            stream=httpx.ByteStream(json.dumps(token_wire()).encode()))
    async def run():
        client=api().DomoHttpClient(source=s,secret_resolver=lambda ref:'client-id' if ref.endswith('/id') else 'private-secret',
            transport=httpx.MockTransport(handler),clock=clock)
        try:
            token=await client.acquire_token(deadline=permit.context.deadline)
            ticks[0]=31
            clock.instant-=timedelta(hours=1)
            with pytest.raises(GatewayError) as caught:
                await client.query(plan=plan,statement=statement,token=token,permit=permit)
            assert caught.value.code=='EXECUTION_TIMEOUT' and permit._state=='unused'
        finally: await client.close()
    asyncio.run(run())
    assert len(calls)==1 and calls[0].url.path=='/oauth/token'


def test_delayed_protocol_parsing_cannot_outlive_original_budget(valid_manifest,clock,monkeypatch):
    import asyncio
    import httpx
    ticks=[0.0]
    monkeypatch.setattr(api(),'monotonic',lambda:ticks[0],raising=False)
    original=api().parse_domo_success
    def delayed(*args,**kwargs):
        value=original(*args,**kwargs);ticks[0]=31;return value
    monkeypatch.setattr(api(),'parse_domo_success',delayed)
    s,plan,statement,token,permit=query_setup(valid_manifest,clock)
    async def handler(request):
        return httpx.Response(200,headers={'content-type':'application/json'},
            stream=httpx.ByteStream(json.dumps(wire()).encode()))
    async def run():
        client=api().DomoHttpClient(source=s,secret_resolver=secrets,transport=httpx.MockTransport(handler),clock=clock)
        try:
            with pytest.raises(GatewayError) as caught:
                await client.query(plan=plan,statement=statement,token=token,permit=permit)
            assert caught.value.code=='EXECUTION_TIMEOUT' and permit._state=='started'
        finally: await client.close()
    asyncio.run(run())


def test_deadline_expiry_precedes_permit_consumption(valid_manifest,clock):
    import asyncio
    import httpx
    from datetime import timedelta
    s,plan,statement,token,permit=query_setup(valid_manifest,clock)
    clock.instant+=timedelta(seconds=31)
    calls=[]
    async def handler(request): calls.append(request);raise AssertionError('No I/O')
    async def run():
        client=api().DomoHttpClient(source=s,secret_resolver=secrets,transport=httpx.MockTransport(handler),clock=clock)
        try:
            with pytest.raises(GatewayError) as caught:
                await client.query(plan=plan,statement=statement,token=token,permit=permit)
            assert caught.value.code=='EXECUTION_TIMEOUT' and permit._state=='unused'
        finally: await client.close()
    asyncio.run(run())
    assert calls==[]


def test_protocol_evidence_cannot_be_constructed_as_an_assertion():
    with pytest.raises(TypeError):
        api().DomoProtocolResult(DATASET,('secret',),('STRING',),(('value',),),'a'*64,'miss')


def test_hostile_protocol_column_is_not_in_repr():
    value=wire();value['columns'][0]='private-provider-column-sentinel'
    assert 'private-provider-column-sentinel' not in repr(parse(value))


def test_terminal_evidence_is_exact_client_response_and_permit_bound(valid_manifest,clock):
    import asyncio
    import httpx
    from gateway.contracts.canonical import canonical_digest
    s,plan,statement,token,permit=query_setup(valid_manifest,clock)
    _,_,_,_,other_permit=query_setup(valid_manifest,clock)
    raw=json.dumps(wire()).encode()
    async def handler(request):
        return httpx.Response(200,headers={'content-type':'application/json; charset=utf-8'},stream=httpx.ByteStream(raw))
    async def run():
        client=api().DomoHttpClient(source=s,secret_resolver=secrets,transport=httpx.MockTransport(handler),clock=clock)
        assert hasattr(client,'success_evidence'),'Exact terminal observation metadata is missing'
        try:
            result=await client.query(plan=plan,statement=statement,token=token,permit=permit)
            await client.close()
            evidence=client.success_evidence(result,permit)
            assert set(evidence)=={'plan_fingerprint','response_profile_digest','response_sha256','status_code',
                'media_type','completed_at','row_count','response_bytes'}
            assert evidence['status_code']==200 and evidence['media_type']=='application/json'
            assert evidence['response_bytes']==len(raw) and evidence['row_count']==1
            assert evidence['plan_fingerprint']==plan.digest
            assert evidence['response_profile_digest']==canonical_digest('domo-query-positional-v1')
            evidence['row_count']=999
            assert client.success_evidence(result,permit)['row_count']==1
            with pytest.raises(GatewayError): client.success_evidence(result,other_permit)
            with pytest.raises(GatewayError): client.success_evidence(parse(wire()),permit)
            object.__setattr__(result,'rows',(('2026-10-05','altered',1),))
            with pytest.raises(GatewayError): client.success_evidence(result,permit)
            from dataclasses import fields
            replacement_wire=wire();replacement_wire['rows'][0][1]='coherent-replacement'
            replacement=parse(replacement_wire)
            for item in fields(result):
                object.__setattr__(result,item.name,getattr(replacement,item.name))
            with pytest.raises(GatewayError): client.success_evidence(result,permit)
        finally: await client.close()
    asyncio.run(run())
