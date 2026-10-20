# Spaceport roadmap

## Frozen

**Frozen 2026-10-16.** Spaceport is the team's capability plane (governed data products over MCP), paused while hangar adoption and flightdeck come first.

- **Allowed while frozen:** security fixes, dependency patches, CI repair, docs fixes and the cleanup issues in the freeze epic ([#10](https://github.com/Sean-Koval/spaceport/issues/10)) (#11, #12, #13, #15, all closed). #14 is post-unfreeze work and is not allowed.
- **Not allowed:** new connectors, live-source activation, deployment.
- **Still works:** the local synthetic demo and the REST/MCP contract. The four MCP tools, their schemas and the connectors do not change.
- **Last CI-verified source:** [run 22](https://github.com/Sean-Koval/spaceport/actions/runs/38800064164) on `2d3cd1e` (15 October). Only CI, naming, licensing and docs changes (#16–#20) merged after it.
- **Unfreeze trigger:** an owner decision recorded on [#10](https://github.com/Sean-Koval/spaceport/issues/10), then noted here with its date and scope.
- **Paused work:** draft [PR #8](https://github.com/Sean-Koval/spaceport/pull/8) (owner approvals for shared-source products) is unmerged and paused; it is not a task to finish while frozen.

Updated 16 October 2026. The table below is history, not active work, completed acceptance or permission to use live accounts, incur spend or deploy. The [approved design](docs/design-records/2026-10-13-spaceport.md) remains the architectural boundary; the [README](README.md) describes the source at the freeze.

## Product outcome

A team publishes a reusable, tested data product once. A person or an agent can find it, understand its meaning and limits, and receive the same authorized answer through REST or MCP. BigQuery and Domo should eventually supply real team data without exposing database credentials, unrestricted SQL or unreviewed business logic to agents.

## Delivered before the freeze

Counts are what the recorded run admitted on its exact source; they do not certify later commits. Every live-source, browser and production gate was open at the freeze. There were never delivery-date commitments.

| Milestone | What shipped | Where | Evidence |
| --- | --- | --- | --- |
| 0. Tested private-main baseline | Private `main` at `e03a5a1` (tested tree `b589ae8`): 1,847 unit/contract tests, 294 backend tests, verified cleanup | [run 38615111132](https://github.com/Sean-Koval/spaceport/actions/runs/38615111132) | [Baseline record](docs/evidence/private-main-baseline.md) |
| 1. Two everyday workflows | Consumer execution workspace (approved filters, sort, typed values, idempotent retry) and the authorized draft inbox: 1,950 unit/contract tests, 329 backend cases, 254 frontend tests | [PR 2](https://github.com/Sean-Koval/spaceport/pull/2), [run 38633087264](https://github.com/Sean-Koval/spaceport/actions/runs/38633087264) | [Acceptance index](docs/evidence/acceptance.md) (rendered-browser and composed-demo acceptance open) |
| 2. Complete synthetic product journey | Nothing: the fresh author → test → publisher → consumer run against the composed stack was not performed | — | [Acceptance index](docs/evidence/acceptance.md): overall acceptance BLOCKED; [limitations](docs/evidence/limitations.md) |
| 2A. GKE organization-runtime foundation | `oidc-synthetic-pilot` runtime, mounted-file configuration, owner-only provisioning, GKE reference workloads: 2,221 unit/contract tests, 346 backend cases; no deployment approved | [PR 3](https://github.com/Sean-Koval/spaceport/pull/3), [run 38650608566](https://github.com/Sean-Koval/spaceport/actions/runs/38650608566) | [Foundation evidence](docs/evidence/organization-runtime-foundation.md), [readiness assessment](docs/operations/organization-pilot.md) |
| 2B. Reviewed connector software | Reviewed PostgreSQL/Cube v2 path, BigQuery driver/admission library, offline Domo and GCS readers | [PR 4](https://github.com/Sean-Koval/spaceport/pull/4) at `3fa6b2f` | [Connector evidence](docs/evidence/reviewed-connectors.md) |
| Connector pilot wave | Operator CLI, retained sample packages, GKE application inventory, durable source holds; BigQuery and Domo product journeys with synthetic provider boundaries | [PR 5](https://github.com/Sean-Koval/spaceport/pull/5) at `27bb504`, [run 22](https://github.com/Sean-Koval/spaceport/actions/runs/38800064164) | [Source pilot runtime](docs/evidence/source-pilot-runtime.md#hosted-attempt-22-exact-source-backend-pass), [pilot runbook](docs/operations/source-pilot.md) |
| 3. One real BigQuery-backed product | Reviewed gateway/Cube serving and durable source control only; live activation gated | — | [BigQuery gates](docs/operations/bigquery.md) |
| 4. First Domo product | Finite approved-statement REST serving and isolated fixture workflows only; live OAuth/PDP and authoritative reconciliation unverified | — | [Domo Phase 1b checklist](docs/operations/domo-phase-1b.md) |
| 5. Private production deployment | Not approved; a synthetic or live-source smoke does not complete it | — | [Production checklist](docs/operations/production-gates.md), [release gates](docs/operations/release-gates.md) |

Before the pilot could be called organization-usable, a separately reviewed security slice was required: protected data-owner approval and revocation for explicit non-PII products, and rejection of unsupported delegated-source modes. That slice is paused in [PR #8](https://github.com/Sean-Koval/spaceport/pull/8). User/agent relationships and source-user entitlement intersection are requirements, not current capabilities.

## 6. Govern MCP tools through Tower (after unfreeze)

**Status: planned; starts only after unfreeze.** Spaceport's four MCP tools ask Tower's policy decision point before serving, and accept act-chain tokens, so one rule set and one receipt cover data products too. Tracked in [#14](https://github.com/Sean-Koval/spaceport/issues/14). The deprecated `DPG_*` client environment names ([#12](https://github.com/Sean-Koval/spaceport/issues/12)) can then be removed in favour of `SPACEPORT_*`.

## Later choices, not commitments

- Product/version history, structural schema-change labels and clearer typed result presentation, evaluated against actual user needs.
- Each new metadata or import/export surface needs an explicit contract and access review.
- A live GCS route and source-policy inheritance; GCS stays the offline exact-generation artifact interface until then.
- Browser SSO/PKCE and production/composed acceptance, under separate operator approval.
- Out of V1: an ontology engine, graph database, ingestion/CDC, arbitrary SQL, cross-product joins, dashboards and agent orchestration. Helix Foundry is a research reference for review-state and provenance UX, not a runtime to adopt.
