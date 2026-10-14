"""Current identity/grant reads through the real migrated PostgreSQL app role."""
import asyncio

import pytest

from gateway.contracts.errors import GatewayError


def store(database):
    from gateway.auth.principals import PrincipalStore
    return PrincipalStore(database.registry.sessions)


def seed(database):
    # Only the fixture's operator/owner has authority to bootstrap identity.
    from gateway.auth.principals import oidc_subject_key
    database.execute_owner("""INSERT INTO gateway.principals (id,subject,kind,service_client_id)
        VALUES ('test-user',%s,'user',NULL),
               ('service-only','service-subject','service','service-client')""",
        (oidc_subject_key("https://issuer.example.test", "verified-user"),))
    database.execute_owner("""INSERT INTO gateway.policies (id,current_revision) VALUES ('policy-a',1),('policy-b',1);
        INSERT INTO gateway.policy_revisions (id,policy_id,revision,content,content_digest,actor_id)
        VALUES ('rev-a','policy-a',1,'{}',repeat('a',64),'operator'),
               ('rev-b','policy-b',1,'{}',repeat('b',64),'operator');
        INSERT INTO gateway.principal_grants (principal_id,policy_id,tenant_id,roles,entitlements)
        VALUES ('test-user','policy-a','north','["consumer"]','{"groups":["north"]}'),
               ('test-user','policy-b','south','["publisher"]','{"groups":["south"]}');""")


@pytest.mark.postgres
def test_current_grants_preserve_policy_and_tenant_scopes_without_merging(registry_database):
    seed(registry_database)
    async def scenario():
        identities = store(registry_database)
        principal = await identities.resolve_subject("verified-user", issuer="https://issuer.example.test")
        assert principal.id == "test-user" and principal.kind == "user"
        assert principal.subject == "verified-user"
        with pytest.raises(GatewayError) as caught:
            await identities.resolve_subject("verified-user", issuer="https://other-issuer.test")
        assert caught.value.code == "UNAUTHENTICATED"
        grants = await identities.get_current(principal.id)
        assert grants.principal_id == "test-user"
        assert [(grant.policy_id, grant.tenant_id, grant.capabilities) for grant in grants.grants] == [
            ("policy-a", "north", ("describe", "discover", "execute", "result")),
            ("policy-b", "south", ("publish",)),
        ]
        assert grants.grants[0].entitlements["groups"] == ("north",)
        assert grants.grants[1].entitlements["groups"] == ("south",)
        with pytest.raises(TypeError):
            grants.grants[0].entitlements["groups"] = ("south",)
        # The same read can participate in a caller-owned audit/policy transaction.
        async with registry_database.registry.transaction() as uow:
            again = await identities.get_current(principal.id, session=uow.session)
            assert again == grants
    asyncio.run(scenario())


@pytest.mark.postgres
def test_grant_and_principal_revocation_take_effect_on_next_read(registry_database):
    seed(registry_database)
    async def scenario():
        identities = store(registry_database)
        assert len((await identities.get_current("test-user")).grants) == 2
        registry_database.execute_owner("UPDATE gateway.principal_grants SET active=false WHERE principal_id='test-user' AND policy_id='policy-a'")
        remaining = await identities.get_current("test-user")
        assert len(remaining.grants) == 1 and remaining.grants[0].policy_id == "policy-b"
        registry_database.execute_owner("UPDATE gateway.principals SET active=false WHERE id='test-user'")
        with pytest.raises(GatewayError) as caught:
            await identities.get_current("test-user")
        assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"
        with pytest.raises(GatewayError) as caught:
            await identities.resolve_subject("verified-user", issuer="https://issuer.example.test")
        assert caught.value.code == "UNAUTHENTICATED"
    asyncio.run(scenario())


@pytest.mark.postgres
def test_unknown_roles_or_malformed_entitlements_fail_closed(registry_database):
    seed(registry_database)
    async def scenario():
        identities = store(registry_database)
        registry_database.execute_owner("UPDATE gateway.principal_grants SET roles='[\"invented-superuser\"]' WHERE policy_id='policy-a'")
        with pytest.raises(GatewayError) as caught:
            await identities.get_current("test-user")
        assert caught.value.code == "UPSTREAM_UNAVAILABLE"
        registry_database.execute_owner("UPDATE gateway.principal_grants SET roles='[\"consumer\"]',entitlements='{\"groups\":\"unbounded-shape\"}' WHERE policy_id='policy-a'")
        with pytest.raises(GatewayError) as caught:
            await identities.get_current("test-user")
        assert caught.value.code == "UPSTREAM_UNAVAILABLE"
    asyncio.run(scenario())


@pytest.mark.postgres
def test_application_cannot_bootstrap_or_expand_principal_grants(registry_database):
    seed(registry_database)
    assert registry_database.denied_as_app("UPDATE gateway.principal_grants SET roles='[\"operator\"]' WHERE principal_id='test-user'") == "permission_denied"
    assert registry_database.denied_as_app("INSERT INTO gateway.principals (id,subject,kind) VALUES ('forged-user','forged','user')") == "permission_denied"
    assert registry_database.denied_as_app("UPDATE gateway.principals SET active=false WHERE id='test-user'") == "permission_denied"


@pytest.mark.postgres
def test_identity_reads_fail_closed_when_app_loses_store_access(registry_database):
    seed(registry_database)
    registry_database.execute_owner('REVOKE SELECT ON gateway.principals FROM "' + registry_database.roles["app"] + '"')
    async def scenario():
        identities = store(registry_database)
        with pytest.raises(GatewayError) as caught:
            await identities.resolve_principal("service-only")
        assert caught.value.code == "UNAUTHENTICATED"
        with pytest.raises(GatewayError) as caught:
            await identities.get_current("test-user")
        assert caught.value.code == "UPSTREAM_UNAVAILABLE"
    asyncio.run(scenario())


@pytest.mark.postgres
def test_current_grants_refresh_existing_transaction_identity_map(registry_database):
    seed(registry_database)
    async def scenario():
        identities = store(registry_database)
        principal = await identities.resolve_subject("verified-user", issuer="https://issuer.example.test")
        async with registry_database.registry.transaction() as uow:
            from gateway.registry.models import PrincipalGrant
            loaded = await uow.session.get(PrincipalGrant, ("test-user", "policy-a", "north"))
            assert loaded.roles == ["consumer"]
            first = await identities.get_current(principal.id, session=uow.session)
            assert first.grants[0].capabilities == ("describe", "discover", "execute", "result")
            registry_database.execute_owner("UPDATE gateway.principal_grants SET roles='[\"author\"]',entitlements='{\"groups\":[\"reviewed-north\"]}' WHERE principal_id='test-user' AND policy_id='policy-a'")
            refreshed = await identities.get_current(principal.id, session=uow.session)
            assert refreshed.grants[0].capabilities == ("author",)
            assert refreshed.grants[0].entitlements["groups"] == ("reviewed-north",)
            assert loaded.roles == ["author"]
    asyncio.run(scenario())
