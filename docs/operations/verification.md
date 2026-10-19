# Development and verification

**As of the freeze (2026-10-16).** This section used to sit on the README front page. Every workflow is manual (`workflow_dispatch`), so nothing runs on a push; the last CI-verified source is recorded in the [README status block](../../README.md#spaceport).

After installing the locked dependencies (the `uv sync` and `npm ci` steps in the README's [synthetic demo steps](../../README.md#try-it-with-synthetic-data) or the [runbook](runbook.md#acquisition-versus-offline-operation)), the source-level checks are:

```sh
uv run --locked --offline pytest -m 'unit or contract' -q
npm --prefix publisher test -- --run
npm --prefix publisher run typecheck
npm --prefix publisher run check:generated
npm --prefix publisher run build
npm --prefix publisher run format:check
```

The unrestricted `make test` includes suites requiring real services. The frontend development server (`npm --prefix publisher run dev`) proxies `/v1` to `127.0.0.1:8000`; it does not start a gateway or provision demo state.

For final acceptance, use a clean checkout on the supported Docker host and the two separate wrapper modes:

```sh
umask 077
BACKEND_PARENT="$(mktemp -d /tmp/spaceport-backend.XXXXXX)"
DEMO_TEST_PARENT="$(mktemp -d /tmp/spaceport-acceptance.XXXXXX)"
unset PYTEST_ADDOPTS PYTEST_PLUGINS
bash scripts/verify_gateway_integration.sh --backend "$BACKEND_PARENT/run"
bash scripts/verify_gateway_integration.sh --demo "$DEMO_TEST_PARENT/run"
```

Each `run` path must be new, with a private, owner-controlled parent outside the repository. Keep package/browser caches outside those run directories. Backend mode requires the complete real PostgreSQL/Cube selection. Demo mode requires all 14 integration slots in two independent clean cycles; the second must reuse prepared dependencies and image IDs without acquisition.

The packet observer is not currently available, so full demo acceptance remains blocked even when individual business suites pass. Ordinary `make verify-demo` covers 13 business-verification slots and omits `acceptance_security`; it cannot substitute for final acceptance. Follow the [exact acceptance procedure](runbook.md#separate-final-acceptance-modes), including cleanup and retained source-bound evidence. The manual partitioned CI path is described in [bounded serial backend verification](serial-gateway-verification.md).
