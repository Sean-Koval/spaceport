"""Existing-API client behavior; provider transports are never involved."""
import importlib.util
import json
from pathlib import Path
import httpx
import pytest

pytestmark = pytest.mark.unit


def api():
    assert importlib.util.find_spec('scripts.spaceport_client') is not None, 'The supported source-pilot CLI client is missing'
    from scripts.spaceport_client import GatewayClient, ClientConfig, CLIError
    return GatewayClient, ClientConfig, CLIError


def private(path, value):
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(0o600)
    return path


def client(tmp_path, handler, **kwargs):
    GatewayClient, ClientConfig, _ = api()
    token = private(tmp_path/'bearer', 'sensitive-bearer-value')
    config = ClientConfig(schema_version=1, gateway_origin='https://gateway.example.com')
    return GatewayClient(config, token_file=token, transport=httpx.MockTransport(handler), **kwargs)


def draft_receipt():
    return dict(id='draft-one', product_id='revenue', revision=1, digest='a'*64)


def diff(run='test-one'):
    return dict(draft_id='draft-one', product_id='revenue', revision=1,
                latest_test_run_id=run,before_digest=None,after_digest='a'*64,changes=[],stale_evidence=[])


def test_save_uses_existing_contract_and_no_secrets_in_receipt(tmp_path, valid_manifest):
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.url == 'https://gateway.example.com/v1/drafts'
        assert request.headers['authorization'] == 'Bearer sensitive-bearer-value'
        assert set(json.loads(request.content)) == {'manifest','expected_revision'}
        assert json.loads(request.content)['expected_revision'] == 0
        return httpx.Response(200,json={**draft_receipt(),'digest':json.loads(request.content)['manifest']['content_digest']})
    with client(tmp_path,handler) as cli:
        receipt=cli.save(valid_manifest,expected_revision=0)
    assert receipt.product_id=='revenue' and receipt.revision==1
    assert len(calls)==1
    assert 'sensitive' not in repr(receipt)


def test_test_post_has_empty_body_and_returns_exact_test(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.method=='POST' and request.content==b''
        return httpx.Response(200,json=dict(id='test-one',draft_id='draft-one',draft_digest='a'*64,
            binding_digest='b'*64,policy_id='access',policy_revision=1,fixture_version='fixture-v1',
            status='passed',created_at='2026-10-15T00:00:00Z',checks=[dict(fixture_id='denied',kind='permission_negative',passed=True,
                expected_error='NOT_FOUND_OR_FORBIDDEN',actual_error='NOT_FOUND_OR_FORBIDDEN')]))
    with client(tmp_path,handler) as cli:
        assert cli.test('draft-one').id=='test-one'
    assert len(calls)==1


def test_publish_only_existing_fields_after_exact_preflight(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        if request.method=='GET':return httpx.Response(200,json=diff())
        assert json.loads(request.content)==dict(tested_digest='a'*64,expected_revision=1,reason='Reviewed exact draft')
        return httpx.Response(200,json=dict(product_id='revenue',version='1.0.0',version_id='version-one',digest='a'*64))
    with client(tmp_path,handler) as cli:
        receipt=cli.publish('draft-one',review={'schema_version':1,'gateway_origin':'https://gateway.example.com','review':diff()},reason='Reviewed exact draft')
    assert receipt.version=='1.0.0'
    assert [r.method for r in calls]==['GET','POST']


def test_publish_changed_evidence_refuses_before_mutation(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json=diff('new-test'))
    with client(tmp_path,handler) as cli, pytest.raises(api()[2]):
        cli.publish('draft-one',review={'schema_version':1,'gateway_origin':'https://gateway.example.com','review':diff()},reason='Reviewed exact draft')
    assert [r.method for r in calls]==['GET']


@pytest.mark.parametrize('gateway',['https://evil.example.com','http://gateway.example.com','https://gateway.example.com:8443','https://gateway.example.com/path'])
def test_origin_override_rejected_before_token_read(tmp_path,gateway):
    GatewayClient,ClientConfig,CLIError=api()
    with pytest.raises(CLIError):
        GatewayClient(ClientConfig(schema_version=1,gateway_origin='https://gateway.example.com'),
            token_file=tmp_path/'missing',gateway=gateway)


def test_redirect_refused_without_second_request(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(307,headers={'location':'https://evil.example.com/stolen'})
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]) as error:
        cli.review('draft-one')
    assert len(calls)==1 and 'stolen' not in str(error.value)


def test_transport_failure_mutation_not_retried_or_logged(tmp_path,valid_manifest):
    calls=[]
    def handler(request):
        calls.append(request)
        raise httpx.ReadError('sensitive-bearer-value')
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]) as error:
        cli.save(valid_manifest,expected_revision=0)
    assert len(calls)==1 and error.value.uncertain is True
    assert 'sensitive' not in str(error.value)


def test_query_operation_saved_before_send_and_reused(tmp_path):
    calls=[];operation=tmp_path/'operation.json'
    request=dict(product_id='revenue',version='1.0.0',inputs={'start':'2026-10-05','end':'2026-10-12'})
    def handler(wire):
        assert operation.is_file()
        state=json.loads(operation.read_text())
        if wire.method=='POST':
            body=json.loads(wire.content)
            assert state['request']==body
            calls.append(body)
        else:
            assert wire.url.path=='/v1/executions/execution-one'
        return httpx.Response(200,json=dict(execution_id='execution-one',state='accepted',accepted_at='2026-10-15T00:00:00Z'))
    with client(tmp_path,handler) as cli:
        cli.query(request,operation_file=operation)
        cli.query(request,operation_file=operation)
    assert len(calls)==1 and calls[0]['idempotency_key']
    assert operation.stat().st_mode & 0o077 == 0
    assert 'sensitive' not in operation.read_text()


def test_query_operation_changed_request_refused(tmp_path):
    calls=[]
    def handler(wire):
        calls.append(wire)
        return httpx.Response(200,json=dict(execution_id='execution-one',state='accepted',accepted_at='2026-10-15T00:00:00Z'))
    with client(tmp_path,handler) as cli:
        cli.query(dict(product_id='revenue',version='1.0.0',inputs={}),operation_file=tmp_path/'op')
        with pytest.raises(api()[2]):
            cli.query(dict(product_id='revenue',version='2.0.0',inputs={}),operation_file=tmp_path/'op')
    assert len(calls)==1


@pytest.mark.parametrize('malformed',['duplicate','oversized','invalid_schema','compressed'])
def test_hostile_success_response_rejected(tmp_path,malformed):
    def handler(_):
        if malformed=='duplicate':return httpx.Response(200,content=b'{"draft_id":"one","draft_id":"two"}')
        if malformed=='oversized':return httpx.Response(200,content=b' ' * (1048576+1))
        if malformed=='compressed':return httpx.Response(200,headers={'content-encoding':'gzip'},content=b'hostile')
        return httpx.Response(200,json={'secret':'sensitive-bearer-value'})
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]) as error:
        cli.review('draft-one')
    assert 'sensitive' not in str(error.value)


def test_symlink_or_world_readable_token_rejected(tmp_path):
    GatewayClient,ClientConfig,CLIError=api()
    config=ClientConfig(schema_version=1,gateway_origin='https://gateway.example.com')
    token=private(tmp_path/'token','sensitive')
    link=tmp_path/'link';link.symlink_to(token)
    with pytest.raises(CLIError):GatewayClient(config,token_file=link)
    token.chmod(0o644)
    with pytest.raises(CLIError):GatewayClient(config,token_file=token)


def test_success_wrong_content_type_rejected(tmp_path):
    def handler(_):return httpx.Response(200,content=json.dumps(diff()),headers={'content-type':'text/html'})
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]):
        cli.review('draft-one')


def test_review_receipt_cross_origin_rejected_before_publication(tmp_path):
    import scripts.spaceport_client as module
    assert hasattr(module,'ReviewReceipt'), 'Origin-pinned review receipt missing'
    receipt=module.ReviewReceipt(schema_version=1,gateway_origin='https://evil.example.com',review=diff())
    calls=[]
    with client(tmp_path,lambda r:calls.append(r)) as cli,pytest.raises(api()[2]):
        cli.publish('draft-one',review=receipt,reason='reviewed')
    assert calls==[]


def test_cli_module_and_safe_parser(capsys):
    assert importlib.util.find_spec('scripts.spaceport') is not None, 'Supported command entrypoint missing'
    from scripts.spaceport import main
    assert main(['product','save','--token','sensitive-bearer-value'])==2
    output=capsys.readouterr()
    assert 'sensitive' not in output.out+output.err


def test_query_cross_origin_resume_refuses_before_network(tmp_path):
    operation=tmp_path/'op'
    def handler(_):return httpx.Response(200,json=dict(execution_id='execution-one',state='accepted',accepted_at='2026-10-15T00:00:00Z'))
    with client(tmp_path,handler) as cli:
        cli.query(dict(product_id='revenue',version='1.0.0',inputs={}),operation_file=operation)
    saved=json.loads(operation.read_text());saved['gateway_origin']='https://other.example.com';private(operation,saved)
    calls=[]
    with client(tmp_path,lambda r:calls.append(r)) as cli,pytest.raises(api()[2]):
        cli.query(dict(product_id='revenue',version='1.0.0',inputs={}),operation_file=operation)
    assert calls==[]


def test_cli_save_and_review_write_origin_bound_private_receipts(tmp_path,valid_manifest,monkeypatch,capsys):
    from scripts import spaceport
    from scripts.spaceport_client import GatewayClient
    config=private(tmp_path/'client.json',{'schema_version':1,'gateway_origin':'https://gateway.example.com'})
    token=private(tmp_path/'session','secret-session-bearer')
    manifest=private(tmp_path/'manifest.json',valid_manifest)
    calls=[]
    def handler(request):
        calls.append(request)
        if request.method=='GET':return httpx.Response(200,json=diff())
        return httpx.Response(200,json={**draft_receipt(),'digest':json.loads(request.content)['manifest']['content_digest']})
    monkeypatch.setattr(spaceport,'GatewayClient',lambda cfg,**kw:GatewayClient(cfg,transport=httpx.MockTransport(handler),**kw))
    shared=['--client-config',str(config),'--token-file',str(token)]
    assert spaceport.main(['product','save',*shared,'--manifest',str(manifest),'--expected-revision','0','--receipt-file',str(tmp_path/'saved')])==0
    assert spaceport.main(['product','review',*shared,'--draft-id','draft-one','--receipt-file',str(tmp_path/'review')])==0
    review=json.loads((tmp_path/'review').read_text())
    assert review['gateway_origin']=='https://gateway.example.com' and review['review']['draft_id']=='draft-one'
    assert (tmp_path/'review').stat().st_mode & 0o077==0
    assert 'secret-session' not in capsys.readouterr().out
    assert len(calls)==2


def test_cli_existing_receipt_refuses_before_network(tmp_path,monkeypatch,capsys):
    from scripts import spaceport
    config=private(tmp_path/'client.json',{'schema_version':1,'gateway_origin':'https://gateway.example.com'})
    receipt=private(tmp_path/'receipt',{})
    calls=[]
    monkeypatch.setattr(spaceport,'GatewayClient',lambda *a,**kw:calls.append(1))
    assert spaceport.main(['product','review','--client-config',str(config),'--token-file',str(tmp_path/'missing'),
        '--draft-id','draft-one','--receipt-file',str(receipt)])==2
    assert calls==[]


def test_private_files_refuse_hardlinks_duplicate_keys_and_symlink_ancestors(tmp_path):
    import os
    from scripts.spaceport_files import read_json,write_json,FileError
    file=private(tmp_path/'file','{"duplicate":1,"duplicate":2}')
    with pytest.raises(FileError):read_json(file)
    private(file,{'ok':1});os.link(file,tmp_path/'hard')
    with pytest.raises(FileError):read_json(file)
    folder=tmp_path/'private';folder.mkdir(mode=0o700)
    alias=tmp_path/'alias';alias.symlink_to(folder,target_is_directory=True)
    with pytest.raises(FileError):write_json(alias/'receipt',{'ok':1})
    assert not (folder/'receipt').exists()


def test_query_operation_schema_rejects_forged_digest(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json=dict(execution_id='execution-one',state='accepted',accepted_at='2026-10-15T00:00:00Z'))
    operation=tmp_path/'operation'
    with client(tmp_path,handler) as cli:
        request=dict(product_id='revenue',version='1.0.0',inputs={})
        cli.query(request,operation_file=operation)
        raw=json.loads(operation.read_text());raw['request_digest']='forged';private(operation,raw)
        with pytest.raises(api()[2]):cli.query(request,operation_file=operation)
    assert len(calls)==1


def test_preview_result_identity_mismatch_refused(tmp_path):
    def handler(_):return httpx.Response(200,json=dict(execution_id='wrong',state='accepted',accepted_at='2026-10-15T00:00:00Z'))
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]):cli.result('execution-one')


def product_description():
    return dict(product_id='usage',version='1.0.0',digest='a'*64,name='Usage',status='published',
        inputs={},outputs={'value':{'type':'string'}},filters={},sort={'fields':[],'max_fields':0},units={},
        timezone='UTC',date_boundaries=None,freshness={'required':False,'max_age_seconds':86400,'meaning':'source_load'},
        provenance={'binding_id':'usage','binding_revision':1,'adapter_revision':'cube-rest-v2','model_bundle_digest':'b'*64,'links':[]},
        partial=False,limits={})


def result_page(rows, cursor=None, **overrides):
    return dict(execution_id='execution-one',state='succeeded',accepted_at='2026-10-15T00:00:00Z',
        started_at='2026-10-15T00:00:00Z',completed_at='2026-10-15T00:00:01Z',error=None,
        product_id='usage',version='1.0.0',product_digest='a'*64,
        typed_schema={'value':{'type':'string'}},rows=rows,units={},grain='daily',effective_range=None,timezone='UTC',
        provenance={'binding_id':'usage','binding_revision':1,'adapter_revision':'cube-rest-v2','model_bundle_digest':'b'*64,'links':[]},
        row_count=2,truncated=False,source_watermark=None,
        freshness={'status':'unknown','assessed_at':'2026-10-15T00:00:01Z','age_seconds':None,'max_age_seconds':86400},
        cache_status='unknown',warnings=[],next_cursor=cursor,**overrides)


def test_complete_pagination_checks_identity_and_all_rows(tmp_path):
    from scripts.spaceport import _complete_result
    from gateway.contracts.query import QueryRequest
    calls=[]
    def handler(request):
        calls.append(request)
        if request.url.params.get('cursor')=='page-two':return httpx.Response(200,json=result_page([{'value':'two'}]))
        return httpx.Response(200,json=result_page([{'value':'one'}],'page-two'))
    with client(tmp_path,handler) as cli:
        result=_complete_result(cli,'execution-one',QueryRequest(product_id='usage',version='1.0.0'))
    assert [row['value'] for row in result.rows]==['one','two'] and result.next_cursor is None
    assert len(calls)==2


def test_complete_pagination_rejects_changed_product_digest(tmp_path):
    from scripts.spaceport import _complete_result
    from gateway.contracts.query import QueryRequest
    def handler(request):
        page=result_page([{'value':'two'}]) if request.url.params else result_page([{'value':'one'}],'page-two')
        if request.url.params:page['product_digest']='c'*64
        return httpx.Response(200,json=page)
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]):
        _complete_result(cli,'execution-one',QueryRequest(product_id='usage',version='1.0.0'))


def test_source_check_receipt_labels_configured_and_observed_without_ledger_claims(tmp_path,monkeypatch,capsys):
    from scripts import spaceport
    from scripts.spaceport_client import GatewayClient
    config=private(tmp_path/'config',{'schema_version':1,'gateway_origin':'https://gateway.example.com'})
    token=private(tmp_path/'token','private-session-token')
    plan=private(tmp_path/'plan',dict(schema_version=1,request={'product_id':'usage','version':'1.0.0','inputs':{}},
        configured_source_id='usage-bq',configured_source_digest='c'*64,configured_package_digest='d'*64,
        expected_product_digest='a'*64,expected_provenance=result_page([])['provenance'],
        expected_rows=[{'value':'one'},{'value':'two'}]))
    def handler(request):
        if '/v1/products/' in request.url.path:return httpx.Response(200,json=product_description())
        if request.method=='POST':return httpx.Response(200,json=dict(execution_id='execution-one',state='succeeded',
            accepted_at='2026-10-15T00:00:00Z',started_at='2026-10-15T00:00:00Z',completed_at='2026-10-15T00:00:01Z'))
        return httpx.Response(200,json=result_page([{'value':'one'},{'value':'two'}]))
    monkeypatch.setattr(spaceport,'GatewayClient',lambda cfg,**kw:GatewayClient(cfg,transport=httpx.MockTransport(handler),**kw))
    assert spaceport.main(['source','check','--client-config',str(config),'--token-file',str(token),'--plan-file',str(plan),
        '--operation-file',str(tmp_path/'operation'),'--receipt-file',str(tmp_path/'receipt')])==0
    receipt=json.loads((tmp_path/'receipt').read_text())
    assert receipt['configured']['source_id']=='usage-bq'
    assert receipt['observed']['known_answer_passed'] is True
    assert set(receipt['observed'])=={'product_id','version','product_digest','provenance','known_answer_passed'}
    assert 'private-session' not in capsys.readouterr().out


def test_source_check_failed_known_answer_has_nonzero_exit(tmp_path,monkeypatch,capsys):
    from scripts import spaceport
    from scripts.spaceport_client import GatewayClient
    config=private(tmp_path/'config',{'schema_version':1,'gateway_origin':'https://gateway.example.com'})
    token=private(tmp_path/'token','private-session-token')
    plan=private(tmp_path/'plan',dict(schema_version=1,request={'product_id':'usage','version':'1.0.0','inputs':{}},
        configured_source_id='usage-bq',configured_source_digest='c'*64,configured_package_digest='d'*64,
        expected_product_digest='a'*64,expected_provenance=result_page([])['provenance'],
        expected_rows=[{'value':'wrong'}]))
    def handler(request):
        if '/v1/products/' in request.url.path:return httpx.Response(200,json=product_description())
        if request.method=='POST':return httpx.Response(200,json=dict(execution_id='execution-one',state='succeeded',
            accepted_at='2026-10-15T00:00:00Z',started_at='2026-10-15T00:00:00Z',completed_at='2026-10-15T00:00:01Z'))
        return httpx.Response(200,json=result_page([{'value':'one'},{'value':'two'}]))
    monkeypatch.setattr(spaceport,'GatewayClient',lambda cfg,**kw:GatewayClient(cfg,transport=httpx.MockTransport(handler),**kw))
    assert spaceport.main(['source','check','--client-config',str(config),'--token-file',str(token),'--plan-file',str(plan),
        '--operation-file',str(tmp_path/'operation'),'--receipt-file',str(tmp_path/'receipt')])==1
    assert json.loads((tmp_path/'receipt').read_text())['observed']['known_answer_passed'] is False


def test_query_resume_changed_token_refuses_new_principal_execution(tmp_path):
    operation=tmp_path/'operation'
    calls=[]
    GatewayClient,ClientConfig,CLIError=api()
    config=ClientConfig(schema_version=1,gateway_origin='https://gateway.example.com')
    token=private(tmp_path/'token','first-employee-bearer')
    def handler(request):
        calls.append(request)
        raise httpx.ReadError('lost receipt')
    query=dict(product_id='revenue',version='1.0.0',inputs={})
    with GatewayClient(config,token_file=token,transport=httpx.MockTransport(handler)) as cli,pytest.raises(CLIError):
        cli.query(query,operation_file=operation,retry_ambiguous=True)
    private(token,'another-employee-bearer')
    with GatewayClient(config,token_file=token,transport=httpx.MockTransport(handler)) as cli,pytest.raises(CLIError):
        cli.query(query,operation_file=operation,retry_ambiguous=True)
    assert len(calls)==1


@pytest.mark.parametrize('field,replacement',[
    ('units',{'value':{'unit':'changed'}}),('grain','changed'),('timezone','America/New_York'),
    ('effective_range',{'start':'2026-10-05','end':'2026-10-12','interval':'[start,end)'})])
def test_complete_pagination_rejects_metadata_drift(tmp_path,field,replacement):
    from scripts.spaceport import _complete_result
    from gateway.contracts.query import QueryRequest
    def handler(request):
        page=result_page([{'value':'two'}]) if request.url.params else result_page([{'value':'one'}],'page-two')
        if request.url.params:page[field]=replacement
        return httpx.Response(200,json=page)
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]):
        _complete_result(cli,'execution-one',QueryRequest(product_id='usage',version='1.0.0'))


def test_empty_continuation_page_refused_before_next_request(tmp_path):
    from scripts.spaceport import _complete_result
    from gateway.contracts.query import QueryRequest
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json=result_page([],'more'))
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]):
        _complete_result(cli,'execution-one',QueryRequest(product_id='usage',version='1.0.0'))
    assert len(calls)==1


def test_total_deadline_stops_before_next_page(tmp_path):
    from scripts.spaceport import _complete_result
    from gateway.contracts.query import QueryRequest
    now=[0.0];calls=[]
    def handler(request):
        calls.append(request);now[0]=11.0
        return httpx.Response(200,json=result_page([{'value':'one'}],'more'))
    with client(tmp_path,handler,deadline_seconds=10,monotonic=lambda:now[0]) as cli,pytest.raises(api()[2]):
        _complete_result(cli,'execution-one',QueryRequest(product_id='usage',version='1.0.0'))
    assert len(calls)==1


def test_pagination_uses_last_freshness_assessment(tmp_path):
    from scripts.spaceport import _complete_result
    from gateway.contracts.query import QueryRequest
    def handler(request):
        page=result_page([{'value':'two'}]) if request.url.params else result_page([{'value':'one'}],'more')
        page['source_watermark']='2026-10-14T00:00:02Z'
        if request.url.params:
            page['freshness']={'status':'stale','assessed_at':'2026-10-15T00:00:03Z','age_seconds':86401,'max_age_seconds':86400}
            page['warnings']=['Recorded source data is stale.']
        else:
            page['freshness']={'status':'fresh','assessed_at':'2026-10-15T00:00:01Z','age_seconds':86399,'max_age_seconds':86400}
        return httpx.Response(200,json=page)
    with client(tmp_path,handler) as cli:
        result=_complete_result(cli,'execution-one',QueryRequest(product_id='usage',version='1.0.0'))
    assert result.freshness.status=='stale' and result.freshness.age_seconds==86401
    assert result.warnings==('Recorded source data is stale.',)


def test_query_resume_changed_source_plan_refuses_relabel(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json=dict(execution_id='execution-one',state='accepted',accepted_at='2026-10-15T00:00:00Z'))
    with client(tmp_path,handler) as cli:
        request=dict(product_id='revenue',version='1.0.0',inputs={})
        cli.query(request,operation_file=tmp_path/'operation',context_digest='a'*64)
        with pytest.raises(api()[2]):
            cli.query(request,operation_file=tmp_path/'operation',context_digest='b'*64)
    assert len(calls)==1


@pytest.mark.parametrize('kind',['review','query','check'])
def test_local_file_versions_reject_boolean(kind):
    from scripts.spaceport_client import ReviewReceipt,QueryOperation
    from scripts.spaceport import SourceCheckPlan
    values={
        'review':(ReviewReceipt,dict(gateway_origin='https://gateway.example.com',review=diff())),
        'query':(QueryOperation,dict(gateway_origin='https://gateway.example.com',request_digest='a'*64,
            credential_fingerprint='b'*64,request={'product_id':'usage','version':'1.0.0'})),
        'check':(SourceCheckPlan,dict(request={'product_id':'usage','version':'1.0.0'},configured_package_digest='a'*64,
            configured_source_digest='b'*64,configured_source_id='usage',expected_product_digest='c'*64,
            expected_provenance=result_page([])['provenance'],expected_rows=[]))}
    cls,raw=values[kind]
    with pytest.raises(ValueError):cls.model_validate({'schema_version':True,**raw})


def test_real_total_timeout_interrupts_drip_response(tmp_path):
    import asyncio,time
    calls=[];closed=[]
    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{'
            await asyncio.sleep(0.2)
            yield b'}'
        async def aclose(self):closed.append(True)
    def handler(request):
        calls.append(request)
        return httpx.Response(200,headers={'content-type':'application/json'},stream=Drip())
    started=time.monotonic()
    with client(tmp_path,handler,deadline_seconds=0.02) as cli,pytest.raises(api()[2]):
        cli.review('draft-one')
    assert time.monotonic()-started<0.5 and len(calls)==1 and closed


@pytest.mark.parametrize('stage',['parse','cleanup'])
def test_deadline_checked_after_parse_and_cleanup(tmp_path,monkeypatch,stage):
    import scripts.spaceport_client as module
    now=[0.0]
    original=module.parse_json
    if stage=='parse':
        def delayed(data):
            result=original(data);now[0]=11.0;return result
        monkeypatch.setattr(module,'parse_json',delayed)
    class Payload(httpx.AsyncByteStream):
        async def __aiter__(self):yield json.dumps(diff()).encode()
        async def aclose(self):
            if stage=='cleanup':now[0]=11.0
    def handler(request):return httpx.Response(200,headers={'content-type':'application/json'},stream=Payload())
    with client(tmp_path,handler,deadline_seconds=10,monotonic=lambda:now[0]) as cli,pytest.raises(api()[2]):
        cli.review('draft-one')


@pytest.mark.parametrize('stage',['preflight-product','preflight-binding','result'])
def test_source_check_pin_drift_refused_before_spend_or_after_race(tmp_path,monkeypatch,stage):
    from scripts import spaceport
    from scripts.spaceport_client import GatewayClient
    config=private(tmp_path/'config',{'schema_version':1,'gateway_origin':'https://gateway.example.com'})
    token=private(tmp_path/'token','private-session-token')
    plan=private(tmp_path/'plan',dict(schema_version=1,request={'product_id':'usage','version':'1.0.0'},
        configured_source_id='usage-bq',configured_source_digest='c'*64,configured_package_digest='d'*64,
        expected_product_digest='a'*64,expected_provenance=result_page([])['provenance'],
        expected_rows=[{'value':'one'},{'value':'two'}]))
    calls=[]
    def handler(request):
        calls.append(request)
        if '/v1/products/' in request.url.path:
            data=product_description()
            if stage=='preflight-product':data['digest']='f'*64
            if stage=='preflight-binding':data['provenance']['binding_revision']=2
            return httpx.Response(200,json=data)
        if request.method=='POST':return httpx.Response(200,json=dict(execution_id='execution-one',state='succeeded',
            accepted_at='2026-10-15T00:00:00Z',started_at='2026-10-15T00:00:00Z',completed_at='2026-10-15T00:00:01Z'))
        data=result_page([{'value':'one'},{'value':'two'}]);data['product_digest']='f'*64
        return httpx.Response(200,json=data)
    monkeypatch.setattr(spaceport,'GatewayClient',lambda cfg,**kw:GatewayClient(cfg,transport=httpx.MockTransport(handler),**kw))
    assert spaceport.main(['source','check','--client-config',str(config),'--token-file',str(token),'--plan-file',str(plan),
        '--operation-file',str(tmp_path/'operation'),'--receipt-file',str(tmp_path/'receipt')])==2
    assert sum(r.method=='POST' for r in calls)==(1 if stage=='result' else 0)
    assert not (tmp_path/'receipt').exists()


def test_operation_fsync_failure_cannot_remove_key_used_by_concurrent_reader(tmp_path,monkeypatch):
    import os
    operation=tmp_path/'operation';calls=[];interleaved=[False]
    GatewayClient,ClientConfig,CLIError=api()
    config=ClientConfig(schema_version=1,gateway_origin='https://gateway.example.com')
    token=private(tmp_path/'token','same-employee-bearer')
    query=dict(product_id='revenue',version='1.0.0',inputs={})
    def handler(request):
        if request.method=='POST':calls.append(json.loads(request.content)['idempotency_key'])
        return httpx.Response(200,json=dict(execution_id='execution-one',state='accepted',accepted_at='2026-10-15T00:00:00Z'))
    fsync=os.fsync
    def interrupted(fd):
        if operation.exists() and not interleaved[0]:
            interleaved[0]=True
            with GatewayClient(config,token_file=token,transport=httpx.MockTransport(handler)) as second:
                second.query(query,operation_file=operation,retry_ambiguous=True)
            raise OSError('simulated uncertain fsync')
        return fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os,'fsync',interrupted)
        with GatewayClient(config,token_file=token,transport=httpx.MockTransport(handler)) as first,pytest.raises(CLIError):
            first.query(query,operation_file=operation)
    assert interleaved[0] and len(calls)==1 and operation.exists()
    with GatewayClient(config,token_file=token,transport=httpx.MockTransport(handler)) as third:
        third.query(query,operation_file=operation)
    assert len(calls)==1 and len(set(calls))==1


def test_resume_requires_file_and_directory_fsync_before_post(tmp_path,monkeypatch):
    import os
    operation=tmp_path/'operation';calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json=dict(execution_id='execution-one',state='accepted',accepted_at='2026-10-15T00:00:00Z'))
    query=dict(product_id='revenue',version='1.0.0',inputs={})
    with client(tmp_path,handler) as cli:cli.query(query,operation_file=operation)
    def fail(_):raise OSError('durability unavailable')
    monkeypatch.setattr(os,'fsync',fail)
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]):cli.query(query,operation_file=operation)
    assert len(calls)==1 and operation.exists()


def test_exclusive_writer_collision_preserves_published_operation(tmp_path):
    from scripts.spaceport_files import write_json,read_json,FileError
    path=tmp_path/'operation'
    write_json(path,{'winner':1})
    with pytest.raises(FileError):write_json(path,{'loser':2})
    assert read_json(path)=={'winner':1}
    assert list(tmp_path.iterdir())==[path]


def test_writer_never_cleans_an_existing_temporary_on_name_collision(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import scripts.spaceport_files as files
    existing=tmp_path/'.spaceport-tmp-fixed';existing.write_text('other writer')
    monkeypatch.setattr(files,'uuid4',lambda:SimpleNamespace(hex='fixed'))
    with pytest.raises(files.FileError):files.write_json(tmp_path/'operation',{'our':1})
    assert existing.read_text()=='other writer'


def test_crash_temporary_cannot_be_used_as_operation(tmp_path):
    from scripts.spaceport_files import read_json,FileError
    temporary=private(tmp_path/'.spaceport-tmp-interrupted',{'looks':'complete'})
    with pytest.raises(FileError):read_json(temporary,private=True,durable=True)


def test_failure_before_publication_never_posts_or_leaves_operation(tmp_path,monkeypatch):
    import os
    calls=[]
    def handler(request):calls.append(request)
    def fail(_):raise OSError('file durability failure')
    monkeypatch.setattr(os,'fsync',fail)
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]):
        cli.query(dict(product_id='usage',version='1.0.0'),operation_file=tmp_path/'operation')
    assert not (tmp_path/'operation').exists() and calls==[]
    assert not list(tmp_path.glob('.spaceport-tmp-*'))


@pytest.mark.parametrize('stage,returncode',[('before-link',71),('after-link',72),('before-directory-fsync',73)])
def test_real_process_crash_publication_boundaries(tmp_path,stage,returncode):
    import hashlib,subprocess,sys
    from gateway.contracts.canonical import canonical_digest
    from gateway.contracts.query import QueryRequest
    from scripts.spaceport_client import QueryOperation
    operation=tmp_path/'operation';calls=[]
    request=QueryRequest(product_id='usage',version='1.0.0')
    state=QueryOperation(gateway_origin='https://gateway.example.com',request_digest=canonical_digest(request),
        credential_fingerprint=hashlib.sha256(b'spaceport-cli-session-v1\0sensitive-bearer-value').hexdigest(),
        request=request.model_copy(update={'idempotency_key':'crash-pinned-key'}))
    program=r'''
import json,os,stat,sys
from scripts.spaceport_files import write_json
stage,path=sys.argv[1:]
link,fsync=os.link,os.fsync
def interrupted_link(*args,**kwargs):
    if stage=='before-link':os._exit(71)
    link(*args,**kwargs)
    if stage=='after-link':os._exit(72)
def interrupted_fsync(fd):
    if stage=='before-directory-fsync' and stat.S_ISDIR(os.fstat(fd).st_mode) and os.path.exists(path):os._exit(73)
    fsync(fd)
os.link=interrupted_link
os.fsync=interrupted_fsync
write_json(path,json.load(sys.stdin))
'''
    child=subprocess.run([sys.executable,'-c',program,stage,str(operation)],input=json.dumps(state.model_dump(mode='json')),
        text=True,capture_output=True,timeout=10)
    assert child.returncode==returncode and child.stdout=='' and child.stderr==''
    def handler(wire):
        calls.append(json.loads(wire.content)['idempotency_key'])
        return httpx.Response(200,json=dict(execution_id='execution-one',state='accepted',accepted_at='2026-10-15T00:00:00Z'))
    if stage=='before-link':
        assert not operation.exists() and list(tmp_path.glob('.spaceport-tmp-*'))
    elif stage=='after-link':
        assert operation.stat().st_nlink==2
        with client(tmp_path,handler) as cli,pytest.raises(api()[2]):
            cli.query(request.model_dump(mode='json'),operation_file=operation)
    else:
        assert operation.stat().st_nlink==1
        with client(tmp_path,handler) as cli:
            cli.query(request.model_dump(mode='json'),operation_file=operation,retry_ambiguous=True)
        assert calls==['crash-pinned-key']
    if stage!='before-directory-fsync':assert calls==[]

@pytest.mark.parametrize('mode',[0o640,0o644,0o604])
def test_client_destination_config_must_be_private_before_token_or_http(tmp_path,monkeypatch,mode):
    from scripts import spaceport,spaceport_client
    config=private(tmp_path/'client.json',{'schema_version':1,'gateway_origin':'https://untrusted.example.com'})
    config.chmod(mode)
    monkeypatch.setattr(spaceport_client,'read_bytes',lambda *a,**k:pytest.fail('Token must not be read'))
    monkeypatch.setattr(spaceport_client.httpx,'AsyncClient',lambda *a,**k:pytest.fail('HTTP must not start'))
    assert spaceport.main(['product','review','--client-config',str(config),'--token-file',str(tmp_path/'token'),
        '--draft-id','draft-one','--receipt-file',str(tmp_path/'receipt')])==2


def test_client_destination_config_rejects_foreign_owner(tmp_path,monkeypatch):
    from scripts.spaceport_client import ClientConfig,CLIError
    from scripts import spaceport_files
    config=private(tmp_path/'client.json',{'schema_version':1,'gateway_origin':'https://gateway.example.com'})
    actual=spaceport_files.os.fstat
    def foreign(fd):
        value=actual(fd)
        if value.st_ino==config.stat().st_ino:
            from types import SimpleNamespace
            fields={name:getattr(value,name) for name in dir(value) if name.startswith('st_')}
            fields['st_uid']=value.st_uid+1
            return SimpleNamespace(**fields)
        return value
    monkeypatch.setattr(spaceport_files.os,'fstat',foreign)
    with pytest.raises(CLIError):ClientConfig.from_file(config)


def test_client_destination_config_requires_private_parent(tmp_path):
    from scripts.spaceport_client import ClientConfig,CLIError
    parent=tmp_path/'shared';parent.mkdir(mode=0o700)
    # CI intentionally uses umask077; create the unsafe fixture explicitly.
    parent.chmod(0o755)
    assert parent.stat().st_mode & 0o777 == 0o755
    config=private(parent/'client.json',{'schema_version':1,'gateway_origin':'https://gateway.example.com'})
    with pytest.raises(CLIError):ClientConfig.from_file(config)
    parent.chmod(0o700)
    assert ClientConfig.from_file(config).gateway_origin=='https://gateway.example.com'
