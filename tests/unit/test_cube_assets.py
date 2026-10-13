"""Offline checks of reviewed assets; never proof of real Cube compatibility."""
import csv
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]
ROOT = Path(__file__).resolve().parents[2]


def test_bundle_catalog_pins_every_deployed_model_byte():
    calculated = json.loads(subprocess.check_output([sys.executable, "scripts/cube_bundle_digest.py"], cwd=ROOT))
    catalog = json.loads((ROOT / "cube/bundles.json").read_text())
    assert calculated == {name: row["bundle_digest"] for name, row in catalog.items()}
    assert calculated["consumer"] != calculated["fixture_test"]
    assert catalog["consumer"]["url"] == "http://cube:4000"
    assert catalog["fixture_test"]["url"] == "http://cube-test:4000"
    assert all(row["secret_ref"].startswith("/run/secrets/") for row in catalog.values())


def test_only_reviewed_model_files_belong_to_digest(tmp_path):
    import runpy
    digest = runpy.run_path(str(ROOT / "scripts/cube_bundle_digest.py"))["bundle_digest"]
    for name in ("orders.yml", "watermark.yml"):
        (tmp_path / name).write_text((ROOT / "cube/model" / name).read_text())
    before = digest(tmp_path)
    (tmp_path / "orders.yml").write_text("Changed reviewed model")
    assert digest(tmp_path) != before
    (tmp_path / "unreviewed.js").write_text("malicious")
    with pytest.raises(ValueError):
        digest(tmp_path)


def test_synthetic_rows_preserve_nulls_and_unsafe_integer_decimal_precision():
    with (ROOT / "fixtures/orders.csv").open() as file:
        rows = list(csv.DictReader(file))
    totals = {tenant: sum((Decimal(row["amount"]) for row in rows if row["tenant_id"] == tenant), Decimal(0)) for tenant in ("north", "south")}
    assert totals == {"north": Decimal("9007199254741023.31"), "south": Decimal("100.01")}
    assert [row for row in rows if row["id"] == "n2"][0]["segment"] == ""
    assert len(rows) == 5


def test_fixture_accounts_match_identity_generator_without_credentials():
    import runpy
    accounts = json.loads((ROOT / "fixtures/accounts.json").read_text())
    generated = runpy.run_path(str(ROOT / "scripts/demo_accounts.py"))["ACCOUNTS"]
    assert {(a["id"], a["tenant_id"], tuple(a["roles"])) for a in accounts["accounts"]} == set(generated)
    assert all(a["entitlements"] == {"groups": [a["tenant_id"]]} for a in accounts["accounts"])
    serialized = json.dumps(accounts)
    assert all(forbidden not in serialized for forbidden in ("dpg_demo_", "password", "digest"))


def test_static_compose_has_only_edge_loopback_with_digest_pins_and_private_network():
    base = json.loads((ROOT / "infra/compose.yaml").read_text())
    test = json.loads((ROOT / "infra/compose.test.yaml").read_text())
    services = {**base["services"], **test["services"]}
    assert {name for name, row in services.items() if row.get("ports")} == {"edge"}
    assert base["services"]["edge"]["ports"][0]["host_ip"] == "127.0.0.1"
    for name, row in services.items():
        if name != "probe":
            assert "@sha256:" in row["image"] and len(row["image"].split("@sha256:")[1]) == 64
        assert row["networks"] == ["private"]
        assert not row.get("network_mode")
    assert set(base["networks"]) == {"private"}
    assert base["networks"]["private"]["internal"] is True
    assert not any("cubestore" in name for name in services)
    for name in ("cube", "cube-test"):
        assert services[name]["environment"]["CUBEJS_DEV_MODE"] == "false"
        assert services[name]["environment"]["CUBEJS_TELEMETRY"] == "false"


def test_node_context_contract_has_zero_skips_and_failures():
    completed = subprocess.run(["node", "--test", "tests/runtime/context.test.js"], cwd=ROOT,
        text=True, capture_output=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "skip" in completed.stdout and ("skipped 0" in completed.stdout or "# skipped 0" in completed.stdout)


def test_policy_fixture_uses_coordinated_strict_rule_wrapper():
    fixture = json.loads((ROOT / "fixtures/policies.json").read_text())
    assert set(fixture) == {"policy_id", "revision", "content"}
    assert fixture["policy_id"] == "revenue-access" and fixture["revision"] == 1
    assert set(fixture["content"]) == {"rules", "reason"}
    rules = fixture["content"]["rules"]
    assert set(rules) == {"tenant_ids", "tenant_constraint", "entitlement_constraint", "visible_fields"}
    assert rules["tenant_ids"] == ["north", "south"]
    assert rules["tenant_constraint"] == {"member": "Orders.tenant_id", "operator": "eq", "source": "grant.tenant_id"}
    assert rules["entitlement_constraint"] == {"member": "Orders.access_group", "operator": "in", "source": "grant.entitlements.groups"}
    assert rules["visible_fields"] == ["day", "segment", "revenue"]
    assert isinstance(fixture["content"]["reason"], str) and 1 <= len(fixture["content"]["reason"]) <= 512


def test_every_model_revalidates_the_committed_source_load_watermark():
    for directory, schema, count in [("model", "demo_source", 1), ("test-model", "test_source", 4)]:
        orders = (ROOT / "cube" / directory / "orders.yml").read_text()
        assert orders.count("refresh_key:") == count
        assert orders.count(f"SELECT MAX(loaded_at) FROM {schema}.watermark") == count
        watermark = (ROOT / "cube" / directory / "watermark.yml").read_text()
        assert "refresh_key:" in watermark and f"SELECT MAX(loaded_at) FROM {schema}.watermark" in watermark
        assert "every: 1 second" in orders and "every: 1 second" in watermark
