"""Receipt/pin checks through real SDK subprocesses; business services are doubles."""
import asyncio
import json
import os

import pytest

from gateway.api.catalog import project_product
from gateway.contracts.execution import ExecutionView, ResultEnvelope
from tests.contract.test_mcp import PROTOCOL, running_contract as running, services
from tests.unit.test_stored_results import TransactionProbe

pytestmark = pytest.mark.contract

CLIENTS = [
    pytest.param(['.venv/bin/python', 'clients/python_agent.py'], id='python'),
    pytest.param(['node', 'clients/typescript_agent.ts'], id='typescript'),
]


@pytest.mark.parametrize('command', CLIENTS)
@pytest.mark.parametrize('profile,expected_calls', [
    ('correct', 5), ('initial_execution', 1), ('initial_product', 1),
    ('accepted_execution', 1), ('running_execution', 2),
    ('final_execution', 3), ('page_execution', 4),
])
def test_clients_validate_each_result_against_receipt_and_pin(
    north_principal, clock, tmp_path, command, profile, expected_calls,
):
    async def scenario():
        svc = services(north_principal, clock)
        probe = TransactionProbe(clock, count=451)
        description = project_product(probe.product, probe.context.decision,
            probe.context.binding, 'published')
        svc.catalog.search.return_value = {
            'items': [description.model_dump(mode='json')], 'next_cursor': None}
        svc.catalog.describe.return_value = description
        # This is the independent submission receipt used by every request.
        svc.execution.submit_and_wait.return_value = ExecutionView(
            execution_id='execution-1', state='accepted', accepted_at=clock.now())
        fetched = 0

        async def get(actor, execution_id, cursor=None):
            nonlocal fetched
            fetched += 1
            assert actor == north_principal and execution_id == 'execution-1'
            if profile not in {'initial_execution', 'initial_product'} and fetched <= 2:
                state = 'accepted' if fetched == 1 else 'running'
                wrong = (profile == 'accepted_execution' and fetched == 1
                    or profile == 'running_execution' and fetched == 2)
                return ExecutionView(execution_id='wrong-pending-execution' if wrong else execution_id,
                    state=state, accepted_at=clock.now(),
                    started_at=clock.now() if state == 'running' else None)
            offset = 0 if cursor is None else int(cursor.removeprefix('page-'))
            values = probe.envelope.model_dump(mode='json')
            values['rows'] = values['rows'][offset:offset + 200]
            values['next_cursor'] = f'page-{offset + 200}' if offset + 200 < 451 else None
            if profile in {'initial_execution', 'final_execution'} or profile == 'page_execution' and offset:
                values['execution_id'] = 'unrequested-execution'
            if profile == 'initial_product':
                values['product_id'] = 'unrequested-product'
            return ResultEnvelope.model_validate(values)

        svc.results.get.side_effect = get
        token = tmp_path / 'token'
        token.write_text('synthetic')
        token.chmod(0o600)
        async with running(svc) as (base, _):
            process = await asyncio.create_subprocess_exec(*command,
                env={**os.environ, 'DPG_MCP_URL': base + '/mcp', 'DPG_TOKEN_FILE': str(token)},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        if profile == 'correct':
            assert process.returncode == 0, stderr.decode()
            assert stderr == b''
            result = json.loads(stdout)
            assert result['compatibility']['protocol'] == PROTOCOL
            assert result['answer']['rows'] == probe.envelope.model_dump(mode='json')['rows']
            assert result['answer']['product_id'] == description.product_id
            assert result['answer']['row_count'] == 451
        else:
            assert process.returncode == 1, (profile, stdout.decode(), stderr.decode())
            assert stdout == b''
            assert stderr == b'{"error":"CLIENT_ACCEPTANCE_FAILED"}\n'
        assert fetched == expected_calls
    asyncio.run(scenario())
