import copy
import json
import pytest
from tests.integration.source_runtime_image_gate import CHECKS, PREFIX, receipt, validate_identity
pytestmark=pytest.mark.contract


def value():
    return dict(schema_version=1,status='passed',versions=dict(cube_server='1.8.5',cube_core='1.8.5',cube_bigquery_driver='1.8.5',
        bigquery_sdk='7.7.0',compute_auth='10.2.1',bigquery_auth='9.10.0',pg='8.18.0'),node='v24.21.0',uid=65532,checks=CHECKS)


def test_image_receipt_discards_chatter_and_retains_exact_package_inventory():
    assert receipt('Untrusted output is discarded\n'+PREFIX+json.dumps(value())+'\n')==value()


@pytest.mark.parametrize('mutate',[
    lambda v:v.update(extra=True),lambda v:v.update(uid=0),lambda v:v.update(schema_version=True),
    lambda v:v.update(node='v25.0.0'),lambda v:v.update(status='failed'),lambda v:v.update(checks=[]),
    lambda v:v['versions'].update(bigquery_sdk='8.0.0'),lambda v:v['versions'].update(pg='9.0.0'),
    lambda v:v['versions'].update(compute_auth='11.0.0'),lambda v:v['versions'].update(cube_server='1.8.6'),
])
def test_image_receipt_is_closed_and_fails_unsupported_package_families(mutate):
    v=copy.deepcopy(value());mutate(v)
    with pytest.raises(ValueError):receipt(PREFIX+json.dumps(v))


@pytest.mark.parametrize('output',['',PREFIX+'{}',PREFIX+'{"schema_version":1,"schema_version":1}', 'x'*65537], ids=['missing', 'malformed', 'duplicate', 'oversized'])
def test_image_receipt_rejects_missing_duplicate_malformed_or_oversize(output):
    with pytest.raises(ValueError):receipt(output)


def test_image_receipt_cannot_be_duplicated():
    line=PREFIX+json.dumps(value())+'\n'
    with pytest.raises(ValueError):receipt(line+line)


def test_image_invocation_accepts_only_current_run_private_identity(monkeypatch):
    label='gateway-cube-'+'a'*16+'-local';image='gateway-cube-'+'a'*16+'-bigquery:local'
    monkeypatch.setenv('GATEWAY_CUBE_RUN_LABEL',label)
    validate_identity(image,label)
    for target in ['cubejs/cube:latest',image.replace('a','b'),image+'-extra']:
        with pytest.raises(ValueError):validate_identity(target,label)


def test_image_helper_leaves_cleanup_margin_inside_outer_thirty_seconds():
    import inspect
    from tests.integration import source_runtime_image_gate as G
    assert inspect.signature(G._bounded).parameters['timeout'].default <= 20


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    import os
    import sys
    from tests.integration import source_runtime_image_gate as G
    script=tmp_path/'docker';log=tmp_path/'commands.jsonl';pid=tmp_path/'run.pid'
    script.write_text('#!'+sys.executable+'\n'+'''import json,os,sys,time
from pathlib import Path
a=sys.argv[1:]
with open(os.environ['FAKE_DOCKER_LOG'],'a') as f:f.write(json.dumps(a)+'\\n')
mode=os.environ['FAKE_DOCKER_MODE']
if a[0]=='run':
 Path(a[a.index('--cidfile')+1]).write_text('a'*64)
 Path(os.environ['FAKE_DOCKER_PID']).write_text(str(os.getpid()))
 if mode=='stall':time.sleep(60)
 elif mode=='oversize':print('x'*70000,flush=True);time.sleep(60)
 elif mode=='malformed':print('private-noise\\nGATEWAY_BIGQUERY_IMAGE_RESULT {}')
 else:print('private-noise\\n'+os.environ['FAKE_DOCKER_RECEIPT'])
elif a[0]=='rm' and mode=='rm_failure':sys.exit(1)
elif a[0]=='ps':pass
''')
    script.chmod(0o700)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    monkeypatch.setenv('FAKE_DOCKER_LOG',str(log));monkeypatch.setenv('FAKE_DOCKER_PID',str(pid))
    monkeypatch.setenv('FAKE_DOCKER_RECEIPT',PREFIX+json.dumps(value()))
    label='gateway-cube-'+'a'*16+'-local';image='gateway-cube-'+'a'*16+'-bigquery:local'
    monkeypatch.setenv('GATEWAY_CUBE_RUN_LABEL',label)
    return G,image,label,log,pid


@pytest.mark.parametrize('mode',['success','rm_failure'])
def test_image_actual_child_success_and_exact_cid_cleanup(fake_docker,monkeypatch,mode):
    import asyncio
    G,image,label,log,pid=fake_docker;monkeypatch.setenv('FAKE_DOCKER_MODE',mode)
    assert asyncio.run(G.execute(image,label))==value()
    commands=[json.loads(line) for line in log.read_text().splitlines()]
    assert commands[0][0]=='run' and commands[0][-1]=='/cube/probe/bigquery-serving-image-smoke.cjs'
    assert commands[1]==['rm','-f','a'*64]
    if mode=='rm_failure':assert commands[2]==['ps','--all','--no-trunc','--filter','id='+'a'*64,'--format','{{.ID}}']
    else:assert len(commands)==2


def test_image_actual_child_malformed_receipt_is_cleaned_without_raw_output(fake_docker,monkeypatch,capsys):
    G,image,label,log,pid=fake_docker;monkeypatch.setenv('FAKE_DOCKER_MODE','malformed')
    monkeypatch.setattr('sys.argv',['image-gate','--image',image,'--run-label',label])
    assert G.main()==1
    output=capsys.readouterr().out
    assert 'private-noise' not in output and output.startswith('GATEWAY_FAILURE_EVIDENCE ')
    assert [json.loads(line) for line in log.read_text().splitlines()][-1]==['rm','-f','a'*64]


@pytest.mark.parametrize('mode',['oversize','stall'])
def test_image_actual_child_bounds_terminate_and_cleanup(fake_docker,monkeypatch,mode):
    import asyncio
    import os
    G,image,label,log,pid=fake_docker;monkeypatch.setenv('FAKE_DOCKER_MODE',mode)
    original=G._bounded
    async def faster(command,timeout=20,maximum=65536):
        return await original(command,timeout=min(timeout,.5),maximum=maximum)
    monkeypatch.setattr(G,'_bounded',faster)
    with pytest.raises((ValueError,TimeoutError)):asyncio.run(G.execute(image,label))
    with pytest.raises(ProcessLookupError):os.kill(int(pid.read_text()),0)
    assert [json.loads(line) for line in log.read_text().splitlines()][-1]==['rm','-f','a'*64]


def test_image_exited_parent_holding_descendant_is_killed(tmp_path):
    import asyncio
    import os
    import signal
    import sys
    import time
    from tests.integration import source_runtime_image_gate as G
    pidfile = tmp_path / 'descendant.pid'
    script = tmp_path / 'fork.py'
    script.write_text("import os,time\nfrom pathlib import Path\npid=os.fork()\nif pid: os._exit(0)\nPath(" + repr(str(pidfile)) + ").write_text(str(os.getpid()))\ntime.sleep(60)\n")
    descendant = None
    try:
        with pytest.raises(TimeoutError):
            asyncio.run(G._bounded([sys.executable, str(script)], timeout=.5))
        descendant = int(pidfile.read_text())
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            status = __import__('pathlib').Path(f'/proc/{descendant}/stat')
            if not status.exists() or status.read_text().split()[2] == 'Z': break
            time.sleep(.01)
        else: pytest.fail('Exited parent left a live stdout-holding descendant')
    finally:
        if descendant is not None:
            try: os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError: pass
