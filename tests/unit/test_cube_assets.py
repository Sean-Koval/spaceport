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


def test_cube_configuration_uses_driver_factory_without_removed_db_type_option():
    # Cube v1.8.5 OptsHandler rejects CreateOptions.dbType before startup.
    # This asset guard is not a substitute for the real Core constructor gate.
    import re
    configuration = (ROOT / "cube/cube.js").read_text()
    assert not re.search(r"^\s+dbType\s*:", configuration, re.MULTILINE)
    assert "driverFactory: () => ({ type: 'postgres'" in configuration


def test_image_auth_regression_has_normal_bounded_native_logger_teardown():
    # Asset guard only. Natural exit of the real release process remains a CI gate.
    source = (ROOT / "tests/runtime/auth-wrapper.test.js").read_text()
    assert "after(async () => {" in source
    assert "await gateway.release();" in source
    assert "resetLogger('warn');" in source and "timeout: 5000" in source
    assert all(value not in source for value in ("process.exit(", "forceExit", "force-exit"))


@pytest.mark.parametrize("mode,expected", [("success", 0), ("error", 7), ("timeout", 124)])
def test_runtime_command_bound_preserves_failure_and_runs_exit_cleanup(mode, expected):
    import re
    source = (ROOT / "scripts/verify_cube_runtime.sh").read_text()
    match = re.search(r"^bounded\(\) \{\n.*?^\}", source, re.MULTILINE | re.DOTALL)
    assert match, "Missing fail-closed runtime command bound"
    command = {"success": "printf completed", "error": "exit 7", "timeout": "sleep 30"}[mode]
    duration = "0.1" if mode == "timeout" else "2"
    code = ("set -euo pipefail\n" + match.group(0) +
        "\ntrap 'printf cleanup-observed' EXIT\n" +
        f"bounded {duration} bash -c '{command}'\n" + "printf accepted-after-command\n")
    completed = subprocess.run(["bash", "-c", code], text=True, capture_output=True, timeout=8)
    assert completed.returncode == expected, completed.stderr
    assert "cleanup-observed" in completed.stdout
    assert ("accepted-after-command" in completed.stdout) == (mode == "success")


def test_runtime_harness_bounds_all_container_exec_and_run_calls():
    source = (ROOT / "scripts/verify_cube_runtime.sh").read_text()
    for line in source.splitlines():
        if ('"${COMPOSE[@]}" exec ' in line or '"${COMPOSE[@]}" run ' in line or
            line.lstrip().startswith("docker run ")):
            assert "bounded " in line, "Unbounded container command"
        if "subprocess.check_output(" in line:
            assert "timeout=10" in line, "Unbounded topology inspection"
    assert 'bounded 60 "${COMPOSE[@]}" exec -T -e GATEWAY_SECURITY_MODULE=' in source
    assert 'bounded 60 "${COMPOSE[@]}" exec -T -e GATEWAY_CUBE_CONFIG_MODULE=' in source
    assert 'bounded 240 "${COMPOSE[@]}" run --rm --no-deps --user "$(id -u):$(id -g)" probe' in source


def test_full_registry_gate_keeps_six_minute_cap_inside_twenty_minute_job():
    # Static budget guard only; this does not establish runtime completion time.
    import re
    source = (ROOT / "scripts/verify_cube_runtime.sh").read_text()
    assert re.findall(r"^\s*bounded ([0-9]+) bash scripts/verify_registry_postgres\.sh(?: --diagnostic)?$", source, re.MULTILINE) == ["360", "360"]
    workflow = (ROOT / ".github/workflows/verify-cube.yml").read_text()
    assert re.findall(r"^\s+timeout-minutes: ([0-9]+)$", workflow, re.MULTILINE) == ["20"]


def test_fixture_delay_is_an_uncorrelated_scalar_initplan_without_shortening_bounds():
    # Asset guard only: the real EXPLAIN ANALYZE and pending-response gate must
    # establish execution counts and compatibility against pinned PostgreSQL/Cube.
    import re
    source = (ROOT / "infra/postgres/init.sql").read_text()
    for name, seconds in [("slow_orders", 12), ("timeout_orders", 90)]:
        match = re.search(rf"CREATE VIEW test_source\.{name} AS (.*?);", source, re.DOTALL)
        assert match, "Missing bounded synthetic fixture"
        query = " ".join(match.group(1).split())
        assert query == ("SELECT o.* FROM test_source.orders o "
            f"WHERE (SELECT test_source.bounded_delay({seconds}))"), "Fixture delay must be a noncorrelated scalar subquery"
    assert "LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog" in source
    assert "seconds < 1 OR seconds > 90" in source
    assert "ALTER ROLE fixture_reader SET statement_timeout = '25s'" in source
    assert "ALTER ROLE fixture_reader SET default_transaction_read_only = on" in source
    assert "REVOKE ALL ON FUNCTION test_source.bounded_delay(integer) FROM PUBLIC" in source
    assert "GRANT EXECUTE ON FUNCTION test_source.bounded_delay(integer) TO fixture_reader" in source


def test_delay_plan_counts_emit_only_fixed_counts_and_ignore_sql_or_error_fields():
    import runpy
    module = runpy.run_path(str(ROOT / "tests/integration/test_cube_compatibility.py"))
    assert "delay_plan_counts" in module, "Missing safe bounded delay-plan counts"
    counts = module["delay_plan_counts"]({"Node Type": "Sort", "Actual Loops": 1,
        "Output": ["untrusted SQL secret"], "Error": "untrusted error secret", "Plans": [
            {"Node Type": "Result", "Parent Relationship": "InitPlan", "Actual Loops": 1,
                "Output": ["test_source.bounded_delay(12)"], "Filter": "untrusted predicate secret"},
            {"Node Type": "Result", "Actual Loops": 3,
                "Output": ["test_source.bounded_delay(1)"]}]})
    assert counts == {"plan_nodes": 3, "delay_nodes": 2, "initplan_delay_nodes": 1, "delay_loops": 4}


@pytest.mark.parametrize("plan", [
    {"Actual Loops": -1}, {"Actual Loops": True}, {"Actual Loops": 1.5},
    {"Actual Loops": 1, "Plans": "untrusted SQL secret"},
    {"Actual Loops": 1, "Output": "test_source.bounded_delay(12)"},
    {"Actual Loops": 1, "Plans": [{"Actual Loops": 1}] * 128},
])
def test_delay_plan_counts_reject_malformed_or_oversized_plans_without_echo(plan):
    import runpy
    module = runpy.run_path(str(ROOT / "tests/integration/test_cube_compatibility.py"))
    assert "delay_plan_counts" in module, "Missing safe bounded delay-plan counts"
    with pytest.raises(ValueError) as caught:
        module["delay_plan_counts"](plan)
    assert str(caught.value) == "Invalid synthetic delay plan"


def exercise_cold_pending_row_assertion(rows):
    """Offline assertion probe only; this does not exercise or accept a backend."""
    import runpy
    module = runpy.run_path(str(ROOT / "tests/integration/test_cube_compatibility.py"))

    class AssertionStack:
        captured = False

        def capture_delay_plans(self):
            self.captured = True

        def load(self, query, *, endpoint, require_pending):
            assert self.captured
            assert query == module["raw_query"]("SlowOrders")
            assert endpoint == "cube-test" and require_pending is True
            return rows

    module["test_cold_bounded_fixture_observes_continue_wait_then_success"](AssertionStack())


@pytest.fixture
def expected_slow_order_rows():
    return [{"SlowOrders.id": identifier, "SlowOrders.segment": segment,
        "SlowOrders.revenue": revenue, "SlowOrders.tenant_id": "north"}
        for identifier, segment, revenue in [
            ("n1", "A", "10.10"), ("n2", None, "20.20"), ("n3", "A", "9007199254740993.01")]]


def test_cold_pending_row_assertion_accepts_exact_three_rows(expected_slow_order_rows):
    exercise_cold_pending_row_assertion(expected_slow_order_rows)


@pytest.mark.parametrize("counterexample", ["duplicate_valid", "corrupt_overwritten"])
def test_cold_pending_row_assertion_rejects_duplicates_and_overwritten_corruption(expected_slow_order_rows, counterexample):
    extra = dict(expected_slow_order_rows[0])
    if counterexample == "corrupt_overwritten":
        extra["SlowOrders.revenue"] = "999.99"
        rows = [extra, *expected_slow_order_rows]
    else:
        rows = [*expected_slow_order_rows, extra]
    with pytest.raises(AssertionError):
        exercise_cold_pending_row_assertion(rows)
