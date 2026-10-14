#!/usr/bin/env python3
"""Checksummed synthetic backups; restore only into a newly-created stack."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time
from contextlib import contextmanager

from scripts.demo_runtime import ROOT,KIND,load_state,private_json,validate_directory,compose,run,verify_images,start,inspect_stack,stream_command,cleanup_failed_state

MAX_ARCHIVE_BYTES=256*1024*1024
FILES={'registry.dump','source.dump','deployment.json','checksums.json'}
ASSETS=('cube/bundles.json','fixtures/products/revenue.json','fixtures/products/revenue.tests.json',
    'fixtures/bindings/revenue.json','fixtures/policies.json','fixtures/accounts.json')


APPLICATION_SERVICES=('edge','gateway','worker','retention')


def availability(state):
    import httpx
    with httpx.Client(base_url='http://127.0.0.1:'+str(state['port']),trust_env=False,timeout=3) as client:
        return {'health':client.get('/healthz').status_code,'ready':client.get('/readyz').status_code}


def recover_availability(state,expected):
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        try:
            if availability(state)==expected:return
        except Exception:pass
        time.sleep(.5)
    raise RuntimeError('Application recovery availability could not be verified')


@contextmanager
def quiesced_application(state):
    records=inspect_stack(state,allow_stopped=APPLICATION_SERVICES)
    running=tuple(name for name in APPLICATION_SERVICES if any(row['service']==name and row['running'] for row in records))
    expected=availability(state) if 'edge' in running and 'gateway' in running else None
    try:
        # Recovery is already established before the first potentially partial stop.
        if running:compose(state,'stop',*running,timeout=180)
        yield
    finally:
        try:
            if running:compose(state,'start',*running,timeout=90)
            final=inspect_stack(state,allow_stopped=APPLICATION_SERVICES)
            before={row['service']:row['running'] for row in records}
            if {row['service']:row['running'] for row in final}!=before:raise RuntimeError('Running state changed')
            if expected is not None:recover_availability(state,expected)
        except Exception:raise RuntimeError('Application recovery failed; operator must inspect the selected private stack') from None


def dump(state,service,database,role,secret,target):
    command=['docker','compose','--project-name',state['project'],'--project-directory',str(ROOT/'infra'),
        '-f',str(Path(state['state_directory'])/'compose.json'),'exec','-T','--user','999:999',service,
        'sh','-c',f'export PGPASSWORD="$(cat /run/secrets/{secret})"; exec pg_dump -h 127.0.0.1 -U {role} --format=custom --no-owner --dbname={database}']
    code=stream_command(command,target,timeout=120,stdout_limit=MAX_ARCHIVE_BYTES//2,stderr_limit=1024*1024)
    with target.open('rb') as file:header=file.read(5)
    if code or header!=b'PGDMP':
        target.unlink(missing_ok=True)
        raise RuntimeError('A complete bounded database backup was not produced')
    return target


def file_digest(path):
    with path.open('rb') as file:return hashlib.file_digest(file,'sha256').hexdigest()


def create_backup(state,target,*,quiesced=False):
    verify_images(state)
    stopped=('edge','gateway','worker','retention') if quiesced else ()
    records=inspect_stack(state,allow_stopped=stopped)
    if quiesced and any(row['running'] for row in records if row['service'] in stopped):
        raise ValueError('Backup recovery point requires quiesced application writers')
    if not target.is_absolute() or os.path.lexists(target): raise ValueError('Choose a new absolute backup filename')
    # Both dumps must complete before an archive/success marker is visible.
    # Workflow is stopped by the caller for a cross-database recovery point.
    with tempfile.TemporaryDirectory(prefix='gateway-backup-') as directory:
        temporary_root=Path(directory)
        registry=dump(state,'registry-db','gateway_registry','gateway_owner','registry_owner',temporary_root/'registry.dump')
        source=dump(state,'demo-db','gateway_demo','synthetic_admin','demo_admin',temporary_root/'source.dump')
        asset_bytes={name:(ROOT/name).read_text() for name in ASSETS}
        deployment={'kind':KIND,'source_digest':state['source_digest'],'source_inventory':state['source_inventory'],
            'app_image':state['app_image'],'edge_image':state['edge_image'],'assets':asset_bytes,
            'secret_references':['registry_owner','registry_app','registry_audit_retention','demo_admin','demo_reader',
                'fixture_reader','fixture_observer','cube_signing_key','cube_test_signing_key','audit_hmac','cursor_key','credential_hashes'],
            'contains_secret_material':False,'scope':'synthetic-demo-only'}
        private_json(temporary_root/'deployment.json',deployment)
        payloads={name:temporary_root/name for name in FILES-{'checksums.json'}}
        private_json(temporary_root/'checksums.json',{name:file_digest(path) for name,path in payloads.items()})
        payloads['checksums.json']=temporary_root/'checksums.json'
        temporary=target.with_name(target.name+'.partial-'+os.urandom(8).hex())
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(descriptor,'wb') as file:
                with tarfile.open(fileobj=file,mode='w:gz',format=tarfile.USTAR_FORMAT) as archive:
                    for name,path in sorted(payloads.items()):
                        entry=tarfile.TarInfo(name);entry.size=path.stat().st_size;entry.mode=0o600;entry.mtime=0
                        with path.open('rb') as source_file:archive.addfile(entry,source_file)
                file.flush();os.fsync(file.fileno())
            inspect_backup(temporary)
            os.link(temporary,target);temporary.unlink()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return target


def inspect_backup(path,*,output_directory=None):
    """Admit each raw tar header before consuming its declared bytes.

    Only four regular USTAR files are supported. No PAX/GNU extension parser is
    allowed to consume attacker-sized metadata before admission.
    """
    import gzip
    if path.is_symlink() or not path.is_file() or path.stat().st_size>MAX_ARCHIVE_BYTES:raise ValueError('Invalid bounded backup file')
    data={};observed={};total=0
    try:
        with gzip.open(path,'rb') as archive:
            while True:
                header=archive.read(512)
                if header==b'\0'*512:
                    # At most the standard tar record padding; no hidden member.
                    tail=archive.read(10240+1)
                    if len(tail)>10240 or any(tail):raise ValueError('Unexpected backup trailer')
                    break
                if len(header)!=512:raise ValueError('Incomplete backup header')
                member=tarfile.TarInfo.frombuf(header,'utf-8','strict')
                total+=member.size
                limit=MAX_ARCHIVE_BYTES//2 if member.name.endswith('.dump') else 1024*1024
                if member.name not in FILES or member.name in observed or len(observed)>=4 or member.type!=tarfile.REGTYPE:
                    raise ValueError('Unexpected backup member')
                if member.size<0 or member.size>limit or total>MAX_ARCHIVE_BYTES:raise ValueError('Backup exceeds its limit')
                digest=hashlib.sha256();remaining=member.size;small=bytearray();prefix=b''
                output=None
                try:
                    if output_directory is not None and member.name.endswith('.dump'):
                        descriptor=os.open(output_directory/member.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                        output=os.fdopen(descriptor,'wb')
                    while remaining:
                        chunk=archive.read(min(65536,remaining))
                        if not chunk:raise ValueError('Incomplete backup payload')
                        remaining-=len(chunk);digest.update(chunk)
                        if len(prefix)<5:prefix=(prefix+chunk)[:5]
                        if output is not None:output.write(chunk)
                        if not member.name.endswith('.dump'):small.extend(chunk)
                    if member.name.endswith('.dump') and prefix!=b'PGDMP':raise ValueError('Invalid database dump')
                    if not member.name.endswith('.dump'):data[member.name]=bytes(small)
                finally:
                    if output is not None:output.close()
                observed[member.name]=digest.hexdigest()
                padding=archive.read((-member.size)%512)
                if len(padding)!=(-member.size)%512 or any(padding):raise ValueError('Invalid backup padding')
        if set(observed)!=FILES:raise ValueError('Required backup member missing')
        hashes=json.loads(data['checksums.json'])
        if hashes!={name:value for name,value in observed.items() if name!='checksums.json'}:raise ValueError('Backup checksum mismatch')
        deployment=json.loads(data['deployment.json'])
        if deployment['kind']!=KIND or deployment['contains_secret_material'] is not False or deployment['scope']!='synthetic-demo-only':raise ValueError('Invalid recovery configuration')
        if set(deployment['assets'])!=set(ASSETS):raise ValueError('Required retained deployment assets missing')
        return data,deployment
    except (tarfile.TarError,OSError,EOFError,KeyError,TypeError,json.JSONDecodeError):
        raise ValueError('Backup is incomplete or invalid') from None


def restore(archive,target,port):
    # No existing path, Compose project, database or volume may be a restore target.
    if os.path.lexists(target): raise ValueError('Restore target must be a new disposable state directory')
    with tempfile.TemporaryDirectory(prefix='gateway-restore-') as directory:
        _,deployment=inspect_backup(archive,output_directory=Path(directory))
        return _restore(deployment,Path(directory),target,port)


def _restore(deployment,directory,target,port):
    from scripts.demo_runtime import prepare
    # Restores use already present exact images/dependencies; never rebuild or
    # refresh source-load timestamps as a substitute for restoring source data.
    if any((ROOT/name).read_text()!=content for name,content in deployment['assets'].items()):
        raise ValueError('Retained deployment assets differ; restore the reviewed source checkout first')
    from scripts.demo_runtime import source_inventory,source_digest,IMAGE_ID,merge_config
    if source_inventory()!=deployment['source_inventory'] or source_digest(source_inventory())!=deployment['source_digest']:
        raise ValueError('Restore requires the exact reviewed source tree')
    for key in ('app_image','edge_image'):
        if not IMAGE_ID.fullmatch(deployment[key]): raise ValueError('Invalid retained image identity')
        if run(['docker','image','inspect',deployment[key],'--format','{{.Id}}'],timeout=15).stdout.strip()!=deployment[key]:
            raise ValueError('Required retained image is not locally available')
    from scripts.demo_runtime import prepare_restored_state
    state=prepare_restored_state(target,port,deployment)
    try:
        # Source/registry initialization here targets only the just-created labeled
        # volumes. The working/source stack has never been selected for a write.
        compose(state,'up','--pull','never','-d','--wait','--wait-timeout','150','registry-db','demo-db')
        for service,database,role,secret,filename,clean in [
            ('registry-db','gateway_registry','gateway_owner','registry_owner','registry.dump',False),
            ('demo-db','gateway_demo','synthetic_admin','demo_admin','source.dump',True)]:
            command=['docker','compose','--project-name',state['project'],'--project-directory',str(ROOT/'infra'),
                '-f',str(Path(state['state_directory'])/'compose.json'),'exec','-T','--user','999:999',service,'sh','-c',
                f'export PGPASSWORD="$(cat /run/secrets/{secret})"; exec pg_restore -h 127.0.0.1 -U {role} --exit-on-error '+('--no-owner ' if service=='registry-db' else '')+
                ('--clean --if-exists ' if clean else '')+f'--dbname={database}']
            with (directory/filename).open('rb') as source:
                code=stream_command(command,directory/(filename+'.output'),input_file=source,timeout=120)
            if code: raise RuntimeError('Fresh disposable restore failed')
        start(state)
        return state
    except BaseException:
        cleanup_failed_state(state)
        raise


def main():
    parser=argparse.ArgumentParser(description='Synthetic demo backup or fresh-stack restore')
    parser.add_argument('operation',choices=['backup','restore'])
    parser.add_argument('--state-dir',required=True,type=Path)
    parser.add_argument('--archive',required=True,type=Path)
    parser.add_argument('--port',type=int,default=18080)
    args=parser.parse_args()
    try:
        if args.operation=='backup':
            state=load_state(args.state_dir);verify_images(state);inspect_stack(state)
            with quiesced_application(state):
                create_backup_quiesced(state,args.archive)
        else:state=restore(args.archive,args.state_dir,args.port)
        print(json.dumps({'status':'PASS','operation':args.operation,'project':state['project']}));return 0
    except Exception:
        print(json.dumps({'status':'BLOCKED','operation':args.operation,'reason':'Backup/restore validation or operation failed; working data was not overwritten.'}));return 1


# Backup quiescence remains explicit; the validated exact images and private
# state replace running-process admission only after the CLI admitted the stack.
def create_backup_quiesced(state,target):
    return create_backup(state,target,quiesced=True)


if __name__=='__main__':raise SystemExit(main())
