# Spaceport

Spaceport is a self-hosted gateway that turns reviewed team data into reusable products that people and agents can discover and query through one governed API. A product defines what an answer means, its typed inputs and outputs, who can use it, how fresh it must be, and the tests required before publication.

The goal is to publish a trusted metric once and reuse the same approved definition across agent clients. The current reference implementation uses synthetic PostgreSQL data through Cube Core, without requiring a model-provider or vendor account. A separate reviewed PostgreSQL/Cube v2 path removes the synthetic Orders-only binding restriction. BigQuery source-control software and Domo/GCS offline readers are included as bounded candidates; live activation remains a separate gate. Domo still requires reconciliation with authoritative cards and Beast Modes.

The private GitHub repository is [Sean-Koval/spaceport](https://github.com/Sean-Koval/spaceport). Python project and package identifiers retain `data-product-gateway` / `data-product-publisher`. Public naming clearance and original-code licensing remain unresolved.

## What is implemented

The source includes:

- A permission-filtered catalog, product definitions, examples, units, grain, freshness requirements and provenance.
- A React publisher with an authorized saved-draft inbox, versioned editor, known-answer and permission-negative test results, definition diffs, and publication of the exact tested revision. Author and publisher are separate roles.
- A consumer workspace with permitted typed filters and sorting, in-memory idempotent request recovery, explicit new runs and pinned result identity checks.
- Immutable published versions, audited lifecycle changes, tenant-scoped queries, durable execution, idempotency, bounded results and authorized pagination.
- A REST API and four fixed MCP tools: `search_products`, `describe_product`, `query_product` and `get_result`. Python and TypeScript client harnesses exercise the same contract.
- Synthetic Compose packaging, migrations, generated demo identities, health/readiness checks, backup/restore commands and verification tooling.

Agents receive approved product capabilities, not database credentials or unrestricted SQL. Source models and bindings are operator-reviewed assets; the publisher does not upload executable models.

## Current status

The reviewed private-main baseline was merged on 14 October 2026. Its exact source tree passed 1,847 unit/contract tests and 294 genuine PostgreSQL/Cube backend tests, with verified cleanup, in [run 38615111132](https://github.com/Sean-Koval/spaceport/actions/runs/38615111132). See the [source-bound baseline record](docs/evidence/private-main-baseline.md). This is not a production release or verified end-to-end installation; later changes need their own checks.

The consumer query workspace and authorized draft inbox merged in [PR 2](https://github.com/Sean-Koval/spaceport/pull/2) on 14 October 2026. Its [merge commit](https://github.com/Sean-Koval/spaceport/commit/7a45b7e22514375de375dc71be1f8c70f843c589) has the exact same tree as [tested source 44062b2e](https://github.com/Sean-Koval/spaceport/commit/44062b2ea8fa5b0d65bd29f5090209de164ff3bf): [run 38633087264](https://github.com/Sean-Koval/spaceport/actions/runs/38633087264) admitted 1,950 unit/contract tests and all 329 genuine PostgreSQL/Cube backend cases, with both cleanup checks passing. The unchanged frontend passed 254 frontend tests, typecheck, generated-client consistency, build and formatting checks. Rendered-browser and composed-demo acceptance remain unverified; live-source and production gates remain open.

The organization-runtime foundation merged in [PR 3](https://github.com/Sean-Koval/spaceport/pull/3) on 15 October 2026. Its [merge commit](https://github.com/Sean-Koval/spaceport/commit/a4e3c4fb36a60effd66a72a30fea6fbbabc44540) preserves tested tree `76611787d8ee0c39478f6377bbb62dca3092e6e8` from source `c060dc23c97cae0123d075ce5d78767baf9773d9`. [Run 38650608566](https://github.com/Sean-Koval/spaceport/actions/runs/38650608566) passed 2,221 unit/contract tests and all 346 genuine PostgreSQL/Cube backend cases, with verified cleanup. The unchanged frontend passed 254 tests and its source checks locally. See the [foundation evidence](docs/evidence/organization-runtime-foundation.md) for the scope and remaining gates.

| Area | Evidence boundary |
| --- | --- |
| Catalog, publishing, execution, REST/MCP and UI | Implemented in source with automated test coverage. The merged PR 3 source passed the complete backend gate; composed-demo acceptance remains open and later changes require their own evidence. |
| Synthetic PostgreSQL / Cube | Reference runtime and fixtures are included. A full successful final-source demo, clean repeat and rendered-browser workflow are still required. |
| Reviewed PostgreSQL / Cube v2 | Versioned operator bindings and same-query source checkpoints, with separate synthetic consumer/fixture assets. See the [connector evidence](docs/evidence/reviewed-connectors.md) for exact tests and remaining live-source prerequisites. |
| BigQuery | Executable bounded driver/admission library candidate with offline tests; gateway runtime selection remains blocked. Packaged-driver, live source controls and completion/release integration remain unverified. |
| Domo | Offline finite named-query reader and strict response contracts. No runtime selection or live authentication; identity/PDP and business-definition reconciliation remain open. |
| GCS artifacts | Offline exact-generation allowlist reader with bounded buffering and authorization callbacks. No public artifact route, live transport or policy inheritance implementation. |
| Studio / production identity | Studio interoperability and live delegated OIDC access are not tested. |
| Offline runtime assurance | Acquisition and runtime are separated in the tooling. Stronger packet-observer evidence remains blocked; an internal Docker network alone is not proof. |

The [acceptance index](docs/evidence/acceptance.md) and [limitations](docs/evidence/limitations.md) explain the required evidence. Older passes apply only to the source and environment they tested. The [roadmap](ROADMAP.md) separates the private-main baseline, next product work and live-source/production gates.

GCP/GKE is the selected organization-deployment target. The [organization-pilot assessment](docs/operations/organization-pilot.md) covers infrastructure, credentials, performance, UI/UX and the remaining decisions. The implemented [runtime foundation](docs/superpowers/specs/2026-10-14-organization-runtime-foundation.md) introduces explicit OIDC/TLS configuration and controlled identity provisioning against synthetic data. Its [source and backend evidence](docs/evidence/organization-runtime-foundation.md) does not activate BigQuery/Domo, implement browser SSO or authorize a rollout.

## Architecture

The React UI and MCP clients call the same Python gateway services. The gateway authenticates the caller, checks current policy, pins a product version and builds a query from its approved binding. A worker processes bounded jobs through Cube; consumers retrieve authorized, typed result envelopes.

- **Gateway:** Python, FastAPI and the MCP SDK; contracts, catalog, authorization, publishing, execution and results.
- **Registry:** PostgreSQL stores definitions, draft revisions, publication evidence, executions, results and redacted audits. Alembic manages schema changes.
- **Semantic execution:** a separate Cube Core service uses reviewed models against a separate synthetic PostgreSQL source. Fixture testing uses an isolated Cube test service.
- **Publisher:** React, TypeScript and Vite with generated OpenAPI types, served by the local edge service.
- **Operations:** separate worker and retention processes; only the edge exposes loopback ingress. Cube and database ports remain private.

REST routes use `/v1`; MCP uses JSON Streamable HTTP at `/mcp`. The [saved-draft inbox](docs/compatibility/drafts.md) is a bounded, authorized REST discovery surface; the four MCP tools are unchanged. See the [OpenAPI snapshot](docs/compatibility/openapi.json), [MCP profile](docs/compatibility/mcp.md) and [tool schemas](docs/compatibility/mcp-tools.json). Result freshness refers to the recorded source-load watermark, not the time a query was fetched.

## Local synthetic setup

These are the implemented startup commands, not a claim that the full flow has passed acceptance. Read the [operations runbook](docs/operations/runbook.md) before running them. Use synthetic data and a new disposable state directory, never production credentials or an existing production database.

Prerequisites:

- Operator-controlled Linux Docker Engine with Docker Compose, `linux/amd64`.
- Exact toolchain: Python **3.12.14**, uv **0.12.23**, Node **24.19.0**, npm **11.9.0**. The Node pin does not supply the required npm version automatically.
- Network access during preparation to acquire locked packages, images and Playwright Chromium; required browser system libraries must already be available before verification.

From the repository root:

```sh
uv sync --locked --dev
npm --prefix publisher ci --ignore-scripts --no-audit --no-fund
npm --prefix clients ci --ignore-scripts --no-audit --no-fund
(cd publisher && npm exec -- playwright install chromium)

umask 077
DEMO_PARENT="$(mktemp -d /tmp/spaceport-demo.XXXXXX)"
STATE_DIR="$DEMO_PARENT/state"
make demo-prepare STATE_DIR="$STATE_DIR"
make demo-up STATE_DIR="$STATE_DIR"
```

The state directory must be absolute, outside the repository and previously nonexistent; its parent must already exist and be private. Preparation generates synthetic credentials, builds locked images and records source/image identities. Keep all generated state private and out of version control. Source changes require a newly prepared state.

Open **http://127.0.0.1:8080**. The synthetic tokens are in `$STATE_DIR/identities/demo-tokens.json`; enter only the intended account's token in the browser. Tokens remain in browser memory.

1. Use `demo-author` to load the revenue template, save a draft and run its fixtures.
2. Use the separate `demo-publisher` account to review and publish that exact tested revision.
3. Use `demo-north` or `demo-south` to discover the product and query the authorized rows.

The first boot intentionally has no published product: `/healthz` can succeed while `/readyz` returns 503. Publication establishes the readiness probe. Demo source data older than its 24-hour freshness limit correctly becomes stale; prepare a fresh stack rather than changing its watermark to hide staleness.

```sh
make demo-down STATE_DIR="$STATE_DIR"
```

Stopping retains volumes and state; `demo-up` resumes them. Startup does not pull images or install dependencies. See the runbook for [backup and fresh-stack restore](docs/operations/runbook.md#backup-and-restore).

## Development and verification

After installing the locked dependencies above, the source-level checks are:

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

The packet observer is not currently available, so full demo acceptance remains blocked even when individual business suites pass. Ordinary `make verify-demo` covers 13 business-verification slots and omits `acceptance_security`; it cannot substitute for final acceptance. Follow the [exact acceptance procedure](docs/operations/runbook.md#separate-final-acceptance-modes), including cleanup and retained source-bound evidence.

## Repository guide

- `gateway/`: contracts, API/MCP, identity/policy, registry, publishing, adapters and execution.
- `publisher/`: UI and component/browser test configuration.
- `cube/`, `fixtures/`, `infra/`: reviewed semantic assets, synthetic data and runtime packaging.
- `clients/`: independent Python and TypeScript MCP acceptance clients.
- `migrations/`, `scripts/`, `tests/`: schema migrations, operational/verifier tools and test suites.
- `docs/`: [approved design](docs/superpowers/specs/2026-10-13-data-product-gateway.md), [implementation plan](docs/superpowers/plans/2026-10-13-data-product-gateway.md), compatibility, operations and evidence.

## Live sources and release boundaries

Start with the [BigQuery gate](docs/operations/bigquery.md), [Domo Phase 1b gate](docs/operations/domo-phase-1b.md), [identity guide](docs/operations/identity.md) and [production checklist](docs/operations/production-gates.md). A configured connection, a mocked test or a synthetic demo does not establish live-source readiness. The [reviewed connector design](docs/superpowers/specs/2026-10-15-reviewed-connectors.md) distinguishes the complete synthetic v2 path from the offline-only BigQuery/Domo/GCS slices.

V1 excludes ETL, arbitrary SQL/formulas, cross-product joins, source writes, dashboards, ontology engines and agent orchestration. Real credentials, paid queries, deployment and public distribution require their own decisions and verification. Preserve [third-party notices](THIRD_PARTY_NOTICES.md) and the [dependency inventory](docs/operations/licensing.md); the repository does not grant an original-code redistribution license.
