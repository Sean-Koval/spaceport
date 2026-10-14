# Spaceport roadmap

Updated 14 October 2026. This is the delivery sequence, not a record of completed acceptance or permission to use live accounts, incur spend or deploy. The [approved design](docs/superpowers/specs/2026-10-13-data-product-gateway.md) remains the architectural boundary; the [README](README.md) describes the current source.

## Product outcome

A team publishes a reusable, tested data product once. A person or an agent can find it, understand its meaning and limits, and receive the same authorized answer through REST or MCP. BigQuery and Domo should eventually supply real team data without exposing database credentials, unrestricted SQL or unreviewed business logic to agents.

Work is ordered by usable outcomes and evidence. Synthetic fixtures, mock contracts, live-source checks and production readiness remain separate. There are no delivery-date commitments in this roadmap.

## 0. Establish a tested private-main baseline

**Status: completed for the merged baseline.** Private `main` commit `e03a5a10fa78da78d18a964bc631a79633344b60` has the exact tested tree `b589ae8a0560dea59b45a112fd2409984128a0d9`: 1,847 unit/contract tests, 294 genuine backend tests and cleanup passed in [run 38615111132](https://github.com/Sean-Koval/spaceport/actions/runs/38615111132). The [baseline record](docs/evidence/private-main-baseline.md) scopes this evidence. Composed-demo acceptance remains open.

Tasks:

1. Resolve the hosted backend failure using retained, source-bound diagnostics and a regression test for each defect. Keep cleanup narrowly scoped to the selected run's resources.
2. Review the final source, run unit/contract and frontend checks, and execute the complete disposable PostgreSQL/Cube backend selection against that exact commit.
3. Integrate the Spaceport presentation, README and this roadmap; merge the reviewed source into private `main` after the agreed checks. Verify the merged source and record exact run links, counts and unresolved gates.

Acceptance:

- Required source-level and complete-backend checks pass on the source being merged, with no hidden skipped/error cases or stale receipts.
- The private-main commit contains the application and documentation; the reported commit matches the remote.
- Remaining demo, packet-observer, live-source and production gaps are explicit. Merging the source does not declare those gates satisfied.

**Dependency satisfied:** new feature work starts from the tested baseline. That result does not certify later feature changes; they require independent review and fresh checks.

## 1. Complete two everyday workflows

**Status: implementation merged in [PR 2](https://github.com/Sean-Koval/spaceport/pull/2); source-level and complete backend checks passed. Rendered-browser and composed-demo acceptance remain open.** The [merge commit](https://github.com/Sean-Koval/spaceport/commit/7a45b7e22514375de375dc71be1f8c70f843c589) has exact tested tree `bbd64ee1f4272e303a180a6c98a35ec7e56a574a`. [Run 38633087264](https://github.com/Sean-Koval/spaceport/actions/runs/38633087264), on source `44062b2ea8fa5b0d65bd29f5090209de164ff3bf`, admitted 1,950 unit/contract tests and all 329 backend cases with verified cleanup. Independent code review and 254 frontend tests passed; the live-source and production gates below remain open.

These are small improvements using the existing product contract. Neither needs a live warehouse or a new execution engine.

### 1A. Finish the consumer execution workspace

**Original problem:** the product page accepted typed inputs but submitted empty filters and sort arrays. An ambiguous submission left the user unable to safely recover from that page, despite server-side idempotency support.

Build:

- Controls derived only from the authorized product's permitted filter fields/operators and sort definition.
- Typed values, including lossless decimal strings, nullable fields, bounded list filters and the special no-value `is_null` operation.
- A client-generated idempotency key for an explicit run, bound to an immutable request snapshot and the selected product version.
- An explicit retry of that same request after an ambiguous network failure. Changed inputs require a new run/key; account or product changes clear the pending client state.
- Result identity checks against the displayed product ID, pinned version/digest and requested execution ID.

Acceptance:

- Only approved filters/operators/sort fields can be submitted; list/sort bounds and duplicate-sort rejection match the gateway contract.
- Dates, nulls and exact decimals retain their meaning. UI validation never substitutes for server authorization or mandatory row constraints.
- Repeated clicks and retry after a lost response cannot silently create a new logical execution. Retry reuses the original request and key without automatic resubmission.
- Account changes, access denial, version navigation and stale responses cannot reveal or reuse another context's request/result state. A same-product result from another version/digest is not displayed as the selected version's answer.
- Component tests, generated-type check, typecheck and production build pass; actual browser behavior is checked when the verified stack is available.

**Ownership:** consumer UI and dedicated tests, principally `publisher/src/pages/Product.tsx` and new query-control helpers. No public API or shared `api.ts` change is expected; coordinate stylesheet changes with 1B.

### 1B. Add an authorized draft inbox

**Original problem:** saved drafts had direct edit/review URLs but no discoverable index. Returning authors and reviewers needed a safe way to find unfinished work.

Build:

- First specify a minimal read-only draft-summary/list contract: stable ordering, bounded pagination, allowlisted metadata and the same policy review rules as existing draft reads.
- Reuse current author-or-publisher review authorization and current edit/publish capabilities. Draft ownership or consumer discovery permission alone must not grant access.
- List latest saved draft heads with a minimal authorized name, product/version, draft ID/revision and capabilities. Use the actual publication relationship to exclude or label published heads; test status alone must not imply publish readiness.
- Filter before pagination or counts, reuse opaque principal/policy-bound cursors with a distinct purpose, and keep the existing 50-item page / 1,000-candidate work bounds. Recheck access before releasing the response.
- Add an inbox with resume/review links, useful empty/loading/error states and navigation back from editor/review flows. Regenerate OpenAPI/client types as one coordinated contract change; keep the four MCP tools unchanged.

Acceptance:

- An authorized author or publisher can rediscover permitted work after navigation/reload; consumers and unrelated tenants receive no draft metadata or counts.
- Grant removal, policy changes, cursor replay and account switching fail closed without retaining protected UI data.
- Summaries do not include full manifests, fixture rows, source credentials or internal authority records. Review links do not grant publish permission or certify tests as current.
- Existing edit/test/publish digest checks and separate author/publisher roles remain intact, including the base and target scopes required for publication.
- New service/transport/security tests, generated-schema consistency, frontend tests and build pass. Database-backed cases run on the real backend before completion is claimed.

**Dependency:** agree the bounded summary contract and permissions before building the UI. **Ownership:** one owner coordinates the draft discovery service/API wiring, required publishing projections, OpenAPI/generated types, `publisher/src/api.ts`, new `pages/Drafts.tsx`, `App.tsx` navigation and dedicated tests. Avoid editing the consumer workspace concurrently.

### Integration and review for this wave

An independent reviewer checks both feature boundaries and adds focused cross-flow regression coverage. A single integrator owns shared navigation/styles, final combined tests and merge ordering; the draft owner owns the API snapshots. Keep pure/mock checks distinct from genuine database and browser evidence. Do not expand this wave into dashboards, charts or broad connector administration.

## 2. Prove the complete synthetic product journey

**Status: required acceptance work remains open.** This can proceed independently of the feature wave where ownership and source receipts permit.

Tasks:

1. Run a fresh author → test → publisher → consumer flow against the composed stack, with no pre-published records or manufactured test evidence.
2. Reconcile one unchanged product through the independent Python and TypeScript MCP clients for both access profiles; check REST parity, revoked access, pagination and retained version identity.
3. Exercise restart, fresh-stack restore, expiry, upstream failures, ambiguous/pending execution and exact-resource cleanup. Inspect the actual UI at normal/narrow sizes and with keyboard navigation.
4. Separately resolve the reviewed packet-observer prerequisite, then run the full acceptance wrapper and a second independent clean cycle with no reacquisition. Any privileged observation needs its own approval.

Acceptance:

- The complete backend gate and both 14-slot demo cycles pass with exact source/image identities, collected test identities, zero undisclosed skips and verified cleanup.
- Startup is initially unpublished; real fixture testing and separate publisher approval produce the release. Registry state, product digests and the original source watermark survive the required recovery tests.
- No-egress claims cover only the interval/topology actually observed with positive controls. Blocked observer evidence stays blocked rather than being replaced by network configuration or mock data.
- The [acceptance index](docs/evidence/acceptance.md) is updated with dated run evidence and residual limits.

**Dependencies:** supported Docker/browser host, prepared locked artifacts, and separately approved observer capability for stronger offline assurance. The unprivileged business workflow can be investigated before that observer is available.

## 3. Deliver one real BigQuery-backed product

**Status: gated; current BigQuery coverage is mocked and non-executable.** Start with one selected metric and a known authoritative answer, not a generic connector catalog.

Tasks:

1. Agree the product owner, business definition, approved project/dataset/region, permitted fields, tenant rules, known-answer cases and freshness source.
2. Review the actual Cube BigQuery driver path and retained models/bindings. Establish enforceable source time/cost/concurrency limits before enabling live execution; local deadlines and row limits are insufficient.
3. Obtain scoped account/query authorization, credential handling and a spending ceiling before the live smoke. Keep secrets outside the repository and product manifests.
4. Run live metadata/result/type/date/freshness checks, tenant/cache isolation, denial/revocation and source-bound tests. Reconcile the product through REST and both MCP clients.

Acceptance:

- The first metric matches its authoritative answer at the documented grain, timezone and precision.
- Live evidence identifies the exact driver/model/binding revisions and verified access/cost bounds; mock results are reported separately.
- Unsupported types, unknown freshness, binding drift and missing source bounds fail closed. No source-writing pre-aggregations or implicit permission expansion is introduced.

**Dependencies:** milestone 2's working reference path, approved live scope and the [BigQuery activation gates](docs/operations/bigquery.md). Production remains a separate milestone.

## 4. Reconcile and serve the first Domo product

**Status: deferred Phase 1b; no adapter exists.** Use a separate read-only named-query adapter only after its source semantics and permission model are proven.

Tasks:

1. Select one authoritative card/Beast Mode/dataset query and the supported API/credential family for the approved tenant.
2. Verify actual PDP behavior, service-account reach, tenant identity, metadata visibility and revocation with independent access profiles.
3. Reconcile calculation order, filters, grain, joins, nulls, decimals, dates and timezone with the original definition. Do not assume its semantics already exist in BigQuery.
4. Implement reviewed typed query bindings with freshness, rate/cost/lifetime limits and sanitized failures, then test the same gateway/MCP product contract.

Acceptance: the product matches its authoritative definition and enforces equivalent permitted data access before aggregation. If either cannot be demonstrated, it remains unavailable. See the full [Domo checklist](docs/operations/domo-phase-1b.md).

**Dependencies:** explicit Domo tenant/API access and live-query authorization, a bounded adapter design, and the shared execution/result contract. No native Cube Domo driver is assumed.

## 5. Qualify a private production deployment

**Status: not approved.** A successful synthetic or live-source smoke does not complete this milestone.

Tasks and acceptance are tracked by the [production checklist](docs/operations/production-gates.md): verified user-bound OIDC/Studio delegation where required; policy review; TLS and ingress/egress controls; external secret management; encrypted storage/backups; approved retention; restore drills; source-cost and cancellation evidence; operational ownership and load/recovery tests. Retain an independently reviewed release commit and rollback procedure before deployment.

Public release is separate: settle original-code licensing, dependency/image/browser notices, public naming and distribution approval. This roadmap does not authorize publishing packages or opening the repository.

## Later choices, not commitments

After the core journeys work, evaluate product/version history, structural schema-change labels and clearer typed result presentation against actual user needs. Each new metadata or import/export surface needs an explicit contract and access review.

An ontology engine, graph database, ingestion/CDC, arbitrary SQL, cross-product joins, dashboards and agent orchestration remain outside V1. Helix Foundry is a research reference for review-state and provenance UX, not a decision to adopt its runtime or expand Spaceport's scope.
