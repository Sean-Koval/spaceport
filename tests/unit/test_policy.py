"""Pure current-policy derivation and redaction; no database substitute."""
import hashlib
import hmac
import importlib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from gateway.auth.principals import PolicyGrant, PrincipalGrants
from gateway.contracts.canonical import canonical_bytes
from gateway.contracts.errors import GatewayError
from gateway.contracts.execution import PinnedProduct
from gateway.contracts.manifest import validate_manifest

pytestmark = pytest.mark.unit


def api():
    try:
        return SimpleNamespace(service=importlib.import_module("gateway.policy.service"),
            audit=importlib.import_module("gateway.policy.audit"))
    except ModuleNotFoundError as error:
        pytest.fail(f"Policy/audit implementation is missing: {error.name}")


def rules(**changes):
    return {"tenant_ids": ["north", "south"],
        "tenant_constraint": {"member": "Orders.tenant_id", "operator": "eq", "source": "grant.tenant_id"},
        "entitlement_constraint": {"member": "Orders.access_group", "operator": "in", "source": "grant.entitlements.groups"},
        "visible_fields": ["day", "segment", "revenue"], **changes}


def grant(*, policy_id="revenue-access", tenant_id="north", capabilities=("discover", "describe", "execute", "result"), entitlements=None):
    return PolicyGrant(policy_id=policy_id, tenant_id=tenant_id, capabilities=capabilities,
        entitlements={"groups": ["north"]} if entitlements is None else entitlements)


def decision(valid_manifest, *grants, action="execute", policy_rules=None, revision=1):
    return api().service.derive_decision(PrincipalGrants(principal_id="north-user", grants=grants),
        action, PinnedProduct(manifest=validate_manifest(valid_manifest), binding_digest="b" * 64,
            version_id="version-1"), api().service.PolicyRules.model_validate(policy_rules or rules()), revision)


def test_exact_scope_derives_immutable_mandatory_predicates(valid_manifest):
    allowed = decision(valid_manifest, grant())
    assert allowed.tenant_id == "north"
    assert [(item.member, item.operator, item.values) for item in allowed.row_constraints] == [
        ("Orders.tenant_id", "eq", ("north",)), ("Orders.access_group", "in", ("north",))]
    assert allowed.visible_fields == ("day", "segment", "revenue")
    assert allowed.principal_id == "north-user" and allowed.policy_revision == 1
    with pytest.raises(ValidationError):
        allowed.tenant_id = "south"
    with pytest.raises(ValidationError):
        allowed.row_constraints[0].values = ("south",)


@pytest.mark.parametrize("capabilities,action", [(("author",), "policy_admin"), (("author",), "publish"),
    (("publish",), "execute"), (("publish",), "result"), (("operate",), "policy_admin"),
    (("audit_view",), "result"), (("execute",), "author")])
def test_roles_never_imply_other_capabilities(valid_manifest, capabilities, action):
    with pytest.raises(GatewayError) as caught:
        decision(valid_manifest, grant(capabilities=capabilities), action=action)
    assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"


@pytest.mark.parametrize("action", ["discover", "describe", "execute", "result", "author", "publish", "policy_admin", "operate", "audit_view"])
def test_explicit_exact_scope_capability_is_independent(valid_manifest, action):
    assert decision(valid_manifest, grant(capabilities=(action,)), action=action).tenant_id == "north"


@pytest.mark.parametrize("scopes", [(), (grant(policy_id="another-policy"),),
    (grant(tenant_id="unknown"),), (grant(entitlements={}),),
    (grant(capabilities=("publish",)), grant(policy_id="another-policy")),
    (grant(entitlements={}), grant(policy_id="another-policy", entitlements={"groups": ["south"]})),
    (grant(), grant(tenant_id="south", capabilities=("publish",), entitlements={"groups": ["south"]}))])
def test_no_cross_policy_tenant_or_entitlement_union(valid_manifest, scopes):
    with pytest.raises(GatewayError) as caught:
        decision(valid_manifest, *scopes)
    assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"


def test_policy_revision_and_tenant_revocation_are_applied_to_each_derivation(valid_manifest):
    assert decision(valid_manifest, grant()).policy_revision == 1
    with pytest.raises(GatewayError) as caught:
        decision(valid_manifest, grant(), policy_rules=rules(tenant_ids=["south"]), revision=2)
    assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"
    with pytest.raises(GatewayError):
        decision(valid_manifest, grant(), policy_rules=rules(tenant_ids=[]), revision=3)


def test_policy_limits_only_tighten_product_limits(valid_manifest):
    valid_manifest["limits"] = {"max_rows": 40, "page_size": 20}
    allowed = decision(valid_manifest, grant(), policy_rules=rules(limits={"max_rows": 30, "page_size": 25}))
    assert allowed.limits.max_rows == 30 and allowed.limits.page_size == 20
    assert allowed.limits.execution_timeout_seconds == 120


def test_visible_fields_are_a_nonempty_subset_of_manifest_outputs(valid_manifest):
    assert decision(valid_manifest, grant(), policy_rules=rules(visible_fields=["day", "revenue"])).visible_fields == ("day", "revenue")
    with pytest.raises(GatewayError) as caught:
        decision(valid_manifest, grant(), policy_rules=rules(visible_fields=["password"]))
    assert caught.value.code == "UPSTREAM_UNAVAILABLE"


@pytest.mark.parametrize("change", [{"sql": "select secret"}, {"tenant_ids": ["north", "north"]},
    {"tenant_ids": "north"}, {"visible_fields": []}, {"visible_fields": ["day", "day"]},
    {"tenant_constraint": {"member": "Orders.tenant_id", "operator": "in", "source": "grant.tenant_id"}},
    {"tenant_constraint": {"member": "Orders.tenant_id", "operator": "eq", "source": "request.tenant_id"}},
    {"entitlement_constraint": {"member": "Orders.access_group", "operator": "in", "source": "request.groups"}},
    {"entitlement_constraint": {"member": "Orders.tenant_id", "operator": "in", "source": "grant.entitlements.groups"}},
    {"limits": {"max_rows": 1001}}, {"limits": {"max_rows": True}}])
def test_malformed_or_client_derived_policy_rules_are_rejected(change):
    with pytest.raises(ValidationError):
        api().service.PolicyRules.model_validate(rules(**change))


def test_mutated_internal_constructs_cannot_bypass_policy_validation(valid_manifest):
    policy = api().service.PolicyRules.model_construct(**rules(tenant_ids=["north", "north"]))
    pinned = PinnedProduct(manifest=validate_manifest(valid_manifest), binding_digest="b" * 64, version_id="version-1")
    with pytest.raises(GatewayError) as caught:
        api().service.derive_decision(PrincipalGrants(principal_id="north-user", grants=[grant()]), "execute", pinned, policy, 1)
    assert caught.value.code == "UPSTREAM_UNAVAILABLE"


def test_client_filter_cannot_overwrite_derived_tenant_or_entitlement(valid_manifest):
    allowed = decision(valid_manifest, grant())
    from gateway.contracts.query import QueryRequest, validate_query
    with pytest.raises(GatewayError) as caught:
        validate_query(validate_manifest(valid_manifest), QueryRequest(product_id="revenue",
            inputs={"start": "2024-03-09", "end": "2024-03-12", "tenant_id": "south"}))
    assert caught.value.code == "INVALID_INPUT"
    assert allowed.row_constraints[0].values == ("north",)
    narrowed = validate_query(validate_manifest(valid_manifest), QueryRequest(product_id="revenue",
        inputs={"start": "2024-03-09", "end": "2024-03-12"}, filters=[{"field": "segment", "operator": "eq", "value": "B"}]))
    assert narrowed.filters[0].value == "B" and allowed.row_constraints[0].values == ("north",)


def test_parameter_summary_is_keyed_canonical_names_only():
    writer = api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!")
    names, digest = writer.parameter_summary({"start": "secret-value", "end": "another"})
    assert names == ("end", "start")
    expected = hmac.new(b"synthetic-operator-key-32-bytes!!!", b"dpg-audit-parameters-v1\0" + canonical_bytes({"start": "secret-value", "end": "another"}), hashlib.sha256).hexdigest()
    assert digest == expected
    assert digest != hashlib.sha256(canonical_bytes({"start": "secret-value", "end": "another"})).hexdigest()
    assert writer.parameter_summary({"end": "another", "start": "secret-value"}) == (names, digest)
    assert api().audit.AuditWriter(b"different-synthetic-key-32-bytes!!").parameter_summary({"end": "another", "start": "secret-value"})[1] != digest
    assert writer.parameter_summary({"start": "changed", "end": "another"})[1] != digest


def test_omitted_parameter_values_do_not_enter_digest():
    writer = api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!")
    redaction = {"start": "digest", "segment": "omit"}
    first = writer.parameter_summary({"start": "2024-03-09", "segment": "sensitive"}, redaction=redaction)
    second = writer.parameter_summary({"start": "2024-03-09", "segment": "different"}, redaction=redaction)
    assert first == second and first[0] == ("segment", "start")
    with pytest.raises(GatewayError) as caught:
        writer.parameter_summary({"unknown": "sensitive"}, redaction=redaction)
    assert caught.value.code == "INVALID_INPUT" and "sensitive" not in str(caught.value)


@pytest.mark.parametrize("key", [b"", b"short", "synthetic-operator-key-32-bytes!!!", None])
def test_audit_requires_explicit_operator_owned_key(key):
    with pytest.raises(ValueError):
        api().audit.AuditWriter(key)


def test_audit_input_has_only_redacted_bounded_fields(north_principal, valid_manifest):
    writer = api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!")
    product = PinnedProduct(manifest=validate_manifest(valid_manifest), binding_digest="b" * 64, version_id="version-1")
    event = writer.event(north_principal, "execute", product=product, policy_revision=2,
        decision="allow", parameters={"start": "2024-03-09", "end": "2024-03-12", "segment": "A"},
        duration_ms=12, row_count=3, correlation_id="request-1")
    serialized = event.model_dump_json()
    assert event.actor_id == north_principal.id and event.service_client_id is None
    assert event.product_id == "revenue" and event.version_id == "version-1"
    assert event.policy_revision == 2 and event.adapter == "cube-rest-v1"
    assert event.parameter_names == ("end", "segment", "start")
    assert event.row_count == 3 and event.duration_ms == 12
    for raw in ("2024-03-09", "2024-03-12", '"A"', "raw_rows", "sql", "token", "credentials"):
        assert raw not in serialized
    with pytest.raises(ValidationError):
        api().audit.AuditEventInput.model_validate({**event.model_dump(), "raw_results": [{"secret": "value"}]})


@pytest.mark.parametrize("field,value", [("duration_ms", -1), ("row_count", True), ("parameter_digest", "not-a-digest"),
    ("parameter_names", ["name", "name"]), ("parameter_names", ["select * from secret"]), ("action", "raw_sql"), ("decision", "maybe")])
def test_invalid_audit_records_cannot_be_serialized(field, value):
    event = {"actor_id": "north-user", "service_client_id": None, "action": "result", "product_id": "revenue",
        "version_id": "version-1", "policy_revision": 1, "decision": "allow", "parameter_names": [],
        "parameter_digest": None, "duration_ms": None, "row_count": None, "adapter": "cube-rest-v1", "correlation_id": "request-1"}
    with pytest.raises(ValidationError):
        api().audit.AuditEventInput.model_validate({**event, field: value})


def test_publisher_without_data_entitlement_can_publish_but_cannot_query(valid_manifest):
    publishing = decision(valid_manifest, grant(capabilities=("publish",), entitlements={}), action="publish")
    assert publishing.row_constraints[0].values == ("north",)
    with pytest.raises(GatewayError) as caught:
        decision(valid_manifest, grant(capabilities=("publish",), entitlements={}), action="execute")
    assert caught.value.code == "NOT_FOUND_OR_FORBIDDEN"


@pytest.mark.parametrize("reason", ["", "   ", "new\npolicy", "sk-sensitive", "Operator leaked dpg_svc_" + "X" * 43,
    "Copied eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.c2lnbmF0dXJl token", "New secret://vault/key"])
def test_policy_revision_reason_is_bounded_and_secret_safe(reason):
    with pytest.raises(ValidationError):
        api().service.PolicyContent.model_validate({"rules": rules(), "reason": reason})


def test_verified_delegated_client_recorded_without_raw_subject(valid_manifest):
    from gateway.contracts.identity import Principal
    actor = Principal(id="north-user", subject="private-issuer-subject", service_client_id="studio-client", kind="user")
    event = api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!").event(actor, "describe", decision="deny", correlation_id="request-1")
    assert event.service_client_id == "studio-client" and event.actor_id == "north-user"
    assert "private-issuer-subject" not in event.model_dump_json()


@pytest.mark.parametrize("field", ["actor_id", "service_client_id", "product_id", "version_id", "policy_id", "adapter", "correlation_id"])
def test_raw_bearer_cannot_be_used_as_audit_identifier(field):
    event = {"actor_id": "north-user", "action": "execute", "decision": "deny", "correlation_id": "request-1"}
    with pytest.raises(ValidationError):
        api().audit.AuditEventInput.model_validate({**event, field: "dpg_svc_" + "X" * 43})


def test_policy_identity_is_explicit_in_query_and_administrative_audit(valid_manifest, north_principal):
    writer = api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!")
    pinned = PinnedProduct(manifest=validate_manifest(valid_manifest), binding_digest="b" * 64, version_id="version-1")
    query = writer.event(north_principal, "execute", product=pinned, policy_revision=1, decision="allow", correlation_id="request-1")
    assert query.policy_id == "revenue-access"
    admin = writer.event(north_principal, "change_policy", policy_id="missing-policy", policy_revision=None,
        decision="deny", parameters={"reason": "Synthetic change"}, correlation_id="request-2")
    assert admin.policy_id == "missing-policy" and admin.product_id is None
    assert "Synthetic change" not in admin.model_dump_json()


def test_audit_policy_identity_nullable_without_fk_for_missing_policy_denials():
    from gateway.registry.models import AuditEvent
    assert "policy_id" in AuditEvent.__table__.columns
    column = AuditEvent.__table__.columns["policy_id"]
    assert column.nullable and not column.foreign_keys


def test_additive_audit_migration_is_frozen_and_downgrade_fails_closed():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    migration = importlib.import_module("migrations.versions.0002_audit_policy_id")
    assert migration.down_revision == "0001_registry"
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_heads() == ["0002_audit_policy_id"]
    with pytest.raises(RuntimeError, match="downgrade is disabled"):
        migration.downgrade()


def test_raw_bearer_cannot_be_used_as_audit_parameter_name():
    with pytest.raises(ValidationError):
        api().audit.AuditEventInput(actor_id="north-user", action="result", decision="deny",
            parameter_names=["dpg_demo_" + "X" * 43], parameter_digest="a" * 64, correlation_id="request-1")


def test_conflicting_explicit_policy_identity_is_rejected(valid_manifest, north_principal):
    writer = api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!")
    pinned = PinnedProduct(manifest=validate_manifest(valid_manifest), binding_digest="b" * 64, version_id="version-1")
    with pytest.raises(GatewayError) as caught:
        writer.event(north_principal, "execute", product=pinned, policy_id="another-policy", decision="allow")
    assert caught.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("correlation", ["", True, "dpg_svc_" + "X" * 43])
def test_invalid_correlation_is_rejected_without_database_or_raw_error(valid_manifest, north_principal, correlation):
    import asyncio
    from gateway.auth.principals import PrincipalStore
    from gateway.registry.repository import Registry, UnitOfWork
    registry = Registry("postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/synthetic")
    policy = api().service.PolicyService(registry, PrincipalStore(registry.sessions),
        api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!"))
    product = PinnedProduct(manifest=validate_manifest(valid_manifest), binding_digest="b" * 64, version_id="version-1")
    async def scenario():
        async with registry.sessions() as session:
            async with session.begin():
                with pytest.raises(GatewayError) as caught:
                    await policy.authorize(north_principal, "execute", product, UnitOfWork(session), correlation_id=correlation)
                assert caught.value.code == "INVALID_INPUT"
                assert not caught.value.request_id.startswith("dpg_svc_") and "XXXXXXXXXXXXXXXX" not in str(caught.value.as_dict())
        await registry.close()
    asyncio.run(scenario())


RECOGNIZED_EMBEDDED_CREDENTIALS = [
    "trace.dpg_svc_" + "X" * 43,
    "dpg_svc_" + "X" * 43 + ".trace",
    "trace.dpg_demo_" + "X" * 43 + ".tail",
    "trace.eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.c2lnbmF0dXJl",
    "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.c2lnbmF0dXJl.trace",
    "trace.eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.c2lnbmF0dXJl.tail",
    "trace.sk-synthetic-key.tail", "trace.ghp_synthetic-key.tail",
    "trace.github_pat_synthetic-key.tail", "trace.AKIAsynthetic-key.tail",
]


@pytest.mark.parametrize("field", ["actor_id", "service_client_id", "product_id", "version_id", "policy_id", "adapter", "correlation_id"])
@pytest.mark.parametrize("value", RECOGNIZED_EMBEDDED_CREDENTIALS)
def test_recognized_embedded_credentials_are_rejected_in_every_raw_audit_identifier(field, value):
    record = {"actor_id": "north-user", "action": "execute", "decision": "deny", "correlation_id": "request-1"}
    with pytest.raises(ValidationError):
        api().audit.AuditEventInput.model_validate({**record, field: value})


@pytest.mark.parametrize("value", RECOGNIZED_EMBEDDED_CREDENTIALS)
def test_recognized_embedded_credentials_are_rejected_in_audit_parameter_names(value):
    with pytest.raises(ValidationError):
        api().audit.AuditEventInput(actor_id="north-user", action="result", decision="deny", parameter_names=[value],
            parameter_digest="a" * 64, correlation_id="request-1")


@pytest.mark.parametrize("value", RECOGNIZED_EMBEDDED_CREDENTIALS)
def test_rejected_embedded_audit_correlation_is_never_echoed(north_principal, value):
    writer = api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!")
    with pytest.raises(GatewayError) as caught:
        writer.event(north_principal, "execute", decision="deny", correlation_id=value)
    assert caught.value.code == "INVALID_INPUT" and value not in str(caught.value.as_dict())
    assert "dpg_svc_" not in caught.value.request_id and "eyJ" not in caught.value.request_id


@pytest.mark.parametrize("value", RECOGNIZED_EMBEDDED_CREDENTIALS)
def test_parameter_summary_rejects_recognized_embedded_credentials_in_names(value):
    writer = api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!")
    with pytest.raises(GatewayError) as caught:
        writer.parameter_summary({value: "synthetic-value"})
    assert caught.value.code == "INVALID_INPUT" and value not in str(caught.value.as_dict())


@pytest.mark.parametrize("failure_stage", ["identity_read", "grant_read", "policy_read", "policy_invalid", "missing_policy"])
def test_policy_change_state_error_emits_redacted_denial_with_same_request_id(failure_stage):
    """Control-flow/redaction only; genuine transaction durability is tested in PostgreSQL."""
    import asyncio
    from contextlib import asynccontextmanager
    from sqlalchemy.exc import SQLAlchemyError
    from gateway.registry.repository import not_found

    class ControlSession:
        async def rollback(self):
            pass

    class ControlRegistry:
        @asynccontextmanager
        async def transaction(self):
            yield SimpleNamespace(session=ControlSession())

    class RecordCollector(api().audit.AuditWriter):
        def __init__(self):
            super().__init__(b"synthetic-operator-key-32-bytes!!!")
            self.records = []
        async def append(self, uow, event):
            self.records.append(event)

    async def identity(actor, uow):
        if failure_stage == "identity_read":
            raise SQLAlchemyError("raw SQL with synthetic-private-value")
    async def grants(principal_id, *, session):
        if failure_stage == "grant_read":
            raise GatewayError("UPSTREAM_UNAVAILABLE", "store-original", "Current access permissions are unavailable.")
        return PrincipalGrants(principal_id=principal_id, grants=[])
    async def current(policy_id, uow):
        if failure_stage == "policy_read":
            raise SQLAlchemyError("raw SQL with synthetic-private-value")
        if failure_stage == "missing_policy":
            raise not_found()
        raise api().service.policy_unavailable()

    audit = RecordCollector()
    policy = api().service.PolicyService(ControlRegistry(), SimpleNamespace(get_current=grants), audit)
    policy._identity, policy._current = identity, current
    from gateway.contracts.identity import Principal
    actor = Principal(id="north-user", subject="synthetic-north", service_client_id=None, kind="demo")
    async def scenario():
        with pytest.raises(GatewayError) as caught:
            await policy.change_policy(actor, "revenue-access", rules(), "Unstored private request reason")
        expected_code = "NOT_FOUND_OR_FORBIDDEN" if failure_stage == "missing_policy" else "UPSTREAM_UNAVAILABLE"
        assert caught.value.code == expected_code
        if failure_stage == "grant_read":
            assert caught.value.safe_message == "Current access permissions are unavailable."
        assert "raw SQL" not in str(caught.value.as_dict())
        assert len(audit.records) == 1
        event = audit.records[0]
        assert event.decision == "deny" and event.action == "change_policy"
        assert event.policy_id == "revenue-access" and event.actor_id == "north-user"
        assert event.correlation_id == caught.value.request_id
        assert event.parameter_names == () and "Unstored private request reason" not in event.model_dump_json()
    asyncio.run(scenario())


@pytest.mark.parametrize("policy_id", ["invalid policy", "trace.dpg_svc_" + "X" * 43])
def test_invalid_administrative_policy_context_is_rejected_before_database(north_principal, policy_id):
    import asyncio
    from gateway.auth.principals import PrincipalStore
    from gateway.registry.repository import Registry
    registry = Registry("postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/synthetic")
    policy = api().service.PolicyService(registry, PrincipalStore(registry.sessions),
        api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!"))
    async def scenario():
        with pytest.raises(GatewayError) as caught:
            await policy.change_policy(north_principal, policy_id, rules(), "Synthetic valid reason")
        assert caught.value.code == "INVALID_INPUT"
        assert policy_id not in str(caught.value.as_dict())
        await registry.close()
    asyncio.run(scenario())


ORDINARY_AUDIT_IDENTIFIERS = ["task-123", "task-run-42", "risk-report", "desk-metrics", "request-1"]


@pytest.mark.parametrize("field", ["actor_id", "service_client_id", "product_id", "version_id", "policy_id", "adapter", "correlation_id"])
@pytest.mark.parametrize("value", ORDINARY_AUDIT_IDENTIFIERS)
def test_ordinary_names_with_short_key_prefix_substrings_remain_valid_audit_identifiers(field, value):
    record = {"actor_id": "north-user", "action": "execute", "decision": "deny", "correlation_id": "request-1"}
    event = api().audit.AuditEventInput.model_validate({**record, field: value})
    assert getattr(event, field) == value and value in event.model_dump_json()


@pytest.mark.parametrize("value", ORDINARY_AUDIT_IDENTIFIERS)
def test_ordinary_names_with_short_key_prefix_substrings_remain_valid_parameter_names(value):
    event = api().audit.AuditEventInput(actor_id="north-user", action="result", decision="deny", parameter_names=[value],
        parameter_digest="a" * 64, correlation_id="request-1")
    assert event.parameter_names == (value,)


@pytest.mark.parametrize("value", ORDINARY_AUDIT_IDENTIFIERS)
def test_ordinary_parameter_names_remain_valid_in_keyed_summary(value):
    writer = api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!")
    names, digest = writer.parameter_summary({value: "synthetic-value"})
    assert names == (value,) and len(digest) == 64


@pytest.mark.parametrize("value", ORDINARY_AUDIT_IDENTIFIERS)
def test_ordinary_correlation_names_remain_valid_in_event_factory(north_principal, value):
    writer = api().audit.AuditWriter(b"synthetic-operator-key-32-bytes!!!")
    assert writer.event(north_principal, "execute", decision="deny", correlation_id=value).correlation_id == value


@pytest.mark.parametrize("value", ORDINARY_AUDIT_IDENTIFIERS)
def test_ordinary_policy_explanations_with_short_key_substrings_remain_valid(value):
    reason = "Reviewed synthetic " + value
    assert api().service.PolicyContent.model_validate({"rules": rules(), "reason": reason}).reason == reason
