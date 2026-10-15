"""Real disposable driver/session option checks, not Cloud SQL or TLS acceptance."""
import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from gateway import pilot_config
from gateway.registry.repository import Registry

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('role', ['app', 'audit_retention'])
def test_pilot_serving_options_reach_each_fresh_database_session(registry_database, monkeypatch, role):
    root = Path(__file__).resolve().parents[2]
    config = pilot_config.PilotRuntimeConfig.model_validate(json.loads(
        (root / 'infra/gke/pilot.example.json').read_text()))
    # Extract only non-secret connection options from the real config contract.
    # The actual connection remains the independently guarded disposable fixture.
    monkeypatch.setattr(pilot_config, 'read_secret', lambda _: 'a' * 43)
    options = make_url(config.database_url(role)).query['options']
    url = registry_database.urls[role].update_query_dict({'options': options})

    async def check():
        registry = Registry(url.render_as_string(hide_password=False))
        try:
            for _ in range(2):  # NullPool creates a fresh connection for each session.
                async with registry.sessions() as session:
                    values = (await session.execute(text(
                        "SELECT current_setting('statement_timeout'), current_setting('lock_timeout')"
                    ))).one()
                    assert tuple(values) == ('4s', '2s')
        finally:
            await registry.close()

    asyncio.run(check())
