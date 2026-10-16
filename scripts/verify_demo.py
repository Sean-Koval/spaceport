#!/usr/bin/env python3
"""Run and classify mandatory suites; absence is BLOCKED, never mock success."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import re
from collections import Counter
import xml.etree.ElementTree as ET
from scripts.demo_runtime import ROOT,load_state,require_docker,inspect_stack,verify_images,run

MANDATORY=('backend_compatibility','runtime_dependencies','publisher_browser','readiness','consumer_rest','mcp_clients',
    'restart_persistence','backup_restore','readiness_faults','retention','redacted_logs','authentication_replacement','offline_egress')


MAX_SUITE_LOG_BYTES=16*1024*1024


def pytest_environment(env=None):
    """Mandatory scope cannot inherit pytest selectors or arbitrary plugins."""
    for source in (os.environ, env or {}):
        if any(source.get(name) for name in ('PYTEST_ADDOPTS','PYTEST_PLUGINS')):
            raise ValueError('Ambient pytest scope/plugin overrides are not admitted')
    return {**(os.environ if env is None else env),'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'}


def known_supervision(result):
    flags={'timed_out','output_limit_exceeded','interrupted','descendant_cleanup','orphaned_descendants'}
    return (type(result) is dict and set(result)==flags|{'exit_status'} and type(result['exit_status']) is int
        and all(type(result[key]) is bool for key in flags))


def process_passed(result):
    return (known_supervision(result) and result.get('exit_status')==0 and result.get('descendant_cleanup') is True
        and all(result.get(key) is False for key in ('timed_out','output_limit_exceeded','interrupted','orphaned_descendants')))


def collect_pytest(command):
    env=pytest_environment()
    from scripts.runtime_process import bounded_process
    if command[:3]!=[sys.executable,'-m','pytest']:raise ValueError('Fixed pytest collection command required')
    with tempfile.TemporaryDirectory(prefix='gateway-collection-') as directory:
        output=Path(directory)/'collection.log'
        result=bounded_process(command,output,timeout=60,env=env,cwd=ROOT,limit_bytes=4*1024*1024)
        if not process_passed(result):raise ValueError('Mandatory pytest collection did not complete')
        return output.read_text()


# This phase consumes already-authorized disposable backend environments. Any
# dependency/image acquisition remains the separate compatibility harness's job.
PREREQUISITES={
    'postgres_registry':('tests/integration/test_registry.py::test_publish_race_commits_exactly_one_version',),
    'cube_compatibility':('tests/integration/test_cube_compatibility.py::test_authenticated_meta_records_actual_release_shape',),
    'bootstrap':('tests/integration/test_operations.py::test_operator_bootstrap_is_idempotent_and_never_publishes',),
}


def prerequisite_command(name):
    if name=='bootstrap':return list(PREREQUISITES[name])
    return [PREREQUISITES[name][0].split('::')[0],'-m','postgres' if name=='postgres_registry' else 'cube']


def prerequisite_images(state):
    result={}
    for service,filename in [('registry-db','compose.yaml'),('demo-db','compose.yaml'),('cube','compose.yaml'),('cube-test','compose.test.yaml')]:
        reference=json.loads((ROOT/'infra'/filename).read_text())['services'][service]['image']
        result[service]={'reference':reference,'image_id':run(['docker','image','inspect',reference,'--format','{{.Id}}'],timeout=15).stdout.strip()}
    return result


def collected_identities(name):
    pytest_environment()
    result=collect_pytest([sys.executable,'-m','pytest',*prerequisite_command(name),'--collect-only','-q'])
    nodes=[line.strip() for line in result.splitlines() if line.startswith('tests/') and '::' in line]
    identities=set()
    for node in nodes:
        parts=node.split('::');classname=parts[0].removesuffix('.py').replace('/','.')
        if len(parts)>2:classname+='.'+'.'.join(parts[1:-1])
        identities.add((classname,parts[-1]))
    if not identities:raise ValueError('Mandatory prerequisite collection is empty')
    return identities


def validate_prerequisites(root,state,images):
    pytest_environment()
    try:
        receipt=json.loads((root/'prerequisites.json').read_text())
        if receipt['classification']!='actual' or receipt['source_digest']!=state['source_digest'] or receipt['images']!=images:
            raise ValueError('Stale prerequisite source/image binding')
        rows=receipt['suites']
        if len(rows)!=len(PREREQUISITES) or {row['suite'] for row in rows}!=set(PREREQUISITES):raise ValueError('Mandatory prerequisite absent')
        for row in rows:
            if not valid_evidence_path(root,row['junit']):raise ValueError('Invalid prerequisite evidence path')
            path=root/row['junit']
            if path.stat().st_size>8*1024*1024 or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:raise ValueError('Prerequisite bytes changed')
            count=junit_count(path)
            cases=list(ET.parse(path).getroot().iter('testcase'))
            identities={(case.get('classname'),case.get('name')) for case in cases}
            if count!=len(cases) or len(identities)!=count or identities!=collected_identities(row['suite']):raise ValueError('Mandatory prerequisite test identity mismatch')
        return receipt
    except (OSError,KeyError,TypeError,ET.ParseError,json.JSONDecodeError) as error:
        raise ValueError('Required compatibility evidence unavailable') from None


def validate_backend_endpoints(images):
    # Do not trust a receipt label or an arbitrary user-supplied database URL.
    from sqlalchemy.engine import make_url
    if os.environ.get('GATEWAY_TEST_POSTGRES_AUTHORIZATION')!='disposable-local-postgres-only':raise ValueError('Disposable registry authority required')
    url=make_url(os.environ.get('GATEWAY_TEST_POSTGRES_ADMIN_URL',''))
    if url.host not in {'127.0.0.1','localhost'} or url.username!='gateway_test_admin' or url.database!='gateway_test_admin' or not url.port:raise ValueError('Invalid disposable registry endpoint')
    identifiers=run(['docker','ps','--quiet','--filter','publish='+str(url.port)],timeout=15).stdout.split()
    if len(identifiers)!=1:raise ValueError('Ambiguous disposable registry container')
    postgres=json.loads(run(['docker','inspect',identifiers[0]],timeout=15).stdout)[0]
    if not postgres['Config']['Labels'].get('gateway.registry.run') or postgres['Image']!=images['registry-db']['image_id']:raise ValueError('Registry image identity mismatch')
    assigned={'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':str(url.port)}]}
    ephemeral={'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':''}]}
    # HostConfig retains the requested empty port for --publish 127.0.0.1::5432.
    # NetworkSettings contains the allocated binding; both must stay loopback-only.
    if (postgres['HostConfig']['PortBindings'] not in (ephemeral,assigned)
        or postgres['NetworkSettings']['Ports']!=assigned):raise ValueError('Registry loopback identity mismatch')
    if os.environ.get('GATEWAY_CUBE_TEST_AUTHORIZATION')!='private-synthetic-cube-only':raise ValueError('Disposable Cube authority required')
    hosts=json.loads(Path(os.environ['GATEWAY_CUBE_TEST_HOSTS_FILE']).read_text())
    if hosts['kind']!='private-synthetic-cube-only':raise ValueError('Invalid Cube evidence identity')
    result={'registry-db':postgres['Id']}
    for service in ('cube','cube-test','demo-db'):
        value=json.loads(run(['docker','inspect',hosts['container_ids'][service]],timeout=15).stdout)[0]
        if value['Image']!=images[service]['image_id'] or value['Config']['Labels'].get('gateway.cube.run')!=hosts['run_label']:raise ValueError('Cube backend image/owner mismatch')
        network=value['NetworkSettings']['Networks']
        if len(network)!=1 or next(iter(network.values()))['IPAddress']!=hosts['hosts'][service]:raise ValueError('Cube endpoint identity mismatch')
        result[service]=value['Id']
    return result


def record_prerequisites(state,root):
    pytest_environment()
    from scripts.demo_runtime import private_json
    verify_images(state);images=prerequisite_images(state);identities=validate_backend_endpoints(images)
    root.mkdir(mode=0o700,parents=False,exist_ok=False)
    rows=[]
    for name in PREREQUISITES:
        output=root/(name+'.xml')
        row=execute_suite(name,[sys.executable,'-m','pytest',*prerequisite_command(name),'-q','--junitxml='+str(output)],root,junit=output)
        if row['status']!='PASS':raise ValueError('Actual prerequisite failed')
        rows.append({'suite':name,'junit':output.name,'sha256':hashlib.sha256(output.read_bytes()).hexdigest()})
    verify_images(state)
    if validate_backend_endpoints(images)!=identities:raise ValueError('Prerequisite backends changed')
    private_json(root/'prerequisites.json',{'classification':'actual','source_digest':state['source_digest'],
        'images':images,'containers':identities,'suites':rows})
    return validate_prerequisites(root,state,images)


def valid_evidence_path(root,path):
    if type(path) is not str: return False
    value=Path(path)
    return not value.is_absolute() and '..' not in value.parts and (root/value).is_file() and not (root/value).is_symlink() and (root/value).resolve().is_relative_to(root.resolve())


def required_suites(versions):
    return (*MANDATORY,'acceptance_security') if versions.get('integration') is True else MANDATORY


def integration_command(output):
    return [sys.executable,'-m','pytest','tests/e2e/test_acceptance.py','tests/security/test_adversarial_matrix.py',
        '-m','e2e','-q','--junitxml='+str(output)]


def business_operation_commands(evidence,*,integration=False):
    commands=[(name,operation_command(name,evidence/(name+'.xml'))) for name in ('runtime_dependencies','readiness','consumer_rest')]
    if integration:commands.append(('acceptance_security',integration_command(evidence/'acceptance_security.xml')))
    return commands


def runtime_binding(source,versions):
    binding={'source_digest':source,**{key:versions[key] for key in ('app_image','edge_image','backend_images')}}
    if versions.get('integration') is True:binding['integration']=True
    return binding


def expected_suite_identities(name):
    pytest_environment()
    if name=='acceptance_security':
        command=integration_command(Path('/unused'))[:-2]+['--collect-only','-q']
        result=collect_pytest(command)
        nodes=[line for line in result.splitlines() if line.startswith('tests/') and '::' in line]
        identities={(line.split('::')[0].removesuffix('.py').replace('/','.'),line.split('::',1)[1]) for line in nodes}
        if not identities:raise ValueError('Integration identity collection is empty')
        return identities
    if name=='publisher_browser':
        content=(ROOT/'tests/e2e/publisher.spec.ts').read_text()
        titles=[json.loads('"'+title+'"') for title in re.findall(r'^test\("((?:[^"\\]|\\.)*)"',content,re.M)]
        if len(titles)!=3 or len(set(titles))!=3:raise ValueError('Publisher identity set requires review')
        return {('publisher.spec.ts',title) for title in titles}
    if name=='mcp_clients':
        result=collect_pytest([sys.executable,'-m','pytest','tests/e2e/test_two_clients.py','--collect-only','-q'])
        nodes=[line for line in result.splitlines() if line.startswith('tests/e2e/test_two_clients.py::')]
        identities={('tests.e2e.test_two_clients',line.split('::',1)[1]) for line in nodes}
        if not identities:raise ValueError('MCP identity collection is empty')
        return identities
    if name not in MANDATORY or name in {'backend_compatibility','offline_egress'}:raise ValueError('Unknown runtime suite')
    return {('tests.integration.test_operations','test_actual_'+name)}


def summarize(records,*,source,versions,evidence_root):
    successful=set();counts=Counter(row.get('suite') for row in records);required=required_suites(versions)
    for row in records:
        name=row.get('suite')
        try:
            pytest_environment()
            if name not in required or counts[name]!=1 or row.get('classification')!='actual' or row.get('status')!='PASS':continue
            paths=row['evidence_paths'];hashes=row['evidence_sha256']
            if not paths or len(paths)!=len(set(paths)) or set(paths)!=set(hashes):raise ValueError('Incomplete evidence digest set')
            if row.get('binding')!=runtime_binding(source,versions):raise ValueError('Runtime source/image binding changed')
            for path in paths:
                if not valid_evidence_path(evidence_root,path) or hashlib.sha256((evidence_root/path).read_bytes()).hexdigest()!=hashes[path]:raise ValueError('Runtime evidence changed')
            if name=='backend_compatibility':
                validate_prerequisites(evidence_root/'prerequisites',{'source_digest':source},versions['backend_images'])
            elif name=='offline_egress':
                from scripts.check_egress import validate_report
                network=json.loads((evidence_root/'egress/network-report.json').read_text())
                if network['source_digest']!=source:raise ValueError('Egress source changed')
                validate_report(network,evidence_root/'egress')
            else:
                junit=row['junit']
                expected_file='publisher-junit.xml' if name=='publisher_browser' else name+'.xml'
                if junit!=expected_file or junit not in paths:raise ValueError('Exact JUnit evidence missing')
                count,identities=junit_details(evidence_root/junit)
                if count!=row['tests'] or identities!=expected_suite_identities(name):raise ValueError('Runtime testcase identities changed')
            successful.add(name)
        except (OSError,KeyError,ValueError,TypeError,ET.ParseError):continue
    missing=sorted(set(required)-successful)
    return {'schema_version':2,'suite':'gateway_complete_offline_demo','classification':'actual',
        'status':'PASS' if not missing else 'BLOCKED','source':source,'versions':versions,
        'recorded_at':datetime.now(timezone.utc).isoformat(),'suites':records,'missing':missing}


def require_actual_stack():
    directory=os.environ.get('GATEWAY_DEMO_STATE_DIR')
    if not directory or os.environ.get('GATEWAY_DEMO_TEST_AUTHORIZATION')!='private-synthetic-gateway-demo':
        raise RuntimeError('Actual operations require an explicitly selected disposable synthetic demo stack')
    require_docker()
    state=load_state(Path(directory));verify_images(state);inspect_stack(state)
    return state


def junit_details(path):
    if path.is_symlink() or path.stat().st_size>8*1024*1024:raise ValueError('Unbounded JUnit evidence')
    data=path.read_bytes()
    if b'<!DOCTYPE' in data or b'<!ENTITY' in data:raise ValueError('Unsupported JUnit declarations')
    document=ET.fromstring(data)
    if document.tag not in {'testsuite','testsuites'}:raise ValueError('Invalid JUnit root')
    suites=[document] if document.tag=='testsuite' else list(document.findall('testsuite'))
    if not suites or any(suite.findall('testsuite') for suite in suites):raise ValueError('Invalid nested JUnit suites')
    cases=list(document.iter('testcase'));count=len(cases)
    if sum(len(suite.findall('testcase')) for suite in suites)!=count:raise ValueError('Testcase outside a declared suite')
    identities={(case.get('classname'),case.get('name')) for case in cases}
    if not count or len(identities)!=count or any(not all(identity) for identity in identities):raise ValueError('Empty or duplicate JUnit cases')
    if any(list(document.iter(outcome)) for outcome in ('failure','error','skipped')):raise ValueError('Mandatory testcase did not pass')
    for suite in suites:
        if int(suite.get('tests',0))!=len(suite.findall('testcase')) or any(int(suite.get(key,0))!=0 for key in ('failures','errors','skipped')):
            raise ValueError('JUnit suite summary differs from actual cases')
    if document.tag=='testsuites':
        if 'tests' in document.attrib and int(document.get('tests'))!=count:raise ValueError('JUnit total differs')
        if any(int(document.get(key,0))!=0 for key in ('failures','errors','skipped')):raise ValueError('JUnit total has unsuccessful cases')
    return count,identities


def junit_count(path):
    return junit_details(path)[0]


class SuiteSupervisionFailure(RuntimeError):
    """Stop dependent work while preserving one safe, blocked suite record."""
    def __init__(self, record):
        self.record=record
        super().__init__('Mandatory suite supervision did not permit further work')


def execute_suite(name,command,evidence,*,env=None,timeout=600,junit=None,binding=None):
    from scripts.pytest_runtime_diagnostics import emit_phase_failure, phase_failure_evidence, evidence_pytest_command
    command=evidence_pytest_command(command)
    path=evidence/(name+'.log')
    # Raw output stays private. Unknown process ownership is fatal to the
    # dependent sequence, unlike an ordinary test failure after verified cleanup.
    status='BLOCKED';count=None;supervision=None;fatal=None
    try:
        admitted=pytest_environment(env)
        from scripts.runtime_process import bounded_process
    except (OSError,ImportError,RuntimeError,ValueError):pass
    else:
        try:
            supervision=bounded_process(command,path,timeout=timeout,env=admitted,cwd=ROOT,limit_bytes=MAX_SUITE_LOG_BYTES)
        except BaseException:
            # Includes cancellation after the supervisor's finally cleanup. If
            # no verified receipt returns, no later dependent test may start.
            fatal='unverified_supervision'
        else:
            if not known_supervision(supervision):
                supervision=None;fatal='unverified_supervision'
            elif not supervision['descendant_cleanup']:fatal='unverified_supervision'
            elif supervision['interrupted']:fatal='suite_interrupted'
            elif process_passed(supervision):
                try:
                    if junit is not None:count=junit_count(junit)
                    status='PASS'
                except (OSError,ValueError,ET.ParseError):pass
    # An unverified descendant could still mutate its files. Do not consume or
    # hash those bytes while ownership is unknown; retain only the blocked row.
    paths=[]
    if fatal!='unverified_supervision':
        if valid_evidence_path(evidence,path.name) and path.stat().st_size<=MAX_SUITE_LOG_BYTES:paths.append(path.name)
        if junit is not None and valid_evidence_path(evidence,junit.name) and junit.stat().st_size<=8*1024*1024:paths.append(junit.name)
    row={'suite':name,'classification':'actual','status':status,'tests':count,'junit':junit.name if junit else None,'binding':binding,
        'process':supervision,'evidence_paths':paths,'evidence_sha256':{name:hashlib.sha256((evidence/name).read_bytes()).hexdigest() for name in paths}}
    if status!='PASS':
        row['failure_evidence']=phase_failure_evidence(path,supervision or {'descendant_cleanup':False})
        emit_phase_failure(row['failure_evidence'])
    if fatal:
        row['reason']=fatal
        raise SuiteSupervisionFailure(row)
    return row


def operation_command(name,output):
    if name not in MANDATORY:raise ValueError('Unknown mandatory suite')
    return [sys.executable,'-m','pytest','tests/integration/test_operations.py::test_actual_'+name,'-q','--junitxml='+str(output)]


def main():
    parser=argparse.ArgumentParser(description='Verify a complete prepared offline synthetic gateway demo')
    parser.add_argument('--state-dir',required=True,type=Path)
    parser.add_argument('--integration',action='store_true',help='Require the fixed composed acceptance/security phase before mutations')
    parser.add_argument('--prerequisites-dir',type=Path,required=True)
    parser.add_argument('--record-prerequisites',action='store_true',help='Separate pre-offline phase using already-authorized compatibility backends')
    args=parser.parse_args()
    records=[];versions={'integration':args.integration};source=None
    try:
        pytest_environment()
        state=load_state(args.state_dir)
        if args.record_prerequisites:
            record_prerequisites(state,args.prerequisites_dir)
            print(json.dumps({'status':'PASS','phase':'compatibility-prerequisites','offline_demo':False}));return 0
        evidence=args.state_dir/'evidence'/('verify-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+os.urandom(4).hex())
        evidence.mkdir(mode=0o700)
        source=state['source_digest']
        import platform,importlib.metadata
        versions={'integration':args.integration,'app_image':state['app_image'],'edge_image':state['edge_image'],'python':platform.python_version(),
            'packages':{name:importlib.metadata.version(name) for name in ('fastapi','uvicorn','mcp')},
            'protocol':'2026-07-28','vendor_connections':[]}
        versions['upstream_images']=sorted({row['image'] for row in json.loads((ROOT/'infra/compose.yaml').read_text())['services'].values()})
        require_docker();verify_images(state);inspect_stack(state)
        versions['backend_images']=prerequisite_images(state)
        receipt=validate_prerequisites(args.prerequisites_dir,state,versions['backend_images'])
        binding=runtime_binding(source,versions)
        from scripts.demo_runtime import private_json
        retained=evidence/'prerequisites';retained.mkdir(mode=0o700)
        import shutil
        for name in ['prerequisites.json']+[row['junit'] for row in receipt['suites']]:
            shutil.copyfile(args.prerequisites_dir/name,retained/name)
        paths=['prerequisites/'+path.name for path in retained.iterdir()]
        records.append({'suite':'backend_compatibility','classification':'actual','status':'PASS','binding':binding,
            'tests':sum(junit_count(retained/row['junit']) for row in receipt['suites']),
            'evidence_paths':paths,'evidence_sha256':{name:hashlib.sha256((evidence/name).read_bytes()).hexdigest() for name in paths}})
        env={**os.environ,'OTEL_SDK_DISABLED':'true','DO_NOT_TRACK':'1','GATEWAY_DEMO_STATE_DIR':str(args.state_dir),'GATEWAY_DEMO_TEST_AUTHORIZATION':'private-synthetic-gateway-demo',
            'GATEWAY_E2E_BASE_URL':'http://127.0.0.1:'+str(state['port']),
            'GATEWAY_E2E_TOKEN_FILE':str(args.state_dir/'identities/demo-tokens.json'),
            'GATEWAY_OPERATIONS_EVIDENCE_DIR':str(evidence),
            'SPACEPORT_MCP_URL':'http://127.0.0.1:'+str(state['port'])+'/mcp',
            'SPACEPORT_DEMO_NORTH_TOKEN_FILE':str(args.state_dir/'identities/demo-north.token'),
            'SPACEPORT_DEMO_SOUTH_TOKEN_FILE':str(args.state_dir/'identities/demo-south.token')}
        import httpx
        with httpx.Client(base_url=env['GATEWAY_E2E_BASE_URL'],trust_env=False,timeout=8) as client:
            initial=client.get('/readyz').status_code
        if initial!=503: raise ValueError('verify-demo requires clean bootstrap before its first publication')
        from scripts.demo_runtime import private_json
        private_json(evidence/'before-publication.json',{'readiness':initial})
        def workflow():
            # Browser publication establishes real fixture evidence before readiness.
            playwright=ROOT/'publisher/playwright.config.ts'
            if playwright.is_file():
                records.append(execute_suite('publisher_browser',['npm','--prefix','publisher','run','test:e2e'],evidence,env=env,timeout=600,junit=evidence/'publisher-junit.xml',binding=binding))
            clients=ROOT/'tests/e2e/test_two_clients.py'
            if clients.is_file():
                output=evidence/'mcp_clients.xml'
                records.append(execute_suite('mcp_clients',[sys.executable,'-m','pytest',str(clients),'-q','--junitxml='+str(output)],evidence,env=env,junit=output,binding=binding))
            for name,command in business_operation_commands(evidence,integration=args.integration):
                output=evidence/(name+'.xml')
                records.append(execute_suite(name,command,evidence,env=env,junit=output,binding=binding))
            return [row for row in records if row['suite'] in {'publisher_browser','mcp_clients','runtime_dependencies','readiness','consumer_rest','acceptance_security'}]
        from scripts.check_egress import verify as observe_workflow
        network=observe_workflow(state,evidence/'egress',workflow=workflow)
        paths=[str(path.relative_to(evidence)) for path in (evidence/'egress').iterdir()]
        records.append({'suite':'offline_egress','classification':'actual','status':network['status'],'binding':binding,
            'evidence_paths':paths,'evidence_sha256':{name:hashlib.sha256((evidence/name).read_bytes()).hexdigest() for name in paths}})
        # Restart/restore/fault suites change namespaces and run after the stable,
        # fully observed successful publish/query window.
        for name in ('restart_persistence','backup_restore','readiness_faults','retention','redacted_logs','authentication_replacement'):
            output=evidence/(name+'.xml')
            records.append(execute_suite(name,operation_command(name,output),evidence,env=env,junit=output,binding=binding))
        for row in records:
            semantic=evidence/(row['suite']+'.json')
            extra=[]
            if semantic.is_file():extra.append(semantic)
            if row['suite']=='publisher_browser':
                extra.extend([evidence/'before-publication.json',evidence/'publisher-network.json'])
                try:
                    network=json.loads((evidence/'publisher-network.json').read_text())
                    if network['base_origin']!=env['GATEWAY_E2E_BASE_URL'] or network['external_requests'] or not network['requests']:
                        raise ValueError('Unproven browser asset boundary')
                    if any(request['origin']!=network['base_origin'] for request in network['requests']):raise ValueError('External browser asset')
                except Exception:row['status']='BLOCKED'
            if row['suite']=='offline_egress':
                extra.extend(sorted((evidence/'egress').glob('*.txt')));extra.append(evidence/'egress/network-report.json')
            for path in extra:
                relative=str(path.relative_to(evidence))
                if not valid_evidence_path(evidence,relative):row['status']='BLOCKED';continue
                if relative not in row['evidence_paths']:row['evidence_paths'].append(relative)
                row['evidence_sha256'][relative]=hashlib.sha256(path.read_bytes()).hexdigest()
        verify_images(state)
        report=summarize(records,source=source,versions=versions,evidence_root=evidence)
        from scripts.demo_runtime import private_json
        private_json(evidence/'report.json',report)
    except Exception as error:
        if isinstance(error,SuiteSupervisionFailure):records.append(error.record)
        report=summarize(records,source=source,versions=versions,evidence_root=Path('/nonexistent'))
        if 'evidence' in locals() and evidence.is_dir() and not (evidence/'report.json').exists():
            from scripts.demo_runtime import private_json
            private_json(evidence/'report.json',report)
    print(json.dumps(report,sort_keys=True))
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
