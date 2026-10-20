# Spaceport architecture at the freeze

**As of the freeze (2026-10-16).** This page holds the overview that used to sit on the README front page. It describes the source as it stood at the freeze, not work in progress; see the [README status block](../README.md#spaceport).

Spaceport is a self-hosted gateway that turns reviewed team data into reusable products that people and agents can discover and query through one governed API. A product defines what an answer means, its typed inputs and outputs, who can use it, how fresh it must be, and the tests required before publication.

The goal is to publish a trusted metric once and reuse the same approved definition across agent clients. The current reference implementation uses synthetic PostgreSQL data through Cube Core, without requiring a model-provider or vendor account. A separate reviewed PostgreSQL/Cube v2 path removes the synthetic Orders-only binding restriction. A reviewed-source pilot adds BigQuery/Cube and finite Domo REST serving, private operator CLI workflows and GKE configuration templates. Its exact-source backend acceptance passed; live activation remains separately gated. GCS remains an offline artifact slice. Domo still requires reconciliation with authoritative cards and Beast Modes.

The private GitHub repository is [Sean-Koval/spaceport](https://github.com/Sean-Koval/spaceport). The uv project is `spaceport` and the publisher package is `spaceport-publisher`; the `gateway/` import package and the `dpg` prefixes in stored identifiers and credential formats are kept for compatibility ([legacy identifiers](compatibility/versions.md#legacy-internal-identifiers)).

## What is implemented

The source includes:

- A permission-filtered catalog, product definitions, examples, units, grain, freshness requirements and provenance.
- A React publisher with an authorized saved-draft inbox, versioned editor, known-answer and permission-negative test results, definition diffs, and publication of the exact tested revision. Author and publisher are separate roles.
- A consumer workspace with permitted typed filters and sorting, in-memory idempotent request recovery, explicit new runs and pinned result identity checks.
- Immutable published versions, audited lifecycle changes, tenant-scoped queries, durable execution, idempotency, bounded results and authorized pagination.
- A REST API and four fixed MCP tools: `search_products`, `describe_product`, `query_product` and `get_result`. Python and TypeScript client harnesses exercise the same contract.
- Synthetic Compose packaging, migrations, generated demo identities, health/readiness checks, backup/restore commands and verification tooling.

Agents receive approved product capabilities, not database credentials or unrestricted SQL. Source models and bindings are operator-reviewed assets; the publisher does not upload executable models.

## How the parts fit

The React UI and MCP clients call the same Python gateway services. The gateway authenticates the caller, checks current policy, pins a product version and builds a query from its approved binding. A worker processes bounded jobs through the reviewed provider adapter; consumers retrieve authorized, typed result envelopes.

- **Gateway:** Python, FastAPI and the MCP SDK; contracts, catalog, authorization, publishing, execution and results.
- **Registry:** PostgreSQL stores definitions, draft revisions, publication evidence, executions, results and redacted audits. Alembic manages schema changes.
- **Semantic execution:** separate Cube services use reviewed PostgreSQL or BigQuery models; Domo executes finite reviewed REST statements. Fixture execution uses independently pinned source identities and assets.
- **Publisher:** React, TypeScript and Vite with generated OpenAPI types, served by the local edge service.
- **Operations:** separate worker and retention processes; only the edge exposes loopback ingress. Cube and database ports remain private.

REST routes use `/v1`; MCP uses JSON Streamable HTTP at `/mcp`. The [saved-draft inbox](compatibility/drafts.md) is a bounded, authorized REST discovery surface; the four MCP tools are unchanged. See the [OpenAPI snapshot](compatibility/openapi.json), [MCP profile](compatibility/mcp.md) and [tool schemas](compatibility/mcp-tools.json). Result freshness refers to the recorded source-load watermark, not the time a query was fetched.

## Repository guide

- `gateway/`: contracts, API/MCP, identity/policy, registry, publishing, adapters and execution.
- `publisher/`: UI and component/browser test configuration.
- `cube/`, `fixtures/`, `infra/`: reviewed semantic assets, synthetic data and runtime packaging.
- `clients/`: independent Python and TypeScript MCP acceptance clients.
- `migrations/`, `scripts/`, `tests/`: schema migrations, operational/verifier tools and test suites.
- `docs/`: the [documentation index](README.md), with [design records](design-records/), compatibility, operations and evidence, and an [archive](archive/README.md) of historical plans and research.
