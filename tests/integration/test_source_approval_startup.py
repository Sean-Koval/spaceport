"""Startup ordering spies; these are not database/provider qualification."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.unit.test_source_approval_tooling import configured_assets, with_approval


def test_missing_approval_stops_before_any_credentials_or_registry_client(tmp_path, monkeypatch):
    from gateway import source_pilot_runtime as runtime
    config, assets = configured_assets(tmp_path)
    events = []
    monkeypatch.setattr(runtime, 'load_source_assets', lambda *args: assets)
    import gateway.source_pilot_bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, 'validate_service_credentials', lambda *args: None)
    monkeypatch.setattr(type(config), 'validate_files', lambda *args, **kwargs: events.append('credential'))
    monkeypatch.setattr(runtime, 'Registry', lambda *args: events.append('registry'))
    with pytest.raises(ValueError, match='Reviewed-source runtime configuration is unavailable'):
        asyncio.run(runtime.build_runtime(config, root=tmp_path))
    assert events == []


def test_second_nonprobe_package_requires_static_approval_before_credentials(tmp_path, monkeypatch):
    from gateway import source_pilot_runtime as runtime
    config, assets = configured_assets(tmp_path)
    config, approval = with_approval(config, assets, tmp_path)
    first = assets.packages.packages[0]
    second = replace(first, package=first.package.model_copy(update={'package_id': 'unapproved-second-package'}))
    assets = replace(assets, packages=SimpleNamespace(packages=(first, second)))
    events = []
    monkeypatch.setattr(runtime, 'load_source_assets', lambda *args: assets)
    import gateway.source_pilot_bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, 'validate_service_credentials', lambda *args: None)
    monkeypatch.setattr(type(config), 'validate_files', lambda *args, **kwargs: events.append('credential'))
    monkeypatch.setattr(runtime, 'Registry', lambda *args: events.append('registry'))
    with pytest.raises(ValueError, match='Reviewed-source runtime configuration is unavailable'):
        asyncio.run(runtime.build_runtime(config, root=tmp_path))
    assert events == []


def seed_package_registry(db, retained):
    """Explicit synthetic owner setup, independent of bootstrap/import commands."""
    import psycopg
    from psycopg.types.json import Jsonb
    from tests.integration.conftest import connection_kwargs
    from gateway.contracts.canonical import canonical_digest, json_value
    from gateway.adapters.reviewed import parse_binding
    from gateway.source_pilot_bootstrap import PolicySeed
    seed = PolicySeed.model_validate(retained.assets['policy_seed_ref'])
    content = seed.content.model_dump(mode='json')
    with psycopg.connect(**connection_kwargs(db.urls['owner'])) as owner:
        owner.execute('INSERT INTO gateway.policies(id,current_revision) VALUES(%s,1)', (seed.policy_id,))
        owner.execute('''INSERT INTO gateway.policy_revisions(id,policy_id,revision,content,content_digest,actor_id)
            VALUES('synthetic-policy',%s,1,%s,%s,'synthetic-owner')''', (seed.policy_id,Jsonb(content),canonical_digest(content)))
        for source, name in ((retained.consumer_source, 'consumer'), (retained.fixture_source, 'fixture')):
            owner.execute('''INSERT INTO gateway.source_pools(capacity_key,provider,physical_identity,max_active,max_dispatches)
                VALUES(%s,'bigquery',%s,2,8000) ON CONFLICT(capacity_key) DO NOTHING''',
                (source.capacity_pool_id,Jsonb(dict(provider='bigquery',billing_project=source.billing_project,location=source.location))))
            owner.execute('''INSERT INTO gateway.source_profiles(source_id,revision,context,provider,capacity_key,descriptor_digest,descriptor)
                VALUES(%s,1,%s,'bigquery',%s,%s,%s)''', (source.source_id,source.context,source.capacity_pool_id,source.digest,Jsonb(source.model_dump(mode='json'))))
            binding = parse_binding(json_value(retained.assets[name+'_binding_ref']))
            owner.execute('''INSERT INTO gateway.binding_revisions(id,binding_id,revision,content,content_digest,model_bundle_digest,adapter_revision,actor_id)
                VALUES(%s,%s,1,%s,%s,%s,'cube-bigquery-v1','synthetic-owner')''',
                (name+'-binding',binding.binding_id,Jsonb(binding.model_dump(mode='json')),binding.digest,binding.model_bundle_digest))


@pytest.mark.postgres
def test_real_owner_import_replay_revoke_and_registry_only_readiness(registry_database, tmp_path):
    from gateway.registry.repository import Registry
    from gateway.source_access_operator import install_approval, revoke_approval, validate_configured_approvals, validate_active_approvals, load_registry_approvals, OperatorError
    config, assets = configured_assets(tmp_path)
    config, approval = with_approval(config, assets, tmp_path)
    retained = assets.packages.packages[0]; db = registry_database
    seed_package_registry(db, retained)
    configured = validate_configured_approvals(config, assets, tmp_path)
    async def scenario():
        owner = Registry(db.urls['owner'].render_as_string(hide_password=False))
        try:
            with pytest.raises(ValueError): await load_registry_approvals(db.registry, configured)
            with pytest.raises(OperatorError): await install_approval(db.registry, retained, approval.canonical_content)
            receipt = await install_approval(owner, retained, approval.canonical_content)
            assert receipt['state'] == 'active' and receipt['replayed'] is False
            active_config, _ = with_approval(config, assets, tmp_path, identity=receipt['approval_id'])
            pins = await load_registry_approvals(db.registry, validate_configured_approvals(active_config, assets))
            await validate_active_approvals(db.registry, pins)
            replay = await install_approval(owner, retained, approval.canonical_content)
            assert replay['approval_id'] == receipt['approval_id'] and replay['replayed'] is True
            await revoke_approval(owner, receipt['approval_id'], receipt['content_digest'], 'synthetic-test-ended')
            with pytest.raises(ValueError): await validate_active_approvals(db.registry, pins)
            replay = await install_approval(owner, retained, approval.canonical_content)
            assert replay['approval_id'] == receipt['approval_id'] and replay['state'] == 'revoked'
            assert db.scalar('SELECT count(*) FROM gateway.source_access_admin_events') == 2
        finally:
            await owner.close()
    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.parametrize('state', ['missing', 'revoked', 'expired'])
def test_real_registry_denial_precedes_every_provider_credential_and_client(registry_database, tmp_path, monkeypatch, state):
    from datetime import timezone
    from gateway import source_pilot_runtime as runtime
    from gateway.registry.repository import Registry
    from gateway.source_access_operator import install_approval, revoke_approval
    from gateway.source_access_contracts import SourceApprovalDocument
    from tests.unit.test_source_approval_tooling import synthetic_approval
    from gateway.source_pilot_config import SourcePilotConfig
    import hashlib
    db = registry_database; config, assets = configured_assets(tmp_path)
    retained = assets.packages.packages[0]; seed_package_registry(db, retained)
    approval = synthetic_approval(retained)
    if state == 'expired':
        expiry = db.scalar("SELECT clock_timestamp()+interval '1 second'")
        approval = SourceApprovalDocument.model_validate(approval.model_dump(mode='json') | {'expires_at': expiry.astimezone(timezone.utc).isoformat().replace('+00:00','Z')})
    raw = approval.canonical_content
    identity = ['11111111-1111-1111-1111-111111111111']
    async def prepare():
        owner = Registry(db.urls['owner'].render_as_string(hide_password=False))
        try:
            if state != 'missing':
                receipt = await install_approval(owner, retained, raw)
                identity[0] = receipt['approval_id']
                if state == 'revoked': await revoke_approval(owner, receipt['approval_id'], receipt['content_digest'], 'synthetic-revoke')
        finally: await owner.close()
    asyncio.run(prepare())
    config = SourcePilotConfig.model_validate(config.model_dump(mode='json') | {'source_approval_refs': [dict(
        package_id=retained.package.package_id, revision=retained.package.revision, approval_id=identity[0],
        approval_revision=approval.revision, approval_digest=approval.content_digest)]})
    if state == 'expired':
        db.scalar('SELECT pg_sleep(greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp()))+0.02))', (expiry,))
        assert db.scalar('SELECT clock_timestamp()>%s', (expiry,))
    events = []
    import gateway.source_pilot_bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, 'validate_service_credentials', lambda *args: None)
    monkeypatch.setattr(type(config), 'validate_files', lambda *args, **kwargs: events.append('registry-auth'))
    monkeypatch.setattr(type(config), 'database_url', lambda *args: db.urls['app'].render_as_string(hide_password=False))
    monkeypatch.setattr(runtime, 'secret_key', lambda *args: events.append('provider-or-signing-secret'))
    monkeypatch.setattr(runtime, 'read_secret', lambda *args: events.append('provider-secret'))
    with pytest.raises(ValueError, match='Reviewed-source runtime configuration is unavailable'):
        asyncio.run(runtime.build_runtime(config, root=tmp_path))
    assert events == ['registry-auth']


@pytest.mark.postgres
@pytest.mark.parametrize('profile', ['legacy', 'reviewed'])
def test_actual_factories_with_real_policy_and_registry_keep_exact_synthetic_only(registry_database, monkeypatch, profile):
    from gateway import runtime as legacy
    from gateway import reviewed_runtime as reviewed
    from gateway.registry.repository import Registry
    from gateway.registry.models import PolicyRow, PolicyRevision, BindingRevision, PrincipalRow, PrincipalGrant
    from gateway.auth.tokens import Authenticator
    from gateway.auth.settings import Settings
    from gateway.auth.principals import PrincipalStore
    from gateway.contracts.canonical import canonical_digest
    from gateway.contracts.errors import GatewayError
    from gateway.contracts.identity import Principal
    from gateway.contracts.execution import PinnedProduct
    from gateway.contracts.manifest import validate_manifest
    from gateway.policy.service import PolicyService
    from gateway.policy.audit import AuditWriter
    from gateway.runtime_config import source_approval_required
    assets = legacy.load_assets() if profile == 'legacy' else reviewed.load_assets()
    db = registry_database; provider_reads = []
    async def scenario():
        owner = Registry(db.urls['owner'].render_as_string(hide_password=False))
        try:
            async with owner.transaction() as uow:
                uow.session.add(PolicyRow(id=assets.policy_id,current_revision=1))
                await uow.session.flush()
                uow.session.add(PolicyRevision(id='synthetic-policy',policy_id=assets.policy_id,revision=1,
                    content=assets.policy_content,content_digest=canonical_digest(assets.policy_content),actor_id='operator'))
                for binding in assets.bindings:
                    uow.session.add(BindingRevision(id=binding.binding_id,binding_id=binding.binding_id,revision=binding.revision,
                        content=binding.model_dump(mode='json'),content_digest=binding.digest,
                        model_bundle_digest=binding.model_bundle_digest,adapter_revision=binding.adapter_revision,actor_id='operator'))
                for account in assets.accounts:
                    uow.session.add(PrincipalRow(id=account['id'],subject='synthetic-'+account['id'],kind='demo'))
                await uow.session.flush()
                for account in assets.accounts:
                    if account['roles']:
                        uow.session.add(PrincipalGrant(principal_id=account['id'],policy_id=assets.policy_id,
                            tenant_id=account['tenant_id'],roles=account['roles'],entitlements=account['entitlements']))
            if profile == 'legacy':
                monkeypatch.setattr(legacy,'Registry',lambda _:db.registry)
                monkeypatch.setattr(legacy,'database_url',lambda _:db.urls['app'].render_as_string(hide_password=False))
                monkeypatch.setattr(legacy,'secret_key',lambda _:b's'*32)
                monkeypatch.setattr(legacy,'read_secret',lambda ref:provider_reads.append(ref))
                runtime = legacy.build_runtime()
            else:
                authenticator = Authenticator(Settings(),PrincipalStore(db.registry.sessions))
                runtime = reviewed.build_runtime(registry=db.registry,authenticator=authenticator,
                    secret_resolver=lambda ref:provider_reads.append(ref),audit_key=b'a'*32,cursor_key=b'b'*32)
            try:
                policy = runtime.services.policy
                assert type(policy) is PolicyService and policy.synthetic_capability is not None
                target = PinnedProduct(manifest=assets.manifest,binding_digest=assets.bindings[0].digest,version_id='synthetic-review')
                author = Principal(id='demo-author',subject='synthetic-demo-author',kind='demo',service_client_id=None)
                async with db.registry.transaction() as uow:
                    decision = await policy.authorize(author,'author',target,uow)
                    assert decision.principal_id == author.id
                modified = assets.manifest.model_dump(mode='json');modified.update(name='Altered package',content_digest=None)
                changed = PinnedProduct(manifest=validate_manifest(modified),binding_digest=target.binding_digest,version_id=target.version_id)
                async with db.registry.transaction() as uow:
                    with pytest.raises(GatewayError): await policy.authorize(author,'author',changed,uow)
                standalone = PolicyService(db.registry,PrincipalStore(db.registry.sessions),AuditWriter(b'c'*32))
                async with db.registry.transaction() as uow:
                    with pytest.raises(GatewayError): await standalone.authorize(author,'author',target,uow)
                token = policy.synthetic_capability
                assert provider_reads == []
            finally:
                await runtime.close()
            assert source_approval_required(target,registry=db.registry,synthetic_capability=token)
        finally:
            await owner.close()
    asyncio.run(scenario())


@pytest.mark.postgres
def test_real_operator_failure_rolls_back_without_receipt_or_private_error(registry_database, tmp_path):
    from gateway.registry.repository import Registry
    from gateway.source_access_operator import install_approval, revoke_approval, OperatorError
    from tests.unit.test_source_approval_tooling import synthetic_approval
    db = registry_database; _, assets = configured_assets(tmp_path); retained = assets.packages.packages[0]
    seed_package_registry(db, retained); approval = synthetic_approval(retained)
    db.execute_owner("CREATE FUNCTION gateway.synthetic_receipt_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'private-review-canary'; END $$")
    trigger = 'CREATE TRIGGER synthetic_receipt_failure BEFORE INSERT ON gateway.source_access_admin_events FOR EACH ROW EXECUTE FUNCTION gateway.synthetic_receipt_failure()'
    async def scenario():
        owner = Registry(db.urls['owner'].render_as_string(hide_password=False))
        try:
            db.execute_owner(trigger)
            with pytest.raises(OperatorError) as failure: await install_approval(owner, retained, approval.canonical_content)
            assert failure.value.uncertain is True and 'private-review-canary' not in str(failure.value)
            assert db.scalar('SELECT count(*) FROM gateway.source_access_approvals') == 0
            db.execute_owner('DROP TRIGGER synthetic_receipt_failure ON gateway.source_access_admin_events')
            receipt = await install_approval(owner, retained, approval.canonical_content)
            db.execute_owner(trigger)
            with pytest.raises(OperatorError) as failure: await revoke_approval(owner, receipt['approval_id'], receipt['content_digest'], 'synthetic-revoke')
            assert 'private-review-canary' not in str(failure.value)
            assert db.scalar('SELECT state FROM gateway.source_access_approval_states') == 'active'
            assert db.scalar('SELECT count(*) FROM gateway.source_access_admin_events') == 1
        finally: await owner.close()
    asyncio.run(scenario())


@pytest.mark.postgres
def test_reference_cli_emits_only_exact_ids_and_denies_after_revoke(registry_database, tmp_path, monkeypatch, capsys):
    import json
    from gateway.registry.repository import Registry
    from gateway.source_access_operator import install_approval, revoke_approval
    from gateway.source_pilot_config import SourcePilotConfig
    from scripts.source_access_approvals import main
    from tests.unit.test_source_approval_tooling import synthetic_approval
    db = registry_database; _, assets = configured_assets(tmp_path); retained = assets.packages.packages[0]
    seed_package_registry(db, retained); approval = synthetic_approval(retained)
    owner_path = tmp_path/'operator-review.json'; owner_path.write_bytes(approval.canonical_content);owner_path.chmod(0o600)
    async def setup():
        owner = Registry(db.urls['owner'].render_as_string(hide_password=False))
        try: return await install_approval(owner, retained, approval.canonical_content)
        finally: await owner.close()
    receipt = asyncio.run(setup())
    monkeypatch.setattr(SourcePilotConfig,'validate_files',lambda *a,**k:None)
    monkeypatch.setattr(SourcePilotConfig,'database_url',lambda *a:db.urls['app'].render_as_string(hide_password=False))
    args = ['reference','--config',str(tmp_path/'pilot.json'),'--package-id',retained.package.package_id,'--revision','1',
        '--approval-file',str(owner_path),'--approval-id',receipt['approval_id'],'--output',str(tmp_path/'runtime-ref.json')]
    assert main(args) == 0
    ref = json.loads((tmp_path/'runtime-ref.json').read_bytes())
    assert set(ref) == {'package_id','revision','approval_id','approval_revision','approval_digest'}
    assert ref['approval_id'] == receipt['approval_id']
    output = capsys.readouterr().out
    assert 'audiences' not in output and 'evidence_ref' not in output and 'runtime-recheck-required' in output
    async def revoke():
        owner = Registry(db.urls['owner'].render_as_string(hide_password=False))
        try: await revoke_approval(owner, receipt['approval_id'], receipt['content_digest'], 'synthetic-revoke')
        finally: await owner.close()
    asyncio.run(revoke())
    args[-1] = str(tmp_path/'after-revoke.json')
    assert main(args) == 2 and not (tmp_path/'after-revoke.json').exists()


@pytest.mark.postgres
def test_operator_cli_roundtrip_uses_owner_only_and_exports_minimal_ref(registry_database, tmp_path, monkeypatch, capsys):
    import json
    from gateway.source_pilot_config import SourcePilotConfig
    from scripts.spaceport import main
    from tests.unit.test_source_approval_tooling import synthetic_approval
    db = registry_database; _, assets = configured_assets(tmp_path)
    retained = assets.packages.packages[0]
    seed_package_registry(db, retained)
    approval = synthetic_approval(retained)
    source = tmp_path/'owner-review.json'
    source.write_bytes(approval.canonical_content); source.chmod(0o600)
    roles = []
    monkeypatch.setattr(SourcePilotConfig, 'validate_files', lambda self, *, role: roles.append(role))
    monkeypatch.setattr(SourcePilotConfig, 'database_url', lambda self, role: db.urls[role].render_as_string(hide_password=False))
    package_args = ['--config', str(tmp_path/'pilot.json'), '--package-id', retained.package.package_id,
        '--revision', str(retained.package.revision), '--approval-file', str(source)]
    install_path, replay_path = tmp_path/'import.json', tmp_path/'replay.json'
    assert main(['approval', 'import', *package_args, '--receipt-file', str(install_path)]) == 0
    installed = json.loads(install_path.read_bytes())
    assert installed['state'] == 'active' and installed['replayed'] is False
    assert main(['approval', 'import', *package_args, '--receipt-file', str(replay_path)]) == 0
    replay = json.loads(replay_path.read_bytes())
    assert replay['approval_id'] == installed['approval_id'] and replay['replayed'] is True
    output = tmp_path/'runtime-ref.json'
    assert main(['approval', 'reference', *package_args, '--approval-id', installed['approval_id'], '--output', str(output)]) == 0
    ref = json.loads(output.read_bytes())
    assert ref == dict(package_id=retained.package.package_id, revision=retained.package.revision,
        approval_id=installed['approval_id'], approval_revision=approval.revision, approval_digest=approval.content_digest)
    revoked_path = tmp_path/'revoke.json'
    assert main(['approval', 'revoke', '--config', str(tmp_path/'pilot.json'),
        '--approval-id', installed['approval_id'], '--expected-digest', approval.content_digest,
        '--reason-code', 'synthetic-cli-ended', '--receipt-file', str(revoked_path)]) == 0
    assert json.loads(revoked_path.read_bytes())['state'] == 'revoked'
    assert roles == ['owner', 'owner', 'app', 'owner']
    assert db.scalar('SELECT count(*) FROM gateway.source_access_admin_events') == 2
    stdout = capsys.readouterr().out
    assert 'audiences' not in stdout and 'synthetic-review' not in stdout and approval.content_digest not in stdout


@pytest.mark.postgres
@pytest.mark.parametrize('provider', ['bigquery', 'domo'])
def test_weekly_journey_seed_installs_exact_test_owner_review(registry_database, tmp_path, provider):
    """Real owner/app bootstrap and approval; never opens a provider connection."""
    from tests.integration.source_pilot_bigquery_seed import seed_bigquery_pilot
    from tests.integration.source_pilot_domo_seed import seed_domo_pilot
    from tests.integration.source_pilot_approval_seed import weekly_approval
    seed = seed_bigquery_pilot if provider == 'bigquery' else seed_domo_pilot
    async def scenario():
        async with seed(registry_database, tmp_path/provider) as state:
            retained = state.assets.packages.packages[0]
            document = weekly_approval(retained)
            assert registry_database.scalar('SELECT count(*) FROM gateway.source_access_approvals WHERE content_digest=%s',
                (document.content_digest,)) == 1
            assert all(principal.service_client_id == 'reviewed-client' for principal in state.principals.values())
            assert registry_database.scalar("SELECT count(*) FROM gateway.principals WHERE id LIKE 'fixture-usage-%%' AND service_client_id IS NULL") == 4
            assert state.policy.source_access is not None
            from gateway.source_access_operator import validate_configured_approvals, load_registry_approvals
            configured = validate_configured_approvals(state.config, state.assets)
            pins = await load_registry_approvals(state.registry, configured)
            assert len(pins) == 1 and pins[0].approval_digest == document.content_digest
            assert len(document.audiences) == 9
    asyncio.run(scenario())
