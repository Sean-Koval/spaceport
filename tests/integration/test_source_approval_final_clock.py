"""Independent final-instant regression: only the already-checked row expires."""
import copy
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from queue import Queue

import psycopg
import pytest
from psycopg.types.json import Jsonb
from tests.integration.conftest import connection_kwargs
from tests.integration.test_source_approval_registry import approval_assets, install, package_expectation

pytestmark = pytest.mark.postgres


def test_final_clock_catches_earlier_expiry_while_later_unexpired_row_blocks(registry_database, valid_manifest):
    db = registry_database
    original, *_ = approval_assets(db, valid_manifest)
    # IDs are database-generated. Find a valid order without mutating protected
    # rows, functions, triggers, UUID generation, or immutable expiry values.
    for attempt in range(64):
        short = copy.deepcopy(original)
        expiry = db.scalar("SELECT clock_timestamp()+interval '2 second'")
        short['expires_at'] = expiry.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        short['review']['review_id'] = f'synthetic-short-{attempt}'
        long = copy.deepcopy(original)
        long['review']['review_id'] = f'synthetic-long-{attempt}'
        first, second = install(db, short), install(db, long)
        if first['id'] < second['id']:
            break
    else:
        pytest.fail('Could not select database-generated short-before-long UUID order')
    expected = [package_expectation(short, first), package_expectation(long, second)]
    pid = Queue()
    def check():
        try:
            with psycopg.connect(**connection_kwargs(db.urls['app'])) as app:
                app.execute("SET LOCAL lock_timeout='6s'")
                pid.put(app.execute('SELECT pg_backend_pid()').fetchone()[0])
                app.execute('SELECT gateway.source_approval_check_packages(%s)', (Jsonb(expected),))
            return 'allowed'
        except psycopg.Error as error:
            return error.sqlstate
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as blocker, ThreadPoolExecutor(1) as pool:
        blocker.execute('SELECT 1 FROM gateway.source_access_approval_states WHERE approval_id=%s FOR UPDATE', (second['id'],))
        future = pool.submit(check)
        target = pid.get(timeout=2)
        deadline = time.monotonic()+3
        while not db.scalar('SELECT cardinality(pg_blocking_pids(%s))>0', (target,)):
            assert time.monotonic()<deadline, 'batch never reached state-lock barrier'
            db.scalar('SELECT pg_sleep(0.005)')
        blocker.execute('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))', (expiry,))
        assert blocker.execute('SELECT expires_at>clock_timestamp() FROM gateway.source_access_approvals WHERE id=%s', (second['id'],)).fetchone()[0] is True
        blocker.commit()
        assert future.result(timeout=3) == '42501'
    assert db.scalar('SELECT state FROM gateway.source_access_approval_states WHERE approval_id=%s', (first['id'],)) == 'active'
