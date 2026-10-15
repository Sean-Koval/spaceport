import importlib.util
import pytest
from types import SimpleNamespace

pytestmark=pytest.mark.unit


def runtime():
    assert importlib.util.find_spec('gateway.source_pilot_runtime') is not None,'Reviewed source runtime missing'
    import gateway.source_pilot_runtime as value
    return value


def test_context_secret_reader_is_lazy_and_rejects_other_context(monkeypatch):
    module=runtime();reads=[]
    from tests.unit.test_source_pilot_builder import source
    sources=SimpleNamespace(sources=(source('consumer'),source('fixture_test')))
    monkeypatch.setattr(module,'_read_file',lambda path,limit:reads.append(path) or b'fake-value\n')
    resolver=module.source_secret_resolver(sources,'fixture_test')
    assert reads==[]
    with pytest.raises(ValueError):resolver('/run/secrets/domo_consumer_id')
    assert reads==[] and resolver('/run/secrets/domo_fixture_test_id')=='fake-value'
    assert reads==['/run/secrets/domo_fixture_test_id']


def test_runtime_identity_refuses_legacy_selector():
    module=runtime()
    from tests.unit.test_source_pilot_config import config
    from gateway.source_pilot_config import SourcePilotConfig
    value=SourcePilotConfig.model_validate(config())
    assert module._identity(value).source=='reviewed_sources'
    settings=value.identity_settings().model_copy(update={'source':'synthetic_postgres'})
    with pytest.raises(ValueError):module._identity(SimpleNamespace(identity_settings=lambda:settings,mcp_resource_url=value.mcp_resource_url))


def test_actual_mcp_manager_lifespan_closes_cleanly(monkeypatch,tmp_path,clock):
    import asyncio
    from unittest.mock import AsyncMock
    from tests.unit.test_pilot_runtime import service_runtime,lifespan
    from tests.unit.test_source_pilot_config import config
    from gateway.source_pilot_config import SourcePilotConfig
    module=runtime();value=SourcePilotConfig.model_validate(config())
    built,requests=service_runtime(value,clock)
    monkeypatch.setattr(module,'build_runtime',AsyncMock(return_value=built))
    async def scenario():
        application=module.SourceApplication(value,tmp_path)
        async with lifespan(application):assert application._state=='started'
        assert application._state=='stopped'
        built.close.assert_awaited_once()
        assert requests==[]
    asyncio.run(scenario())
