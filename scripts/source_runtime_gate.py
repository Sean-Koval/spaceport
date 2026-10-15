"""Bounded host controls for the run-private synthetic source acceptance gate.

Only the four fixed seeded transitions can reach bootstrap credentials. Node
receives readonly acknowledgements, never owner credentials or a control socket.
Arbitrary child output is bounded and discarded; only closed-schema evidence is
emitted. This is test tooling, never an organization runtime control plane.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import signal

CONTROL='GATEWAY_SOURCE_CONTROL '
RESULT='GATEWAY_SOURCE_RUNTIME_RESULT '
OPERATIONS=('revoke_queued','reclaim','close_main','close_zero')
CASES=('main','revocation','reclaim','zero_job','unknown')
CHECKS=('http_jwt_scope','compiler_retained_bytes','continue_wait','main_refresh_budget',
    'same_job_repoll','cache_eviction','cube_restart','current_authority_revocation',
    'late_metadata_observation','generation_reclaim','zero_job_settlement','unknown_submission_hold')
FAILURES=frozenset(('source-config','source-startup','source-main','source-revocation',
    'source-reclaim','source-zero','source-unknown','source-cleanup'))
ROOT=Path(__file__).resolve().parents[1]


class GateError(ValueError):
    def __init__(self,checkpoint='source-runtime-wrapper'):
        self.checkpoint=checkpoint
        super().__init__(checkpoint)


def _json(value):
    def unique(pairs):
        result={}
        for key,item in pairs:
            if key in result:raise GateError()
            result[key]=item
        return result
    try:return json.loads(value,object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(GateError()))
    except (ValueError,UnicodeError,RecursionError):raise GateError() from None


def _result(value):
    if type(value) is not dict or type(value.get('schema_version')) is not int or value['schema_version']!=1:
        raise GateError()
    if set(value)=={'schema_version','status','checkpoint'} and value['status']=='failed' and type(value['checkpoint']) is str and value['checkpoint'] in FAILURES:
        raise GateError(value['checkpoint'])
    expected={'schema_version':1,'status':'passed','operations':list(OPERATIONS),'cases':list(CASES),
        'checks':list(CHECKS),'provider':'fake','registry':'postgresql'}
    if value!=expected:raise GateError()
    return value


async def _stop(process):
    # Each child is created in its own process group. Remove local descendants,
    # including a blocked docker CLI; outer run-labeled cleanup removes containers.
    try:os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    try:await asyncio.wait_for(process.wait(),5)
    except asyncio.TimeoutError:raise GateError('source-cleanup') from None


async def execute(command,control,*,timeout=110):
    if type(timeout) not in (int,float) or not 0<timeout<=110:raise GateError()
    loop=asyncio.get_running_loop();deadline=loop.time()+timeout
    process=None;result=None;seen=[];total=0
    try:
        process=await asyncio.create_subprocess_exec(*command,cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,
            limit=8192,start_new_session=True)
        async with asyncio.timeout_at(deadline):
            while True:
                raw=await process.stdout.readline()
                if not raw:break
                total+=len(raw)
                if len(raw)>8192 or total>262144:raise GateError()
                line=raw.decode('utf-8').rstrip('\r\n')
                if line.startswith('# '):line=line[2:]
                if line.startswith(CONTROL):
                    value=_json(line[len(CONTROL):])
                    if (result is not None or type(value) is not dict or set(value)!={'operation'}
                        or len(seen)>=len(OPERATIONS) or value['operation']!=OPERATIONS[len(seen)]):raise GateError()
                    try:await control(value['operation'])
                    except Exception:raise GateError('source-runtime-control') from None
                    seen.append(value['operation'])
                elif line.startswith(RESULT):
                    if result is not None:raise GateError()
                    result=_result(_json(line[len(RESULT):]))
            code=await process.wait()
            if code!=0 or result is None or seen!=list(OPERATIONS):raise GateError('source-runtime-child')
            return result
    except GateError:raise
    except (OSError,ValueError,UnicodeError,TimeoutError):raise GateError() from None
    finally:
        if process is not None:await _stop(process)


async def _control_command(command):
    process=None
    try:
        process=await asyncio.create_subprocess_exec(*command,cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,
            limit=8192,start_new_session=True)
        async with asyncio.timeout(15):
            total=0
            while chunk:=await process.stdout.read(8192):
                total+=len(chunk)
                if total>65536:raise GateError('source-runtime-control')
            if await process.wait()!=0:raise GateError('source-runtime-control')
    finally:
        if process is not None:await _stop(process)


def commands(project,run_label):
    if (not re.fullmatch(r'gateway-cube-[a-f0-9]{16}',project)
        or run_label!=os.environ.get('GATEWAY_CUBE_RUN_LABEL')
        or not (run_label==project+'-local' or re.fullmatch(r'[0-9]+-[0-9]+',run_label))):raise GateError()
    compose=['docker','compose','--project-name',project,
        '-f','infra/compose.yaml','-f','infra/compose.test.yaml',
        '-f','infra/compose.reviewed.yaml','-f','infra/compose.source-test.yaml']
    node=[*compose,'exec','-T','-e','GATEWAY_SOURCE_TEST_AUTHORIZATION=private-synthetic-source-only',
        '-e','GATEWAY_SOURCE_FIXTURE_FILE=/source-evidence/source-runtime-fixture.json',
        '-e','GATEWAY_BIGQUERY_RUNTIME_DIR=/cube/conf/bigquery-runtime','cube-reviewed',
        'node','--test','--test-reporter=spec','tests/runtime/bigquery-serving.test.cjs']
    async def control(operation):
        if operation not in OPERATIONS:raise GateError()
        await _control_command([*compose,'run','--rm','--no-deps','--user',f'{os.getuid()}:{os.getgid()}',
            'source-bootstrap','control','--fixture-file','/evidence/source-runtime-fixture.json','--operation',operation])
    return node,control


def main():
    parser=argparse.ArgumentParser(description='Required private source runtime acceptance')
    parser.add_argument('--project',required=True)
    parser.add_argument('--run-label',required=True)
    args=parser.parse_args()
    try:
        command,control=commands(args.project,args.run_label)
        result=asyncio.run(execute(command,control))
    except (GateError,OSError,KeyboardInterrupt) as error:
        checkpoint=error.checkpoint if isinstance(error,GateError) else 'source-runtime-wrapper'
        print('GATEWAY_FAILURE_EVIDENCE '+json.dumps({'kind':'checkpoint','checkpoint':checkpoint,'exit_status':1}),flush=True)
        return 1
    print(RESULT+json.dumps(result,sort_keys=True,separators=(',',':')),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
