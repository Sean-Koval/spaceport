# Source pilot runtime and product onboarding

Status: all three component designs independently approved for implementation on 15 October 2026. Detailed contracts: [setup/workflow](2026-10-15-source-pilot-setup.md), [BigQuery and shared source lifecycle](2026-10-15-bigquery-pilot-runtime.md), [Domo runtime](2026-10-15-domo-pilot-runtime.md). No live activity or deployment is approved.

## Goal and authority

Provide a runnable, configured Spaceport pilot for approved BigQuery tables/views and Domo datasets, with an explicit operator setup and a supported team author → test → publish → consume workflow. Code, templates, documentation, offline tests and private feature publication are in scope. Creating cloud resources, IAM/OAuth grants, credentials, paid queries or deployment is not authorized by this work. The next feature PR requires separate merge approval.

Baseline: private main `3fa6b2f698fa406918d82173d4422166c059b84b`, tree `a3155cbfb8fc5c073a4c2d184ecd75d0b99d0024`, after approved PR 4 merge. Source suite: 2,586 passed; prior exact-code hosted backend: 359 cases and cleanup passed. These results do not establish the new wave's acceptance.

## Proposed bounded delivery

1. Registry-backed durable source sessions, job/send intents and terminal settlement shared by the new source paths. Legacy Cube/PostgreSQL per-generation reservations remain unchanged. The existing offline POSIX BigQuery library remains separately qualified; it is not silently repurposed as a distributed serving authority.
2. Genuine BigQuery-through-Cube serving with explicitly reviewed driver/model assets, enforced job and aggregate byte budgets, trusted request identity, bounded output, durable reconciliation and explicit GKE workload-identity mode.
3. Genuine Domo serving through the documented exact-query REST API and finite reviewed statements. Official Domo MCP is not a substitute for exact approved SQL; discovery/author assistance is a later optional integration. A shared source identity does not inherit each caller's Domo PDP.
4. Operator-reviewed product packages and an offline-first setup CLI, with separately invoked network commands for draft save, fixture tests, publication and approved source verification. Reuse the existing public request/result and four-tool MCP contracts.
5. Complete renderable GKE workload configuration for an existing cluster, registry and identity provider, with explicit prerequisites and a separately reviewable activation overlay. Rendering never applies resources or discovers credentials.

## Safety invariants to close before implementation

- One source hold per logical execution/fixture, independent of worker lease generations; expired/revoked callers cannot release unknown source work.
- Source capacity keys survive profile revisions and credential rotation. Crash/reclaim cannot evade holds or duplicate unknown Domo sends.
- Admission, sealing, budget reservation and completion transitions have explicit atomic transactions and least-privilege roles. Post-deadline metadata-only settlement cannot admit queries or release result data.
- Every consumer and fixture request is bound to retained product/binding/model/source identity and exact accepted query plus policy scope. Fixture endpoints and data are independently pinned.
- Configuration never self-certifies verification. Startup/readiness does not incur a source query. Explicit live verification is bounded and reports evidence identities separately from fixture acceptance.
- Source credentials never enter product manifests, browser state, public API responses, ordinary logs or CLI arguments. OIDC caller identity, Spaceport policy and source workload identity are distinct.
- Unknown Domo query outcomes remain quarantined unless a supported independent terminal proof exists; no arbitrary timeout or operator assertion stands in for evidence.
- Provider-returned metadata and query outputs are hostile input. BigQuery anonymous-result metadata needs explicit validation; permanent destination/write features remain forbidden.

## Team example

The initial package is an account-usage report over a selected seven-day window, returning daily account rows. Usage counts use exact scale-zero decimal values. It must not label daily rows as a weekly aggregation or claim distinct weekly-user semantics. A data owner reviews source definitions and expected fixtures; operators register the retained package and identity grants; an author saves and tests a draft; a separate publisher publishes the exact approved revision. Consumers use existing REST/MCP/UI contracts. Source-specific finite Domo variants and BigQuery date-range limits must be explicit.

## Reviewed implementation inputs

- Source ledger schema, transition/privilege matrix, crash and concurrent admission tests.
- BigQuery runtime topology, credential variant, complete Cube request/job lifecycle and actual-package tests.
- Domo gateway binding/normalization/publishing seams, documented OAuth/transport contract and unknown-outcome handling.
- Product-package schema, CLI commands, bootstrap/registration behavior and complete GKE artifact inventory.
- Independent review findings and final acceptance matrix.

Independent review closed fixture suite ownership, per-case settlement, mapped source-role privileges, bounded lock/scan/growth behavior, legacy-role migration compatibility and coherent current-authority fencing before every source admission/data RPC. The implementation plan assigns one owner per shared file. Real PostgreSQL and full actual-image runtime acceptance remain mandatory; a completed library alone does not satisfy this wave.
