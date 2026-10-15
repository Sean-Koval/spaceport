"""Pure exact-package approvals; all content is synthetic and grants no authority."""
import copy
import hashlib
import importlib
import json

import pytest

from gateway.contracts.canonical import canonical_bytes, manifest_digest
from gateway.contracts.identity import Decision, Principal
from gateway.contracts.manifest import Limits, validate_manifest
from gateway.contracts.query import QueryRequest

pytestmark = pytest.mark.unit


def api():
    try:
        return importlib.import_module("gateway.source_access_contracts")
    except ModuleNotFoundError:
        pytest.fail("Source approval contracts are not implemented")


def domain(name, type_, *, values=(), minimum=None, maximum=None, nullable=False):
    return dict(name=name, kind="enum" if values else "range", type=type_, values=list(values),
                minimum=minimum, maximum=maximum, nullable=nullable, classification="non-personal")


def audience(**changes):
    return {"id": "north-consumer", "context": "consumer", "principal_id": "north-user",
            "principal_kind": "user", "client_ids": ["test-client"], "actions": ["execute", "result"],
            "tenant_id": "north", "row_constraints": [
                {"member": "Orders.tenant_id", "operator": "eq", "values": ["north"]},
                {"member": "Orders.access_group", "operator": "in", "values": ["north", "east"]}],
            "visible_fields": ["day", "segment", "revenue"], "fixture_case_ids": [], **changes}


def principal(**changes):
    return Principal(id="north-user", subject="synthetic-north", service_client_id="test-client", kind="user", **changes)


def decision(**changes):
    return Decision.model_validate({"principal_id": "north-user", "policy_revision": 1,
        "tenant_id": "north", "row_constraints": [
            {"member": "Orders.tenant_id", "operator": "eq", "values": ["north"]},
            {"member": "Orders.access_group", "operator": "in", "values": ["north"]}],
        "visible_fields": ["day", "revenue"], "limits": Limits().model_dump(), **changes})


@pytest.fixture
def package(valid_manifest):
    valid_manifest["adapter_revision"] = "cube-bigquery-v1"
    manifest = validate_manifest(valid_manifest)
    doc = {"schema_version": 1, "mode": "shared_non_pii", "revision": 1,
        "supersedes_id": None, "expires_at": "2030-01-01T00:00:00Z",
        "product": {"product_id": manifest.product_id, "version": manifest.version,
                    "manifest_digest": manifest_digest(manifest), "policy_id": manifest.policy_id},
        "bindings": {"consumer": {"binding_id": manifest.binding_id, "revision": 1, "content_digest": "b" * 64},
                     "fixture": {"binding_id": "fixture-binding", "revision": 1, "content_digest": "c" * 64}},
        "sources": {"consumer": {"source_id": "consumer-source", "revision": 1, "provider": "bigquery", "context": "consumer",
                    "descriptor_digest": "d" * 64, "physical_identity_digest": "e" * 64, "credential_binding_digest": "8" * 64},
                    "fixture": {"source_id": "fixture-source", "revision": 1, "provider": "bigquery", "context": "fixture_test",
                    "descriptor_digest": "f" * 64, "physical_identity_digest": "0" * 64, "credential_binding_digest": "9" * 64}},
        "artifacts": {"model_bundle_digest": manifest.model_bundle_digest,
                    "fixture_model_bundle_digest": "1" * 64, "statement_catalog_digest": None,
                    "fixture_statement_catalog_digest": None, "fixture_catalog_digest": "2" * 64,
                    "schema_profile": "result-v1", "normalization_profile": "cube-v1"},
        "lineage": [{"id": name, "source_context": context, "source_member": "Orders." + name,
                     "purpose": "exposed", "classification": "non-personal"}
                    for context in ("consumer", "fixture_test") for name in manifest.outputs] + [
                    {"id": name, "source_context": context, "source_member": "Orders." + name,
                     "purpose": "hidden_scope", "classification": "non-personal"}
                    for context in ("consumer", "fixture_test") for name in ("tenant_id", "access_group")],
        "input_domains": [domain("start", "date", minimum="2024-01-01", maximum="2025-01-01"),
                          domain("end", "date", minimum="2024-01-01", maximum="2025-01-01"),
                          domain("segment", "string", values=("A", "B"), nullable=True)],
        "filter_domains": [domain("segment", "string", values=("A", "B"), nullable=True),
                           domain("day", "date", minimum="2024-01-01", maximum="2025-01-01")],
        "audiences": [audience()],
        "review": {"review_id": "synthetic-review", "operator_id": "synthetic-operator",
            "evidence_ref": "synthetic-evidence", "evidence_digest": "3" * 64, "purpose": "aggregate-revenue",
            "lineage_reviewed": True, "source_publication_controlled": True,
            "shared_identity_approved": True, "inference_risk_reviewed": True}}
    return doc, manifest


def parsed(package):
    doc, manifest = package
    module = api()
    approval = module.validate_source_approval(canonical_bytes(doc))
    pins = module.ReviewedPackagePins.model_validate({key: doc[key] for key in
        ("product", "bindings", "sources", "artifacts", "lineage", "input_domains", "filter_domains")} |
        {"manifest": manifest, "approval_digest": approval.content_digest})
    return approval, pins


def test_canonical_domain_separated_digest_and_no_full_manifest_in_approval(package):
    approval, pins = parsed(package)
    raw = canonical_bytes(package[0])
    assert approval.canonical_content == raw
    assert approval.content_digest == hashlib.sha256(b"spaceport-source-approval-v1\0" + raw).hexdigest()
    assert approval.package_pins_digest == pins.content_digest
    assert "manifest" not in type(approval).model_fields
    assert pins.manifest.content_digest == approval.product.manifest_digest


@pytest.mark.parametrize("mutator", [
    lambda d: d.update(sql="SELECT customer FROM private"),
    lambda d: d["review"].update(samples=["private"]),
    lambda d: d["sources"]["consumer"].update(credentials="secret://synthetic"),
    lambda d: d["product"].update(manifest={}),
    lambda d: d["artifacts"].update(statement="SELECT 1"),
    lambda d: d["review"].update(evidence_ref="https://example.com/private"),
    lambda d: d["review"].update(purpose="Unbounded free text here"),
    lambda d: d["input_domains"][2].update(kind="regex", values=[]),
    lambda d: d["input_domains"][2].update(type="string", kind="range", values=[], minimum="A", maximum="Z"),
    lambda d: d["review"].update(lineage_reviewed=False),
    lambda d: d["review"].update(shared_identity_approved=1),
    lambda d: d["audiences"][0].update(client_ids=["*"]),
    lambda d: d["audiences"][0].update(client_ids=[None]),
    lambda d: d["audiences"][0].update(fixture_case_ids=["case-1"]),
    lambda d: d["sources"]["fixture"].update(context="consumer"),
    lambda d: d.pop("supersedes_id"),
])
def test_closed_nested_shapes_and_reviewed_only_content(package, mutator):
    doc = copy.deepcopy(package[0]); mutator(doc)
    with pytest.raises(ValueError, match="Invalid source approval"):
        api().validate_source_approval(canonical_bytes(doc))


@pytest.mark.parametrize("section", ["lineage", "input_domains", "filter_domains"])
@pytest.mark.parametrize("classification", ["personal", "sensitive-personal", "unknown", None])
def test_every_inventory_entry_requires_explicit_nonpersonal(package, section, classification):
    doc = copy.deepcopy(package[0]); doc[section][0]["classification"] = classification
    with pytest.raises(ValueError): api().validate_source_approval(canonical_bytes(doc))


@pytest.mark.parametrize("value", [True, False, 1.5, 0, -1, 2147483648])
def test_revision_is_exact_bounded_positive_integer(package, value):
    doc = copy.deepcopy(package[0]); doc["revision"] = value
    with pytest.raises(ValueError): api().validate_source_approval(canonical_bytes(doc))


@pytest.mark.parametrize("expiry", [None, "2030-01-01", "2030-01-01T00:00:00", "2030-01-01T00:00:00+00:00", "2030-13-01T00:00:00Z", "2030-01-01T00:00:00+00:60"])
def test_expiry_is_explicit_canonical_utc_timestamp(package, expiry):
    doc = copy.deepcopy(package[0]); doc["expires_at"] = expiry
    with pytest.raises(ValueError): api().validate_source_approval(canonical_bytes(doc))


def test_duplicate_json_names_and_noncanonical_bytes_rejected(package):
    raw = canonical_bytes(package[0])
    for bad in (b'{"mode":"shared_non_pii",' + raw[1:], b" " + raw, json.dumps(package[0]).encode(), b"x" * 65537):
        with pytest.raises(ValueError, match="Invalid source approval"):
            api().validate_source_approval(bad)


def test_exact_collection_bounds(package):
    doc = copy.deepcopy(package[0])
    doc["audiences"] = [audience(id=f"entry-{i}") for i in range(101)]
    with pytest.raises(ValueError): api().validate_source_approval(canonical_bytes(doc))
    doc = copy.deepcopy(package[0])
    doc["lineage"] = [dict(doc["lineage"][0], id=f"lineage-{i}") for i in range(201)]
    with pytest.raises(ValueError): api().validate_source_approval(canonical_bytes(doc))


def test_complete_tuple_allows_narrower_values_and_fields_without_mutation(package):
    approval, pins = parsed(package); request_decision = decision()
    before = request_decision.model_dump()
    assert api().evaluate_source_access(approval, principal(), request_decision,
        action="execute", context="consumer", pins=pins) == "north-consumer"
    assert request_decision.model_dump() == before


@pytest.mark.parametrize("changes", [
    {"principal_id": "other"}, {"tenant_id": "east"}, {"visible_fields": ["secret"]},
    {"row_constraints": [{"member": "Orders.tenant_id", "operator": "eq", "values": ["north"]}]},
    {"row_constraints": [{"member": "Orders.tenant_id", "operator": "eq", "values": ["north"]},
                         {"member": "Orders.access_group", "operator": "in", "values": ["west"]}]},
    {"row_constraints": [{"member": "Orders.tenant_id", "operator": "in", "values": ["north"]},
                         {"member": "Orders.access_group", "operator": "in", "values": ["north"]}]},
])
def test_audience_requires_exact_complete_scope(changes):
    entry = api().SourceAudienceEntry.model_validate(audience())
    assert not api().audience_contains(entry, principal(), decision(**changes), action="execute", context="consumer")


def test_no_cross_entry_union_and_ambiguous_matches_deny(package):
    for entries in ([audience(id="left", visible_fields=["day"]), audience(id="right", visible_fields=["revenue"])],
                    [audience(id="first"), audience(id="second")]):
        doc = copy.deepcopy(package[0]); doc["audiences"] = entries
        approval, pins = parsed((doc, package[1]))
        with pytest.raises(ValueError, match="Source access denied"):
            api().evaluate_source_access(approval, principal(), decision(), action="execute", context="consumer", pins=pins)


@pytest.mark.parametrize("values", [["north", "north"], [True, 1], [1, 1.0], [False, 0.0]])
def test_typed_duplicates_and_cross_type_collisions_deny(values):
    raw = audience(); raw["row_constraints"][1]["values"] = values
    with pytest.raises(ValueError): api().SourceAudienceEntry.model_validate(raw)
    entry = api().SourceAudienceEntry.model_validate(audience())
    broken = decision().model_copy(update={"row_constraints": decision().row_constraints + (decision().row_constraints[0],)})
    assert not api().audience_contains(entry, principal(), broken, action="execute", context="consumer")


def test_revalidate_constructed_copied_mutated_unknown_field_models():
    module = api(); entry = module.SourceAudienceEntry.model_validate(audience())
    broken = entry.model_copy(update={"unknown": "secret"})
    assert not module.audience_contains(broken, principal(), decision(), action="execute", context="consumer")
    actor = principal().model_copy(update={"service_client_id": None})
    assert not module.audience_contains(entry, actor, decision(), action="execute", context="consumer")
    object.__setattr__(entry, "actions", ("execute", "*"))
    assert not module.audience_contains(entry, principal(), decision(), action="execute", context="consumer")


def test_wrong_client_context_action_and_typed_scalar_cannot_match():
    module = api(); entry = module.SourceAudienceEntry.model_validate(audience())
    assert not module.audience_contains(entry, principal().model_copy(update={"service_client_id": "another"}), decision(), action="execute", context="consumer")
    assert not module.audience_contains(entry, principal(), decision(), action="publish", context="consumer")
    assert not module.audience_contains(entry, principal(), decision(), action="execute", context="fixture_test")
    raw = audience(); raw["row_constraints"][0]["values"] = [True]
    entry = module.SourceAudienceEntry.model_validate(raw)
    altered = decision().model_dump(); altered["row_constraints"] = [dict(r) for r in altered["row_constraints"]]
    altered["row_constraints"][0]["values"] = [1]
    assert not module.audience_contains(entry, principal(), Decision.model_validate(altered), action="execute", context="consumer")


def test_fixed_fixture_identity_is_separate_and_cannot_authorize_a_decision():
    module = api()
    entry = module.SourceAudienceEntry.model_validate(audience(id="fixed-case", context="fixture_test", principal_id="fixture-north",
        principal_kind="demo", client_ids=[None], fixture_case_ids=["north-total"]))
    actor = Principal(id="fixture-north", subject="fixed-fixture", service_client_id=None, kind="demo")
    assert module.reviewed_fixture_identity(entry, actor, "north-total")
    assert not module.reviewed_fixture_identity(entry, actor, "another")
    assert not module.audience_contains(entry, actor, decision(), action="execute", context="fixture_test", fixture_case_id="north-total")
    assert not module.reviewed_fixture_identity(module.SourceAudienceEntry.model_validate(audience()), principal(), "north-total")


def test_modified_valid_approval_is_rejected_by_protected_digest(package):
    approval, pins = parsed(package)
    changed = copy.deepcopy(package[0]); changed["audiences"][0]["actions"].append("publish")
    other = api().validate_source_approval(canonical_bytes(changed))
    with pytest.raises(ValueError): api().evaluate_source_access(other, principal(), decision(), action="execute", context="consumer", pins=pins)


@pytest.mark.parametrize("section,key", [("product", "manifest_digest"), ("artifacts", "model_bundle_digest"),
    ("artifacts", "fixture_catalog_digest"), ("artifacts", "normalization_profile")])
def test_every_package_pin_and_full_manifest_are_rechecked(package, section, key):
    approval, pins = parsed(package)
    changed = pins.model_dump(); changed[section][key] = "z" if key.endswith("profile") else "9" * 64
    forged = api().ReviewedPackagePins.model_construct(**changed)
    with pytest.raises(ValueError): api().evaluate_source_access(approval, principal(), decision(), action="execute", context="consumer", pins=forged)


def test_full_manifest_content_and_limits_cannot_be_bypassed(package):
    approval, pins = parsed(package)
    changed = pins.manifest.model_copy(update={"description": "Changed text", "content_digest": None})
    forged = pins.model_copy(update={"manifest": changed})
    with pytest.raises(ValueError): api().evaluate_source_access(approval, principal(), decision(), action="execute", context="consumer", pins=forged)
    narrow_manifest = copy.deepcopy(package[1].model_dump()); narrow_manifest["content_digest"] = None
    narrow_manifest["limits"]["max_rows"] = 10; narrow_manifest["limits"]["page_size"] = 10
    manifest = validate_manifest(narrow_manifest)
    doc = copy.deepcopy(package[0]); doc["product"]["manifest_digest"] = manifest_digest(manifest)
    approval, pins = parsed((doc, manifest))
    with pytest.raises(ValueError): api().evaluate_source_access(approval, principal(), decision(), action="execute", context="consumer", pins=pins)


def test_reviewed_domain_values_checked_with_defaults_before_any_persistence(package):
    _, pins = parsed(package)
    request = QueryRequest(product_id="revenue", inputs={"start": "2024-03-09", "end": "2024-03-12"})
    assert api().validate_reviewed_values(pins, pins.manifest, request) is None
    for inputs in ({"start": "2026-01-01", "end": "2026-01-02"},
                   {"start": "2024-03-09", "end": "2024-03-12", "segment": "private-customer"}):
        with pytest.raises(ValueError, match="Invalid reviewed values") as caught:
            api().validate_reviewed_values(pins, pins.manifest, QueryRequest(product_id="revenue", inputs=inputs))
        assert "private-customer" not in str(caught.value)
    with pytest.raises(ValueError):
        api().validate_reviewed_values(pins, pins.manifest, request.model_copy(update={"filters": ({"field": "segment", "operator": "eq", "value": "C"},)}))


def test_missing_domains_inventory_and_unreviewed_manifest_defaults_deny(package):
    module = api(); approval, pins = parsed(package)
    for changes in ({"input_domains": pins.input_domains[:-1]}, {"lineage": pins.lineage[1:]}):
        with pytest.raises(ValueError): module.evaluate_source_access(approval, principal(), decision(), action="execute", context="consumer", pins=pins.model_copy(update=changes))
    doc = copy.deepcopy(package[0]); doc["input_domains"][2]["values"] = ["A"]
    with pytest.raises(ValueError): parsed((doc, package[1]))


def test_schema_version_is_integer_one_and_not_boolean_or_float(package):
    for value in (True, False, 1.0, 2, "1.0"):
        raw = json.dumps(dict(package[0], schema_version=value), separators=(",", ":"), sort_keys=True).encode()
        with pytest.raises(ValueError): api().validate_source_approval(raw)


@pytest.mark.parametrize("value", [1.5, 1.0, 0.5])
def test_document_never_contains_float_scalars(package, value):
    doc = copy.deepcopy(package[0]); doc["audiences"][0]["row_constraints"][1]["values"] = [value]
    with pytest.raises(ValueError): api().SourceApprovalDocument.model_validate(doc)
    with pytest.raises(ValueError): api().validate_source_approval(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode())


@pytest.mark.parametrize("section,key", [("review", "operator_id"), ("product", "product_id"), ("review", "evidence_ref")])
def test_recognized_credentials_cannot_hide_in_identifiers(package, section, key):
    doc = copy.deepcopy(package[0]); doc[section][key] = "ghp_synthetic_private_token"
    with pytest.raises(ValueError, match="Invalid source approval") as caught:
        api().validate_source_approval(canonical_bytes(doc))
    assert "ghp_" not in str(caught.value)


def test_case_specific_fixture_evaluation_requires_exact_case(package):
    doc = copy.deepcopy(package[0]); doc["audiences"] = [audience(id="fixed-case", context="fixture_test",
        principal_id="fixture-north", principal_kind="demo", client_ids=[None], fixture_case_ids=["north-total"])]
    approval, pins = parsed((doc, package[1]))
    actor = Principal(id="fixture-north", subject="fixed-fixture", service_client_id=None, kind="demo")
    scoped = decision(principal_id="fixture-north")
    assert api().evaluate_source_access(approval, actor, scoped, action="execute", context="fixture_test",
        pins=pins, fixture_case_id="north-total") == "fixed-case"
    for case_id in (None, "wrong-case"):
        with pytest.raises(ValueError): api().evaluate_source_access(approval, actor, scoped,
            action="execute", context="fixture_test", pins=pins, fixture_case_id=case_id)


def test_offline_pins_are_inert_and_expired_documents_still_parse(package):
    module = api(); approval, pins = parsed(package)
    unapproved = approval.package_pins(pins.manifest)
    assert unapproved.approval_digest is None
    with pytest.raises(ValueError): module.evaluate_source_access(approval, principal(), decision(), action="execute", context="consumer", pins=unapproved)
    expired = copy.deepcopy(package[0]); expired["expires_at"] = "2000-01-01T00:00:00Z"
    assert module.validate_source_approval(canonical_bytes(expired)).expires_at == expired["expires_at"]


def test_physical_identity_covers_full_target_and_actor_not_capacity_pool():
    from tests.unit.test_bigquery_serving_source import source_value
    from gateway.adapters.bigquery_runtime import BigQuerySource
    from gateway.contracts.canonical import canonical_digest
    module = api(); source = BigQuerySource.model_validate(source_value())
    identity = module.physical_source_identity(source)
    assert set(identity) == {"provider", "billing_project", "data_project", "dataset", "tables", "location", "dataset_location", "cube_endpoint", "auth"}
    assert module.physical_source_identity_digest(source) == canonical_digest(identity)
    changed = BigQuerySource.model_validate(source_value(dataset="another_dataset"))
    assert source.capacity_pool_id == changed.capacity_pool_id
    assert module.physical_source_identity_digest(source) != module.physical_source_identity_digest(changed)
    with pytest.raises(ValueError): module.physical_source_identity(source.model_copy(update={"unexpected": "secret"}))


def test_domo_physical_identity_pins_subject_but_does_not_claim_secret_alias_drift():
    from tests.unit.test_domo_source_config import source_dict
    from gateway.adapters.domo_runtime import DomoSource
    module = api(); raw = source_dict(); source = DomoSource.model_validate(raw)
    identity = module.physical_source_identity(source)
    assert set(identity) == {"provider", "api_origin", "dataset_ids", "auth"}
    assert set(identity["auth"]) == {"mode", "client_id_sha256", "expected_customer", "expected_user_id", "expected_role"}
    raw["auth"]["expected_user_id"] += 1
    changed = DomoSource.model_validate(raw)
    assert module.physical_source_identity_digest(changed) != module.physical_source_identity_digest(source)
    raw = source_dict(); raw["auth"]["client_secret_ref"] = "/run/secrets/rotated-alias"
    alias = DomoSource.model_validate(raw)
    assert alias.digest != source.digest
    assert module.physical_source_identity_digest(alias) == module.physical_source_identity_digest(source)


def test_decimal_ranges_accept_lossless_query_scale_without_rounding_bounds():
    module = api()
    raw = domain("amount", "decimal", minimum="0", maximum="99999999999999999999999999999999999999")
    value = module.ReviewedValueDomain.model_validate(raw)
    assert value.maximum == raw["maximum"]
    assert module._domain_contains(value, "10.10")
    assert not module._domain_contains(value, "1e3")


def test_evaluator_does_not_call_custom_context_hooks(package):
    approval, pins = parsed(package)
    class HostileContext(str):
        def __eq__(self, other):
            raise AssertionError("untrusted comparison hook")
        def __hash__(self):
            raise AssertionError("untrusted hash hook")
    with pytest.raises(ValueError, match="Source access denied"):
        api().evaluate_source_access(approval, principal(), decision(), action="execute", context=HostileContext("consumer"), pins=pins)


def test_runtime_projection_is_closed_and_contains_no_review_identifiers(package):
    approval, pins = parsed(package)
    projection = approval.runtime_projection("12345678-1234-1234-1234-123456789abc")
    payload = projection.model_dump(mode="json")
    assert set(payload) == {"approval_id", "approval_revision", "approval_digest", "expires_at",
        "product", "bindings", "sources", "artifacts", "lineage", "input_domains", "filter_domains", "audiences", "projection_digest"}
    plain = dict(payload); plain.pop("projection_digest")
    assert projection.projection_digest == hashlib.sha256(b"spaceport-source-projection-v1\0" + canonical_bytes(plain)).hexdigest()
    encoded = canonical_bytes(payload)
    assert b"synthetic-review" not in encoded and b"synthetic-evidence" not in encoded and b"synthetic-operator" not in encoded
    assert api().evaluate_runtime_source_access(projection, principal(), decision(), action="execute", context="consumer", pins=pins) == "north-consumer"
    with pytest.raises(ValueError): api().RuntimeApprovalProjection.model_validate(payload | {"review": package[0]["review"]})


def test_runtime_projection_mutation_requires_new_digest_but_pure_hash_is_not_provenance(package):
    approval, pins = parsed(package)
    projection = approval.runtime_projection("12345678-1234-1234-1234-123456789abc")
    entry = projection.audiences[0].model_copy(update={"actions": ("execute", "result", "publish")})
    forged = projection.model_copy(update={"audiences": (entry,)})
    with pytest.raises(ValueError, match="Source access denied"):
        api().evaluate_runtime_source_access(forged, principal(), decision(), action="publish", context="consumer", pins=pins)
    payload = forged.model_dump(mode="json"); payload.pop("projection_digest")
    payload["projection_digest"] = hashlib.sha256(b"spaceport-source-projection-v1\0" + canonical_bytes(payload)).hexdigest()
    rehashed = api().RuntimeApprovalProjection.model_validate(payload)
    # Plain values cannot prove provenance. Final SQL must compare this entire
    # projection against the protected approval record before granting access.
    assert api().evaluate_runtime_source_access(rehashed, principal(), decision(), action="publish", context="consumer", pins=pins) == "north-consumer"


def test_runtime_projection_rechecks_package_identity_limits_and_exact_client(package):
    approval, pins = parsed(package)
    projection = approval.runtime_projection("12345678-1234-1234-1234-123456789abc")
    for forged in (projection.model_copy(update={"approval_revision": True}),
                   projection.model_copy(update={"approval_id": "*"}),
                   projection.model_copy(update={"expires_at": None})):
        with pytest.raises(ValueError): api().evaluate_runtime_source_access(forged, principal(), decision(), action="execute", context="consumer", pins=pins)
    with pytest.raises(ValueError): api().evaluate_runtime_source_access(projection,
        principal().model_copy(update={"service_client_id": "another"}), decision(), action="execute", context="consumer", pins=pins)
    with pytest.raises(ValueError): api().evaluate_runtime_source_access(projection, principal(), decision(), action="execute", context="consumer",
        pins=pins.model_copy(update={"approval_digest": "9" * 64}))


def test_case_review_allows_authenticated_fixed_identity_but_never_author_or_publisher():
    module = api()
    entry = module.SourceAudienceEntry.model_validate(audience(context="fixture_test", fixture_case_ids=["north-total"]))
    assert module.reviewed_fixture_identity(entry, principal(), "north-total")
    for action in ("author", "publish"):
        with pytest.raises(ValueError): module.SourceAudienceEntry.model_validate(audience(
            context="fixture_test", fixture_case_ids=["north-total"], actions=[action]))
    assert not module.reviewed_fixture_identity(module.SourceAudienceEntry.model_validate(audience(
        context="fixture_test", actions=["author"])), principal(), "north-total")


def test_expected_bigquery_inventory_is_structural_and_never_classifies():
    from gateway.adapters.bigquery_runtime import BigQueryBinding
    from tests.unit.test_bigquery_serving_source import binding_value
    module = api(); binding = BigQueryBinding.model_validate(binding_value())
    inventory = module.expected_binding_inventory(binding, ("start", "end", "account"))
    assert len(inventory) == 10
    assert all(set(item) == {"id", "source_context", "source_member", "purpose"} for item in inventory)
    assert {item["source_member"] for item in inventory if item["purpose"] == "grouping"} == {"Usage.day", "Usage.account"}
    assert {item["id"]: item["source_member"] for item in inventory if item["purpose"] == "source_input"} == {
        "account": "Usage.account", "start": "Usage.day", "end": "Usage.day"}
    assert inventory == module.expected_binding_inventory(binding, ("account", "end", "start"))
    with pytest.raises(ValueError): module.expected_binding_inventory(binding.model_copy(update={"unknown": True}), ("start", "end", "account"))
    with pytest.raises(ValueError): module.expected_binding_inventory(binding, ("start", "end", "account", "unreviewed"))


def test_expected_domo_inventory_covers_checkpoint_scope_and_grain(valid_manifest):
    from tests.contract.test_domo_runtime import setup
    module = api(); _, _, binding, product, *_ = setup(valid_manifest)
    inventory = module.expected_binding_inventory(binding, tuple(product.manifest.inputs))
    assert all("classification" not in item for item in inventory)
    assert {item["source_member"] for item in inventory if item["purpose"] == "hidden_scope"} == set(binding.constraint_members)
    assert {item["id"] for item in inventory if item["purpose"] == "grouping"} == set(binding.grain_keys)
    assert {item["id"] for item in inventory if item["purpose"] == "source_input"} == set(product.manifest.inputs)


def test_nested_hostile_metaclasses_are_rejected_before_hash_or_equality_hooks():
    module = api(); called = []
    class HostileMeta(type):
        def __hash__(cls):
            called.append("hash")
            raise RuntimeError("PRIVATE-METACLASS-CANARY")
        def __eq__(cls, other):
            called.append("eq")
            raise RuntimeError("PRIVATE-METACLASS-CANARY")
    class Hostile(metaclass=HostileMeta):
        pass
    entry = module.SourceAudienceEntry.model_validate(audience())
    object.__setattr__(entry, "visible_fields", Hostile())
    assert not module.audience_contains(entry, principal(), decision(), action="execute", context="consumer")
    assert called == []
    for field in ("subject", "service_client_id"):
        actor = principal().model_copy(update={field: Hostile()})
        assert not module.audience_contains(module.SourceAudienceEntry.model_validate(audience()), actor, decision(), action="execute", context="consumer")
    assert called == []


def test_mutated_pydantic_internal_containers_cannot_invoke_hooks():
    module = api(); called = []
    class Hostile:
        def __bool__(self):
            called.append("bool")
            raise RuntimeError("PRIVATE-BOOL-CANARY")
    entry = module.SourceAudienceEntry.model_validate(audience())
    object.__setattr__(entry, "__pydantic_extra__", Hostile())
    assert not module.audience_contains(entry, principal(), decision(), action="execute", context="consumer")
    assert called == []


@pytest.mark.parametrize("section,key", [("sources", "source_id"), ("bindings", "binding_id")])
def test_consumer_and_fixture_logical_identities_are_separate(package, section, key):
    doc = copy.deepcopy(package[0]); doc[section]["fixture"][key] = doc[section]["consumer"][key]
    with pytest.raises(ValueError): api().validate_source_approval(canonical_bytes(doc))


def test_decimal_enum_values_have_canonical_document_spelling():
    with pytest.raises(ValueError): api().ReviewedValueDomain.model_validate(domain("amount", "decimal", values=("10.10",)))
    assert api().ReviewedValueDomain.model_validate(domain("amount", "decimal", values=("10.1",))).values == ("10.1",)
