# BigQuery and Domo pilot, and release boundaries

**As of the freeze (2026-10-16).** These sections used to sit on the README front page. None of this is active work: new connectors, live-source activation and deployment are not allowed while Spaceport is frozen (see the [README status block](../../README.md#spaceport)).

## State at freeze

The README keeps a four-row summary; this is the full table it points to.

| Area | Available now | Still gated |
| --- | --- | --- |
| Product workflow | Catalog, draft editor, isolated fixtures, separate publisher, immutable versions, REST + four MCP tools | Full composed-browser acceptance |
| PostgreSQL / Cube | Synthetic reference plus reviewed v2 bindings and same-query checkpoints | Operator-qualified organization models and producer guarantees |
| BigQuery | Reviewed Cube/SDK runtime, bounded jobs, durable source holds, full synthetic-provider journey | Live identity, permissions, cost, freshness and network qualification |
| Domo | Finite reviewed SQL over direct REST, isolated fixture/consumer identities, single-send accounting | Live OAuth/PDP, dataset/business-definition qualification; shared credentials do not inherit employee access |
| GCS | Offline exact-generation artifact reader | Public route, live transport and source-policy inheritance |
| Access security | Current user/client authentication and product policy checks | Revocable owner approval for non-PII products; full ReBAC, agent delegation and per-user source entitlement intersection |
| Deployment | Operator CLI, images, GKE review templates | Authorized infrastructure/credentials, browser SSO/PKCE, production rollout |

Organization activation stays blocked. Owner approvals exist only in paused draft [PR #8](https://github.com/Sean-Koval/spaceport/pull/8), and a passing fixture journey does not establish live credentials, user-level Domo permissions, PII safety or production readiness.

## BigQuery and Domo pilot

**Organization activation is blocked pending the separately reviewed data-access security gate.** This merged connector pilot does not implement per-user Domo entitlement intersection, a user–agent delegation relationship model, or a complete PII field policy. Current product labels and shared source credentials are not proof of those controls. Revocable data-owner approval for explicitly reviewed non-PII shared-identity products was drafted in [PR #8](https://github.com/Sean-Koval/spaceport/pull/8), which is paused unmerged at the freeze; ultimate per-user access remains a separate provider-qualified mode.

Start with the [operator setup and team workflow](source-pilot.md). The supported CLI builds and validates a fixed weekly-account-usage package offline, renders a zero-replica GKE review bundle, and guides author → isolated fixture test → separate publisher → bounded consumer check. Existing cluster, registry, IdP, source views/datasets, reviewed network access and operator-managed credentials are prerequisites. Rendering does not create them or deploy anything.

BigQuery uses GKE Workload Identity; Domo uses separately restricted OAuth integration identities. Domo MCP is not the approved-statement serving transport. Shared Domo credentials do not inherit each employee’s PDP. No organization query, cloud deployment or browser SSO is established by the fixture evidence.

## Live sources and release boundaries

Start with the [BigQuery gate](bigquery.md), [Domo Phase 1b gate](domo-phase-1b.md), [identity guide](identity.md) and [production checklist](production-gates.md). A configured connection, a mocked test or a synthetic demo does not establish live-source readiness. The [source pilot design](../design-records/2026-10-15-source-pilot-runtime.md) describes the merged BigQuery/Domo serving paths; GCS remains the earlier offline artifact slice.

V1 excludes ETL, arbitrary SQL/formulas, cross-product joins, source writes, dashboards, ontology engines and agent orchestration. Real credentials, paid queries, deployment and public distribution require their own decisions and verification. Preserve [third-party notices](../../THIRD_PARTY_NOTICES.md) and the [dependency inventory](licensing.md). Original code is licensed under [Apache-2.0](../../LICENSE); that license does not cover third-party components and does not authorize publishing images or browser bundles, which remain release gates.
