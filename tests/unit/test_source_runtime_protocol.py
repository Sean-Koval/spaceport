"""Bounded local process tests for synthetic host-controlled source acceptance."""
import asyncio
import json
import sys

import pytest

pytestmark=[pytest.mark.unit,pytest.mark.contract]


def module():
    from scripts import source_runtime_gate
    return source_runtime_gate


def child(lines, tail=''):
    code='import sys,time\n'
    for line in lines:
        code+='print('+repr(line)+',flush=True)\n'
    return [sys.executable,'-c',code+tail]


def receipt():
    m=module()
    return {'schema_version':1,'status':'passed','operations':list(m.OPERATIONS),
        'cases':list(m.CASES),'checks':list(m.CHECKS),'provider':'fake','registry':'postgresql'}


def test_required_host_controls_and_exact_safe_result():
    m=module();seen=[]
    async def control(op): seen.append(op)
    lines=['ignored bounded provider output']+[m.CONTROL+json.dumps({'operation':op}) for op in m.OPERATIONS]+[m.RESULT+json.dumps(receipt())]
    result=asyncio.run(m.execute(child(lines),control,timeout=5))
    assert result==receipt() and seen==list(m.OPERATIONS)


@pytest.mark.parametrize('bad',[
    'GATEWAY_SOURCE_CONTROL {"operation":"drop_database"}',
    'GATEWAY_SOURCE_CONTROL {"operation":"reclaim"}',
    'GATEWAY_SOURCE_CONTROL {"operation":"revoke_queued","extra":1}',
    'GATEWAY_SOURCE_CONTROL {"operation":"revoke_queued","operation":"reclaim"}',
])
def test_invalid_control_cannot_invoke_privileged_callback(bad):
    m=module();seen=[]
    async def control(op):seen.append(op)
    with pytest.raises(m.GateError):asyncio.run(m.execute(child([bad]),control,timeout=5))
    assert seen==[]


def test_duplicate_control_fails_without_repeating_privileged_action():
    m=module();seen=[]
    async def control(op):seen.append(op)
    line=m.CONTROL+json.dumps({'operation':m.OPERATIONS[0]})
    with pytest.raises(m.GateError):asyncio.run(m.execute(child([line,line]),control,timeout=5))
    assert seen==[m.OPERATIONS[0]]


@pytest.mark.parametrize('tail',['','sys.exit(1)','time.sleep(30)'])
def test_no_success_receipt_or_nonzero_child_is_never_acceptance(tail):
    m=module()
    async def control(op):raise AssertionError('unexpected control')
    with pytest.raises(m.GateError):asyncio.run(m.execute(child([],tail),control,timeout=.1 if 'sleep' in tail else 5))


def test_oversized_line_fails_boundedly():
    m=module()
    async def control(op):raise AssertionError('unexpected control')
    with pytest.raises(m.GateError):asyncio.run(m.execute(child(['x'*9000]),control,timeout=5))


def test_control_failure_withholds_success():
    m=module()
    async def control(op):raise ValueError('private details must not escape')
    with pytest.raises(m.GateError,match='source-runtime-control'):
        asyncio.run(m.execute(child([m.CONTROL+json.dumps({'operation':m.OPERATIONS[0]})]),control,timeout=5))


@pytest.mark.parametrize('kind',['nonzero','missing_control','duplicate_receipt','extra_field','bad_failure'])
def test_result_receipt_cannot_override_process_or_protocol_failure(kind):
    m=module();seen=[]
    async def control(op):seen.append(op)
    value=receipt()
    lines=[m.CONTROL+json.dumps({'operation':op}) for op in m.OPERATIONS]
    if kind=='missing_control':lines=[]
    if kind=='extra_field':value['secret']='never printed'
    if kind=='bad_failure':value={'schema_version':1,'status':'failed','checkpoint':['private']}
    lines.append(m.RESULT+json.dumps(value))
    if kind=='duplicate_receipt':lines.append(lines[-1])
    with pytest.raises(m.GateError):
        asyncio.run(m.execute(child(lines,'sys.exit(1)' if kind=='nonzero' else ''),control,timeout=5))


def test_commands_pin_run_identity_and_privileged_operation_inventory(monkeypatch):
    m=module();project='gateway-cube-'+'a'*16
    monkeypatch.setenv('GATEWAY_CUBE_RUN_LABEL',project+'-local')
    node,control=m.commands(project,project+'-local')
    assert node[:5]==['docker','compose','--project-name',project,'-f']
    assert 'source-bootstrap' not in node and 'cube-reviewed' in node
    assert node[-1]=='tests/runtime/bigquery-serving.test.cjs'
    with pytest.raises(m.GateError):m.commands(project,'123-1')
    with pytest.raises(m.GateError):asyncio.run(control('drop_database'))


def test_protocol_failure_checkpoints_are_admitted_by_bounded_diagnostics():
    m=module()
    from scripts.pytest_runtime_diagnostics import CHECKPOINTS
    assert m.FAILURES|{'source-runtime-wrapper','source-runtime-control','source-runtime-child'}<=CHECKPOINTS
