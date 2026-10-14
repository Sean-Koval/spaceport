"""Genuine disposable PostgreSQL checks for explicit pilot identity provisioning."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from gateway.auth.principals import PrincipalStore, oidc_subject_key
from gateway.auth.settings import Settings
from gateway.contracts.canonical import canonical_digest
from gateway.contracts.errors import GatewayError
from gateway.pilot_bootstrap import load_identity_mapping, provision
from gateway.registry.models import PolicyRevision, PolicyRow
from gateway.registry.repository import Registry
from gateway.runtime_config import load_assets

pytestmark = pytest.mark.postgres


def pilot_config(*, issuer='https://issuer.example.test'):
    settings = Settings(mode='production', source='synthetic_postgres', oidc_issuer=issuer,
        oidc_audience='spaceport', oidc_jwks_url=issuer + '/jwks',
        oidc_client_ids=['publisher-ui'], oidc_access_token_profile='rfc9068')
    return SimpleNamespace(identity_settings=lambda: settings, probe_principal_id='north-user', load_assets=load_assets)


def mapping_file(tmp_path, cfg, *, author_consumer=False):
    data = {'version': 1, 'principals': [
        {'id': 'north-user', 'kind': 'user', 'subject': 'synthetic-reviewed-north',
         'grants': [{'tenant_id': 'north', 'roles': ['consumer']}]},
        {'id': 'author-user', 'kind': 'user', 'subject': 'synthetic-reviewed-author',
         'grants': [{'tenant_id': 'north', 'roles': ['author', 'consumer'] if author_consumer else ['author']}]},
        {'id': 'publisher-user', 'kind': 'user', 'subject': 'synthetic-reviewed-publisher',
         'grants': [{'tenant_id': 'north', 'roles': ['publisher']}]},
    ]}
    path = tmp_path / 'identities.json'
    path.write_text(json.dumps(data)); path.chmod(0o600)
    return load_identity_mapping(path, cfg)


def provision_once(database, cfg, mapping):
    async def run():
        owner = Registry(database.urls['owner'].render_as_string(hide_password=False))
        try:
            await provision(owner, cfg, mapping)
        finally:
            await owner.close()
    asyncio.run(run())


def test_pilot_bootstrap_stores_issuer_bound_users_and_inert_fixture_identities(registry_database, tmp_path):
    cfg = pilot_config(); mapping = mapping_file(tmp_path, cfg)
    provision_once(registry_database, cfg, mapping)
    assert registry_database.scalar('SELECT count(*) FROM gateway.principals') == 6
    assert registry_database.scalar('SELECT count(*) FROM gateway.principals WHERE kind=\'user\'') == 3
    assert registry_database.scalar("SELECT count(*) FROM gateway.principals WHERE kind='demo' AND id IN ('fixture-north','fixture-south','fixture-denied')") == 3
    assert registry_database.scalar("SELECT count(*) FROM gateway.principals WHERE id LIKE 'demo-%'") == 0
    assert registry_database.scalar("SELECT subject FROM gateway.principals WHERE id='north-user'") == oidc_subject_key(cfg.identity_settings().oidc_issuer, 'synthetic-reviewed-north')
    assert registry_database.scalar('SELECT count(*) FROM gateway.principal_grants') == 5
    assert registry_database.scalar("SELECT count(*) FROM gateway.principal_grants WHERE entitlements = jsonb_build_object('groups',jsonb_build_array(tenant_id))") == 5
    assert registry_database.scalar('SELECT count(*) FROM gateway.binding_revisions') == 2
    for table in ('products', 'draft_revisions', 'product_versions', 'test_runs', 'audit_events'):
        assert registry_database.scalar('SELECT count(*) FROM gateway.' + table) == 0
    async def resolve():
        identities = PrincipalStore(registry_database.registry.sessions)
        actual = await identities.resolve_subject('synthetic-reviewed-north', issuer=cfg.identity_settings().oidc_issuer)
        assert actual.id == 'north-user' and actual.kind == 'user'
        with pytest.raises(GatewayError):
            await identities.resolve_subject('synthetic-reviewed-north', issuer='https://other.example.test')
        with pytest.raises(GatewayError):
            await identities.resolve_subject('synthetic-fixture-north', issuer=cfg.identity_settings().oidc_issuer)
    asyncio.run(resolve())


def test_pilot_service_mapping_remains_distinct_from_oidc_user_lookup(registry_database, tmp_path):
    cfg = pilot_config(); mapping_file(tmp_path, cfg)
    path = tmp_path / 'identities.json'; data = json.loads(path.read_text())
    data['principals'].append({'id': 'report-service', 'kind': 'service',
        'service_client_id': 'synthetic-reviewed-north',
        'grants': [{'tenant_id': 'south', 'roles': ['consumer']}]})
    path.write_text(json.dumps(data))
    hashes = tmp_path / 'hashes.json'
    hashes.write_text(json.dumps({'version': 1, 'credentials': [
        {'digest': 'a' * 64, 'principal_id': 'report-service', 'kind': 'service', 'active': False}]}))
    hashes.chmod(0o600)
    settings = cfg.identity_settings().model_copy(update={'credential_hash_file': str(hashes)})
    cfg.identity_settings = lambda: settings
    mapping = load_identity_mapping(path, cfg)
    provision_once(registry_database, cfg, mapping)
    assert registry_database.scalar("SELECT kind FROM gateway.principals WHERE id='report-service'") == 'service'
    assert registry_database.scalar("SELECT service_client_id FROM gateway.principals WHERE id='report-service'") == 'synthetic-reviewed-north'
    async def resolve():
        identities = PrincipalStore(registry_database.registry.sessions)
        user = await identities.resolve_subject('synthetic-reviewed-north', issuer=settings.oidc_issuer)
        service = await identities.resolve_principal('report-service')
        assert user.id == 'north-user' and service.kind == 'service'
        assert user.id != service.id
    asyncio.run(resolve())


def test_pilot_rerun_preserves_revocations_deleted_grants_and_operator_policy_head(registry_database, tmp_path):
    cfg = pilot_config(); mapping = mapping_file(tmp_path, cfg)
    provision_once(registry_database, cfg, mapping)
    registry_database.execute_owner("UPDATE gateway.principals SET active=false WHERE id='north-user'; UPDATE gateway.principal_grants SET active=false WHERE principal_id='publisher-user'; DELETE FROM gateway.principal_grants WHERE principal_id='author-user'; UPDATE gateway.principal_grants SET roles='[\"author\"]', entitlements='{\"groups\":[\"operator-edited\"]}' WHERE principal_id='fixture-north'")
    async def policy_change():
        owner = Registry(registry_database.urls['owner'].render_as_string(hide_password=False))
        try:
            async with owner.transaction() as uow:
                original = await uow.session.scalar(select(PolicyRevision).where(PolicyRevision.policy_id == 'revenue-access'))
                revised = dict(original.content); revised['reason'] = 'Operator-owned revised policy'
                uow.session.add(PolicyRevision(id='operator-policy-2', policy_id='revenue-access', revision=2,
                    content=revised, content_digest=canonical_digest(revised), actor_id='operator'))
                await uow.session.flush()
                (await uow.session.get(PolicyRow, 'revenue-access')).current_revision = 2
        finally:
            await owner.close()
    asyncio.run(policy_change())
    provision_once(registry_database, cfg, mapping)
    assert registry_database.scalar("SELECT active FROM gateway.principals WHERE id='north-user'") is False
    assert registry_database.scalar("SELECT active FROM gateway.principal_grants WHERE principal_id='publisher-user'") is False
    assert registry_database.scalar("SELECT count(*) FROM gateway.principal_grants WHERE principal_id='author-user'") == 0
    assert registry_database.scalar("SELECT entitlements FROM gateway.principal_grants WHERE principal_id='fixture-north'") == {'groups': ['operator-edited']}
    assert registry_database.scalar("SELECT current_revision FROM gateway.policies WHERE id='revenue-access'") == 2


@pytest.mark.parametrize('change', ['grants', 'issuer', 'subject', 'missing_fixture'])
def test_pilot_rerun_rejects_divergent_immutable_mapping_without_writing(registry_database, tmp_path, change):
    cfg = pilot_config(); mapping = mapping_file(tmp_path, cfg)
    provision_once(registry_database, cfg, mapping)
    if change == 'grants': mapping = mapping_file(tmp_path, cfg, author_consumer=True)
    elif change == 'issuer': cfg = pilot_config(issuer='https://other.example.test'); mapping = mapping_file(tmp_path, cfg)
    elif change == 'subject': registry_database.execute_owner("UPDATE gateway.principals SET subject='operator-changed-subject' WHERE id='north-user'")
    elif change == 'missing_fixture': registry_database.execute_owner("DELETE FROM gateway.principals WHERE id='fixture-denied'")
    before = registry_database.scalar('SELECT count(*) FROM gateway.principals')
    with pytest.raises(ValueError, match='reviewed bootstrap'):
        provision_once(registry_database, cfg, mapping)
    assert registry_database.scalar('SELECT count(*) FROM gateway.principals') == before
    assert registry_database.scalar('SELECT count(*) FROM gateway.policy_revisions') == 1
    assert registry_database.scalar('SELECT count(*) FROM gateway.binding_revisions') == 2


@pytest.mark.parametrize('partial', ['principal', 'policy', 'binding', 'product', 'audit'])
def test_pilot_bootstrap_rejects_partially_populated_registry_atomically(registry_database, tmp_path, partial):
    if partial == 'principal':
        registry_database.execute_owner("INSERT INTO gateway.principals (id,subject,kind) VALUES ('existing','existing','user')")
    elif partial == 'policy':
        registry_database.execute_owner("INSERT INTO gateway.policies (id,current_revision) VALUES ('other-policy',1); INSERT INTO gateway.policy_revisions (id,policy_id,revision,content,content_digest,actor_id) VALUES ('other-policy-1','other-policy',1,'{}',repeat('a',64),'operator')")
    elif partial == 'binding':
        registry_database.execute_owner("INSERT INTO gateway.binding_revisions (id,binding_id,revision,content,content_digest,model_bundle_digest,adapter_revision,actor_id) VALUES ('other-binding-1','other-binding',1,'{}',repeat('a',64),repeat('b',64),'cube-rest-v1','operator')")
    elif partial == 'product':
        registry_database.execute_owner("INSERT INTO gateway.products (id) VALUES ('existing-product')")
    elif partial == 'audit':
        registry_database.execute_owner("INSERT INTO gateway.audit_events (id,actor_id,action,decision,correlation_id) VALUES ('existing-audit','operator','operation','deny','synthetic-correlation')")
    cfg = pilot_config(); mapping = mapping_file(tmp_path, cfg)
    with pytest.raises(ValueError, match='empty registry'):
        provision_once(registry_database, cfg, mapping)
    assert registry_database.scalar("SELECT count(*) FROM gateway.principals WHERE id='north-user'") == 0
    assert registry_database.scalar("SELECT count(*) FROM gateway.policies WHERE id='revenue-access'") == 0


def test_pilot_bootstrap_transaction_rolls_back_if_grant_insert_is_rejected(registry_database, tmp_path):
    registry_database.execute_owner("CREATE FUNCTION gateway.reject_pilot_grant() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic rejection' USING ERRCODE='42501'; END $$; CREATE TRIGGER reject_pilot_grant BEFORE INSERT ON gateway.principal_grants FOR EACH ROW EXECUTE FUNCTION gateway.reject_pilot_grant()")
    cfg = pilot_config(); mapping = mapping_file(tmp_path, cfg)
    with pytest.raises(GatewayError):
        provision_once(registry_database, cfg, mapping)
    for table in ('principals', 'policies', 'policy_revisions', 'binding_revisions', 'principal_grants'):
        assert registry_database.scalar('SELECT count(*) FROM gateway.' + table) == 0


def test_application_role_cannot_run_readonly_pilot_rerun(registry_database, tmp_path):
    cfg = pilot_config(); mapping = mapping_file(tmp_path, cfg)
    provision_once(registry_database, cfg, mapping)
    with pytest.raises(ValueError, match='owner'):
        asyncio.run(provision(registry_database.registry, cfg, mapping))


def test_concurrent_owner_provisioning_serializes_and_is_idempotent(registry_database, tmp_path):
    cfg = pilot_config(); mapping = mapping_file(tmp_path, cfg)
    async def concurrent():
        owners = [Registry(registry_database.urls['owner'].render_as_string(hide_password=False)) for _ in range(2)]
        try:
            await asyncio.gather(*(provision(owner, cfg, mapping) for owner in owners))
        finally:
            await asyncio.gather(*(owner.close() for owner in owners))
    asyncio.run(concurrent())
    assert registry_database.scalar('SELECT count(*) FROM gateway.principals') == 6
    assert registry_database.scalar('SELECT count(*) FROM gateway.policy_revisions') == 1
