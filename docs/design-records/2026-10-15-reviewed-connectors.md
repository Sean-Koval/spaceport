# Reviewed connector wave

15 October 2026. Design candidate for technical review. No live source activation.

## Outcome and boundaries

Build a complete fixture-tested, versioned reviewed Cube binding path for operational PostgreSQL. Add an executable offline-tested BigQuery source-control candidate with retained model assets, plus bounded offline Domo named-query and GCS artifact read interfaces. Keep original synthetic v1 assets immutable. REST, MCP, publisher schemas and ordinary product/query inputs stay unchanged. No arbitrary SQL, model upload, bucket exploration, ETL, source writes, credentials, IAM grants, paid queries or deployment. Browser SSO/PKCE and production qualification are separate gates.

## Reviewed Cube v2

Keep `CubeBinding` / `cube-rest-v1` validation and bundle bytes unchanged. Add a discriminated operator-owned `ReviewedCubeBinding` / `cube-rest-v2` supporting one reviewed data cube, one daily time dimension, string dimensions, exact decimal measures, explicit independent tenant/access-group constraint members and a committed per-tenant source-load watermark. Retain model/security assets under a new directory with their own immutable digest. Schema configuration is reviewed deployment data, never a public query input.

Every prepare/poll/normalization/result retrieval resolves the exact retained binding and validates current trusted policy. The existing policy engine already derives member constraints server-side. Resolve mandatory scope roles from the binding, never from caller or prepared-query content. Preserve all grain, row, byte, date, field visibility and normalization limits. Unknown or stale required source freshness fails closed; event time/query time cannot become source-load freshness.

Use the worker claim's complete immutable ExecutionSpec and executed decision as explicit trusted v2 poll context. Recompute the expected pure Cube query from its normalized accepted request, pinned product and execution authority, and compare canonical bytes with the stored prepared query before I/O. Poll without this context fails closed. This avoids changing persisted `PreparedQuery`'s three-key JSON shape and its database trigger. Revalidate every prepared member, kind, filter, sort, limit and timezone against the resolved binding and current decision before I/O. V1 calls remain compatible. FixtureRunner and result replay use the same versioned binding semantics.

New reviewed Cube security independently enforces the same explicit tenant/access-group rules, with scope-partitioned caches/queues. Keep schema/scope descriptors free of their own bundle digest to avoid hashing cycles. Retain distinct consumer and isolated fixture bundles. The locked v2 freshness design selects one exact hidden committed-checkpoint dimension in the SAME reviewed data query/SQL statement. The reviewed model correlates a unique per-tenant checkpoint with data; the producer commits data and checkpoint atomically. The join must not multiply rows, and scope correlation must cover the effective tenant/group data. Uniqueness is enforced by the reviewed source schema where possible; actual producer behavior remains an explicit operator prerequisite, not a timestamp-label guarantee. The adapter verifies a single consistent timestamp and strips only the pinned hidden field, preserving that watermark with cached rows. Hidden-field collision, mixed timestamps and drift fail closed. V2 makes no second watermark query. Empty result freshness remains unknown and fails when freshness is required. Cache age is assessed from the retained source timestamp. This replaces the earlier immutable-snapshot proposal; no snapshot-republish mechanism is included in this wave.

## BigQuery source-control candidate (runtime disabled)

Keep BigQuery behind the selected Cube 1.8.5 driver. Normal upstream queries omit required per-job controls; the new reviewed path must enforce `maximumBytesBilled` and `jobTimeoutMs` at every actual job submission, including watermark and refresh-key work. A stable signed gateway execution/request identity binds all subordinate jobs to one aggregate admission budget. Repeated polling of the same job must not create a new billing opportunity. Deterministic source job identity and reconciliation handle ambiguous submission without new-job retry.

A durable admission ledger must retain consumed reservations across restart. Missing, corrupt or ambiguous state fails closed. Specify a bounded single-instance deployment if that is the only proven storage topology; do not claim multipod safety. Disable unguarded streaming, dataset load/discovery, export and source-write entry points. Read-only flags or local timeouts are not proof of provider cancellation. Before returning Completed, atomically seal the execution against all future admissions and require independent provider-DONE evidence for every previously admitted job. This needs a separately reviewed private signed seal/status control contract; Cube 1.8.5 has no extendApp hook, so any entrypoint integration must use actual supported APIs. Missing or uncertain terminal evidence keeps the gateway source reservation held.

`jobTimeoutMs` remains best-effort. Never populate proven lifetime/statement fields from configuration; uncertain work retains source reservations. Enforced request construction and synthetic driver tests do not establish actual provider billing, image-package compatibility, IAM, live result hydration or operational readiness. Live activation stays blocked until separately authorized source-specific evidence.

The shipped BigQuery slice is the actual driver subclass and durable admission/seal library, plus retained model/binding assets. Test the real filesystem journal/locking/crash behavior; fake only SDK/provider boundaries. Do NOT implement the programmatic server, signed control route, gateway SourceAttemptContext hook, or completion/release wiring in this wave. A stable explicit internal API supports that later work. The admission engine is useful tested software, but it is not a complete BigQuery serving connector. Existing BigQuery selection/live gates remain blocked. The full sealed-completion protocol above is a next-step integration requirement, not a claim that gateway capacity is now safe for live BigQuery.

## Domo offline interface

Implement a separate injected-transport reader, not a disguised Cube source. A finite catalog maps an entire normalized approved request and exact authorization-scope digest to immutable operator-reviewed statement bytes. No interpolation, dynamic SQL compiler, formulas or unreviewed date/filter/sort combinations. Statement identity, authoritative definition digest and dataset identity are pinned.

Validate ordered unique positional columns, row widths/counts, strict output types and exact decimal strings. Bound raw and normalized response bytes to 1 MiB and rows to 1,000 or lower current limits. Required freshness comes from reviewed source-load evidence, never `fromcache` or fetch time. Authorize before submission and again before release; changed principal/tenant/policy/resource/scope fails closed. One request, zero retries, bounded deadline. No credentials or default HTTP factory. This is an executable offline contract slice; runtime selection remains unavailable until the Cube-specific persisted/normalization seams receive a separately reviewed Domo integration design and actual source gates pass.

## GCS offline artifact interface

An immutable operator allowlist pins artifact ID/revision, associated product identity, owner/classification/policy/tenant, exact bucket/object, generation and metageneration, expected size/SHA-256/media type and any required reviewed source watermark. Generation values are canonical positive decimal strings. Descriptors/results contain no URL, prefix, glob, secrets or arbitrary headers.

Injected transport performs at most one bounded metadata read (64 KiB) and one content read (1 MiB), using exact-generation/metageneration preconditions. Reject wrong identity/version, 412, redirects, compression/transcoding, range/partial results, wrong media type, size/hash mismatch and overflow. No fallback to latest generation, retries, cache, extraction or temporary files. Authorize before revealing metadata or reading, then reauthorize before releasing a complete bounded buffer. Timeout or revocation releases no bytes. Total deadline includes authorization and streaming and never exceeds 120 seconds or tighter policy.

This is internal software, with no artifact route or MCP tool. The injected fresh audited authorization gate is mandatory; its fake implementation does not prove deployed access controls. Product-policy inheritance versus a separate artifact action is an explicit future contract decision. Actual storage-class/location/egress cost and source access need separate approval/evidence.

## Signed capability URL hardening

Reject case-insensitive and percent-encoded GCS V4 signing keys and V2 `Signature` / `GoogleAccessId` in existing metadata links; cover other equivalent signed-access credential keys deliberately. Such bearer capabilities bypass later gateway-policy revocation. New artifact contracts have no URL field.

## Verification and acceptance

1. Preserve existing v1 immutable bundle hashes and all current tests. Synchronized baseline: 2,221 unit/contract tests passed; 373 service-dependent cases deselected. This is not live/backend evidence for new code.
2. Exercise v2 prepare/poll/normalize/retrieval with non-Orders reviewed fields, immutable binding drift, exact authority, two tenants, narrower same-tenant entitlements, revocation, fixture separation, decimals/nulls/dates/freshness and limits. Add genuine disposable PostgreSQL/Cube integration cases to the manual backend gate where applicable.
3. Test every BigQuery job path and aggregate admission: deterministic IDs, replay, concurrent requests, crash/restart, corruption, unknown submission, changed SQL/config, budget exhaustion, malformed and oversized response, timeout and cancellation uncertainty. Distinguish fake SDK from actual pinned-image behavior.
4. Domo/GCS tests deny network fallback and cover immutable configs, missing gate, hostile inputs, schema/object drift, pre/post authorization, redacted failures and no partial release.
5. Full source unit/contract, Node tests, unchanged API/MCP snapshot checks and frontend checks; independent architecture and code review; exact-source manual hosted backend check requested through the parent coordinator. No CI dispatch by workers. Report passed, failed, collected-only and not-run separately.
6. Publish only a private draft feature PR. No main merge, deployment, live credentials or source calls.

## Technical review outcome

The narrowed durable BigQuery library design, v2 accepted-query and same-query checkpoint protocol, and offline Domo/GCS boundaries were independently reviewed and implemented. Exact-source hosted run 18 passed the disposable backend and guarded actual-image package gates. See [the evidence record](../evidence/reviewed-connectors.md#hosted-gate-attempt-18-pass); serving integration, real-provider/source prerequisites and production approval remain separate.
