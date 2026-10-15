"""Closed CI orchestration probes; no hosted/runtime acceptance claim."""
import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def driver():
    assert (ROOT / 'scripts/ci_partition_driver.py').is_file()
    return importlib.import_module('scripts.ci_partition_driver')


def test_controls_are_closed_and_caps_unchanged(driver):
    assert driver.SOURCE_SECONDS == 180
    assert driver.BACKEND_SECONDS == 570
    assert driver.CHILD_SECONDS == 600
    assert driver.CLEANUP_SECONDS == 120
    assert driver.JOB_SECONDS == 1200
    for mode in ('other', 'backend', 'demo', '--eval'):
        with pytest.raises(SystemExit):
            driver.parse_args([mode])


def test_candidate_cannot_be_controller_root_or_symlink(driver, tmp_path):
    candidate = tmp_path / 'candidate'; candidate.mkdir()
    link = tmp_path / 'link'; link.symlink_to(candidate)
    assert driver.checked_directory(candidate) == candidate
    for path in (Path('relative'), link, candidate/'..'):
        with pytest.raises(ValueError): driver.checked_directory(path)
    with pytest.raises(ValueError): driver.require_separate_roots(ROOT, ROOT)


def test_workload_environment_cannot_write_github_control_files(driver):
    env = {'PATH': '/usr/bin', 'PYTHONPATH':'hostile', 'PYTHONHOME':'hostile',
        'GITHUB_OUTPUT':'secret-output', 'GITHUB_ENV':'secret-env', 'GITHUB_PATH':'secret-path',
        'ACTIONS_RUNTIME_TOKEN':'secret-token', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN':'secret-oidc',
        'GATEWAY_TEST_POSTGRES_ADMIN_URL':'synthetic-db', 'CI':'true'}
    safe = driver.workload_environment(env)
    assert safe['GATEWAY_TEST_POSTGRES_ADMIN_URL'] == 'synthetic-db'
    assert safe['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] == '1'
    assert not set(env).intersection(safe).intersection({'PYTHONPATH','PYTHONHOME','GITHUB_OUTPUT',
        'GITHUB_ENV','GITHUB_PATH','ACTIONS_RUNTIME_TOKEN','ACTIONS_ID_TOKEN_REQUEST_TOKEN'})


@pytest.mark.parametrize('name', ['PYTEST_ADDOPTS','PYTEST_PLUGINS'])
def test_scope_overrides_are_rejected(driver, name):
    with pytest.raises(ValueError): driver.workload_environment({name:'evil'})


def test_inline_helper_code_must_be_exact_trusted_here_document(driver):
    sources = driver.trusted_inline_sources()
    assert sources
    assert 'import os; os.system("arbitrary")\n' not in sources
    with pytest.raises(ValueError): driver.check_inline_source('import os; os.system("arbitrary")\n')
    assert driver.check_inline_source(next(iter(sources))) is None


def test_private_json_rejects_duplicate_keys_and_symlinks(driver, tmp_path):
    path=tmp_path/'receipt.json';path.write_text('{"x":1,"x":2}');path.chmod(0o600)
    with pytest.raises(ValueError):driver.read_private(path)
    path.write_text('{"x":1}')
    link=tmp_path/'link';link.symlink_to(path)
    with pytest.raises(ValueError):driver.read_private(link)


def test_budget_reserves_cleanup_and_margin(driver):
    assert driver.work_remaining(100, now=100) == 1020
    assert driver.work_remaining(100, now=1119) == 1
    assert driver.cleanup_remaining(100, now=1220) == 20
    with pytest.raises(ValueError):driver.work_remaining(100,now=99)
    with pytest.raises(ValueError):driver.work_remaining(100,now=1120)
    with pytest.raises(ValueError):driver.cleanup_remaining(100,now=1240)


def test_source_identity_requires_exact_sha_and_clean_checkout(driver, tmp_path):
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    subprocess.run(['git','-C',str(tmp_path),'config','user.name','CI Test'],check=True)
    subprocess.run(['git','-C',str(tmp_path),'config','user.email','ci@example.invalid'],check=True)
    (tmp_path/'file').write_text('a')
    subprocess.run(['git','-C',str(tmp_path),'add','file'],check=True)
    subprocess.run(['git','-C',str(tmp_path),'commit','-qm','seed'],check=True)
    sha=subprocess.check_output(['git','-C',str(tmp_path),'rev-parse','HEAD'],text=True).strip()
    assert driver.git_identity(tmp_path,sha)['commit']==sha
    with pytest.raises(ValueError):driver.git_identity(tmp_path,'0'*40)
    (tmp_path/'file').write_text('changed')
    with pytest.raises(ValueError):driver.git_identity(tmp_path,sha)


def test_isolated_loader_ignores_candidate_scripts_and_sitecustomize(driver,tmp_path):
    (tmp_path/'scripts').mkdir()
    (tmp_path/'scripts/__init__.py').write_text('raise RuntimeError("candidate import")')
    (tmp_path/'sitecustomize.py').write_text('raise RuntimeError("candidate site")')
    command=[sys.executable,'-I','-S',str(ROOT/'scripts/ci_partition_driver.py'),'controller-check']
    result=subprocess.run(command,cwd=tmp_path,env={**os.environ,'PYTHONPATH':str(tmp_path)},
        capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='trusted-controller-ok'


def test_bootstrap_dependency_path_cannot_resolve_into_candidate(driver,tmp_path,monkeypatch):
    trusted=tmp_path/'trusted';trusted.mkdir()
    candidate=tmp_path/'candidate';candidate.mkdir()
    site=candidate/'lib/python3.12/site-packages';site.mkdir(parents=True)
    (trusted/'.venv').symlink_to(candidate)
    monkeypatch.setattr(driver,'TRUSTED_ROOT',trusted)
    with pytest.raises(ValueError,match='Candidate dependencies'):
        driver.trusted_dependencies(candidate)


@pytest.mark.parametrize('environment', [{'X':'a'*(128*1024)}, {f'X{i}':'a'*32768 for i in range(33)}])
def test_transport_environment_has_single_value_and_total_bounds(driver,environment):
    with pytest.raises(ValueError):driver.check_environment_budget(environment)


def test_real_workload_collection_preserves_exact_markers_without_executing_tests(driver,tmp_path):
    import json
    candidate=tmp_path/'candidate';candidate.mkdir()
    (candidate/'tests').mkdir()
    (candidate/'tests/test_tiny.py').write_text('import pytest\n'
        '@pytest.mark.unit\ndef test_source():\n    raise AssertionError("must not execute")\n'
        '@pytest.mark.postgres\ndef test_backend():\n    raise AssertionError("must not execute")\n')
    output=tmp_path/'inventory.json'
    result=subprocess.run([sys.executable,'-I',str(ROOT/'scripts/ci_partition_driver.py'),
        'workload-collect','--candidate',str(candidate),'--output',str(output),'--selection','backend'],
        env=driver.workload_environment(),capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    rows=driver.read_private(output)
    assert [r['nodeid'] for r in rows]==['tests/test_tiny.py::test_backend']
    assert rows[0]['markers']==['postgres']


def test_private_byte_reader_checks_size_before_loading(driver,tmp_path):
    target=tmp_path/'large';target.write_bytes(b'x'*100);target.chmod(0o600)
    with pytest.raises(ValueError):driver.private_bytes(target,50)
    assert driver.private_bytes(target,100)==b'x'*100


def test_source_proof_is_checked_before_any_workload_or_backend_preparation(driver,tmp_path,monkeypatch):
    from types import SimpleNamespace
    root=tmp_path/'run';root.mkdir(mode=0o700)
    candidate=tmp_path/'candidate';candidate.mkdir()
    ctx={'job_id':'sql_owners','started':1}
    events=[]
    monkeypatch.setattr(driver,'initialize',lambda args:(candidate,root,ctx))
    monkeypatch.setattr(driver,'modules',lambda candidate:(None,None,None))
    def denied(*args):raise ValueError('Missing source proof')
    monkeypatch.setattr(driver,'admit_source',denied)
    monkeypatch.setattr(driver,'observed_tools',lambda *_:events.append('tools'))
    monkeypatch.setattr(driver,'phase',lambda *_:events.append('phase'))
    assert driver.run_work(SimpleNamespace(job='sql_owners'))==1
    assert events==[]
    assert not (root/'work.json').exists()


def test_transport_reader_rejects_extra_fixed_prefix_slots(driver,monkeypatch):
    monkeypatch.setenv('SOURCE_MANIFEST_CHUNK_8','unexpected')
    with pytest.raises(ValueError,match='Unexpected transport'):
        driver.input_transport('SOURCE_MANIFEST','manifest')


def test_manifest_generation_refuses_candidate_local_output(driver,tmp_path,monkeypatch):
    candidate=tmp_path/'candidate';candidate.mkdir(mode=0o700)
    monkeypatch.setenv('REVIEWED_SOURCE_SHA','a'*40)
    monkeypatch.setattr(driver,'source_identity',lambda *_:{'commit':'a'*40,'tree':'b'*40,'digest':'c'*64})
    with pytest.raises(ValueError,match='Private external'):
        driver.manifest_command(candidate,candidate/'manifest.json')


def test_finalization_has_its_own_unextended_sixty_second_reserve(driver):
    assert driver.finalization_remaining(100,now=100)==60
    assert driver.finalization_remaining(100,now=1240)==60
    assert driver.finalization_remaining(100,now=1299)==1
    with pytest.raises(ValueError):driver.finalization_remaining(100,now=99)
    with pytest.raises(ValueError):driver.finalization_remaining(100,now=1300)


def test_fixed_probe_packaging_uses_candidate_packages_without_importing_them(driver,tmp_path):
    candidate=tmp_path/'candidate';site=candidate/'.venv/lib/python3.12/site-packages'
    site.mkdir(parents=True)
    (site/'sitecustomize.py').write_text('raise AssertionError("must not import")')
    assert driver.python_layout(candidate,'import sysconfig; print(sysconfig.get_path("purelib"))')==str(site)
    with pytest.raises(ValueError):driver.python_layout(candidate,'import os; os.system("evil")')
    for value in ('import sys; print(sys.base_prefix)','import sysconfig; print(sysconfig.get_path("purelib"))'):
        assert "-c '"+value+"'" in (ROOT/'scripts/verify_cube_runtime.sh').read_text()


def test_missing_work_or_cleanup_cannot_emit_source_or_lane_outputs(driver,tmp_path,monkeypatch):
    monkeypatch.setattr(driver,'load_context',lambda *_:{'job_id':'source'})
    emitted=[]
    monkeypatch.setattr(driver,'export_transport',lambda *args:emitted.append(args))
    with pytest.raises(FileNotFoundError):driver.finalize(tmp_path,tmp_path)
    assert emitted==[]


def test_cleanup_still_runs_after_candidate_identity_changes(driver,tmp_path,monkeypatch):
    from types import SimpleNamespace
    candidate=tmp_path/'candidate';candidate.mkdir()
    root=tmp_path/'root';root.mkdir(mode=0o700)
    identity={'controller_digest':'a'*64}
    driver.write_private(root/'context.json',{'candidate':str(candidate),'identity':identity,'job_id':'source','started':1})
    monkeypatch.setattr(driver,'controller_digest',lambda:'a'*64)
    monkeypatch.setattr(driver,'cleanup_remaining',lambda _:120)
    def never_context(*args,**kwargs):raise AssertionError('Cleanup cannot require unchanged candidate')
    monkeypatch.setattr(driver,'context',never_context)
    calls=[]
    result={'exit_status':0,'timed_out':False,'output_limit_exceeded':False,'interrupted':False,
        'descendant_cleanup':True,'orphaned_descendants':False}
    def bounded(command,output,**kwargs):calls.append((command,kwargs));return result
    monkeypatch.setattr(driver,'trusted_module',lambda _:SimpleNamespace(bounded_process=bounded))
    monkeypatch.setattr(driver,'modules',lambda _:(None,SimpleNamespace(process_passed=lambda row:row==result),None))
    assert driver.cleanup(candidate,root)==0
    assert calls[0][0][1:3]==['-I','-S']
    assert calls[0][1]['cwd']==ROOT
    assert calls[0][1]['timeout']==115
    assert driver.read_private(root/'cleanup-receipt.json')['exact_resources_verified'] is True


def test_failed_descendant_cleanup_cannot_become_cleanup_pass(driver,tmp_path,monkeypatch):
    from types import SimpleNamespace
    candidate=tmp_path/'candidate';candidate.mkdir()
    root=tmp_path/'root';root.mkdir(mode=0o700)
    driver.write_private(root/'context.json',{'candidate':str(candidate),'identity':{'controller_digest':'a'*64},'job_id':'source','started':1})
    monkeypatch.setattr(driver,'controller_digest',lambda:'a'*64)
    monkeypatch.setattr(driver,'cleanup_remaining',lambda _:120)
    result={'exit_status':0,'timed_out':False,'output_limit_exceeded':False,'interrupted':False,
        'descendant_cleanup':False,'orphaned_descendants':True}
    actual_verify=importlib.import_module('scripts.verify_demo')
    monkeypatch.setattr(driver,'trusted_module',lambda _:SimpleNamespace(bounded_process=lambda *a,**k:result))
    monkeypatch.setattr(driver,'modules',lambda _:(None,actual_verify,None))
    assert driver.cleanup(candidate,root)==1
    assert driver.read_private(root/'cleanup-receipt.json')['status']=='BLOCKED'


def test_full_selected_source_collection_has_unique_bounded_identities(request):
    nodes=[item.nodeid for item in request.session.items
        if item.get_closest_marker('unit') or item.get_closest_marker('contract')]
    assert nodes and len(nodes)==len(set(nodes))
    assert all(len(node.encode('utf-8'))<=1024 and node.isprintable() for node in nodes)


@pytest.mark.parametrize('source_shape',['valid','identity_list','receipt_list'])
def test_real_aggregate_cli_uses_trusted_context_and_bounded_malformed_receipts(driver,tmp_path,source_shape):
    import hashlib,json,shutil
    from scripts import ci_partition_contract as c
    from tests.unit.test_ci_partition_contract import inventory,receipt
    trusted=tmp_path/'trusted';trusted.mkdir()
    for name in (*driver.CONTROLLERS,'.gitignore'):
        target=trusted/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
    subprocess.run(['git','init','-q',str(trusted)],check=True)
    subprocess.run(['git','-C',str(trusted),'config','user.name','CI Test'],check=True)
    subprocess.run(['git','-C',str(trusted),'config','user.email','ci@example.invalid'],check=True)
    subprocess.run(['git','-C',str(trusted),'add','.'],check=True)
    subprocess.run(['git','-C',str(trusted),'commit','-qm','trusted test controller'],check=True)
    sha=subprocess.check_output(['git','-C',str(trusted),'rev-parse','HEAD'],text=True).strip()
    controller=driver.digest({name:hashlib.sha256((trusted/name).read_bytes()).hexdigest() for name in driver.CONTROLLERS})
    source={'commit':'a'*40,'tree':'b'*40,'digest':'c'*64}
    manifest=c.build_manifest(source,controller,inventory(c))
    who={'repository':'Sean-Koval/spaceport','source_commit':source['commit'],'source_tree':source['tree'],
        'source_digest':source['digest'],'workflow_file':driver.WORKFLOW,
        'workflow_ref':'Sean-Koval/spaceport/'+driver.WORKFLOW+'@refs/heads/main','workflow_sha':sha,
        'controller_digest':controller,'run_id':'123','run_attempt':'1'}
    env=driver.workload_environment()
    env.update(GITHUB_REPOSITORY=who['repository'],GITHUB_WORKFLOW_REF=who['workflow_ref'],GITHUB_WORKFLOW_SHA=sha,
        GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',REVIEWED_SOURCE_SHA=source['commit'],REVIEWED_MANIFEST_SHA256=c.sha256(manifest))
    def transport(prefix,value,kind):
        env.update({prefix+'_'+key.upper():value for key,value in c.encode_transport(value,kind=kind).items()})
    transport('SOURCE_MANIFEST',manifest,'manifest')
    for job in ('source',*c.LANES):
        value=receipt(c,manifest,job);value['identity']=dict(who);value['cleanup']['identity']=dict(who)
        if job=='source' and source_shape=='identity_list':value['identity']=[]
        if job=='source' and source_shape=='receipt_list':value=[]
        transport('SOURCE_RECEIPT' if job=='source' else job.upper()+'_RECEIPT',value,'receipt')
        env[job.upper()+'_RESULT']='success'
    result=subprocess.run([sys.executable,'-I','-S',str(trusted/'scripts/ci_partition_driver.py'),'aggregate'],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=10)
    assert 'Traceback' not in result.stderr and len(result.stderr)<256
    report=json.loads(result.stdout)
    assert report['status']==('PASS' if source_shape=='valid' else 'FAIL')
    assert result.returncode==(0 if source_shape=='valid' else 1)
    if source_shape!='valid':assert report['failures']==['SOURCE_RECEIPT_INVALID']
