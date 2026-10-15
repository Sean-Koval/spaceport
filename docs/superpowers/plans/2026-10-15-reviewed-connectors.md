# Reviewed connectors implementation plan

> **For agentic workers:** Use subagent-driven-development with test-first implementation and independent final review. Keep source-level evidence distinct from actual backend acceptance.

**Goal:** Deliver versioned reviewed Cube bindings and safe offline Domo/GCS contracts without activating live sources.

**Architecture:** Preserve synthetic v1 and public contracts. Extend the internal Cube path with explicit retained v2 authority. Implement Domo/GCS as injected offline readers; BigQuery execution controls require a separately closed design review before implementation.

**Tech Stack:** Existing locked Python/Pydantic/httpx, Node, Cube 1.8.5, PostgreSQL registry. No new dependency by default.

**Spec:** ../specs/2026-10-15-reviewed-connectors.md

## Global constraints

- No live credentials, cloud resources, paid queries, source writes, deployment or merge.
- Keep four MCP tools and existing OpenAPI/UI contract unchanged.
- Preserve synthetic v1 schema and loaded bundle bytes/digests.
- Caps: 1,000 rows, 1 MiB complete result, 120 seconds or stricter current policy.
- No lifetime/cost claims from mocks; source reservations remain held on uncertainty.
- Branch-only private draft PR; parent controls manual CI dispatch.

## Review focus

- Pending/replayed queries must resolve immutable authority again and cannot derive it from selected members.
- Driver auxiliary jobs and ambiguous submissions cannot evade aggregate budget or release safety.
- Current-policy revocation between initial read and release must discard buffered data.
- Freshness is producer checkpoint evidence, never request/object update time.
- Signed capability URLs and unredacted upstream errors must not escape.

### Task 1: Complete reviewed Cube v2 path

**Files:** Add gateway/adapters/reviewed.py, tests/contract/test_reviewed_cube.py, tests/unit/test_reviewed_bindings.py and separate cube/reviewed-postgres assets. Update gateway/adapters/base.py, cube.py, bundles.py, normalize.py, gateway/execution/service.py, results.py, worker.py, and gateway/publishing/test_runner.py. Add dedicated genuine backend tests where supported.

**Interfaces:** ReviewedCubeBinding with adapter_revision cube-rest-v2; discriminated Binding; parse_binding(value); validate_binding_decision(binding, decision, request_id); security_contract(binding). Extend poll with optional trusted ExecutionSpec/executed-decision keywords. Worker and FixtureRunner pass this exact immutable authority only for the versioned path; v1 retains compatibility. Recompute and compare the accepted query before each source poll. Endpoints identify v2 explicitly and reject missing context. Keep stored PreparedQuery unchanged.

- [x] Write failing tests for two non-Orders models, wrong scope/member/type/grain, immutable identity and trusted poll context.
- [x] Implement contracts and thread exact binding-aware validation through prepare/poll/normalization/result replay/fixtures. V2 metadata checks kind and type. Prepared-query validation checks allowed selection/filter/sort/time/date/limits before I/O, and exact canonical equality to the trusted accepted query. Correlated producer checkpoint/data evidence uses a pinned hidden checkpoint dimension in the same SQL statement, with unique scoped checkpoint joins and atomic data/checkpoint publication as reviewed prerequisites. Strip only the exact hidden field; reject mixed/colliding fields; empty results have unknown freshness. No separate v2 watermark read or snapshot-republish mechanism.
- [x] Add separate reviewed security/model assets; no synthetic v1 edits. Test independent mandatory AND predicates, watermark tenant scope and scope partitioning in Node.
- [x] Add operator loading/example and fixture-tested end-to-end prepare→poll→normalize lifecycle; record actual-runtime evidence separately.
- [x] Run focused Python/Node tests, old v1 regression suite, compile and diff checks. Commit code and full test output summary.

### Task 2: Implement offline named-query and artifact readers

**Files:** Add gateway/adapters/domo.py, gateway/artifacts/gcs.py, tests/contract/test_domo.py, tests/contract/test_gcs_artifacts.py, synthetic fixtures/adapter/domo.json and gcs.json, docs/operations/gcs-artifacts.md. Update Domo operations status. Modify manifest.Link validator with focused contract regressions.

**Interfaces:** DomoNamedQueryBinding, DomoQueryVariant, ReviewedStatement, DomoReadPlan, DomoValidatedResult; compile_named_query and validate_domo_response; injected DomoReader. GcsArtifactDescriptor, StaticArtifactCatalog, mandatory authorization gate, injected GcsObjectTransport, GcsArtifactReader, URL-free ArtifactBytes. No default network/auth factory or runtime registration.

- [x] Write failing tests with socket denial for missing gates/transports, immutable identity, finite request selection and exact-object generation.
- [x] Implement finite Domo selection with no interpolation, strict ordered positional/type/count/decimal normalization, bounded bytes, source freshness, pre/post current authorization and zero retries.
- [x] Implement allowlisted GCS metadata/content reads with exact version preconditions, 64 KiB metadata and 1 MiB buffer caps, digest/size/media/encoding checks, overall deadline and pre/post current authorization. No URL/cache/temp/extract/list/write path.
- [x] Add case/encoding-variant signed-capability URL rejection. Test hostile strings, schema/object drift, overflow, timeout, mid-read revocation, redacted errors and no partial result.
- [x] Run focused tests, public snapshot regression and diff checks. Commit with honest offline-only docs.

### Task 3: BigQuery enforcing software slice

**Review gate: approved for the narrowed library slice.** One process/private local POSIX volume, externally pinned UUID, explicit initialization, exclusive owner-only lock with no stealing, serialized hash-chained journal and fsynced head/directory before acknowledgment or SDK submission. Corruption, unsafe storage, missing volume or durability failure poisons/fails closed. Full signed-control authentication/replay/deadline design remains future integration, outside this implementation.

- [x] Close per-execution aggregate budget and stable deterministic semantic job identity design, including auxiliary jobs and ambiguous submissions. Reuse first-admitted config/timeout for repeated identical jobs; generation fencing never resets budget.
- [x] Close atomic admission/seal, fsync and restart, process exclusivity, volume loss/corruption, late jobs and all-DONE completion evidence. Reserve full per-job bytes before SDK submission, never refund or replace unknown jobs, and bound journal/state growth without unchanged-poll appends.
- [x] Implement only reviewed driver/ledger library interfaces with real durable-filesystem/locking/crash tests, fake SDK/provider boundaries and retained model assets; no programmatic server, control endpoint, SourceAttemptContext hook or completion/release wiring. No live activation switch.
- [x] Independently review exact upstream compatibility and test every admitted/blocked driver entrypoint; keep source lifetime unproven.

### Task 4: Integrate and verify exact candidate

- [x] Integrate isolated commits; review all shared seams and maintain unchanged v1 assets.
- [x] Run full Python unit/contract tests, Node tests, frontend tests/typecheck/generated check/build/format, public contract consistency, link and diff checks.
- [x] Independent whole-branch review and focused hostile-input cross-flow regressions; fix findings and rerun affected plus aggregate checks.
- [x] Publish private draft PR from current main ancestry, verify remote tree equality, send exact source SHA and required manual backend run to parent.
- [x] Inspect actual final-source results; label unavailable live/cloud/browser evidence explicitly. No main merge.

## Completion evidence

The bounded implementation tasks above are complete. Exact-source hosted run 18 passed 2,586 unit/contract tests and 359 genuine backend cases, with cleanup. See [the evidence record](../../evidence/reviewed-connectors.md#hosted-gate-attempt-18-pass) for source identity, the guarded package-versus-provider boundary and remaining deployment/live-source gates. No main merge is included.
