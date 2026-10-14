#!/usr/bin/env python3
"""List only machine summaries; never emit raw private diagnostic captures."""
import argparse
import hashlib
import json
from pathlib import Path
from scripts.demo_runtime import load_state
from scripts.verify_demo import summarize,valid_evidence_path


def latest_evidence(root,*,state=None):
    paths=sorted(root.glob('verify-*/report.json'))
    if not paths:return {'status':'BLOCKED','reason':'No complete full-demo report exists'}
    path=paths[-1]
    value=json.loads(path.read_text())
    if state is not None and (value.get('source')!=state['source_digest'] or
        any(value.get('versions',{}).get(key)!=state[key] for key in ('app_image','edge_image'))):
        return {'status':'BLOCKED','reason':'Evidence source/image binding differs from the selected state'}
    for row in value['suites']:
        expected=row.get('evidence_sha256',{})
        if set(expected)!=set(row.get('evidence_paths',[])):
            row['status']='BLOCKED';continue
        if any(not valid_evidence_path(path.parent,name) or hashlib.sha256((path.parent/name).read_bytes()).hexdigest()!=digest
            for name,digest in expected.items()):row['status']='BLOCKED'
    report=summarize(value['suites'],source=value['source'],versions=value['versions'],evidence_root=path.parent)
    return {'status':report['status'],'source':report['source'],'missing':report['missing'],'report':str(path),
        'suites':[{key:row.get(key) for key in ('suite','classification','status','tests','evidence_paths')} for row in report['suites']]}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--state-dir',type=Path,required=True);args=parser.parse_args()
    try:
        state=load_state(args.state_dir);report=latest_evidence(args.state_dir/'evidence',state=state)
        if report.get('source') is not None and report['source']!=state['source_digest']:
            report={'status':'BLOCKED','reason':'Evidence belongs to a different source build'}
    except Exception:report={'status':'BLOCKED','reason':'Evidence is unavailable or inconsistent'}
    print(json.dumps(report,sort_keys=True));return 0 if report['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
