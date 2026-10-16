# Spaceport documentation index

**As of the freeze (2026-10-16).** Spaceport is frozen; see the [README status block](../README.md#spaceport) and [ROADMAP](../ROADMAP.md#frozen). Every document below describes the repository as it stood at the freeze. Nothing here is a plan of record for new work until the owner unfreezes the project. Evidence documents record what was verified at a specific source identity; they are not refreshed and do not cover later commits.

The [README](../README.md) is the entry point for what Spaceport is and how to run the synthetic demo. This page lists every documentation directory so the material is easy to find.

## Specifications and plans

Approved and candidate designs, with their implementation plans. Freshness: as of freeze; designs are the architectural boundary, and later changes need their own review.

- `superpowers/` (`superpowers/specs/` and `superpowers/plans/`):
  - [Approved design](superpowers/specs/2026-10-13-spaceport.md) and its [implementation plan](superpowers/plans/2026-10-13-spaceport.md): the original gateway design (written under the former name, Data Product Gateway).
  - [Organization runtime foundation](superpowers/specs/2026-10-14-organization-runtime-foundation.md) and [plan](superpowers/plans/2026-10-14-organization-runtime-foundation.md): OIDC/TLS runtime for GCP/GKE.
  - [Reviewed connector wave](superpowers/specs/2026-10-15-reviewed-connectors.md) and [plan](superpowers/plans/2026-10-15-reviewed-connectors.md).
  - [Source pilot runtime](superpowers/specs/2026-10-15-source-pilot-runtime.md) and [plan](superpowers/plans/2026-10-15-source-pilot-runtime.md), with component designs for [setup and team workflow](superpowers/specs/2026-10-15-source-pilot-setup.md), [BigQuery](superpowers/specs/2026-10-15-bigquery-pilot-runtime.md) and [Domo](superpowers/specs/2026-10-15-domo-pilot-runtime.md).
- `specs/`: internal implementation contracts.
  - [Source lifecycle interface v1](specs/source-lifecycle-interface.md): the shared source-hold lifecycle contract.
  - [Domo component acceptance](specs/domo-component-acceptance.md): what the Domo pilot path must prove.

## Operations

How to run, configure and gate the system. Freshness: as of freeze; no deployment exists, and every live-source or production step stays gated.

- `operations/`:
  - [Synthetic demo runbook](operations/runbook.md): prepare, run, back up and restore the private synthetic stack.
  - [Reviewed-source pilot](operations/source-pilot.md): operator setup and the author/test/publish workflow for BigQuery and Domo products.
  - [Organization pilot (GCP/GKE)](operations/organization-pilot.md): deployment-readiness assessment, not approval.
  - [Identity and deployment modes](operations/identity.md).
  - [Production gates](operations/production-gates.md): what must be satisfied before real data or deployment.
  - [Licensing and dependencies](operations/licensing.md), with the generated [dependency inventory](operations/dependency-inventory.json) and verbatim upstream notices under `operations/notices/`.
  - Source-specific gates: [BigQuery through Cube](operations/bigquery.md), [offline BigQuery controls](operations/bigquery-controls.md), [Domo phase 1b](operations/domo-phase-1b.md), [GCS artifacts](operations/gcs-artifacts.md), [reviewed PostgreSQL v2](operations/reviewed-postgres.md).
  - [Packet observer proposal](operations/observer-proposal.md): blocked, not implemented.
- `ops/`:
  - [Bounded serial backend verification](ops/serial-gateway-verification.md): the manual partitioned CI workflow.

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

## Research and design preview

Background research and a visual study. Freshness: dated research notes (13 October 2026); recommendations, not selected dependencies.

- `research/`: [integrations and SDKs](research/integrations.md), [performance and reliability](research/performance.md), [publisher UI libraries](research/publisher-ui.md), [Foundry-like references](research/foundry-references.md), [Helix Foundry reuse](research/helix-foundry.md), [nao and dashboard references](research/nao-dashboard-references.md).
- `design/`: [Spaceport theme preview](design/spaceport-preview/README.md), an offline HTML design study that changes no runtime behavior.
- `assets/`: README banner images.

## Not yet indexed

Draft PR [#8](https://github.com/Sean-Koval/spaceport/pull/8) (source-access security) is paused at the freeze. It would add a spec, a plan, evidence reviews and operator documents; index them here if it merges.
