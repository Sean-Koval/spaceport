# Source pilot implementation plan

Baseline: main `3fa6b2f`, exact local tree `a3155cb`. Private feature development only; next merge needs explicit approval. No live credentials, grants, cloud changes, paid source queries or deployment.

## Reviewed setup/workflow component

Independent design review approved the [setup component](../specs/2026-10-15-source-pilot-setup.md) on 15 October 2026. Common source-ledger and source dispatch details were subsequently independently approved in the linked source specifications. Do not fake completed source integration.

1. Create isolated setup worktree; preserve legacy demo/pilot entrypoints and immutable v1 assets.
2. Add strict product/config/catalog models with no-follow bounded reads, digest checks, trusted origins, no credentials or sockets during validation/rendering, and explicit new identity profile with old-profile rejection regressions.
3. Implement supported existing-API draft save/test/review/publish/query/source-check CLI, private token and operation files, exact immutable receipts, no mutation retry after ambiguity, stable execution idempotency and honest source-versus-fixture output.
4. Render complete selected GKE application inventory with reviewed zero-replica base, separate activation overlay and separate owner jobs. No apply, IAM or credential creation. Assert source service/config/module coherence and restrictive actual network prerequisites.
5. Add concrete account-usage packages and generalized source-pilot runtime/bootstrap using independently approved binding/session interfaces. Separate fixture identity from author budget ownership. Replayed provision plans must not regrant revoked access.
6. Test first; independent component review; focused and full regression tests. Report installed/configured, offline execution, genuine backend and real-provider evidence separately.

## Shared source implementation (design approved)

- BQ owner: registry-backed sessions/jobs/migrations and queue claims, BQ serving driver/model/auth/runtime and metadata-only reconciler.
- Domo owner: source-specific prepared binding, exact REST/OAuth transport, response/normalization, source-semantic dispatch and coordinated worker/fixture seams.
- Setup owner: new config/catalog/bootstrap/runtime/CLI/GKE/sample package modules and `gateway/auth/settings.py`; no source security duplicates.
- Shared files receive one editor before coding; additive migration names and adapter/session interfaces will be recorded after review.

## Integration gate

Integrate reviewed commits in the coordinator worktree. Verify original public API/MCP schemas and v1 asset identities, run all source/Node/frontend checks, then ask parent to dispatch the exact-source genuine backend/actual-image gate. Parent owns CI and budget; project ledger begins at 250 completed minutes. Recheck billing before projected 300. No weakened limits or hidden skips to make a gate pass.

## Reviewed Domo transport/semantic component

Independent component review approved [the Domo design](../specs/2026-10-15-domo-pilot-runtime.md). Implement exact OAuth/query transport, immutable v2 binding/plan and strict normalization with failing-first tests. Shared lifecycle/dispatch integration waits for its reviewed interfaces; no helper-only completion claim. No live source requests.

Ownership: BQ owns `source_lifecycle.py`, registry models, migration 0005 and `execution/queue.py`; Domo owns source-specific modules, generic binding/preparation/normalization dispatch, migration 0006, `execution/{service,worker,results}.py`, `publishing/{service,test_runner}.py` and `api/{choices,readiness}.py`; setup owns new runtime/catalog/bootstrap/config/CLI/GKE modules and `auth/settings.py`. Domo and BQ coordinate registry model definitions and preparation APIs without overlapping edits. Source-specific sample wire assets belong to Domo/BQ, while installable package examples belong to setup.

## Shared lifecycle and BigQuery implementation steps

The independent reviewer approved the [shared/BQ design](../specs/2026-10-15-bigquery-pilot-runtime.md) after current-authority fencing and all lifecycle/privilege closures.

1. Ledger owner writes failing-first contract and real PostgreSQL tests for stable operation groups, exact sessions, BQ intents/Domo single sends, case/whole-suite budgets, seal/settlement, coherent authority stamps, roles and legacy migrations. Implement migration 0005, models/service and queue integration without changing legacy reservation semantics.
2. Node owner implements new versioned BQ serving runtime in `cube/bigquery-runtime/`: explicit WIF client, PostgreSQL ledger client, signed request context, generation-scoped Cube caches, bounded SDK jobs/results, versioned provider metadata, metadata-only reconciler and no-I/O startup. Prior POSIX library remains unchanged.
3. Domo owner integrates typed preparations (0006) and shared worker/fixture/result/publishing dispatch against the fixed lifecycle APIs; exact single-send HTTP path and terminal proof are independently reviewed.
4. Node/ledger owners build the actual-image full HTTP/compiler/real-registry/SDK/fake-provider harness and real concurrent/crash/role tests. No synthetic provider success is called live-provider evidence. Preserve manual CI, source/job caps and exact-source receipt discipline.
5. Setup composes validated catalogs and real adapter factories into runnable source-pilot entrypoints and complete GKE artifacts. Run the entire author/test/publish/execute/result journey for both sample sources with production services and only external provider boundaries faked.
6. Coordinator integrates and runs all regressions, independent whole-source review, unchanged public schema/v1 asset checks, then requests parent-owned exact-source hosted gate. No merge until separately approved.

Node implementation may be assigned to a separate specialist; it owns only `cube/bigquery-runtime/` and dedicated Node/runtime tests. Ledger owner retains shared schema/functions, queue and Python BQ binding/adapter interfaces. Existing harness script edits are coordinated with the coordinator, not concurrent.
