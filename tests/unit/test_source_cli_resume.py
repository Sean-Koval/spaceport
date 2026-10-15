"""Known execution resumption never repeats a source-dispatching POST."""
import json
import httpx
import pytest
from tests.unit.test_source_cli import client, private, api

pytestmark=pytest.mark.unit
REQUEST={'product_id':'usage','version':'1.0.0'}
ACCEPTED={'execution_id':'execution-one','state':'accepted','accepted_at':'2026-10-15T00:00:00Z'}


def test_known_execution_resume_gets_only_with_fresh_token(tmp_path):
    from scripts.spaceport_client import GatewayClient,ClientConfig
    calls=[]
    def handler(wire):
        calls.append(wire)
        return httpx.Response(200,json=ACCEPTED)
    operation=tmp_path/'operation'
    with client(tmp_path,handler) as cli:cli.query(REQUEST,operation_file=operation)
    token=private(tmp_path/'bearer','refreshed-token')
    with GatewayClient(ClientConfig(schema_version=1,gateway_origin='https://gateway.example.com'),
            token_file=token,transport=httpx.MockTransport(handler)) as cli:
        cli.query(REQUEST,operation_file=operation)
    assert [r.method for r in calls]==['POST','GET']
    assert calls[-1].url.path=='/v1/executions/execution-one'
    assert (tmp_path/'operation.execution.json').is_file()


def test_ambiguous_operation_requires_explicit_retry_same_token(tmp_path):
    calls=[]
    def handler(wire):
        calls.append(wire)
        raise httpx.ReadError('response lost')
    operation=tmp_path/'operation'
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]):cli.query(REQUEST,operation_file=operation)
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]):cli.query(REQUEST,operation_file=operation)
    assert len(calls)==1
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]):
        cli.query(REQUEST,operation_file=operation,retry_ambiguous=True)
    assert len(calls)==2
    assert json.loads(calls[0].content)==json.loads(calls[1].content)


@pytest.mark.parametrize('change',['corrupt','wrong-origin','wrong-operation','missing'])
def test_bad_sidecar_never_reposts(tmp_path,change):
    calls=[]
    def handler(wire):calls.append(wire);return httpx.Response(200,json=ACCEPTED)
    operation=tmp_path/'operation';sidecar=tmp_path/'operation.execution.json'
    with client(tmp_path,handler) as cli:cli.query(REQUEST,operation_file=operation)
    if change=='missing':sidecar.unlink()
    elif change=='corrupt':sidecar.write_text('{')
    else:
        data=json.loads(sidecar.read_text())
        data['gateway_origin' if change=='wrong-origin' else 'operation_digest']='https://evil.example.com' if change=='wrong-origin' else 'a'*64
        sidecar.write_text(json.dumps(data))
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]):cli.query(REQUEST,operation_file=operation)
    assert len(calls)==1


def test_orphan_sidecar_blocks_new_operation(tmp_path):
    calls=[]
    private(tmp_path/'operation.execution.json',{'unrelated':True})
    with client(tmp_path,lambda request:calls.append(request)) as cli,pytest.raises(api()[2]):
        cli.query(REQUEST,operation_file=tmp_path/'operation')
    assert calls==[] and not (tmp_path/'operation').exists()


def test_source_check_pending_resume_completes_without_second_post(tmp_path,monkeypatch,capsys):
    from scripts import spaceport
    from scripts.spaceport_client import GatewayClient
    from tests.unit.test_source_cli import product_description,result_page
    config=private(tmp_path/'config',{'schema_version':1,'gateway_origin':'https://gateway.example.com'})
    token=private(tmp_path/'token','first-token')
    plan=private(tmp_path/'plan',dict(schema_version=1,request=REQUEST,configured_source_id='usage-source',
        configured_source_digest='c'*64,configured_package_digest='d'*64,expected_product_digest='a'*64,
        expected_provenance=result_page([])['provenance'],expected_rows=[{'value':'one'},{'value':'two'}]))
    calls=[]
    def handler(wire):
        calls.append(wire)
        if '/products/' in wire.url.path:return httpx.Response(200,json=product_description())
        if wire.method=='POST':return httpx.Response(200,json=ACCEPTED)
        return httpx.Response(200,json=result_page([{'value':'one'},{'value':'two'}]))
    monkeypatch.setattr(spaceport,'GatewayClient',lambda cfg,**kw:GatewayClient(cfg,transport=httpx.MockTransport(handler),**kw))
    args=['source','check','--client-config',str(config),'--token-file',str(token),'--plan-file',str(plan),
        '--operation-file',str(tmp_path/'operation'),'--receipt-file']
    assert spaceport.main([*args,str(tmp_path/'pending')])==4
    pending=json.loads((tmp_path/'pending').read_text())
    assert pending['observed'] is None and pending['outcome_status']=='pending'
    assert json.loads(capsys.readouterr().out)['status']=='pending'
    private(token,'refreshed-token')
    assert spaceport.main([*args,str(tmp_path/'complete')])==0
    complete=json.loads((tmp_path/'complete').read_text())
    assert complete['observed']['known_answer_passed'] and complete['outcome_status']=='passed'
    assert [r.method for r in calls]==['GET','POST','GET','GET']


def test_initial_success_that_expires_on_read_is_failed_not_pending(tmp_path,monkeypatch):
    from scripts import spaceport
    from scripts.spaceport_client import GatewayClient
    config=private(tmp_path/'config',{'schema_version':1,'gateway_origin':'https://gateway.example.com'})
    token=private(tmp_path/'token','first-token');request=private(tmp_path/'request',REQUEST)
    def handler(wire):
        return httpx.Response(200,json={**ACCEPTED,'state':'succeeded','completed_at':'2026-10-15T00:00:01Z'} if wire.method=='POST'
            else {**ACCEPTED,'state':'expired'})
    monkeypatch.setattr(spaceport,'GatewayClient',lambda cfg,**kw:GatewayClient(cfg,transport=httpx.MockTransport(handler),**kw))
    assert spaceport.main(['product','query','--client-config',str(config),'--token-file',str(token),
        '--request-file',str(request),'--operation-file',str(tmp_path/'operation'),'--receipt-file',str(tmp_path/'receipt')])==1
    assert json.loads((tmp_path/'receipt').read_text())['state']=='expired'


def test_acceptance_persistence_failure_is_uncertain_and_never_reposts(tmp_path,monkeypatch):
    import scripts.spaceport_client as module
    from scripts.spaceport_files import FileError
    calls=[];writer=module.write_json
    def write(path,value):
        if str(path).endswith('.execution.json'):raise FileError()
        return writer(path,value)
    monkeypatch.setattr(module,'write_json',write)
    def handler(wire):calls.append(wire);return httpx.Response(200,json=ACCEPTED)
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]) as error:cli.query(REQUEST,operation_file=tmp_path/'operation')
    assert error.value.uncertain and len(calls)==1
    with client(tmp_path,handler) as cli,pytest.raises(api()[2]):cli.query(REQUEST,operation_file=tmp_path/'operation')
    assert len(calls)==1


def test_corrupt_sidecar_refuses_even_explicit_ambiguous_retry(tmp_path):
    def handler(wire):return httpx.Response(200,json=ACCEPTED)
    operation=tmp_path/'operation'
    with client(tmp_path,handler) as cli:cli.query(REQUEST,operation_file=operation)
    private(tmp_path/'operation.execution.json',{'schema_version':True})
    calls=[]
    with client(tmp_path,lambda wire:calls.append(wire)) as cli,pytest.raises(api()[2]):
        cli.query(REQUEST,operation_file=operation,retry_ambiguous=True)
    assert calls==[]
