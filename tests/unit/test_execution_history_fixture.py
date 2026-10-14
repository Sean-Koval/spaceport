"""SQL-emission checks for owner-only history setup, not PostgreSQL proof."""
from datetime import timedelta

import psycopg
import pytest
from sqlalchemy.engine import URL

from tests.integration.conftest import RegistryDatabase
from tests.integration.test_execution_queue import rewind_execution

pytestmark = pytest.mark.unit


class RecordingOwnerConnection:
    """Record transaction/statement boundaries before any backend execution."""
    def __init__(self, *, fail_update=False):
        self.events = []
        self.fail_update = fail_update

    def __enter__(self):
        self.events.append(('begin',))
        return self

    def execute(self, statement, params=()):
        self.events.append(('execute', statement, params))
        if self.fail_update and 'UPDATE gateway.executions' in statement:
            raise psycopg.OperationalError('Synthetic history update failure')

    def __exit__(self, error_type, error, traceback):
        self.events.append(('exit', error_type))


def database_with_owner(monkeypatch, connection):
    url = URL.create('postgresql+psycopg', host='127.0.0.1', port=5432,
        database='synthetic-unused', username='synthetic-owner', password='unused')
    opened = []
    def connect(**kwargs):
        opened.append(kwargs)
        return connection
    monkeypatch.setattr(psycopg, 'connect', connect)
    database = RegistryDatabase(None, {}, {'owner':url}, 'unused', [], [])
    return database, opened


@pytest.mark.parametrize('seconds', [121, 11])
def test_history_rewind_emits_separate_commands_in_one_owner_transaction(monkeypatch, seconds):
    connection = RecordingOwnerConnection()
    database, opened = database_with_owner(monkeypatch, connection)
    rewind_execution(database, 'execution-1', seconds=seconds)
    assert len(opened) == 1
    assert opened[0]['user'] == 'synthetic-owner'
    assert connection.events == [
        ('begin',),
        ('execute', 'ALTER TABLE gateway.executions DISABLE TRIGGER immutable_content', ()),
        ('execute', 'UPDATE gateway.executions SET created_at=created_at-%s, '
            'deadline_at=deadline_at-%s WHERE id=%s',
            (timedelta(seconds=seconds), timedelta(seconds=seconds), 'execution-1')),
        ('execute', 'ALTER TABLE gateway.executions ENABLE TRIGGER immutable_content', ()),
        ('exit', None),
    ]


def test_history_rewind_error_leaves_owner_transaction_exceptional(monkeypatch):
    connection = RecordingOwnerConnection(fail_update=True)
    database, opened = database_with_owner(monkeypatch, connection)
    with pytest.raises(psycopg.OperationalError, match='Synthetic history update failure'):
        rewind_execution(database, 'execution-1', seconds=121)
    assert len(opened) == 1
    # The real psycopg connection rolls back the transaction, including the
    # trigger-disable DDL, on an exceptional exit. This only checks that exit.
    assert connection.events[0] == ('begin',)
    assert connection.events[-1] == ('exit', psycopg.OperationalError)
    assert len([event for event in connection.events if event[0] == 'execute']) == 2
