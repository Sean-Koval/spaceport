"""Offline orchestration controls, never evidence of a private runtime pass."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def harness():
    # Missing implementation must be a test failure, not collection failure.
    assert (ROOT / 'scripts/gateway_integration.py').is_file(), 'Integration orchestrator is missing'
    return importlib.import_module('scripts.gateway_integration')


def test_modes_are_explicit_and_have_exact_argument_arity(harness):
    for args in ([], ['--other', '/tmp/x'], ['--backend'], ['--demo', '/tmp/a', 'extra']):
        with pytest.raises(SystemExit) as error:
            harness.parse_args(args)
        assert error.value.code == 2
    assert harness.parse_args(['--backend', '/tmp/run']).mode == 'backend'
    assert harness.parse_args(['--demo', '/tmp/run']).mode == 'demo'


def test_paths_reject_existing_relative_repo_and_symlink_ancestors(harness, tmp_path):
    private = tmp_path / 'private'; private.mkdir(mode=0o700)
    link = tmp_path / 'linked'; link.symlink_to(private, target_is_directory=True)
    for path in (Path('relative'), private, ROOT / 'new-state', link / 'child', private / '..' / 'escaped'):
        with pytest.raises(ValueError):
            harness.validate_new_directory(path)
    assert harness.validate_new_directory(private / 'receipts', private_parent=True) == private / 'receipts'
    private.chmod(0o755)
    with pytest.raises(ValueError):
        harness.validate_new_directory(private / 'receipts', private_parent=True)


def test_phase_budget_refuses_work_that_cannot_finish_and_keeps_cleanup_reserve(harness, monkeypatch):
    monkeypatch.setattr(harness.time, 'monotonic', lambda: 100.0)
    budget = harness.Budget()
    assert budget.remaining() == 1080
    monkeypatch.setattr(harness.time, 'monotonic', lambda: 851.0)
    assert budget.remaining() == 329
    with pytest.raises(harness.Blocked, match='budget'):
        budget.admit(330)
    assert budget.admit(120) == 120


def write_junit(path, *, name='test_a', skipped=False):
    path.write_text('<testsuites><testsuite tests="1" errors="0" failures="0" skipped="'+str(int(skipped))+'">'
        '<testcase classname="tests.integration.test_registry" name="'+name+'">'+('<skipped/>' if skipped else '')+
        '</testcase></testsuite></testsuites>')


def test_backend_junit_requires_complete_collected_identity_set(harness, tmp_path):
    output=tmp_path/'backends.xml'; write_junit(output)
    expected={('tests.integration.test_registry','test_a')}
    assert harness.validate_backend_junit(output,expected)==1
    with pytest.raises(ValueError,match='identity'):
        harness.validate_backend_junit(output,expected|{('tests.security.test_matrix','test_b')})
    write_junit(output,skipped=True)
    with pytest.raises(ValueError):harness.validate_backend_junit(output,expected)


def test_collection_rejects_empty_duplicate_and_unexpected_lines(harness):
    good='tests/integration/test_registry.py::test_a\n\n1 test collected in 0.01s\n'
    assert harness.parse_collection(good)=={('tests.integration.test_registry','test_a')}
    for source in ('',good+good,'ERROR collecting tests/integration/test_registry.py'):
        with pytest.raises(ValueError):harness.parse_collection(source)


def test_failure_summary_never_contains_raw_diagnostics(harness, tmp_path):
    program = tmp_path / 'private-program.py'
    program.write_text('print("PRIVATE_SENTINEL");raise SystemExit(7)')
    report = harness.run_phase('sample', [sys.executable, str(program)],
        tmp_path, harness.Budget(), 5)
    assert report['exit_status']==7 and report['status']=='BLOCKED'
    assert 'PRIVATE_SENTINEL' not in json.dumps(report)
    assert 'PRIVATE_SENTINEL' in (tmp_path/'sample.log').read_text()
    assert report['command']==[sys.executable,str(program)]
    assert report['started_at'] and report['finished_at']


def test_timeout_retains_explicit_status_and_reaps_process(harness,tmp_path):
    report=harness.run_phase('timeout',[sys.executable,'-c','import time;time.sleep(10)'],tmp_path,harness.Budget(),.05)
    assert report['status']=='BLOCKED' and report['exit_status']==124
    assert report['timed_out'] is True


def test_source_change_prevents_acceptance(harness,tmp_path,monkeypatch):
    receipt={'classification':'actual','source_digest':'a','images':{},'junit':'backends.xml','sha256':'bad','tests':1}
    (tmp_path/'backends.json').write_text(json.dumps(receipt))
    monkeypatch.setattr(harness,'current_source',lambda:{'source_digest':'b'})
    with pytest.raises(ValueError):harness.validate_backend_report(tmp_path,{})


def test_phase_output_limit_fails_closed_and_is_bounded(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'MAX_LOG_BYTES', 1024)
    row = harness.run_phase('flood', [sys.executable, '-c', 'print("x"*10000)'], tmp_path, harness.Budget(), 5)
    assert row['status'] == 'BLOCKED' and row['exit_status'] == 125
    assert row['output_limit_exceeded'] is True
    assert (tmp_path / 'flood.log').stat().st_size <= 1024


def test_unaffordable_phase_is_recorded_without_starting_process(harness,tmp_path):
    row=harness.run_phase('unaffordable',[sys.executable,'-c','raise SystemExit(0)'],tmp_path,harness.Budget(1),5)
    assert row['status']=='BLOCKED' and row['reason']=='insufficient_remaining_phase_budget'
    assert row['exit_status'] is None
    assert not (tmp_path/'unaffordable.log').exists()


def test_cleanup_registry_rejects_forged_unbounded_resource_labels(harness,tmp_path):
    for project,label in [('production','anything'),('gateway-cube-'+ 'a'*16,'')]:
        with pytest.raises(ValueError):harness.register_compatibility(tmp_path,'cube',project,label)
    project='gateway-cube-'+ 'a'*16
    harness.register_compatibility(tmp_path,'cube',project,project+'-local')
    row=json.loads((tmp_path/'cube-cleanup.json').read_text())
    assert row=={'kind':'cube','project':project,'run_label':project+'-local'}


def test_demo_runs_fresh_repeat_after_blocked_observer_without_reacquisition(harness,tmp_path,monkeypatch):
    calls=[]
    state={'source_inventory':{},'source_digest':'source'}
    monkeypatch.setattr(harness.runtime,'load_state',lambda path:state)
    monkeypatch.setattr(harness.runtime,'verify_images',lambda state:None)
    monkeypatch.setattr(harness.verify_demo,'prerequisite_images',lambda state:{})
    monkeypatch.setattr(harness.verify_demo,'validate_prerequisites',lambda *args:None)
    monkeypatch.setattr(harness,'cleanup_compatibility',lambda root:None)
    def execute(name,command,root,budget,seconds,env=None):
        calls.append((name,command,env))
        return {'phase':name,'status':'BLOCKED' if name.startswith(('verify-state','evidence-state')) else 'PASS'}
    monkeypatch.setattr(harness,'run_phase',execute)
    monkeypatch.setattr(harness,'browser_evidence',lambda root:{'status':'PASS','executable':'/prepared/chromium'})
    monkeypatch.setattr(harness,'validate_demo_report',lambda state:{'status':'BLOCKED','missing':['offline_egress']})
    rows=[];harness.demo_cycles(tmp_path,rows,harness.Budget())
    names=[name for name,_,_ in calls]
    assert names.index('compatibility-prerequisites') < names.index('start-state-a')
    assert names.index('verify-state-a') < names.index('stop-state-a') < names.index('prepare-state-b-offline') < names.index('verify-state-b')
    assert [name for name in names if name.endswith('dependencies')]==['publisher-dependencies']
    assert not any(command[:3]==['npm','--prefix','clients'] for _,command,_ in calls)
    for name,command,env in calls[names.index('start-state-a'):]:
        assert env['UV_OFFLINE']=='1' and env['npm_config_offline']=='true'
        assert not ({'install','ci','pull','build'} & set(command))
    assert '--integration' in calls[names.index('verify-state-b')][1]


def test_cleanup_continues_through_failed_restored_state_and_preserves_image_owner_last(harness,tmp_path,monkeypatch):
    for name in ('state-a','state-b','state-a/restored','state-b/restored'):
        path=tmp_path/name;path.mkdir(mode=0o700);(path/'cleanup.json').write_text('{}')
    monkeypatch.setattr(harness,'cleanup_compatibility',lambda root:None)
    monkeypatch.setattr(harness.runtime,'load_cleanup_state',lambda path:{'name':str(path.relative_to(tmp_path))})
    calls=[]
    def clean(state):
        calls.append(state['name'])
        if state['name']=='state-b/restored':raise RuntimeError('unavailable')
        return {'status':'PASS'}
    monkeypatch.setattr(harness.runtime,'cleanup',clean)
    with pytest.raises(harness.Blocked):harness.cleanup_run(tmp_path)
    assert calls==['state-b/restored','state-a/restored','state-b','state-a']
    assert json.loads((tmp_path/'cleanup.json').read_text())['status']=='BLOCKED'


def test_backend_receipt_rejects_changed_and_partial_test_sets(harness,tmp_path,monkeypatch):
    import hashlib
    output=tmp_path/'backends.xml';write_junit(output)
    source={'git_commit':'a'*40,'source_digest':'b'}
    monkeypatch.setattr(harness,'current_source',lambda:source)
    expected={('tests.integration.test_registry','test_a')}
    monkeypatch.setattr(harness,'collect_backend_identities',lambda:expected)
    receipt={'classification':'actual','status':'PASS','source':source,'images':{},'junit':'backends.xml',
        'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'tests':1}
    (tmp_path/'backends.json').write_text(json.dumps(receipt))
    assert harness.validate_backend_report(tmp_path,{})['tests']==1
    monkeypatch.setattr(harness,'collect_backend_identities',lambda:expected|{('tests.security.test_matrix','test_b')})
    with pytest.raises(ValueError):harness.validate_backend_report(tmp_path,{})
    monkeypatch.setattr(harness,'collect_backend_identities',lambda:expected)
    output.write_text(output.read_text()+'\n')
    with pytest.raises(ValueError):harness.validate_backend_report(tmp_path,{})


def test_demo_admission_rejects_downgrade_to_ordinary_mode(harness,tmp_path,monkeypatch):
    root=tmp_path/'evidence'/'verify-one';root.mkdir(parents=True)
    state={'state_directory':str(tmp_path),'source_digest':'s','app_image':'a','edge_image':'e'}
    (root/'report.json').write_text(json.dumps({'source':'s','versions':{'integration':False,'app_image':'a','edge_image':'e'},'suites':[]}))
    with pytest.raises(ValueError,match='integration'):
        harness.validate_demo_report(state)


def test_preflight_failure_still_records_exact_cleanup_and_no_acquisition(harness,tmp_path,monkeypatch):
    def blocked(mode):raise harness.Blocked('private detail must not leak')
    monkeypatch.setattr(harness,'preflight',blocked)
    calls=[]
    def execute(name,*args,**kwargs):
        calls.append(name)
        return {'phase':name,'status':'PASS','exit_status':0}
    monkeypatch.setattr(harness,'run_phase',execute)
    report=harness.integrate('demo',tmp_path/'run')
    assert report['status']=='BLOCKED' and calls==['exact-cleanup']
    assert 'private detail' not in json.dumps(report)
    assert json.loads((tmp_path/'run/integration.json').read_text())==report


@pytest.mark.parametrize('normal_exit,new_session', [(False, False), (True, False), (True, True)])
def test_all_owned_descendants_are_reaped_even_across_timeout_groups(harness,tmp_path,normal_exit,new_session):
    import signal
    target=tmp_path/'pid'
    sleeper=tmp_path/'sleeper.py'
    sleeper.write_text('import os,time,pathlib;pathlib.Path('+repr(str(target))+').write_text(str(os.getpid()));time.sleep(30)')
    if normal_exit:
        code=('import subprocess,sys,time;subprocess.Popen([sys.executable,'+repr(str(sleeper))+'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session='+repr(new_session)+');time.sleep(.1)')
        command=[sys.executable,'-c',code];seconds=3
    else:
        command=['bash','-c','timeout --signal=TERM --kill-after=5s 30s "$1" "$2"; :','test',sys.executable,str(sleeper)];seconds=.3
    row=harness.run_phase('descendants',command,tmp_path,harness.Budget(),seconds)
    pid=int(target.read_text());alive=False
    try:
        try:os.kill(pid,0);alive=True
        except ProcessLookupError:pass
        assert not alive, 'Owned descendant survived phase completion'
        assert row['status']=='BLOCKED'
    finally:
        if alive:
            try:os.kill(pid,signal.SIGKILL)
            except ProcessLookupError:pass


@pytest.mark.parametrize('key,value',[('PYTEST_ADDOPTS','-k test_publish_race_commits_exactly_one_version'),('PYTEST_PLUGINS','unreviewed_selector')])
def test_ambient_pytest_selectors_cannot_redefine_backend_union(harness,monkeypatch,key,value):
    monkeypatch.setenv(key,value)
    with pytest.raises((ValueError,harness.Blocked),match='pytest|Pytest|PYTEST'):
        harness.collect_backend_identities()


def test_cleanup_is_repeatable_and_replaces_blocked_result_only_after_fresh_scan(harness,tmp_path,monkeypatch):
    scans=[]
    def scan(root):
        scans.append(root)
        if len(scans)==1:raise RuntimeError('first scan unavailable')
    monkeypatch.setattr(harness,'cleanup_compatibility',scan)
    with pytest.raises(harness.Blocked):harness.cleanup_run(tmp_path)
    assert json.loads((tmp_path/'cleanup.json').read_text())['status']=='BLOCKED'
    harness.cleanup_run(tmp_path);harness.cleanup_run(tmp_path)
    assert len(scans)==3
    assert json.loads((tmp_path/'cleanup.json').read_text())['status']=='PASS'


def test_cleanup_result_rejects_symlink_without_touching_destination(harness,tmp_path,monkeypatch):
    monkeypatch.setattr(harness,'cleanup_compatibility',lambda root:None)
    other=tmp_path/'other';other.write_text('preserve')
    (tmp_path/'cleanup.json').symlink_to(other)
    with pytest.raises(ValueError):harness.cleanup_run(tmp_path)
    assert other.read_text()=='preserve'


def test_job_budget_accounts_for_setup_and_reserves_shared_cleanup_window(harness,monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    monkeypatch.setenv('GATEWAY_INTEGRATION_FIRST_STEP_EPOCH','1000')
    monkeypatch.setattr(harness.time,'time',lambda:1300)
    allowance=harness.job_budget()
    assert allowance['work_seconds']==720
    assert allowance['cleanup_deadline_epoch']==2140
    assert allowance['setup_elapsed_seconds']==300
    assert allowance['runner_margin_seconds']==60
    monkeypatch.setenv('GATEWAY_INTEGRATION_FIRST_STEP_EPOCH','1400')
    with pytest.raises(harness.Blocked):harness.job_budget()
    monkeypatch.delenv('GATEWAY_INTEGRATION_FIRST_STEP_EPOCH')
    with pytest.raises(harness.Blocked):harness.job_budget()


def test_pid_reuse_is_never_permission_to_signal_another_process(monkeypatch):
    from scripts import runtime_process as process
    signalled=[];closed=[]
    monkeypatch.setattr(process.os,'pidfd_open',lambda pid:17)
    monkeypatch.setattr(process,'process_info',lambda pid:(1,'new-start','S'))
    monkeypatch.setattr(process.signal,'pidfd_send_signal',lambda *args:signalled.append(args))
    monkeypatch.setattr(process.os,'close',closed.append)
    process.signal_exact(123,'old-start',15)
    assert signalled==[] and closed==[17]


def test_required_supervision_capability_fails_closed_before_child_start(tmp_path,monkeypatch):
    from scripts import runtime_process as process
    monkeypatch.setattr(process.sys,'platform','unsupported')
    with pytest.raises(RuntimeError,match='supervision'):
        process.bounded_process([sys.executable,'-c','raise SystemExit(0)'],tmp_path/'output',timeout=1)
    assert not (tmp_path/'output').exists()


def test_outer_termination_relays_to_nested_supervisor_and_reaps_child(tmp_path):
    import signal,time
    target=tmp_path/'child-pid';program=tmp_path/'caller.py';log=tmp_path/'caller.log'
    sleeper='import os,time,pathlib;pathlib.Path('+repr(str(target))+').write_text(str(os.getpid()));time.sleep(30)'
    program.write_text('from scripts.runtime_process import bounded_process\nfrom pathlib import Path\nimport sys\n'
        'bounded_process([sys.executable,"-c",'+repr(sleeper)+'],Path('+repr(str(log))+'),timeout=30)\n')
    caller=subprocess.Popen([sys.executable,str(program)],cwd=ROOT,env={**os.environ,'PYTHONPATH':str(ROOT)},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    pid=None
    try:
        deadline=time.monotonic()+5
        while not target.exists() and time.monotonic()<deadline:time.sleep(.01)
        pid=int(target.read_text())
        caller.send_signal(signal.SIGTERM)
        assert caller.wait(timeout=8)==143
        with pytest.raises(ProcessLookupError):os.kill(pid,0)
    finally:
        if caller.poll() is None:caller.kill();caller.wait()
        if pid is not None:
            try:os.kill(pid,signal.SIGKILL)
            except ProcessLookupError:pass


def test_units_gate_rejects_zero_exit_partial_or_skipped_junit(harness,tmp_path,monkeypatch):
    expected={('tests.unit.test_a','test_a'),('tests.unit.test_b','test_b')}
    monkeypatch.setattr(harness,'collect_test_identities',lambda selection:expected)
    def execute(rows,name,command,root,budget,seconds,env=None):
        (root/'units-contracts.xml').write_text('<testsuites><testsuite tests="1" errors="0" failures="0" skipped="0"><testcase classname="tests.unit.test_a" name="test_a"/></testsuite></testsuites>')
        rows.append({'phase':name,'status':'PASS'})
    monkeypatch.setattr(harness,'required_phase',execute)
    with pytest.raises(ValueError,match='identity'):
        harness.verify_units(tmp_path,[],harness.Budget())


def test_persisted_cleanup_deadline_cannot_gain_a_new_window(harness,tmp_path,monkeypatch):
    (tmp_path/'budget.json').write_text(json.dumps({'first_step_epoch':1000,'cleanup_deadline_epoch':2140}))
    monkeypatch.setattr(harness.time,'time',lambda:2125)
    assert harness.cleanup_remaining(tmp_path)==15
    monkeypatch.setattr(harness.time,'time',lambda:2130)
    with pytest.raises(harness.Blocked):harness.cleanup_remaining(tmp_path)
    (tmp_path/'budget.json').write_text(json.dumps({'first_step_epoch':1000,'cleanup_deadline_epoch':2300}))
    with pytest.raises(ValueError):harness.cleanup_remaining(tmp_path)


@pytest.mark.parametrize('operation,number',[('open',38),('open',1),('signal',38),('signal',1)])
def test_real_pidfd_capability_denial_starts_no_command(tmp_path,monkeypatch,operation,number):
    from scripts import runtime_process as process
    target=tmp_path/'must-not-start'
    def unavailable(*args,**kwargs):raise OSError(number,'injected unavailable capability')
    if operation=='open':monkeypatch.setattr(process.os,'pidfd_open',unavailable)
    else:monkeypatch.setattr(process.signal,'pidfd_send_signal',unavailable)
    command=[sys.executable,'-c','from pathlib import Path;Path('+repr(str(target))+').write_text("started")']
    with pytest.raises(RuntimeError,match='supervision|capability'):
        process.bounded_process(command,tmp_path/'output',timeout=.2)
    assert not target.exists()


def test_transient_descendant_signal_error_does_not_abandon_owned_child(tmp_path,monkeypatch):
    from scripts import runtime_process as process
    import errno,signal
    original=process.os.pidfd_open;failed=[];target=tmp_path/'pid'
    def interrupted_open(pid,*args,**kwargs):
        if pid!=os.getpid() and not failed:
            failed.append(pid);raise OSError(errno.EINTR,'injected interruption')
        return original(pid,*args,**kwargs)
    monkeypatch.setattr(process.os,'pidfd_open',interrupted_open)
    command=[sys.executable,'-c','import os,time;from pathlib import Path;Path('+repr(str(target))+').write_text(str(os.getpid()));time.sleep(30)']
    pid=None
    try:
        result=process.bounded_process(command,tmp_path/'output',timeout=.2)
        pid=int(target.read_text())
        assert result['exit_status']!=0 and result['descendant_cleanup'] is True
        with pytest.raises(ProcessLookupError):os.kill(pid,0)
    finally:
        if target.exists():
            try:os.kill(int(target.read_text()),signal.SIGKILL)
            except ProcessLookupError:pass


@pytest.mark.parametrize('payload',['not-json',''])
def test_malformed_supervisor_result_is_explicit_failure_after_tree_reaped(tmp_path,monkeypatch,payload):
    from scripts import runtime_process as process
    import signal
    target=tmp_path/'pid'
    monkeypatch.setattr(process.json,'dumps',lambda value:payload)
    command=[sys.executable,'-c','import os,time;from pathlib import Path;Path('+repr(str(target))+').write_text(str(os.getpid()));time.sleep(30)']
    try:
        with pytest.raises(RuntimeError,match='supervisor result'):
            process.bounded_process(command,tmp_path/'output',timeout=.2)
        with pytest.raises(ProcessLookupError):os.kill(int(target.read_text()),0)
    finally:
        if target.exists():
            try:os.kill(int(target.read_text()),signal.SIGKILL)
            except ProcessLookupError:pass


@pytest.mark.parametrize('prctl_result',[-1,0])
def test_process_local_prctl_must_be_verified_before_launch(tmp_path,monkeypatch,prctl_result):
    from scripts import runtime_process as process
    class UnavailablePrctl:
        def prctl(self,*args):return prctl_result
    monkeypatch.setattr(process.ctypes,'CDLL',lambda *args,**kwargs:UnavailablePrctl())
    target=tmp_path/'must-not-start'
    command=[sys.executable,'-c','from pathlib import Path;Path('+repr(str(target))+').write_text("started")']
    result=process.bounded_process(command,tmp_path/'output',timeout=.2)
    assert result['exit_status']!=0 and result['descendant_cleanup'] is True
    assert not target.exists()


def test_unverified_descendant_cleanup_is_fatal_to_later_phases(harness,tmp_path,monkeypatch):
    from scripts import runtime_process as process
    monkeypatch.setattr(process,'bounded_process',lambda *args,**kwargs:{'exit_status':126,'descendant_cleanup':False})
    with pytest.raises(harness.Blocked,match='later phases'):
        harness.run_phase('unverified',[sys.executable],tmp_path,harness.Budget(),1)


def test_backend_outer_allowance_uses_remaining_budget_and_observed_overhead(harness,monkeypatch):
    budget=harness.Budget();monkeypatch.setattr(budget,'remaining',lambda:850)
    assert harness.backend_phase_allowance(budget)==840
    monkeypatch.setattr(budget,'remaining',lambda:1000)
    assert harness.backend_phase_allowance(budget)==900
    monkeypatch.setattr(budget,'remaining',lambda:789)
    with pytest.raises(harness.Blocked,match='compatibility'):
        harness.backend_phase_allowance(budget)


@pytest.mark.parametrize('mode',['backend','demo'])
@pytest.mark.parametrize('acquisition_status',[0,7])
def test_clean_checkout_acquires_clients_once_before_unit_contract_gate(harness,tmp_path,monkeypatch,mode,acquisition_status):
    from types import SimpleNamespace
    calls=[]
    monkeypatch.setattr(harness,'preflight',lambda mode:None)
    monkeypatch.setattr(harness,'current_source',lambda:{'source_digest':'fixed'})
    monkeypatch.setattr(harness.runtime,'run',lambda *args,**kwargs:SimpleNamespace(stdout='fixed'))
    def phase(name,command,*args,**kwargs):
        calls.append((name,command))
        return {'phase':name,'status':'BLOCKED' if name=='client-dependencies' and acquisition_status else 'PASS','exit_status':acquisition_status if name=='client-dependencies' else 0}
    monkeypatch.setattr(harness,'run_phase',phase)
    def units(root,rows,budget):
        assert acquisition_status==0, 'A failed dependency acquisition must prevent units'
        assert [name for name,_ in calls]==['client-dependencies'], 'Clean checkout needs locked client dependencies before unit/contracts'
        calls.append(('units',[]))
        raise harness.Blocked('Stop this offline ordering control before backend or demo work')
    monkeypatch.setattr(harness,'verify_units',units)
    report=harness.integrate(mode,tmp_path/'fresh-run')
    assert report['status']=='BLOCKED'
    clients=[command for name,command in calls if name=='client-dependencies']
    assert clients==[['npm','--prefix','clients','ci','--ignore-scripts','--no-audit','--no-fund']]
    assert ('units',[]) in calls if acquisition_status==0 else ('units',[]) not in calls
    assert calls[-1][0]=='exact-cleanup'


@pytest.mark.parametrize('mode',['backend','demo'])
@pytest.mark.parametrize('wrong_tool',['node','npm'])
def test_both_modes_require_exact_host_node_and_npm_before_acquisition(harness,monkeypatch,mode,wrong_tool):
    from types import SimpleNamespace
    monkeypatch.setattr(harness.runtime,'require_docker',lambda:None)
    def run(command,**kwargs):
        if command[0]=='git':return SimpleNamespace(stdout='')
        if command[0]==wrong_tool:return SimpleNamespace(stdout='wrong-version')
        return SimpleNamespace(stdout='v24.19.0' if command[0]=='node' else '11.9.0')
    monkeypatch.setattr(harness.runtime,'run',run)
    with pytest.raises(harness.Blocked,match='Exact Node|Exact npm'):
        harness.preflight(mode)
