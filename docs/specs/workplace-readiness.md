# Workplace readiness: scoped restart, 18 October 2026

## Decision and boundary

The owner requested that the team resume Spaceport development on 18 October 2026, with security, controls, audit, authentication, access, performance, usability and Agent Factory branding as adoption priorities. This supersedes the development pause for the bounded work below. It does not approve a merge, deployment, credentials, access grants, provider spending or live organization data.

This is a delivery plan, not acceptance evidence. The baseline inspected is main `157a47aa954ad8a15fad59d5dec218e80ccf8eae`. Historical evidence remains tied to its original source. Changes require independent review and fresh exact-source tests.

## Current state

- The synthetic catalog, typed queries, author → isolated fixture test → separate publisher → immutable version workflow, REST API and four MCP tools are implemented.
- Reviewed PostgreSQL, BigQuery/Cube and finite Domo REST serving software exists. The last successful connector backend run is [run 22](https://github.com/Sean-Koval/spaceport/actions/runs/38800064164), tested source `2d3cd1e4319f36a70bb95379f6713584abc5c4f2`: 3,336 source tests and 406 backend cases. Provider boundaries were synthetic; this does not qualify organization access.
- [PR 8](https://github.com/Sean-Koval/spaceport/pull/8) adds revocable owner approval for explicitly reviewed non-personal shared-source products. Its latest published candidate failed hosted acceptance and remains draft. Its older local corrections are unavailable in the resumed workspace and must be reconstructed and retested.
- [PR 16](https://github.com/Sean-Koval/spaceport/pull/16) is merged. Its trusted verifier supports the finite version-2 fixture protocol; the security candidate must match it without weakening authority, limits or failure reporting.
- The UI uses manually pasted, in-memory bearer tokens. Browser SSO/PKCE, a verified user–agent delegation chain, general relationship authorization, and per-user source entitlement intersection are not complete.
- Product labels and non-personal-data approval are not a PII classifier or field-level protection system. GCS remains an offline artifact slice.
- Full composed-browser acceptance, production load/restore qualification and live IdP/provider/GKE qualification remain open.

## Wave 1: restore a trustworthy candidate

1. Reconcile PR 8 with current main, preserving reviewed source-approval controls and newer identifier/docs changes. Keep failed hosted receipts intact.
2. Reconstruct the synthetic source fixture with just-in-time, single-use case activation. Each case starts its existing lease only when exercised. Immutable approval, package, source, publication and actor pins must survive activation; replacement approvals, replay, concurrent activation and lost acknowledgments fail closed. Never renew an expired owner merely to pass a test.
3. Preserve the finite protocol, strict acknowledgment schema, existing source budgets, unknown-provider capacity holds and current trusted controller. Add delayed activation, expiry, replay, replacement and crash/unknown-obligation tests. Local database tests and actual image/hosted evidence remain separate.
4. Require exact-source source and backend inventories, zero unexplained missing/skipped cases, cleanup and aggregate acceptance before recommending a merge. Hosted dispatch remains separately reviewed and budgeted; no timeout or runner-class increases.
5. Align UI with released Agent Factory/Airframe assets and provenance. Reuse open [PR 22](https://github.com/Sean-Koval/spaceport/pull/22) and coordinate [PR 23](https://github.com/Sean-Koval/spaceport/pull/23) rather than duplicating their work. Before adopting PR 23, reconcile its proposed agent guard (which prohibits finishing PR 8) with this dated owner-requested restart; retain its unrelated restrictions and historical evidence protection. Record the scoped decision in its requested issue/roadmap locations through an authorized publication, rather than silently importing contradictory instructions. Bundle permitted fonts locally; the UI must not add third-party runtime fetches. Preserve accessible contrast, keyboard flows, responsive layouts and truthful status language.

## Wave 2: authenticated people and delegated agents

Before implementation, review one identity contract with the workspace/Tower integration in [issue 14](https://github.com/Sean-Koval/spaceport/issues/14). Do not invent a second policy graph or accept unsigned client assertions.

- Browser: authorization-code flow with PKCE, state/nonce and exact redirect/resource binding; issuer/audience/client checks; bounded in-memory session; refresh/logout/account-switch behavior; no token in URLs, logs or persistent browser storage.
- Agent: authenticated user and agent identities plus signed, audience-bound, short-lived delegation restricted to tenant, product, action and purpose. Define chain validation, replay limits and revocation. A product permission cannot manufacture source permission.
- Authorization is an intersection: active user/agent delegation, current relationship entitlement, product policy, current provider-user entitlement, and permitted field use. Recheck before submission, completion, result pages and idempotent replay. A cached workspace decision needs an explicit expiry/revocation contract.
- BigQuery service-account identity is a shared integration mode. App tenant predicates do not prove that each employee has equivalent BigQuery IAM rights.
- Domo user-scoped MCP OAuth is a candidate delegated transport, but exact reviewed query semantics, tool/schema stability, field restrictions and revocation need provider qualification. Do not forward those tokens to REST without a documented audience contract, substitute natural-language SQL, or fall back to a shared credential after denial.
- Deny wrong issuer/audience/client, expired or revoked actor chains, cross-tenant/product use, forged on-behalf-of claims and unsupported delegated modes. Keep delegated result caching disabled until identity/policy/projection binding and revocation are independently proven.

## Wave 3: data protection, audit and operability

### Data protection

Require data-owner classification of every output and filterable field, a permitted-purpose projection, and reviewed masking/suppression where needed. Apply it before provider data reaches agent responses, result persistence, caches, fixtures, errors or audit. Test direct and indirect disclosure through filters, sorting, counts, metadata, empty results and aggregation. Default-deny unclassified fields and changed schemas. An approved non-personal aggregate is the initial scope; sensitive personal data requires a separately reviewed policy.

### Audit and retention

Record sanitized identity/actor, tenant, product version, action, policy/approval revision, decision, execution/correlation identifier and bounded provider cost/status. Never record tokens, source rows or unbounded exceptions. Define which events are durably committed with authority changes and which failure modes block release.

Application append-only behavior does not defend against a database owner. Define administrator threat model, separately controlled append-only export/retention, access review, integrity checks and audit failure handling before claiming tamper resistance.

Inventory retention for results, query inputs, authorization snapshots, prepared plans, executions, idempotency keys, provider holds, audit exports, backups and fixture artifacts. Current result/audit cleanup does not cover every stored category. Define deletion/recovery rules and legal/operational retention with owners; unresolved provider obligations must not be discarded to satisfy a local retention timer.

### Performance and recovery

Current limits are ceilings, not measured throughput. The source worker executes serially and the registry uses NullPool. Establish safe queue/hold/cost/latency telemetry before changing concurrency or connection pooling.

Use synthetic load at 1, 5, 10 and 30 clients to measure latency distribution, queue saturation, connection/memory bounds and cross-process capacity enforcement. Add soak, restart, ambiguous send, cancellation and retained-hold cases. Agree a supported workload and SLO from measurements; do not promise a throughput number first or hide failures by increasing caps.

Qualify encrypted off-host backups and a fenced restore into a fresh environment. Reconcile provider obligations before admitting new source work. Demonstrate revocation, source ledger and policy continuity; document recovery objectives and named operators.

## First workplace pilot

Recommend one BigQuery-backed, non-personal aggregate with a named owner and audience after Wave 1 and applicable identity gates. The team supplies a metric definition, grain/timezone, reviewed views, source/fixture identities, allowed columns/parameters, known answers, freshness and cost limits. The operator packages and validates it; an author tests it; a separate publisher approves the exact tested revision; consumers receive only the approved product.

GCP/GKE remains the target. Existing infrastructure, TLS/DNS, IdP, separate registry roles, immutable images, secret delivery/Workload Identity, bounded egress and approved source/fixture datasets are prerequisites. Configuration rendering alone proves none of those identities or permissions. Live qualification and spending need separate approval. Domo follows only when its authoritative metric and permission model are demonstrated.

## Exit gates

| Gate | Required evidence |
| --- | --- |
| Candidate correctness | Fresh exact-source source/backend union, cleanup and aggregate pass; independent review; no bypassed authority or cap |
| Everyday journey | Real browser author/test/publish/consumer flow plus independent REST/Python/TypeScript MCP parity; interrupted/repeated/revoked flows |
| Identity/access | IdP and actor-chain negative tests; current source permission intersection; release/page/replay revocation tests |
| Data protection | Classified allowlisted fields and pre-egress enforcement; schema-drift, cross-user/tenant and indirect-disclosure negatives |
| Audit/recovery | Complete sanitized events, documented administrator threat model, retention inventory and encrypted fenced restore |
| Performance | Reproducible synthetic load/soak receipt, safe saturation and stable resource bounds against an agreed SLO |
| Operational qualification | Separately approved target identity, permissions, source semantics/freshness/cost and rollback evidence |

No single gate substitutes for another. A polished UI, passing mock, configured principal name or merged commit is not workplace readiness.
