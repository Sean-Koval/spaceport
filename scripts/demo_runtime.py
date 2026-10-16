#!/usr/bin/env python3
"""Bounded operations on one explicitly selected synthetic private Docker stack."""
from contextlib import nullcontext
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import selectors
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
KIND = 'private-synthetic-gateway-demo'
RUNNING = ('registry-db','demo-db','cube','cube-test','gateway','worker','retention','edge')
PROJECT = re.compile(r'gateway-demo-[a-f0-9]{16}')
IMAGE_ID = re.compile(r'sha256:[a-f0-9]{64}')


def stream_command(args,target,*,timeout=120,stdout_limit=4*1024*1024,stderr_limit=1024*1024,input_file=None):
    """Bound both output streams while running; never buffer a database dump."""
    descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    child=None
    try:
        with os.fdopen(descriptor,'wb') as output, selectors.DefaultSelector() as selector:
            child=subprocess.Popen(args,cwd=ROOT,stdin=input_file if input_file is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            selector.register(child.stdout,selectors.EVENT_READ,'stdout')
            selector.register(child.stderr,selectors.EVENT_READ,'stderr')
            counts={'stdout':0,'stderr':0};deadline=time.monotonic()+timeout
            while selector.get_map():
                if time.monotonic()>=deadline:raise subprocess.TimeoutExpired(args,timeout)
                for key,_ in selector.select(min(.1,max(0,deadline-time.monotonic()))):
                    data=os.read(key.fd,65536)
                    if not data:selector.unregister(key.fileobj);continue
                    counts[key.data]+=len(data)
                    if counts[key.data]>(stdout_limit if key.data=='stdout' else stderr_limit):
                        raise RuntimeError('Synthetic command output budget exceeded')
                    if key.data=='stdout':output.write(data)
            result=child.wait(timeout=max(.001,deadline-time.monotonic()))
            output.flush();os.fsync(output.fileno())
            return result
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        if child is not None:
            if child.poll() is None:child.kill()
            child.wait(timeout=5)
            child.stdout.close();child.stderr.close()


def run(args,*,timeout=120,input=None,check=True):
    with tempfile.TemporaryDirectory(prefix='gateway-command-') as directory:
        target=Path(directory)/'stdout'
        with tempfile.TemporaryFile() as stdin:
            if input is not None:stdin.write(input.encode());stdin.seek(0)
            code=stream_command(args,target,timeout=timeout,input_file=stdin)
        result=subprocess.CompletedProcess(args,code,target.read_text(), '')
    if check and result.returncode:
        raise RuntimeError('A bounded synthetic operation failed; no raw diagnostics were logged')
    return result


def private_json(path,value):
    descriptor = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'w') as file:
        json.dump(value,file,indent=2);file.write('\n');file.flush();os.fsync(file.fileno())


def require_docker(*, observer=None):
    observe = observer if observer is not None else lambda _: nullcontext()
    with observe('preflight_docker_binary'):
        if shutil.which('docker') is None: raise RuntimeError('BLOCKED: Docker is required for actual demo operations')
    with observe('preflight_docker_info'):
        run(['docker','info','--format','{{.ServerVersion}}'],timeout=15)
    with observe('preflight_compose'):
        run(['docker','compose','version'],timeout=15)


def source_inventory():
    roots=['gateway','migrations','fixtures','cube','publisher','infra','scripts','tests','clients']
    files=[ROOT/name for name in ('pyproject.toml','uv.lock','alembic.ini','.dockerignore','docs/compatibility/openapi.json')]
    for name in roots:
        files.extend(path for path in (ROOT/name).rglob('*') if path.is_file() and not path.is_symlink()
            and not {'node_modules','dist','__pycache__','evidence','test-results','playwright-report'}&set(path.parts))
    return {str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(files))}


def source_digest(inventory):
    return hashlib.sha256(json.dumps(inventory,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def validate_directory(path,new=False):
    if not path.is_absolute() or '..' in path.parts or path.is_symlink(): raise ValueError('Choose an absolute private external state directory')
    resolved=path.resolve()
    if resolved==ROOT or ROOT in resolved.parents: raise ValueError('State and secrets must stay outside the repository')
    if new:
        if os.path.lexists(path): raise ValueError('New state directory must not already exist')
        path.mkdir(mode=0o700,parents=False,exist_ok=False)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o700 or info.st_uid!=os.getuid():
        raise ValueError('State requires an owner-controlled 0700 directory')
    return path


def load_state(path):
    path=validate_directory(Path(path))
    target=path/'state.json'
    if target.is_symlink() or not target.is_file() or target.stat().st_size>1_048_576: raise ValueError('Invalid synthetic state')
    state=json.loads(target.read_text())
    required={'kind','project','run_label','app_image','edge_image','source_digest','source_inventory','port','state_directory','phase'}
    if set(state)!=required or state['kind']!=KIND or not PROJECT.fullmatch(state['project']): raise ValueError('Invalid synthetic state identity')
    if state['phase']!='prepared': raise ValueError('Incomplete synthetic preparation')
    if state['run_label']!=state['project'] or state['state_directory']!=str(path): raise ValueError('State identity does not match selected directory')
    if not all(IMAGE_ID.fullmatch(state[key]) for key in ('app_image','edge_image')): raise ValueError('Images must be content-addressed local builds')
    if type(state['port']) is not int or not 1024<=state['port']<=65535: raise ValueError('Invalid loopback port')
    if state['source_digest']!=source_digest(state['source_inventory']): raise ValueError('Invalid source provenance')
    return state


def compose(state,*args,timeout=180,check=True,input=None):
    return run(['docker','compose','--project-name',state['project'],'--project-directory',str(ROOT/'infra'),
        '-f',str(Path(state['state_directory'])/'compose.json'),*args],timeout=timeout,check=check,input=input)


def verify_images(state):
    for key in ('app_image','edge_image'):
        observed=run(['docker','image','inspect',state[key],'--format','{{.Id}}'],timeout=15).stdout.strip()
        if observed!=state[key]: raise ValueError('Prepared image identity changed')
    if source_inventory()!=state['source_inventory']: raise ValueError('Prepared source changed; use a new isolated state directory to rebuild')


def merge_config(base,overrides):
    """Compose-compatible service-map overlay, before normalization by Docker."""
    import copy
    result=copy.deepcopy(base)
    for overlay in overrides:
        for section,values in overlay.items():
            for name,value in values.items():
                if section=='services' and name in result.setdefault(section,{}):
                    result[section][name].update(copy.deepcopy(value))
                else: result.setdefault(section,{})[name]=copy.deepcopy(value)
    return result


def load_cleanup_state(path):
    path=validate_directory(Path(path));target=path/'cleanup.json'
    if target.is_symlink() or not target.is_file() or target.stat().st_size>4096:raise ValueError('Missing bounded cleanup identity')
    value=json.loads(target.read_text())
    if set(value)!={'kind','project','run_label','state_directory','phase'} or value['kind']!=KIND or value['phase']!='allocated':
        raise ValueError('Invalid cleanup identity')
    if not PROJECT.fullmatch(value['project']) or value['project']!=value['run_label'] or value['state_directory']!=str(path):
        raise ValueError('Cleanup identity differs from selected directory')
    return value


def prepare(path,port=8080,*,restored=None):
    require_docker()
    if type(port) is not int or not 1024<=port<=65535:raise ValueError('Invalid loopback port')
    path=validate_directory(Path(path),new=True)
    project='gateway-demo-'+secrets.token_hex(8)
    identity=dict(kind=KIND,project=project,run_label=project,state_directory=str(path),phase='allocated')
    # Persist before secrets, pulls, builds, networks or volumes are allocated.
    private_json(path/'cleanup.json',identity)
    try:return _prepare(path,port,project,restored=restored)
    except BaseException:
        cleanup_failed_state(identity)
        raise


def _prepare(path,port,project,*,restored):
    # Preparation is intentionally separate from offline execution. Failed
    # preparations stay private for diagnosis, and are never reused as success.
    from scripts.cube_runtime_secrets import prepare as prepare_secrets
    from scripts.demo_accounts import generate
    secret_dir=path/'secrets';secret_dir.mkdir(mode=0o700)
    prepare_secrets(secret_dir)
    for name in ('audit_hmac','cursor_key'):
        descriptor=os.open(secret_dir/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'w') as file:
            file.write(secrets.token_urlsafe(32)+'\n');file.flush();os.fsync(file.fileno());os.fchmod(file.fileno(),0o444)
    generate(path/'identities')
    tokens=json.loads((path/'identities/demo-tokens.json').read_text())
    for principal in ('demo-north','demo-south'):
        descriptor=os.open(path/'identities'/(principal+'.token'),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'w') as file:
            file.write(tokens[principal]+'\n');file.flush();os.fsync(file.fileno())
    # No bearer material is mounted into application containers. This hash-only
    # file is readable by the chosen non-root image UID behind private traversal.
    auth_dir=path/'auth';auth_dir.mkdir(mode=0o755)
    shutil.copyfile(path/'identities/credential-hashes.json',auth_dir/'credential-hashes.json')
    (auth_dir/'credential-hashes.json').chmod(0o444)
    (path/'evidence').mkdir(mode=0o700)
    base=json.loads((ROOT/'infra/compose.yaml').read_text())
    test=json.loads((ROOT/'infra/compose.test.yaml').read_text())
    overlay=json.loads((ROOT/'infra/compose.demo.yaml').read_text())
    inventory=source_inventory()
    if restored is None:
        for image in sorted({row['image'] for row in base['services'].values()}):
            run(['docker','pull','--platform','linux/amd64',image],timeout=240)
    ids={} if restored is None else {'app':restored['app_image'],'edge':restored['edge_image']}
    for kind,dockerfile in ([] if restored is not None else [('app','gateway/Dockerfile'),('edge','publisher/Dockerfile')]):
        tag=project+'-'+kind+':prepared'
        run(['docker','build','--platform','linux/amd64','--label','gateway.demo.run='+project,
            '--label','gateway.cube.run='+project,'--label','gateway.source.digest='+source_digest(inventory),'--tag',tag,'--file',dockerfile,'.'],timeout=600)
        ids[kind]=run(['docker','image','inspect',tag,'--format','{{.Id}}'],timeout=15).stdout.strip()
        if not IMAGE_ID.fullmatch(ids[kind]): raise ValueError('Invalid built image identity')
    if source_inventory()!=inventory: raise ValueError('Source changed during image preparation')
    state=dict(kind=KIND,project=project,run_label=project,app_image=ids['app'],edge_image=ids['edge'],
        source_digest=source_digest(inventory),source_inventory=inventory,port=port,state_directory=str(path),phase='prepared')
    env={'GATEWAY_APP_IMAGE':ids['app'],'GATEWAY_EDGE_IMAGE':ids['edge'],'GATEWAY_PROBE_IMAGE':ids['app'],
        'GATEWAY_CUBE_RUN_LABEL':project,'GATEWAY_RUNTIME_SECRET_DIR':str(secret_dir),
        'GATEWAY_RUNTIME_IDENTITY_DIR':str(path/'identities'),'GATEWAY_RUNTIME_AUTH_DIR':str(path/'auth'),'GATEWAY_RUNTIME_EVIDENCE_DIR':str(path/'evidence'),
        'GATEWAY_DEMO_PORT':str(port)}
    combined=merge_config(base,[test,overlay]);combined.pop('name',None)
    # The compatibility-only pytest probe is never part of the gateway runtime.
    combined['services'].pop('probe')
    raw=json.dumps(combined)
    for key,value in env.items(): raw=re.sub(r'\$\{'+key+r'(?::[^}]*)?\}',lambda _:value,raw)
    if '${' in raw: raise ValueError('Unresolved synthetic composition variable')
    config=json.loads(raw)
    config['services']['edge']['ports']=[{'target':8080,'published':str(port),'host_ip':'127.0.0.1','protocol':'tcp'}]
    private_json(path/'compose.json',config)
    # Canonical Compose admission must succeed before the prepared marker exists.
    compose(state,'config','--quiet',timeout=20)
    private_json(path/'state.json',state)
    return state


def prepare_restored_state(path,port,deployment):
    return prepare(path,port,restored=deployment)


def start(state):
    verify_images(state)
    compose(state,'up','--pull','never','-d','--wait','--wait-timeout','150','registry-db','demo-db','cube','cube-test',timeout=180)
    for service in ('migrate','bootstrap'):
        compose(state,'run','--rm','--no-deps','--pull','never',service,timeout=120)
    compose(state,'up','--pull','never','-d','--wait','--wait-timeout','150','gateway','worker','retention','edge',timeout=180)
    return inspect_stack(state)


def inspect_stack(state,*,allow_stopped=()):
    identifiers=compose(state,'ps','--all','--quiet',timeout=15).stdout.split()
    records=[]
    if not identifiers: raise RuntimeError('Required synthetic runtime is not running')
    for identifier in identifiers:
        data=json.loads(run(['docker','inspect',identifier],timeout=15).stdout)[0]
        label=data['Config']['Labels'];service=label.get('com.docker.compose.service')
        if label.get('com.docker.compose.project')!=state['project'] or label.get('gateway.cube.run')!=state['run_label']:
            raise ValueError('Runtime resource is outside this synthetic state')
        if service not in RUNNING or (not data['State']['Running'] and service not in allow_stopped): raise ValueError('Unexpected or stopped runtime service')
        networks=data['NetworkSettings']['Networks']
        if set(networks)!={state['project']+'_private'}: raise ValueError('Runtime network attachment widened')
        host=data['HostConfig']
        if host.get('Privileged') or host.get('NetworkMode') in {'host','none'}:raise ValueError('Runtime isolation widened')
        if service in {'gateway','worker','retention','edge'} and host.get('ReadonlyRootfs') is not True:
            raise ValueError('Runtime root filesystem is writable')
        ports=host.get('PortBindings') or {}
        expected={'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':str(state['port'])}]} if service=='edge' else {}
        if ports!=expected: raise ValueError('Runtime ingress widened')
        if data['HostConfig'].get('Dns')!=['127.0.0.1']: raise ValueError('External resolver configuration is forbidden')
        if service in ('gateway','worker','retention') and data['Image']!=state['app_image']: raise ValueError('Application image changed')
        if service in {'registry-db','demo-db','cube','cube-test'}:
            baseline=json.loads((ROOT/('infra/compose.test.yaml' if service=='cube-test' else 'infra/compose.yaml')).read_text())
            expected_image=baseline['services'][service]['image']
            if data['Config'].get('Image')!=expected_image:raise ValueError('Pinned upstream service image changed')
            exact=run(['docker','image','inspect',expected_image,'--format','{{.Id}}'],timeout=15).stdout.strip()
            if data['Image']!=exact:raise ValueError('Runtime upstream image differs from prepared digest')
        if service=='edge' and data['Image']!=state['edge_image']: raise ValueError('Publisher image changed')
        records.append({'service':service,'id':identifier,'pid':data['State']['Pid'],'image':data['Image'],'running':data['State']['Running']})
    if {row['service'] for row in records}!=set(RUNNING) or len(records)!=len(RUNNING): raise ValueError('Mandatory runtime service is missing or duplicated')
    network=json.loads(run(['docker','network','inspect',state['project']+'_private'],timeout=15).stdout)[0]
    if network.get('Internal') is not True or network['Labels'].get('gateway.cube.run')!=state['run_label']:
        raise ValueError('Private network identity changed')
    return records


def remove_exact(kind,identifier,*,command=run):
    if kind not in {'container','network','volume','image'} or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',identifier):
        raise ValueError('Invalid exact removal target')
    args=['docker',kind,'rm',*(['--force','--volumes'] if kind=='container' else []),identifier]
    result=command(args,timeout=30,check=False)
    if result.returncode:
        flags=['--all'] if kind=='container' else []
        if kind!='volume':flags+=['--no-trunc']
        result=command(['docker',kind,'ls',*flags,'--quiet'],timeout=15,check=False)
        if result.returncode or identifier in result.stdout.split(): raise RuntimeError('Cleanup failed and resource absence is unverified')


def cleanup(state):
    """Attempt every exact-label resource, then independently inventory leftovers."""
    identity=load_cleanup_state(Path(state['state_directory']))
    if identity['project']!=state['project']:raise ValueError('Cleanup target mismatch')
    listings={};failures=[]
    for kind in ('container','network','volume','image'):
        flags=['--all'] if kind=='container' else []
        if kind!='volume':flags+=['--no-trunc']
        listing=['docker',kind,'ls',*flags,'--quiet','--filter','label=gateway.cube.run='+identity['run_label']]
        listings[kind]=listing
        try:
            for identifier in set(run(listing,timeout=15).stdout.split()):
                try:remove_exact(kind,identifier)
                except Exception:failures.append(kind)
        except Exception:failures.append(kind)
    leftovers={}
    for kind,listing in listings.items():
        try:leftovers[kind]=run(listing,timeout=15).stdout.split()
        except Exception:leftovers[kind]=None
    passed=all(value==[] for value in leftovers.values())
    report={'status':'PASS' if passed else 'BLOCKED','project':identity['project'],
        'leftovers':leftovers,'failed_operations':sorted(set(failures))}
    path=Path(state['state_directory'])/'cleanup-report.json'
    temporary=path.with_name('cleanup-report-'+secrets.token_hex(8)+'.json')
    private_json(temporary,report);os.replace(temporary,path)
    if not passed:raise RuntimeError('Exact cleanup incomplete; retained cleanup-report.json identifies leftovers')
    return report


def cleanup_failed_state(state):
    cleanup(state)
    path=Path(state['state_directory'])
    # These paths were allocated only after the exclusive new-directory check.
    # Keep identity/report for later independent inspection, without bearer data.
    for name in ('secrets','identities','auth'):
        target=path/name
        if target.exists():shutil.rmtree(target)
    for name in ('compose.json','state.json'):(path/name).unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description='Private synthetic demo lifecycle')
    parser.add_argument('operation',choices=['prepare','up','down','inspect','cleanup'])
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--port',type=int,default=8080)
    args=parser.parse_args()
    try:
        require_docker()
        if args.operation=='prepare':state=prepare(args.state_dir,args.port)
        else:
            state=load_cleanup_state(args.state_dir) if args.operation=='cleanup' else load_state(args.state_dir)
            if args.operation=='up':start(state)
            elif args.operation=='down':compose(state,'down',timeout=180) # Volumes intentionally retained.
            elif args.operation=='cleanup':cleanup(state)
            else:inspect_stack(state)
        print(json.dumps({'operation':args.operation,'status':'PASS','project':state['project'],
            'url':'http://127.0.0.1:'+str(state.get('port',args.port)),'first_run':'Readiness remains unavailable until the reviewed product is published.'}))
        return 0
    except (ValueError,OSError,RuntimeError,subprocess.SubprocessError):
        print(json.dumps({'operation':args.operation,'status':'BLOCKED','reason':'Required tools, exact state, prepared images, or private runtime verification failed.'}))
        return 1


if __name__=='__main__':raise SystemExit(main())
