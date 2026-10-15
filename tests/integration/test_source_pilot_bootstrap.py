"""Real isolated PostgreSQL bootstrap with two explicit evidence classes.

Three scoped policy/replay/lock tests stub only the source grant helper. The
complete-bootstrap test uses the real provisioner and role-limited lifecycle.
Neither class calls a provider transport or uses organizational data.
"""
import asyncio
import pytest
from tests.unit.test_source_pilot_builder import source,write_files

pytestmark=pytest.mark.postgres


def prepared(tmp_path,monkeypatch):
    from gateway.source_pilot_builder import build_weekly_files
    from gateway.source_pilot_config import SourcePilotConfig
    from gateway.source_pilot_assets import load_source_assets
    import gateway.source_lifecycle as lifecycle
    calls=[]
    monkeypatch.setattr(lifecycle,'provision_sources',lambda connection,descriptors,roles,**kwargs:calls.append((descriptors,roles,kwargs)),raising=False)
    files=build_weekly_files(consumer_source=source('consumer'),fixture_source=source('fixture_test'),
        consumer_definition_digest='d'*64,fixture_definition_digest='e'*64)
    write_files(tmp_path,files);config=SourcePilotConfig.model_validate(files['pilot.json'])
    return config,load_source_assets(config,tmp_path),calls


def apply(database,config,assets):
    from gateway.registry.repository import Registry
    from gateway.source_pilot_bootstrap import provision
    async def run():
        owner=Registry(database.urls['owner'].render_as_string(hide_password=False))
        try:await provision(owner,config,assets)
        finally:await owner.close()
    asyncio.run(run())


def test_real_bootstrap_is_idempotent_and_does_not_publish_or_restore_revocation(registry_database,tmp_path,monkeypatch):
    config,assets,calls=prepared(tmp_path,monkeypatch)
    apply(registry_database,config,assets)
    assert registry_database.scalar('SELECT count(*) FROM gateway.principals')==8
    assert registry_database.scalar('SELECT count(*) FROM gateway.binding_revisions')==2
    assert registry_database.scalar('SELECT count(*) FROM gateway.products')==0
    registry_database.execute_owner("UPDATE gateway.principals SET active=false WHERE id='usage-consumer-all'")
    registry_database.execute_owner("DELETE FROM gateway.principal_grants WHERE principal_id='usage-consumer-core'")
    apply(registry_database,config,assets)
    assert registry_database.scalar("SELECT active FROM gateway.principals WHERE id='usage-consumer-all'") is False
    assert registry_database.scalar("SELECT count(*) FROM gateway.principal_grants WHERE principal_id='usage-consumer-core'")==0
    assert len(calls)==2


def test_real_replay_missing_principal_does_not_recreate_revoked_identity(registry_database,tmp_path,monkeypatch):
    config,assets,calls=prepared(tmp_path,monkeypatch)
    apply(registry_database,config,assets)
    registry_database.execute_owner("DELETE FROM gateway.principal_grants WHERE principal_id='usage-consumer-core'")
    registry_database.execute_owner("DELETE FROM gateway.principals WHERE id='usage-consumer-core'")
    with pytest.raises(ValueError):apply(registry_database,config,assets)
    assert registry_database.scalar("SELECT count(*) FROM gateway.principals WHERE id='usage-consumer-core'")==0


def test_complete_bootstrap_uses_real_source_provisioner_and_separate_runtime_roles(registry_database,tmp_path):
    from tests.integration.source_pilot_domo_seed import seed_domo_pilot
    async def scenario():
        async with seed_domo_pilot(registry_database,tmp_path/'package') as seed:
            assert seed.principals['usage-author'].kind=='user'
            assert set(seed.lifecycles)=={'consumer','fixture_test'}
            assert registry_database.scalar('SELECT count(*) FROM gateway.principals')==8
            assert registry_database.scalar('SELECT count(*) FROM gateway.products')==0
            async with seed.registry.transaction() as uow:
                from sqlalchemy import text
                assert await uow.session.scalar(text('SELECT gateway.source_check_epoch(:epoch)'),{'epoch':seed.config.deployment_epoch}) is True
    asyncio.run(scenario())


def test_provision_waits_for_admission_capacity_before_policy_locks(registry_database,tmp_path,monkeypatch):
    config,assets,_=prepared(tmp_path,monkeypatch);apply(registry_database,config,assets)
    from gateway.registry.repository import Registry
    from gateway.source_pilot_bootstrap import provision
    from gateway.execution.queue import lock_execution_capacity
    from sqlalchemy import event,text
    async def scenario():
        owner=Registry(registry_database.urls['owner'].render_as_string(hide_password=False));attempted=asyncio.Event();task=None
        def statement(_connection,_cursor,sql,_parameters,_context,_many):
            if 'capacity_locks' in sql:attempted.set()
        event.listen(owner.engine.sync_engine,'before_cursor_execute',statement)
        try:
            async with registry_database.registry.transaction() as admission:
                await lock_execution_capacity(admission)
                task=asyncio.create_task(provision(owner,config,assets))
                await asyncio.wait_for(attempted.wait(),1)
                # Admission can still take policy authority while provisioning
                # waits on the capacity-first lock, so the cycle is impossible.
                assert await admission.session.scalar(text("SELECT id FROM gateway.policies WHERE id=:id FOR UPDATE NOWAIT"),
                    {'id':assets.packages.packages[0].package.manifest.policy_id}) is not None
                assert not task.done()
            await asyncio.wait_for(task,3)
        finally:
            if task is not None and not task.done():
                task.cancel()
                try:await task
                except asyncio.CancelledError:pass
            await owner.close()
    asyncio.run(scenario())
