#!/usr/bin/env python3
"""Reproducible lock inventory and verbatim installed dependency notices.

This copies upstream notice files; it does not assign an original-code license
or decide whether distribution obligations have been satisfied.
"""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import tomllib
import os
import base64
import tarfile

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/operations'
NOTICE=ROOT/'THIRD_PARTY_NOTICES'


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def save_notice(prefix,name,data):
    if len(data)>2_000_000:raise ValueError('Upstream notice requires manual size review')
    safe=re.sub('[^A-Za-z0-9_.-]','_',prefix+'-'+name)
    path=NOTICE/safe
    if path.exists() and path.read_bytes()!=data:raise ValueError('Existing upstream notice differs; review the new upstream bytes')
    path.write_bytes(data)
    return str(path.relative_to(ROOT))


def npm_provenance(root,key,package,name):
    directory=root/key;metadata=directory/'package.json';receipt=root/'node_modules/.package-lock.json'
    if metadata.is_symlink() or not metadata.is_file() or metadata.stat().st_size>2_000_000:raise ValueError('Installed npm metadata unavailable')
    installed=json.loads(metadata.read_text())
    if installed.get('name')!=name or installed.get('version')!=package['version']:raise ValueError('Installed npm identity differs from frozen lock')
    if receipt.is_symlink() or not receipt.is_file():raise ValueError('npm installation receipt unavailable')
    record=json.loads(receipt.read_text()).get('packages',{}).get(key,{})
    if any(record.get(field)!=package.get(field) for field in ('version','integrity','resolved')):
        raise ValueError('npm installation receipt differs from frozen lock')
    return {'package_json_sha256':digest(metadata),'installation_receipt_sha256':digest(receipt),
        'locked_artifact_integrity':package.get('integrity')}


def npm_artifact(root,key,package,caches):
    """Confirm notice/package bytes against the lock-integrity-verified npm tarball."""
    integrity=package.get('integrity','')
    if not integrity.startswith('sha512-'):raise ValueError('Unsupported npm artifact integrity')
    hexdigest=base64.b64decode(integrity[7:],validate=True).hex()
    artifact=None
    for cache in caches:
        candidate=cache/'_cacache/content-v2/sha512'/hexdigest[:2]/hexdigest[2:4]/hexdigest[4:]
        if candidate.is_file() and not candidate.is_symlink():artifact=candidate;break
    if artifact is None:return None
    if artifact.stat().st_size>100*1024*1024:raise ValueError('npm artifact exceeds review bound')
    with artifact.open('rb') as file:
        if hashlib.file_digest(file,'sha512').hexdigest()!=hexdigest:raise ValueError('Cached npm artifact integrity mismatch')
    directory=root/key
    files={path.name:path for path in directory.iterdir() if path.is_file() and not path.is_symlink()
        and (path.name=='package.json' or re.match(r'(?i)(licen[cs]e|copying|notice)(?:\.|$)',path.name))}
    verified=set();expanded=0;notice_bytes={};seen=set();root_name=None
    with tarfile.open(artifact,'r|gz') as archive:
        for member in archive:
            expanded+=max(member.size,0)
            if expanded>300*1024*1024:raise ValueError('Expanded npm artifact exceeds review bound')
            parts=Path(member.name).parts
            if not parts or member.name.startswith('/') or '..' in parts:raise ValueError('Invalid npm artifact path')
            if root_name is None:root_name=parts[0]
            if parts[0]!=root_name:raise ValueError('Unexpected npm artifact root')
            name='/'.join(parts[1:])
            is_notice=len(parts)==2 and re.match(r'(?i)(licen[cs]e|copying|notice)(?:\.|$)',name)
            if name in files or is_notice:
                if name in seen:raise ValueError('Duplicate npm artifact entry')
                seen.add(name)
                if not member.isfile() or member.size<0 or member.size>2_000_000:raise ValueError('Invalid npm notice artifact')
                data=archive.extractfile(member).read(2_000_001)
                if len(data)!=member.size:raise ValueError('Incomplete npm notice artifact')
                if name in files:
                    if data!=files[name].read_bytes():raise ValueError('Installed npm bytes differ from locked artifact')
                    verified.add(name)
                if is_notice:notice_bytes[name]=data

    if verified!=set(files):raise ValueError('Installed npm notice missing from locked artifact')
    return {'artifact_sha512':hexdigest,'verified_file_sha256':{name:digest(path) for name,path in files.items()},
        'installed_missing_notices':sorted(set(notice_bytes)-set(files)),
        'artifact_notice_sha256':{name:hashlib.sha256(data).hexdigest() for name,data in notice_bytes.items()},
        'notice_bytes':notice_bytes}


def generate():
    NOTICE.mkdir(parents=True,exist_ok=True)
    rows=[]
    lock=tomllib.loads((ROOT/'uv.lock').read_text())
    for package in lock['package']:
        if package['name']=='spaceport':continue
        row={'ecosystem':'python','name':package['name'],'version':package['version'],'license':'UNVERIFIED','notice_paths':[]}
        try:
            installed=importlib.metadata.distribution(package['name'])
            if installed.version!=package['version']:raise ValueError('Installed dependency differs from the frozen lock')
            metadata=installed.metadata
            value=metadata.get('License-Expression') or metadata.get('License')
            row['license']=value if value and len(value)<512 else 'SEE_UPSTREAM_NOTICE'
            for file in installed.files or []:
                path=Path(str(file))
                if '.dist-info' not in str(path) or not re.match(r'(?i)(licen[cs]e|copying|notice)',path.name):continue
                row['notice_paths'].append(save_notice('python-'+package['name']+'-'+package['version'],str(path).replace('/','-'),installed.locate_file(file).read_bytes()))
        except importlib.metadata.PackageNotFoundError:
            row['unavailable_reason']='Conditional or currently uninstalled locked artifact; no license inferred'
        rows.append(row)
    caches=[Path(value) for value in os.environ.get('GATEWAY_NPM_CACHES',str(Path.home()/'.npm')).split(os.pathsep)]
    for project in ('publisher','clients'):
        npm=json.loads((ROOT/project/'package-lock.json').read_text())
        for key,package in npm['packages'].items():
            if not key:continue
            name=package.get('name') or key.rsplit('node_modules/',1)[1]
            row={'ecosystem':'npm','project':project,'name':name,'version':package['version'],'license':package.get('license','UNVERIFIED'),
                'development':bool(package.get('dev',False)),'optional':bool(package.get('optional',False)),
                'integrity':package.get('integrity'),'notice_paths':[]}
            directory=ROOT/project/key
            if directory.is_dir():
                row['installed_provenance']=npm_provenance(ROOT/project,key,package,name)
                artifact=npm_artifact(ROOT/project,key,package,caches)
                if artifact is None:row['unavailable_reason']='Locked artifact unavailable in explicit local cache; installed notice bytes not admitted'
                else:
                    notices=artifact.pop('notice_bytes')
                    row['installed_provenance'].update(artifact)
                    for name,data in sorted(notices.items()):
                        row['notice_paths'].append(save_notice('npm-'+row['name']+'-'+package['version'],name,data))
            else:row['unavailable_reason']='Conditional/uninstalled package; license field comes from lock only'
            rows.append(row)
    for row in rows:row['notice_sha256']={name:digest(ROOT/name) for name in row['notice_paths']}
    # The original-code license is the owner's choice recorded in pyproject.toml and LICENSE.
    original=tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['license']
    output={'schema_version':1,'original_code_license':original,
        'distribution':'BLOCKED_PENDING_OWNER_AND_LICENSE_REVIEW',
        'locks':{'python':{'path':'uv.lock','sha256':digest(ROOT/'uv.lock')},
            'publisher':{'path':'publisher/package-lock.json','sha256':digest(ROOT/'publisher/package-lock.json')},
            'clients':{'path':'clients/package-lock.json','sha256':digest(ROOT/'clients/package-lock.json')}},
        'packages':rows,'gaps':['Container OS/image package inventory is not generated until the exact images are built.',
            'Conditional packages without installed bytes have not had notice contents verified.',
            'Installed npm artifacts without a matching local integrity-verified tarball are explicitly unavailable.']}
    (OUT/'dependency-inventory.json').write_text(json.dumps(output,indent=2,sort_keys=True)+'\n')
    return output


if __name__=='__main__':
    value=generate();print(json.dumps({'packages':len(value['packages']),'distribution':value['distribution']}))
