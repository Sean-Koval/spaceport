# Spaceport documentation index

**As of the freeze (2026-10-16).** Spaceport is frozen; see the [README status block](../README.md#spaceport) and [ROADMAP](../ROADMAP.md#frozen). Every document below describes the repository as it stood at the freeze. Nothing here is a plan of record for new work until the owner unfreezes the project. Evidence documents record what was verified at a specific source identity; they are not refreshed and do not cover later commits.

The [README](../README.md) is the entry point for what Spaceport is and how to run the synthetic demo. [Architecture at the freeze](architecture.md) holds the longer overview that used to be on the README: what is implemented, how the parts fit, and a repository guide. This page lists every documentation directory so the material is easy to find.

## Design records and specifications

Approved and candidate designs. Freshness: as of freeze; designs are the architectural boundary, and later changes need their own review.

- `design-records/`:
  - [Approved design](design-records/2026-10-13-spaceport.md): the original gateway design (written under the former name, Data Product Gateway).
  - [Organization runtime foundation](design-records/2026-10-14-organization-runtime-foundation.md): OIDC/TLS runtime for GCP/GKE.
  - [Reviewed connector wave](design-records/2026-10-15-reviewed-connectors.md).
  - [Source pilot runtime](design-records/2026-10-15-source-pilot-runtime.md), with component designs for [setup and team workflow](design-records/2026-10-15-source-pilot-setup.md), [BigQuery](design-records/2026-10-15-bigquery-pilot-runtime.md) and [Domo](design-records/2026-10-15-domo-pilot-runtime.md).
- `specs/`: internal implementation contracts.
  - [Source lifecycle interface v1](specs/source-lifecycle-interface.md): the shared source-hold lifecycle contract.
  - [Domo component acceptance](specs/domo-component-acceptance.md): what the Domo pilot path must prove.

## Operations

How to run, configure and gate the system. Freshness: as of freeze; no deployment exists, and every live-source or production step stays gated.

- `operations/`:
  - [Synthetic demo runbook](operations/runbook.md): prepare, run, back up and restore the private synthetic stack.
  - [Development and verification](operations/verification.md): source-level checks and the two final acceptance wrapper modes.
  - [BigQuery and Domo pilot, and release boundaries](operations/release-gates.md): why organization activation is blocked, and what live sources and release still require.
  - [Reviewed-source pilot](operations/source-pilot.md): operator setup and the author/test/publish workflow for BigQuery and Domo products.
  - [Organization pilot (GCP/GKE)](operations/organization-pilot.md): deployment-readiness assessment, not approval.
  - [Identity and deployment modes](operations/identity.md).
  - [Production gates](operations/production-gates.md): what must be satisfied before real data or deployment.
  - [Licensing and dependencies](operations/licensing.md), with the generated [dependency inventory](operations/dependency-inventory.json) and verbatim upstream notices under [`THIRD_PARTY_NOTICES/`](../THIRD_PARTY_NOTICES/) at the repository root.
  - Source-specific gates: [BigQuery through Cube](operations/bigquery.md), [offline BigQuery controls](operations/bigquery-controls.md), [Domo phase 1b](operations/domo-phase-1b.md), [GCS artifacts](operations/gcs-artifacts.md), [reviewed PostgreSQL v2](operations/reviewed-postgres.md).
  - [Packet observer proposal](operations/observer-proposal.md): blocked, not implemented.
  - [Bounded serial backend verification](operations/serial-gateway-verification.md): the manual partitioned CI workflow.

## Compatibility

Pinned versions and wire contracts. Freshness: as of freeze; pins are exact, and any dependency patch allowed under the freeze must update them.

- `compatibility/`:
  - [Pinned versions](compatibility/versions.md), including the [legacy internal identifiers](compatibility/versions.md#legacy-internal-identifiers) kept from the former name.
  - [MCP profile](compatibility/mcp.md) and its [tool schema snapshot](compatibility/mcp-tools.json): the four fixed MCP tools and the client harnesses.
  - [OpenAPI snapshot](compatibility/openapi.json) and [saved-draft discovery](compatibility/drafts.md): the REST contract.
  - [Cube compatibility](compatibility/cube.md): image pins and the synthetic Cube boundary.

## Evidence

What was verified, at which source identity. Freshness: frozen records; do not edit recorded facts, and do not read them as covering later commits.

- `evidence/`:
  - [Acceptance index](evidence/acceptance.md) and [limitations](evidence/limitations.md): overall acceptance is BLOCKED; these list what is proven and what is not.
  - [Security matrix](evidence/security.md).
  - [Private-main baseline](evidence/private-main-baseline.md), [organization runtime foundation](evidence/organization-runtime-foundation.md), [reviewed connectors](evidence/reviewed-connectors.md) and [source pilot runtime](evidence/source-pilot-runtime.md): per-milestone evidence boundaries.

## Archive and assets

Historical material kept for provenance. Freshness: frozen; these are not instructions, a task list or a plan of record (see the [archive README](archive/README.md)).

- `archive/`:
  - `archive/plans/`: the implementation plans for the [approved design](archive/plans/2026-10-13-spaceport.md), [organization runtime foundation](archive/plans/2026-10-14-organization-runtime-foundation.md), [reviewed connectors](archive/plans/2026-10-15-reviewed-connectors.md) and [source pilot runtime](archive/plans/2026-10-15-source-pilot-runtime.md).
  - `archive/research/`: dated research notes (13 October 2026), recommendations rather than selected dependencies: [integrations and SDKs](archive/research/integrations.md), [performance and reliability](archive/research/performance.md), [publisher UI libraries](archive/research/publisher-ui.md), [Foundry-like references](archive/research/foundry-references.md), [Helix Foundry reuse](archive/research/helix-foundry.md), [nao and dashboard references](archive/research/nao-dashboard-references.md).
- `assets/`: README images. `assets/brand/` holds the banner, mark and favicon; `assets/front/review.png` is the README picture, regenerated by `scripts/capture_front.py` from the publisher and the mock gateway in `scripts/front_mock_gateway.py`.

Spaceport's visual system is Airframe ([agent-factory/airframe](https://github.com/Sean-Koval/agent-factory/tree/main/airframe)). The earlier HTML design study `spaceport-preview` is deleted; git history keeps it, last at commit [3832dba](https://github.com/Sean-Koval/spaceport/commit/3832dba) under `docs/archive/spaceport-preview/`.

## Not yet indexed

Draft PR [#8](https://github.com/Sean-Koval/spaceport/pull/8) (source-access security) is paused at the freeze. It would add a spec, a plan, evidence reviews and operator documents; if it ever merges, index its spec under `design-records/`, its plan under `archive/plans/` and the rest in their sections here.
