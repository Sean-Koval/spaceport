"""Optional live gate, separately reported from mock contracts.

Default is NOT RUN. Opt-in is not authorization. Even with valid one-shot
operator approval this pinned driver's source cost/lifetime gate blocks before
resolving credentials or sending a query. This is a gate, not live success.
There is deliberately no direct BigQuery SDK/SQL bypass or environment-driven
endpoint factory. A runnable smoke path requires separately reviewed retained
BigQuery model/binding/driver assets and actual bounded-source evidence first.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

from gateway.adapters.bigquery import require_live_source_bounds, validate_live_authorization
from gateway.contracts.errors import GatewayError

pytestmark = pytest.mark.live_bigquery


def test_explicitly_authorized_bigquery_cube_smoke():
    opt_in = os.environ.get("GATEWAY_BIGQUERY_LIVE", "")
    if not opt_in:
        # Do not read configuration, authorization, credentials or open sockets.
        pytest.skip("NOT RUN: separate live BigQuery authorization is absent")
    if opt_in != "1":
        pytest.fail("BLOCKED: live BigQuery opt-in must be explicit", pytrace=False)
    authorization_path = os.environ.get("GATEWAY_BIGQUERY_LIVE_AUTHORIZATION_FILE")
    if not authorization_path:
        pytest.fail("BLOCKED: separate scoped BigQuery authorization is required", pytrace=False)
    try:
        # Operator approval file contains settings/references only, never keys.
        # Cap the read before JSON allocation. No dynamic imports/runner commands.
        with Path(authorization_path).open("rb") as stream:
            wire = stream.read(16_385)
        if len(wire) > 16_384:
            raise ValueError
        value = json.loads(wire.decode("utf-8"))
        if type(value) is not dict or set(value) != {"settings", "authorization"}:
            raise ValueError
        validate_live_authorization(value["settings"], value["authorization"],
            now=datetime.now(timezone.utc))
        # Always blocks for Cube 1.8.5. No secret resolver or network can run.
        require_live_source_bounds(value["settings"])
    except GatewayError as caught:
        pytest.fail("BLOCKED: " + caught.safe_message, pytrace=False)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        pytest.fail("BLOCKED: invalid or unavailable BigQuery approval record", pytrace=False)
    pytest.fail("BLOCKED: reviewed bounded BigQuery deployment smoke runner is unavailable", pytrace=False)
