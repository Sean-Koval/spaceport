"""Mandatory real-Cube gate. Absence of the synthetic stack is a blocker, not a skip.

Run in the prepared probe container on the isolated network. No arbitrary URL,
external credentials, production data or Docker socket is accepted here.
"""
from decimal import Decimal
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import re
import time
import uuid

import httpx
import jwt
import psycopg
import pytest

pytestmark = pytest.mark.cube
ROOT = Path(__file__).resolve().parents[2]
AUTHORIZATION = "private-synthetic-cube-only"
FAILURE_BODY_BYTES = 4096


def delay_plan_counts(plan):
    """Count bounded EXPLAIN ANALYZE nodes only; never return plan text."""
    counts = {"plan_nodes": 0, "delay_nodes": 0, "initplan_delay_nodes": 0, "delay_loops": 0}
    pending = [(plan, 0)]
    while pending:
        node, depth = pending.pop()
        if type(node) is not dict or depth > 16 or counts["plan_nodes"] >= 128:
            raise ValueError("Invalid synthetic delay plan")
        loops, children, output = node.get("Actual Loops"), node.get("Plans", []), node.get("Output", [])
        if (type(loops) is not int or not 0 <= loops <= 10000
            or type(children) is not list or len(children) > 128
            or type(output) is not list or len(output) > 32
            or any(type(value) is not str or len(value) > 1024 for value in output)):
            raise ValueError("Invalid synthetic delay plan")
        counts["plan_nodes"] += 1
        if any(re.fullmatch(r"(?:test_source\.)?bounded_delay\([0-9]{1,2}\)", value) for value in output):
            counts["delay_nodes"] += 1
            counts["initplan_delay_nodes"] += int(node.get("Parent Relationship") == "InitPlan")
            counts["delay_loops"] += loops
        pending.extend((child, depth + 1) for child in children)
    return counts


def failure_error_class(response):
    """Fixed diagnostic enums only. Never emit any response text or field names."""
    if response.extensions.get("diagnostic_complete") is not True or len(response.content) > FAILURE_BODY_BYTES:
        return "unknown"
    try:
        text = response.content.decode("utf-8", errors="strict")
        # Bound collection depth before JSON decoding; only a flat object is
        # supported. Braces/brackets inside escaped strings are ordinary text.
        depth, quoted, escaped = 0, False, False
        for character in text:
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character in "[{":
                depth += 1
                if depth > 1:
                    return "unknown"
            elif character in "]}":
                depth -= 1
                if depth < 0:
                    return "unknown"

        def strict_pairs(pairs):
            body = {}
            for key, value in pairs:
                if key in body:
                    raise ValueError()
                body[key] = value
            return body

        def reject_constant(_):
            raise ValueError()

        body = json.loads(text, object_pairs_hook=strict_pairs, parse_constant=reject_constant)
        if (type(body) is not dict or not set(body) <= {"error", "type", "requestId", "stack", "plainError"}
            or type(body.get("error")) is not str or not body["error"]
            or any(value is not None and (type(value) is not str or len(value.encode("utf-8")) > 1024)
                for value in body.values())):
            return "unknown"
        error = body["error"]
        if error in {"canceling statement due to statement timeout", "Error: canceling statement due to statement timeout"}:
            return "source_statement_timeout"
        if re.fullmatch(r"Query execution timeout after [0-9]{1,12}(?:\.[0-9]{1,12})? min of waiting", error):
            return "queue_execution_timeout"
        if body.get("type") == "UserError":
            return "user_error"
    except (ValueError, UnicodeError, RecursionError):
        pass
    return "unknown"


class CubeStack:
    def __init__(self):
        self.hosts = {"cube": "cube", "cube-test": "cube-test", "demo-db": "demo-db"}
        self.secrets = Path("/run/secrets")
        self.evidence = Path("/evidence")
        self.host_mode = False
        metadata_path = os.environ.get("GATEWAY_CUBE_TEST_HOSTS_FILE")
        if metadata_path:
            metadata_file = Path(metadata_path)
            if not metadata_file.is_absolute() or metadata_file.is_symlink():
                raise ValueError("Invalid synthetic host metadata")
            metadata = json.loads(metadata_file.read_text())
            if set(metadata) != {"kind", "hosts", "run_label", "project", "container_ids"} or metadata["kind"] != AUTHORIZATION or not metadata["run_label"]:
                raise ValueError("Invalid synthetic host metadata")
            if set(metadata["hosts"]) != set(self.hosts):
                raise ValueError("Invalid synthetic host mapping")
            for address in metadata["hosts"].values():
                ip = ipaddress.IPv4Address(address)
                if not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                    raise ValueError("Only actual private synthetic bridge addresses are accepted")
            if metadata["run_label"] != os.environ.get("GATEWAY_CUBE_RUN_LABEL") or not re.fullmatch(r"gateway-cube-[a-f0-9]{16}", metadata["project"]):
                raise ValueError("Synthetic runtime label mismatch")
            if set(metadata["container_ids"]) != set(self.hosts):
                raise ValueError("Missing synthetic runtime identity")
            for name, identifier in metadata["container_ids"].items():
                if not re.fullmatch(r"[a-f0-9]{64}", identifier):
                    raise ValueError("Invalid container identity")
                inspected = json.loads(subprocess.check_output(["docker", "inspect", identifier], timeout=5))
                if len(inspected) != 1:
                    raise ValueError("Ambiguous synthetic container")
                actual = inspected[0]
                labels = actual["Config"]["Labels"]
                network_name = metadata["project"] + "_private"
                attachments = actual["NetworkSettings"]["Networks"]
                if labels.get("com.docker.compose.service") != name or labels.get("com.docker.compose.project") != metadata["project"] or labels.get("gateway.cube.run") != metadata["run_label"] or actual["Name"] != "/" + metadata["project"] + "-" + name + "-1" or not actual["State"]["Running"] or actual["State"]["Health"]["Status"] != "healthy" or list(attachments) != [network_name] or attachments[network_name]["IPAddress"] != metadata["hosts"][name] or actual["HostConfig"]["PortBindings"]:
                    raise ValueError("Stale or widened synthetic runtime")
                network = json.loads(subprocess.check_output(["docker", "network", "inspect", network_name], timeout=5))[0]
                if not network["Internal"] or network["Labels"]["gateway.cube.run"] != metadata["run_label"] or network["Labels"]["com.docker.compose.project"] != metadata["project"]:
                    raise ValueError("Untrusted synthetic network")
            self.hosts = metadata["hosts"]
            self.evidence = metadata_file.parent
            self.secrets = self.evidence.parent / "secrets"
            self.host_mode = True
        self.catalog = json.loads((ROOT / "cube/bundles.json").read_text())
        self.client = httpx.Client(timeout=20, trust_env=False, follow_redirects=False)

    def token(self, *, endpoint="cube", tenant="north", principal="demo-north", constraints=None, **patch):
        bundle = self.catalog["fixture_test" if endpoint == "cube-test" else "consumer"]
        now = int(time.time())
        payload = {"iss": "dpg-gateway", "aud": "dpg-cube", "iat": now, "exp": now + 60,
            "principal_id": principal, "tenant_id": tenant, "policy_revision": 1,
            "model_bundle_digest": bundle["bundle_digest"], "context": bundle["context"],
            "row_constraints": constraints or [
                {"member": "Orders.tenant_id", "operator": "eq", "values": [tenant]},
                {"member": "Orders.access_group", "operator": "in", "values": [tenant]}], **patch}
        key = (self.secrets / ("cube_test_signing_key" if endpoint == "cube-test" else "cube_signing_key")).read_text().strip()
        return jwt.encode(payload, key, algorithm="HS256")

    def request(self, path, *, endpoint="cube", token=None, body=None, diagnose_failure=False):
        headers = {"X-Request-Id": str(uuid.uuid4())}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        method = "POST" if body is not None else "GET"
        if diagnose_failure:
            headers["Accept-Encoding"] = "identity"
            with self.client.stream(method, f"http://{self.hosts[endpoint]}:4000{path}", headers=headers, json=body) as response:
                if response.status_code == 200:
                    response.read()
                    return response
                raw, complete = bytearray(), False
                try:
                    if response.headers.get("content-encoding", "identity") == "identity":
                        for chunk in response.iter_raw(chunk_size=512):
                            if len(raw) + len(chunk) > FAILURE_BODY_BYTES:
                                break
                            raw.extend(chunk)
                        else:
                            complete = True
                except httpx.HTTPError:
                    pass
                # An oversized/incomplete body can split a secret. Suppress the
                # whole capture. Accumulation is bounded before each append.
                return httpx.Response(response.status_code, content=bytes(raw) if complete else b"",
                    extensions={"diagnostic_complete": complete})
        return self.client.request(method, f"http://{self.hosts[endpoint]}:4000{path}", headers=headers, json=body)

    def load(self, query, *, endpoint="cube", require_pending=False, **scope):
        started = time.monotonic()
        deadline = started + 120
        pending = []
        while time.monotonic() < deadline:
            response = self.request("/cubejs-api/v1/load", endpoint=endpoint,
                token=self.token(endpoint=endpoint, **scope), body={"query": query, "cache": "must-revalidate"}, diagnose_failure=True)
            if response.status_code != 200:
                # Keep the exact success-status requirement and suppress pytest
                # introspection of query/response locals on this failure path.
                pytest.fail(f"Authenticated synthetic load failed: status={response.status_code} "
                    f"error_class={failure_error_class(response)} elapsed_seconds={time.monotonic() - started:.3f} "
                    f"pending_count={len(pending)}", pytrace=False)
            body = response.json()
            if body.get("error") == "Continue wait":
                pending.append({"status": response.status_code, "error": body["error"], "keys": sorted(body)})
                time.sleep(0.25)
                continue
            assert "error" not in body, "Synthetic Cube query returned an upstream error"
            assert isinstance(body.get("data"), list), "Actual Cube response did not contain rows"
            if require_pending:
                assert pending, "Cold bounded delay must produce a real Continue wait response"
                self.capture("pending.json", pending)
            self.capture("load-shape.json", {"status": response.status_code, "keys": sorted(body),
                "data": body["data"], "annotation": body.get("annotation")})
            return body["data"]
        pytest.fail("Synthetic Cube load exceeded its 120-second gate deadline", pytrace=False)

    def capture(self, name, value):
        # Only explicitly selected synthetic rows/metadata are persisted. Never JWT,
        # connection URL, context, echoed query, SQL, raw error or environment.
        self.evidence.mkdir(exist_ok=True)
        (self.evidence / name).write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")

    def connect(self, role="fixture_reader"):
        return psycopg.connect(host=self.hosts["demo-db"], port=5432, dbname="gateway_demo", user=role,
            password=(self.secrets / role).read_text().strip(), connect_timeout=5)

    def capture_delay_plans(self):
        # Same grouping, ordering, limit and mandatory filters as raw_query's
        # source shape. The one-second LEGACY replay is diagnostic only; neither
        # deployed view nor the actual Cube pending query shortens its delay.
        legacy = """SELECT id, segment, tenant_id, sum(amount) FROM (
            SELECT o.* FROM test_source.orders o
            CROSS JOIN (SELECT test_source.bounded_delay(1) AS done OFFSET 0) delay WHERE delay.done
            ) legacy_orders WHERE tenant_id='north' AND access_group IN ('north')
            GROUP BY 1,2,3 ORDER BY 1 LIMIT 1001"""
        fixed = """SELECT id, segment, tenant_id, sum(amount) FROM test_source.slow_orders
            WHERE tenant_id='north' AND access_group IN ('north')
            GROUP BY 1,2,3 ORDER BY 1 LIMIT 1001"""
        evidence = {}
        try:
            with self.connect() as connection:
                for name, query in [("legacy_replay", legacy), ("scalar_fixture", fixed)]:
                    started = time.monotonic()
                    result = connection.execute("EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON, TIMING FALSE, SUMMARY FALSE) " + query).fetchone()[0]
                    counts = delay_plan_counts(result[0]["Plan"])
                    evidence[name] = {**counts, "elapsed_seconds": round(time.monotonic() - started, 3)}
        except (psycopg.Error, ValueError, KeyError, IndexError, TypeError):
            pytest.fail("Synthetic read-only delay-plan probe failed", pytrace=False)
        # Retain this safe output even if the subsequent Cube query fails. The
        # full raw plans, SQL, errors and connection details are never emitted.
        self.capture("source-delay-plan.json", evidence)
        print("Synthetic source delay-plan counts: " + json.dumps(evidence, sort_keys=True))
        fixed_counts = evidence["scalar_fixture"]
        if (fixed_counts["delay_nodes"], fixed_counts["initplan_delay_nodes"], fixed_counts["delay_loops"]) != (1, 1, 1):
            pytest.fail("Synthetic scalar delay must execute exactly one InitPlan loop", pytrace=False)


@pytest.fixture(scope="module")
def cube_stack():
    if os.environ.get("GATEWAY_CUBE_TEST_AUTHORIZATION") != AUTHORIZATION:
        pytest.fail("BLOCKED: explicitly prepared private synthetic Cube stack is required", pytrace=False)
    try:
        stack = CubeStack()
        response = stack.request("/livez")
        if response.status_code != 200:
            raise ValueError
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, httpx.HTTPError):
        pytest.fail("BLOCKED: prepared real pinned Cube stack is unavailable", pytrace=False)
    yield stack
    stack.client.close()


def raw_query(cube="Orders", filters=None):
    return {"dimensions": [f"{cube}.id", f"{cube}.segment", f"{cube}.tenant_id"],
        "measures": [f"{cube}.revenue"], "order": {f"{cube}.id": "asc"}, "limit": 1001,
        "filters": filters or []}


@pytest.mark.parametrize("endpoint", ["cube", "cube-test"])
@pytest.mark.parametrize("path", ["/cubejs-api/v1/meta", "/cubejs-api/v1/load"])
def test_missing_context_is_denied(cube_stack, endpoint, path):
    body = {"query": raw_query(), "cache": "must-revalidate"} if path.endswith("load") else None
    response = cube_stack.request(path, endpoint=endpoint, body=body)
    assert response.status_code in {401, 403}
    assert "data" not in response.json()


@pytest.mark.parametrize("patch", [{"exp": 1}, {"tenant_id": "south"}, {"row_constraints": []},
    {"model_bundle_digest": "0" * 64}, {"iss": "attacker"}, {"aud": "attacker"}, {"roles": ["admin"]}])
def test_invalid_context_is_denied(cube_stack, patch):
    response = cube_stack.request("/cubejs-api/v1/meta", token=cube_stack.token(**patch))
    assert response.status_code in {401, 403}
    assert "cubes" not in response.json()


def test_authenticated_meta_records_actual_release_shape(cube_stack):
    response = cube_stack.request("/cubejs-api/v1/meta", token=cube_stack.token())
    assert response.status_code == 200
    body = response.json()
    assert {cube["name"] for cube in body["cubes"]} == {"Orders", "Watermark"}
    cube_stack.capture("meta.json", body)


@pytest.mark.parametrize("tenant,expected", [
    ("north", {"n1": ("A", "10.10"), "n2": (None, "20.20"), "n3": ("A", "9007199254740993.01")}),
    ("south", {"s1": ("A", "100.00"), "s2": ("B", "0.01")}),
])
def test_known_answer_lossless_rows(cube_stack, tenant, expected):
    rows = cube_stack.load(raw_query(), tenant=tenant, principal=f"demo-{tenant}")
    assert {row["Orders.id"]: (row["Orders.segment"], row["Orders.revenue"]) for row in rows} == expected
    assert {row["Orders.tenant_id"] for row in rows} == {tenant}
    assert all(isinstance(row["Orders.revenue"], str) for row in rows)


def test_hostile_client_or_cannot_remove_mandatory_predicate(cube_stack):
    rows = cube_stack.load(raw_query(filters=[{"or": [
        {"member": "Orders.tenant_id", "operator": "equals", "values": ["south"]},
        {"member": "Orders.segment", "operator": "set"}]}]))
    assert {row["Orders.id"] for row in rows} == {"n1", "n3"}
    assert cube_stack.load(raw_query(filters=[{"member": "Orders.tenant_id", "operator": "equals", "values": ["south"]}])) == []


def test_actual_cache_isolation_both_tenant_orders_and_entitlements(cube_stack):
    query = raw_query()
    for tenants in [("north", "south", "north"), ("south", "north", "south")]:
        for tenant in tenants:
            rows = cube_stack.load(query, tenant=tenant, principal=f"demo-{tenant}")
            assert {row["Orders.tenant_id"] for row in rows} == {tenant}
    base = [{"member": "Orders.tenant_id", "operator": "eq", "values": ["north"]},
            {"member": "Orders.access_group", "operator": "in", "values": ["north"]}]
    for constraints, expected in [(base, {"n1", "n2", "n3"}),
        (base + [{"member": "Orders.segment", "operator": "eq", "values": ["A"]}], {"n1", "n3"}),
        (base, {"n1", "n2", "n3"})]:
        # Same principal, same tenant, same query, changed mandatory entitlements.
        rows = cube_stack.load(query, constraints=constraints)
        assert {row["Orders.id"] for row in rows} == expected


def test_missing_explicit_cache_semantics_are_denied(cube_stack):
    for cache in [None, "stale-if-slow", "stale-while-revalidate"]:
        response = cube_stack.request("/cubejs-api/v1/load", token=cube_stack.token(),
            body={"query": raw_query(), **({"cache": cache} if cache else {})})
        assert response.status_code in {400, 401, 403}
        assert "data" not in response.json()


@pytest.mark.parametrize("path,body", [
    ("/cubejs-api/v1/sql", {"query": raw_query()}),
    ("/cubejs-api/v1/cubesql", {"query": "select 1"}),
    ("/cubejs-api/graphql", {"query": "{ __schema { types { name } } }"}),
    ("/playground", None), ("/cubejs-system/v1/context", None), ("/dev", None),
])
def test_sql_graphql_and_dev_ingress_are_unavailable(cube_stack, path, body):
    response = cube_stack.request(path, token=cube_stack.token(), body=body)
    assert response.status_code in {400, 401, 403, 404}


def test_native_sql_and_websocket_ports_are_closed(cube_stack):
    for host in ("cube", "cube-test"):
        for port in (15432, 3306, 4444, 3000):
            with socket.socket() as connection:
                connection.settimeout(1)
                assert connection.connect_ex((cube_stack.hosts[host], port)) != 0


def test_cold_bounded_fixture_observes_continue_wait_then_success(cube_stack):
    cube_stack.capture_delay_plans()
    rows = cube_stack.load(raw_query("SlowOrders"), endpoint="cube-test", require_pending=True)
    assert len(rows) == 3, "Cold bounded fixture must return exactly three north rows"
    assert {row["SlowOrders.id"]: (row["SlowOrders.segment"], row["SlowOrders.revenue"]) for row in rows} == {
        "n1": ("A", "10.10"), "n2": (None, "20.20"), "n3": ("A", "9007199254740993.01")}
    assert {row["SlowOrders.tenant_id"] for row in rows} == {"north"}


def test_source_roles_are_read_only_separate_and_statement_timeout_is_server_enforced(cube_stack):
    with cube_stack.connect() as connection:
        assert connection.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
        assert connection.execute("SHOW statement_timeout").fetchone()[0] == "25s"
        flags = connection.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
        assert flags == (False, False, False, False, False)
        assert connection.execute("SELECT has_schema_privilege(current_user,'demo_source','USAGE'), has_schema_privilege(current_user,'test_source','CREATE')").fetchone() == (False, False)
    for statement in ["INSERT INTO test_source.orders VALUES ('forged','north','north',now(),'A',1)",
        "CREATE TABLE test_source.forged(id int)", "UPDATE test_source.orders SET amount=0"]:
        with cube_stack.connect() as connection:
            with pytest.raises(psycopg.Error) as error:
                connection.execute(statement)
            assert error.value.sqlstate in {"42501", "25006"}
    started = time.monotonic()
    with cube_stack.connect() as connection:
        with pytest.raises(psycopg.errors.QueryCanceled) as error:
            connection.execute("SELECT test_source.bounded_delay(90)")
        assert error.value.sqlstate == "57014"
    elapsed = time.monotonic() - started
    assert 24 <= elapsed < 35, "Independent source statement timeout did not stop synthetic delay"
    cube_stack.capture("source-timeout.json", {"sqlstate": "57014", "elapsed_seconds": round(elapsed, 3)})


def test_full_effective_compose_and_actual_topology_publish_only_loopback_edge(cube_stack):
    compose = json.loads((cube_stack.evidence / "compose.json").read_text())
    assert {name for name, value in compose["services"].items() if value.get("ports")} == {"edge"}
    assert all(port.get("host_ip") == "127.0.0.1" for port in compose["services"]["edge"]["ports"])
    assert all(network.get("internal") is True for network in compose["networks"].values())
    topology = json.loads((cube_stack.evidence / "topology.json").read_text())
    assert {row["service"] for row in topology if row["ports"]} == {"edge"}
    assert all(binding["HostIp"] == "127.0.0.1" for row in topology for bindings in row["ports"].values() if bindings for binding in bindings)
    assert all(row["internal_networks_only"] for row in topology)


def test_half_open_dst_boundary_and_empty_result(cube_stack):
    query = raw_query("BoundaryOrders", filters=[
        {"member": "BoundaryOrders.ordered_at", "operator": "afterOrOnDate", "values": ["2024-03-10T05:00:00Z"]},
        {"member": "BoundaryOrders.ordered_at", "operator": "beforeDate", "values": ["2024-03-11T04:00:00Z"]},
    ])
    query["timezone"] = "UTC"
    rows = cube_stack.load(query, endpoint="cube-test")
    assert {row["BoundaryOrders.id"] for row in rows} == {"dst-start", "dst-end-before"}
    query["filters"] = [{"member": "BoundaryOrders.ordered_at", "operator": "afterOrOnDate", "values": ["2025-01-01T00:00:00Z"]}]
    assert cube_stack.load(query, endpoint="cube-test") == []


def test_daily_grain_and_exact_decimal_totals(cube_stack):
    query = {"measures": ["Orders.revenue"], "dimensions": ["Orders.segment"],
        "timeDimensions": [{"dimension": "Orders.ordered_at", "granularity": "day"}],
        "timezone": "America/New_York", "limit": 1001,
        "filters": [{"member": "Orders.ordered_at", "operator": "afterOrOnDate", "values": ["2024-03-09T00:00:00"]},
                    {"member": "Orders.ordered_at", "operator": "beforeDate", "values": ["2024-03-12T00:00:00"]}]}
    for tenant, total in [("north", Decimal("9007199254741023.31")), ("south", Decimal("100.01"))]:
        rows = cube_stack.load(query, tenant=tenant, principal=f"demo-{tenant}")
        assert sum((Decimal(row["Orders.revenue"]) for row in rows), Decimal(0)) == total
        assert all("Orders.ordered_at.day" in row for row in rows)
        if tenant == "north":
            assert {row["Orders.ordered_at.day"][:10] for row in rows} == {"2024-03-09", "2024-03-10", "2024-03-11"}


def test_runtime_watermark_is_load_time_persisted_across_restart(cube_stack):
    rows = cube_stack.load({"dimensions": ["Watermark.loaded_at"], "limit": 1001})
    assert len(rows) == 1
    watermark = rows[0]["Watermark.loaded_at"]
    assert watermark[:10] != "2024-03-12", "Running seed must not copy the fixed unit-test watermark"
    snapshot = cube_stack.evidence / "watermark-before-restart.json"
    if snapshot.exists():
        assert json.loads(snapshot.read_text())["watermark"] == watermark
        assert os.environ.get("GATEWAY_CUBE_RESTART_PHASE") == "after"
    else:
        cube_stack.capture("watermark-before-restart.json", {"watermark": watermark})
    assert cube_stack.load({"dimensions": ["Watermark.loaded_at"], "limit": 1001}) == rows


def test_source_timeout_through_cube_observes_source_stop(cube_stack):
    query = raw_query("TimeoutOrders")
    start = time.monotonic()
    response = cube_stack.request("/cubejs-api/v1/load", endpoint="cube-test",
        token=cube_stack.token(endpoint="cube-test"), body={"query": query, "cache": "must-revalidate"})
    assert response.status_code == 200 and response.json().get("error") == "Continue wait"
    observed_active = False
    source_stopped = False
    with cube_stack.connect("fixture_observer") as observer:
        observer.autocommit = True
        while time.monotonic() - start < 35:
            active = observer.execute("SELECT count(*) FROM pg_stat_activity WHERE usename='fixture_reader' AND state='active' AND query LIKE '%%timeout_orders%%' AND wait_event='PgSleep'").fetchone()[0]
            observed_active |= active > 0
            if observed_active and active == 0:
                source_stopped = True
                break
            time.sleep(0.2)
    elapsed = time.monotonic() - start
    assert observed_active, "Source observer never saw the real Cube query's bounded delay"
    assert source_stopped and elapsed < 35, "Source continued after its independent PostgreSQL timeout"
    # A subsequent poll must expose an actual error, never successful partial rows.
    response = cube_stack.request("/cubejs-api/v1/load", endpoint="cube-test",
        token=cube_stack.token(endpoint="cube-test"), body={"query": query, "cache": "must-revalidate"})
    assert "error" in response.json() and response.json()["error"] != "Continue wait"
    assert "data" not in response.json()
    cube_stack.capture("cube-source-stop.json", {"observed_source_active": observed_active,
        "observed_source_stopped": source_stopped, "elapsed_seconds": round(elapsed, 3),
        "poll_status": response.status_code, "poll_keys": sorted(response.json())})


def test_probe_outbound_is_blocked(cube_stack):
    if cube_stack.host_mode:
        # The preparation runner needs network for dependency/image retrieval.
        # The already-run private probe records its actual negative sockets.
        recorded = json.loads((cube_stack.evidence / "probe-egress.json").read_text())
        assert recorded == {"blocked": [True, True], "probe": "isolated_private_network"}
        return
    failures = []
    for address, port in [("1.1.1.1", 443), ("8.8.8.8", 53)]:
        with socket.socket() as connection:
            connection.settimeout(2)
            failures.append(connection.connect_ex((address, port)) != 0)
    assert all(failures), "Prepared synthetic probe unexpectedly has outbound connectivity"
    cube_stack.capture("probe-egress.json", {"blocked": failures, "probe": "isolated_private_network"})
