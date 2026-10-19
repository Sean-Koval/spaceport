# Executable BigQuery pilot: design proposal

15 October 2026. Prepared against Spaceport integration tree `a3155cbfb8fc5c073a4c2d184ecd75d0b99d0024`. Design only; no repository edits, credentials, IAM, provider requests, resources, or deployment performed. This proposal requires coordinator/user review before an implementation plan or code.

## 1. Outcome and chosen approach

An operator can install one reviewed product package mapping their own BigQuery table/view, start a private GKE pilot using existing approved infrastructure, and run the normal draft/test/publish/consumer REST/MCP flow. Configuration and rendering are offline. Source checks are explicit, bounded operations through ordinary execution or the existing isolated fixture capability, never startup probes or an unrestricted SQL endpoint.

Use a **new PostgreSQL-backed serving ledger in the existing registry**. Preserve the existing POSIX ledger, bounded driver, fictional bundle, disabled profile, and `cube-rest-v1/v2` byte contracts. They are independently tested prior work, not serving evidence for this new path. The new source runtime must be independently qualified.

Alternatives considered:

1. Reuse the POSIX ledger for serving. This adds a second durable authority, signed registration/seal/status protocol, retained single-writer block volume, and manual stale-lock recovery after SIGKILL. Safe but a less usable first GKE deployment.
2. Move the serving ledger into the existing registry, with narrow source-runtime roles and transactional admission/seal/settlement. Recommended: fewer distributed failure modes and no new durable service or private control HTTP API.
3. Build a general connector scheduler/distributed control plane. Out of scope. The design has only BigQuery job and Domo single-send semantics needed by this pilot.

The current queue's per-generation `SourceReservation` is not reused for the new connectors. Its current-lease-only release rule and capacity checks prevent reclaim at concurrency one while the old hold is retained. Legacy behavior stays intact; new connectors get one stable logical operation hold independent of their worker lease.

## 2. Topology and responsibility

Reference deployment uses the existing PostgreSQL registry and the existing API/worker/retention separation, plus:

- A pinned Cube 1.8.5 consumer process with the new BigQuery serving driver.
- A separate fixture Cube process with an independently retained fixture bundle and narrow fixture-dataset identity. Its model/source context cannot be selected by a public request.
- One metadata-only BigQuery reconciler process. It has a separate database role and separate GKE Workload Identity principal. It cannot admit queries or read result rows.

Each process initially has one replica. PostgreSQL transaction tests exercise concurrent clients, but deployment evidence does not claim HA, automatic regional disaster recovery, or arbitrary multipod qualification. Cube memory caches/queues are ephemeral. There is no Redis, additional PVC, POSIX journal migration, `extendApp`, or Node-to-gateway completion webhook.

Cube uses its actual maintained programmatic `CubejsServer` interface where a launcher is needed; its ordinary HTTP interface remains private. Gateway workers send signed exact execution context over the existing private Cube load route. Network policy permits only gateway-to-Cube, required database connections, and the exact reviewed Google/auth endpoints. No Cube SQL, GraphQL, developer, upload, export, or discovery port is exposed. TLS/secret delivery remains an operator prerequisite.

The reconciler's provider custom-role requirements are `bigquery.jobs.get` and, if approved cancellation is enabled, `bigquery.jobs.update` on the billing project. It has no `bigquery.jobs.create` and no `bigquery.tables.getData`. Google documents the extra table-data permission for `jobs.getQueryResults`, so this is an actual data-plane privilege separation, not merely a renamed SDK client. The execution identity needs Job User on the billing project and Data Viewer only on the approved input datasets/views; it must not have permanent dataset/table write privileges.

## 3. Common durable source ownership

The shared design uses three concepts, with provider-specific job/send records rather than a generic workflow language:

### `source_operation_groups`

One immutable group per ordinary execution or fixture **test run**, not per fixture case. It pins the owner kind/ID, capacity principal (the initiating actor for fixtures), source family, immutable fixture/catalog plan when relevant, original deadline, submission-count ceiling, and optional aggregate reserved-byte ceiling. The group is the logical hold included in `held_source_capacity` alongside legacy consumer and fixture reservations, with no double counting of a new-path fixture.

Existing `FixtureAttempt` records represent individual cases, while `TestRun` is inserted at suite completion. Therefore a fixture group itself is the new durable start-of-suite authority: immutable draft-revision/catalog/policy/actor and exact bounded case IDs. It does not require a foreign key to a nonexistent future `TestRun`. Each case session links its actual `FixtureAttempt`; completed `TestRun` evidence links back to the exact group. A crashed/incomplete suite still has a valid independently retained hold.

A consumer execution normally has one child session. A fixed fixture suite has separate exact-case sessions because its cases can have different authorization scopes. All cases share the group budget. Case identities and upper bounds are installed before any case dispatch; retries/reclaims cannot add cases, reset counters, or refund consumed budget. Permission/input-negative cases submit nothing. A deliberate new authorized test run is a new group and is still subject to global/source admission limits.

The fixture invocation gets a private UUID and durable group row before its first case. The immutable membership is the exact ordered bounded case-ID/catalog list; a session may be materialized once when its case obtains fresh authority, uniquely keyed by `(group_id, case_id)`. A closed group forbids creating any further session. **Case completion requires only its own sealed+terminal session**, so earlier cases can finish while later cases remain planned. The suite's hold settles only after the group is closed and every materialized session is sealed+terminal. Aborting/expiring a suite closes the group, seals every materialized session, and rules out all untouched cases; absent sessions have no submission authority and need no invented provider proof. A consumer group closes with its one session. This distinction prevents a suite-wide settlement deadlock at the first case.

### `source_sessions`

One immutable exact source/query/authorization identity within its group. Required identity includes:

- Session UUID plus an unpredictable private provider-namespace nonce; neither is derived only from a caller request ID.
- Owning execution/fixture case, principal, tenant, consumer/fixture context, source profile identity/revision, and physical source-capacity key.
- Exact accepted request/execution, prepared-query, authorization-scope/full-decision, product/version, binding, model bundle, source-definition, and catalog digests relevant to the provider.
- An internal authority stamp tied to the exact committed allow-audit identity and protected structural authorization snapshot described below. It is not merely a policy revision or caller-supplied digest.
- Original absolute deadline and fixed per-job/count/aggregate budgets; later current-policy deadlines may only tighten.
- Current worker generation/fence, admission state (`open` or `sealed`), seal reason, and immutable settlement evidence/timestamp when safe.

Worker leases are scheduling ownership. A reclaim increments generation and reuses the same group/session and all existing jobs/budget. It is allowed at full capacity because it does not allocate a new source hold, but still requires current authorization and an unexpired original authority window. A reclaim does not unseal a session. Terminal/expired gateway executions cannot be revived by source settlement.

### `bigquery_source_jobs`

One immutable intent per `(session_id, semantic_key)`, where the semantic key hashes exact generated SQL, exact positional parameter values, and the pinned source identity. Persist only fingerprints and safe configuration, not SQL, parameter values, credentials, rows, or raw provider errors. The row includes deterministic provider job ID, billing project/location, submitting principal identity, first timeout, maximum bytes, submitted-field-presence facts, request/configuration digests, and observation state.

The job ID hashes the deployment epoch, private session namespace, and semantic key. It does not depend on worker generation or remaining timeout. The first admitted timeout/configuration never changes. Every different SQL/parameter operation, including refresh/auxiliary work, consumes another fixed full per-job byte reservation. No refund occurs after success, provider rejection, cancellation, timeout, or ambiguity. Per-job/count caps and aggregate group/session caps use exact integers bounded by the declared contract.

Observation states distinguish `intent`, `pending/running`, and independently matched provider `DONE` with success/error. Cancellation request/acknowledgement is separate intent, never terminal evidence. Terminal observations cannot regress.

### `domo_source_sends`

The shared migration also owns a Domo sibling table, unique by `session_id`. It retains exact plan/statement/dataset/source/API/response-profile digests, sender-nonce hash, send-intent time and state `intent | terminal_success | proven_not_sent`. It stores no SQL, token or rows. The Domo implementation supplies bounded typed observation values. Its source-runtime role can arm exactly once under a fresh fence and later append exact-intent evidence; it cannot release a group directly. A one-use opaque permit remains in the original sender's memory. Only that still-live original permit, before transport invocation, may attest a definite no-I/O abort. Restart, reclaim and missing responses cannot mint that proof or another send right. A full response can settle through observation authority after data authority has expired, while all rows remain withheld.

## 4. Capacity scope, transactions, and roles

The physical BigQuery capacity key is canonically derived from provider + billing project + query location. It excludes connection name/revision, product, dataset, model digest, credentials, and service-account rotation. New connection aliases and credential rotation therefore cannot evade unknown holds. Profile-to-pool mappings are operator-owned immutable deployment data. All connections to that project/location in this registry use the same pool.

There are two explicit limits:

1. Logical source-operation groups count against existing deployment/per-principal concurrency, including legacy holds.
2. Unresolved provider operations count against deployment-wide and per-physical-source active-job limits. A single session with multiple auxiliary jobs cannot multiply the actual provider concurrency ceiling.

Unknown intents count as active operations. A provider-DONE job can free an active-job slot, but its consumed byte/count budget remains spent and its group remains held until sealed and fully terminal. These are limits for Spaceport work coordinated by this registry; they do not cap other applications or a second independent registry using the same Google project.

The total lock order is: existing deployment capacity row → product head → draft revision → policy head → policy revision → execution state → execution lease (or fixture-attempt state) → physical source-pool row → operation group → source session → provider job/send. Transactions omit irrelevant classes but never reverse them; multiple rows of one class are ordered by immutable ID. Existing consumer queue operations need no product/draft lock for immutable published versions. Provider admission/observation/settlement starts with deployment capacity, then physical pool/group/session/job, and never subsequently takes policy/product/execution row locks; current lease/owner status is read under the already-held deployment capacity serialization point. Heartbeat remains the existing one-row lease operation with no later capacity lock. Admission and sealing take the same session lock. Every SQL function has bounded typed arguments and no dynamic identifiers; security-definer functions have a fixed safe `search_path`, explicit owner, revoked `PUBLIC` execution, and narrowly granted role access. No transaction remains open across a provider RPC.

Current authorization is checked independently of a live lease. Existing principals/grants have no revision counter, so the new source path stores a protected structural snapshot, not a fictitious version. It covers the principal ID/subject binding/kind/service-client identity/active flag; the complete bounded grant set for the relevant policy (including every tenant, active flag, role and entitlement, canonically ordered); current policy head and immutable revision/content digest; exact product/version lifecycle; and, for fixtures, exact draft head/revision/catalog plus both the initiating author's author authority and the fixed fixture principal's data authority. A second same-policy tenant grant must change the stamp, even if the previously selected grant is unchanged.

The new audited gate obtains these inputs in one coherent SQL statement snapshot after acquiring the applicable locks in the order above, derives its decision from those exact inputs, and retains that SAME snapshot with its allow audit and source attempt. It must not derive from one grant read and then certify a newer independently reloaded snapshot. Before committing authority it compares current protected values again and aborts on a difference. Snapshot size is bounded (64 KiB and at most the existing 1,000 grants per principal); malformed/oversized state denies. The private snapshot is never returned to Cube or the Domo transport: narrowly granted functions expose only an equality verdict and safe stamp/decision identity. This leaves the policy engine in Python while DB functions compare exact protected JSONB/row values; no weak hash or caller assertion substitutes for equality.

Every new job admission, Domo arm/send check and ordinary metadata/result SDK RPC compares that current authoritative snapshot as well as generation, lease and deadline. Any change, including revocation, policy-head change, lifecycle withdrawal, draft movement or newly ambiguous grants, denies dispatch/data even within the old token/lease window; the worker seals its session. Reclaim cannot replace the old session's authorization identity with a different scope. Metadata-only terminal observation and computed settlement do not depend on continued data authority. Reads for these comparisons take no late policy/principal locks and obey the total lock order. There is an unavoidable last-DB-check→network-call race: a revocation committing immediately after the check cannot retract a request already authorized/in flight. Rechecks after responses and before result commitment withhold rows; durable intent ownership still covers source work. This pilot does not claim atomic transactions spanning PostgreSQL and Google/Domo.

New roles:

- Gateway application: authorize/audit/create or reclaim groups and sessions through existing trusted execution/fixture services; seal its exact sessions; read safe status. It cannot mark provider terminal or directly release a held source group.
- BigQuery execution runtime: read only its source-session/attempt projection; admit BigQuery intents under a current fence; check active authority; record matched observations for its exact known jobs. It cannot change policies, principals, products, results, source profile mappings, budgets, or accepted query authority.
- BigQuery reconciler: obtain bounded exact-job observation leases, record matched metadata/cancel observations, seal only already-expired/cancelled groups under a DB predicate, and request computed settlement. It cannot admit intents, renew execution authority, unseal, fetch canonical query/results, or write gateway results.
- Domo source runtime: a separate role bound to allowed Domo source/context rows. It may arm one exact intent and record its bounded source-specific terminal/not-sent evidence, including after execution expiry; it cannot mark a BQ job, write gateway results, change policy, or release a group by assertion. Ordinary application role cannot mark Domo terminal. Fixture and consumer role credentials are separately context-bound.
- Migration/bootstrap owner: validates already-provisioned distinct non-elevated roles and creates schemas/grants/profile mappings separately. Migration never creates cloud identities or database login credentials. Its credentials are never supplied to API, worker, Cube, or reconciler.

Database rows/functions enforce identity, generation, budget, immutable transitions, and proof prerequisites. They cannot authenticate a Google response by themselves; the reviewed provider worker, its TLS/authentication and metadata validation remain trusted computing components. This is not a claim of safety after arbitrary runtime-code or database-owner compromise.

Every provider-role function first checks an operator-owned mapping from the real database login (`session_user`, not the definer's `current_user`) to the exact provider, source profile and consumer/fixture context. Source-runtime credentials cannot enumerate or mutate arbitrary sessions merely by guessing an ID. Reconciler mapping may span the explicitly approved consumer/fixture profiles in its billing project, but exposes only known-job fingerprints/configuration/status. Role membership/SET ROLE paths and owner credentials are prohibited as in the existing registry role checks.

Bounded maintenance: one pass selects at most 32 expired/terminal-owner groups ordered by deadline/ID and closes/seals them in bounded transactions; each pass then takes at most 8 due exact-job reconciliation leases, with no more than 2 provider metadata RPCs concurrently and a 5-second per-RPC deadline. Persist backoff/next-observation times capped at 60 seconds; a repeated unknown outcome is not completion. Successful cancellation acknowledgement does not shorten retention. No raw provider diagnostics are retained.

Initial hard ledger growth caps are 1,000 retained groups, 8 sessions per group, and 8 provider submissions per group, with deployment totals of 8,000 sessions and 8,000 provider intents. Lower operator caps are allowed. A fixture plan can have at most 16 total cases, of which at most 8 are source-positive; only source-positive cases can create sessions. These new-path limits do not lower legacy fixture limits. Creation/admission at a cap fails closed under the capacity lock; no silent eviction/pruning. Consumer and fixture groups have an original overall deadline of at most 120 seconds from group admission. Every case's SourceAttemptContext is clamped to that original group deadline as well as its existing FixtureAttempt deadline; later cases never obtain a fresh suite-wide 120-second window. The old immutable FixtureAttempt snapshot schema need not change. These are admission/response deadlines, not source-lifetime guarantees.

Migration compatibility: `0005_source_lifecycle` creates PUBLIC-revoked source objects while preserving successful migration/runtime with the existing four legacy roles alone. New source-role grants occur only in explicit source-pilot provisioning after validating the separately pre-created distinct non-elevated login roles and exact provider/context mappings. No unconditional fifth-role prerequisite is imposed on old demo/pilot installs, and no role/password is automatically created. Test both role sets. New `source_operation_groups` privileges needed by the invoker-rights capacity view reveal only safe capacity identity to the ordinary app.

## 5. Internal API and trusted dispatch

Python adds an internal frozen `SourceAttemptContext`, constructed only by the queue/fixture service after its fresh authorization, audit, durable session creation, and current lease fence have committed. It includes session/group identity, generation, exact authority/query/binding/bundle/source digests, physical capacity key, and tightened deadline. It is not a field in a public request, manifest, `PreparedQuery`, or persisted three-key Cube preparation payload.

Suggested narrow service methods:

- `source_attempt(claim, prepared, current_decision) -> SourceAttemptContext`
- `seal_source(context, reason) -> SourceStatus`
- `source_status(session_id) -> SourceStatus`
- `complete(claim, completed)`: for the new source path, requires matching durable sealed/settled status; an adapter's `Completed` alone is insufficient and cannot release a source hold.

Adapter-neutral integration is registered from the pinned binding revision, never from a user flag. The trusted `AdapterRegistry` exposes `requires_source_context`, `provider_kind`, `prepare`, `source_plan(spec, prepared, executed_decision) -> SourceSessionSpec`, `execute_step(..., source_attempt, ...) -> Pending | Completed`, and normalization/replay. Legacy wrappers keep existing Cube behavior. Domo never returns Pending after a send and never resubmits an armed session. Worker/FixtureRunner use one gate → trusted context → execute step → seal session → inspect session-terminal → complete path. Consumer completion also closes its single-session group; fixture-case completion need not wait for group release.

`SourceSessionSpec` contains the immutable source/profile/context, owner/case, execution/prepared-query/scope/full-decision/binding/bundle/definition/catalog digests and fixed budget. It contains no credentials, SQL, SDK object, callable, public endpoint override or authority assertion. `SourceAttemptContext` adds the independently committed session/group IDs, current generation/fence and absolute deadline. Domo and BQ preparation types remain distinct; legacy Cube preparations retain their exact three-key JSON shape.

Suggested asynchronous Node ledger interface:

- `checkAttempt(sessionId, generation, exactContext)`: immutable match, exact protected current-authority snapshot, current generation/lease, open admission when applicable, current DB deadline.
- `admitJob(context, semanticKey, firstConfig) -> {submitOnce, job}`: recheck current authority and fence, then transactionally commit full intent/budgets before source submission. The permit is returned only after a confirmed commit; an immediate pre-RPC check remains required.
- `checkDataRpc(context, jobId)`: check protected current-authority equality, current generation, current lease, original/tightened deadline and exact job before each normal SDK call; recheck after awaited metadata/results before accepting them.
- `observeJob(jobId, matchedEvidence)`: exact-job metadata-only authority, independent of expired data/execution authority. The SQL function compares the supplied observed identity/configuration digest to the immutable intent.
- `sealSession(context/reaperAuthority, reason)`: atomic irreversible denial of all new job keys, across main, refresh, queued and inherited driver paths.
- `settleGroup(groupId)`: derives safety under locks; never accepts a caller `all_done=true` flag.

The new Cube load token extends the existing independent scope enforcement with session/generation and exact prepared-query/source/binding identities. Use maintained JWT verification and the existing explicitly pinned private signing configuration, with a distinct audience/schema for the new serving revision. Tokens are short lived and never increase the DB deadline. Node verifies the raw load body's canonical query digest before Cube normalization and independently applies retained tenant/access-group query rewriting.

Gateway sends `X-Request-Id = sessionUUID.gGENERATION`. `checkAuth` rejects missing/repeated/comma-joined/conflicting request IDs and any mismatch with the signed context. No caller header can register a session. The exact pinned Cube middleware order checks authentication before extracting the request ID, and its query path propagates request ID into driver query options. This propagation must be exercised end to end in the actual image, not assumed from a subclass test.

For the pilot, partition orchestration/cache/queue identity by bundle, source identity, full scope, session identity **and generation**, with bounded Cube orchestrator cache size/TTL. A reclaimed generation cannot join an old queued promise or stale driver-factory closure. Driver factory captures immutable source/scope/session identity; every invocation parses the exact full request ID, checks it against its expected generation, and resolves current DB authority before any SDK RPC. Old in-flight calls retain their old fence and cannot release rows. Provider IDs, semantic keys and budgets remain stable across generations in PostgreSQL. Compiler cache is keyed by immutable bundle plus source identity; supported retained static YAML models cannot use COMPILE_CONTEXT/securityContext/source-dependent interpolation. Any future context-dependent model requires a different reviewed compiler-cache key.

This avoids cross-execution query coalescing or borrowing another execution's budget. Eviction/restart may lose cached results but cannot reset provider intents or budget. Actual package tests must verify eviction/reclaim and re-poll attach to the same provider job, with no fresh job. Request ID grammar is matched as a whole; no permissive prefix or span stripping is allowed. If the actual package adds an internal suffix, implementation must present and review that exact propagation format before changing the parser. A sealed zero-job session is safe only because admission is atomically closed, no admitted intent exists, and the exact returned cached result remains independently authorized and normalized.

## 6. Execution, completion, timeout, and crash behavior

1. Queue admission creates/reuses the stable hold. Preparation does not query BigQuery; Cube model metadata is static reviewed schema.
2. Fresh queue gate supplies trusted context. Cube checks context/body; the driver obtains a confirmed durable intent before the first `jobs.insert`.
3. First submission uses that job ID, standard positional SQL, `maximumBytesBilled`, original bounded `jobTimeoutMs`, disabled query cache, and disabled automatic SDK job retries. It supplies no destination/write/export/load/script/session/connection/UDF options.
4. Poll/retry/reclaim attaches to that exact existing job. A creation error, conflict, lost response, or ambiguous database commit never creates a new provider ID or automatically resubmits an unknown intent. Missing-job 404 is not proof of no submission and does not release capacity.
5. Matched provider `DONE` is recorded before accepting result rows. An error is terminal source evidence but fails the result.
6. When Cube yields candidate data, the worker atomically seals its source session, then waits within the original execution deadline for all admitted jobs to be independently terminal. It keeps the bounded candidate result in memory. `Completed` or successful HTTP delivery does not settle source ownership.
7. The queue stores results only after current result authorization, exact normalization and durable source settlement. Existing policy/grain/row/byte/freshness checks remain. If result normalization/revocation fails after source termination, source capacity can still settle without releasing rows.
8. On deadline, authorization failure, cancellation or unrecoverable result failure, seal immediately when the DB is available. If that call is interrupted, the reaper seals expired sessions from the authoritative DB deadline. Local timeout, stopped worker and cancelled HTTP wait do not release anything.
9. Metadata-only reconciliation continues for the exact durable jobs after lease expiry, gateway failure, revocation or deadline. It never obtains rows or creates a job. It can request cancellation after validating the exact job; only later provider-DONE evidence settles it.

Crash matrix:

| Interruption | Required outcome |
| --- | --- |
| Before session/intent commit | No provider RPC; uncommitted state has no authority |
| Ambiguous intent commit | Read exact session/key after reconnect; no automatic submission; retain any committed intent |
| Intent committed, process dies before/after insert | Same job identity remains owned; lookup only; no new budget/new ID |
| Insert succeeded, reply lost | Lookup exact ID, validate full intent, continue only within current data authority |
| Worker lease expires/reclaims | Same hold and jobs, higher generation; old generation cannot admit/fetch/release rows |
| Principal/grant/policy/lifecycle changes while SQL is queued | Next pre-RPC authority comparison denies, even if token/lease remain live; zero new provider submit/data fetch; existing intents remain held |
| Old data RPC returns after fencing/deadline | Withhold rows; exact terminal metadata may still be recorded through observation authority |
| Candidate rows arrive while refresh job runs | Seal all admission, retain hold until that refresh job is provider-DONE |
| Seal races late new SQL | Exactly one transaction wins; if admission wins it is counted, otherwise zero provider submit |
| Cancel acknowledged but RUNNING/unknown | Hold remains; continue metadata-only reconciliation |
| Gateway result commit fails after source settled | No source work re-created; result recovery/timeout follows existing gateway rules |
| Database outage | No new admissions/results; in-flight intents remain owned; later reconcile exact IDs |
| Process restart | Read durable session/job state; no POSIX stale-lock problem and no budget reset |
| Registry restore to older state | Unsupported normal restart; stop admission and require operator reconciliation/external epoch fencing before reactivation |

The irreducible commit-before-RPC ambiguity can leave a never-submitted intent held indefinitely. This conservative pilot does not automatically clear it from a 404, elapsed time, or timeout. Operational tooling reports the safe intent identity and blocker; any later release/repair procedure needs its own exact proof and review. Registry backup rollback can lose acknowledged intents, so merely using PostgreSQL does not solve coordinated rollback safety.

Deployment/source epoch is pinned in an operator-owned external versioned configuration and matched to the registry on every admission. This catches mismatched epoch/restore combinations; it does **not** automatically detect a rollback within the same epoch. Any registry point-in-time restore, clone, replacement or failover that may lose acknowledged commits requires admission disabled and old execution identities/processes fenced before recovery. An operator must independently reconcile all potentially surviving provider jobs and preserve unresolved source-pool obligations before provisioning a new external epoch and enabling new work. Ordinary crash restart against the same committed registry is distinct. Automated restore/reconciliation is outside this pilot. Never clear holds by changing epoch, connection alias or source descriptor.

Held/unknown groups, sessions and intents are never retention-deleted. Terminal pruning is unavailable in this initial slice; a future reviewed pruning procedure must first make the related execution/fixture run unreplayable, expire all dispatch authority, preserve source-pool/epoch identities and retain enough settlement evidence to prevent replay. Existing retention code receives no grant to delete the new ledger tables.

## 7. Provider metadata compatibility

The old offline validator remains unchanged. A new versioned serving validator accepts required Google-populated defaults without weakening submitted request control.

Required match: exact provider job ID, billing project, location, original submitting principal, query/parameter fingerprints and types/order, standard-SQL/query-cache settings, timeout and byte ceiling, and query job type. On terminal successful queries require `statistics.query.statementType = SELECT`. Scripts/parent-child jobs, sessions, connections, UDFs, external table definitions, load/copy/extract configs and unknown behavior-changing fields fail closed.

The versioned response profile explicitly lists harmless echoes: `configuration.jobType` may be absent or `QUERY`; `dryRun` may be absent or `false`; query `priority` may be absent or `INTERACTIVE`; `createSession` may be absent or `false`. Under the required explicit `useLegacySql=false`, documented ignored legacy-only flags `allowLargeResults` and `flattenResults` may be absent or bounded booleans. Query text/parameters, `useQueryCache=false`, `useLegacySql=false`, positional mode, bytes and timeout must still match the immutable intent. Only the separately constrained anonymous destination/create/write defaults below are exceptions. Reservation overrides, default datasets, billing tiers, nonempty labels, script/system variables, schema/partition/clustering/encryption settings, and other unreviewed configuration fields remain forbidden. Bounded output-only wrapper/diagnostic metadata is never authority, logged, dereferenced, or copied into a subsequent request. Actual-SDK fixtures based on official REST shapes must exercise these normal defaults and each forbidden mutation; no live smoke may automatically expand the profile.

The Job API explicitly says BigQuery may populate `configuration.query.destinationTable` for anonymous results. The serving validator may accept this **response-only** field only when the exact committed submit intent proves destination/create/write options were absent, the unpredictable job ID and provider principal match, the destination is in the billing project, its bounded hidden dataset ID begins `_`, it is disjoint from every reviewed input, and all other immutable configuration matches. Only documented default `CREATE_IF_NEEDED`/`WRITE_EMPTY` echoes may accompany it. It is never used as a source address, never dereferenced, never logged, and never supplied in a retry. Results are read only by exact job ID.

An underscore prefix alone is not proof of anonymity. The trust boundary also requires reviewed request provenance and independently qualified no-permanent-write IAM for the submitting principal. An arbitrary permanent `_fake` destination with missing/mismatched intent or principal must fail. No broad metadata listing is introduced to guess anonymity. Unknown/mismatched provenance remains held. Actual-provider verification must report the exact observed mapping and permission evidence; it must not silently edit an allowlist to make a smoke pass.

`jobTimeoutMs` is best effort. Do not populate a proven upstream-lifetime field from configuration. Post-SDK parsed-object metadata/result admission remains explicitly distinct from provider wire/allocation bounds. Retain 64 KiB parsed metadata and 1 MiB normalized result limits (or tighter configured limits), one-page/limit-plus-one row checks and no partial release. Until a bounded SDK transport seam is independently proven, the pilot also requires an enforced container/heap memory ceiling and documents that a huge provider response can kill the process, leaving its durable holds for reconciliation. `maxResults` is not a byte cap.

## 8. Explicit GKE authentication and own-table product packages

The only initial live auth variant is `gke-workload-identity` with an explicit operator-approved **linked IAM service-account email**. The reference uses GKE WIF KSA→GSA linkage so job `user_email` and the expected submitting identity have a concrete qualified mapping. Direct-KSA principal forms can be added later with actual metadata evidence; no fictional email assertion is used.

Construct the maintained Google `Compute` auth client explicitly and pass it through the actual SDK's supported `authClient` option; this path is visible in the inspected SDK 7.9.4/common/auth-library 9.15.1 dependencies and still needs the immutable-image probe. Do not use a dummy `keyFilename`, generic ADC file search, key JSON, Base64 credentials, environment token URI, or arbitrary SDK option spread. Reject conflicting `GOOGLE_APPLICATION_CREDENTIALS`, Cube BigQuery key/credential/export variables (including per-data-source variants), metadata-host overrides, and proxy/universe/endpoint overrides. Pin explicit project/location and ordinary Google endpoints. Runtime home/root is read-only and contains no user ADC file. Auth construction is lazy; validation/startup does not fetch a token or issue a query.

Suggested operator-owned BQ profile fields: `provider='bigquery'`, `source_id`, revision, billing/data projects, dataset/view allowlist, location, expected execution/fixture/reconciler GSA identities, retained bundle/binding identities, registry role/CA/secret references, maximum jobs per session/group (at most 8), per-job and total bytes, active-job source/global ceilings, timeout (at most 120 seconds), result/metadata limits, and fixed private Cube endpoints. Physical capacity key is computed, not a free alias. Values do not assert successful live verification.

Add a new serving binding revision, `cube-bigquery-v1`, rather than altering legacy v2. It retains the reviewed scalar/daily-grain/scope rules and adds an explicit freshness variant:

- `unknown`: no checkpoint field exists or is selected, source watermark is `null`, and the product must have `freshness.required=false`. Unexpected checkpoint fields are rejected rather than accepted as proof.
- `same-query-checkpoint`: preserve the existing correlated unique per-tenant atomic source-load checkpoint contract and strict timestamp handling. The producer contract needs actual operator evidence.

This allows a first test on a normal user table without fabricating a source-load watermark. The operator maps only reviewed table/view identifiers and columns in an immutable package; no runtime SQL/model upload or discovery. A single-tenant pilot may use a reviewed constant tenant/access-group projection, explicitly documented as such. NUMERIC/decimal-string handling, TIMESTAMP/date-zone semantics and unsupported types remain exact. New source bytes and controls produce a new retained bundle digest.

The existing author/test/publisher boundaries remain. Before publication, `product test` uses the already-approved finite isolated fixture capability, with ordinary current authorization, audit, and the shared aggregate group budget. The separate CLI `source check` is only an ordinary known-answer execution against an already-published version. There is no separate privileged qualification/activation API, magic `verified=true` flag, or bypass for arbitrary unpublished manifests. Offline validate/render must clearly differ from explicit budgeted source I/O.

The first BQ package uses the setup/Domo sample business contract: `weekly-account-usage`, daily `(day, account)` rows over a seven-day interval, exact `usage_events` decimal(38,0), and optional unknown freshness. Its source SQL/physical mapping is BQ-specific and independently retained; matching output labels do not certify cross-source business equivalence.

## 9. Required implementation footprint

- New reviewed migration(s), registry models and source lifecycle service for groups/sessions/provider intents/permissions/capacity views; old queue and fixture paths unchanged when no new source profile is selected.
- New source-aware worker/fixture seams supplying `SourceAttemptContext`, stable reclaim and proof-only completion; no public query or three-key `PreparedQuery` mutation.
- New `cube/bigquery-runtime/` package containing bounded async PostgreSQL ledger, versioned serving driver/metadata validator, security config, actual Cube launcher and metadata-only reconciler. Do not silently repurpose prior fictional assets.
- New source-pilot composition and immutable operator package loader, plus explicit `cube-bigquery-v1` binding/normalization dispatch and freshness variants.
- GKE templates for the Cube contexts/reconciler, role-specific identities/secrets, resource bounds and narrow networking; offline renderer/checks only in this work.
- Actual-image mocked-provider full-runtime harness and real PostgreSQL concurrency/crash tests. No new live CI credential or automatic cloud query.

## 10. Acceptance matrix

### Offline/source and genuine local backend acceptance

1. Legacy v1/v2 bundles, old POSIX library/profile, public schemas/MCP contracts and all existing test selections remain intact unless an explicitly reviewed integration schema delta is required by Domo.
2. Real PostgreSQL role tests reject direct owner/policy/result mutations, forged terminal/release booleans, changed immutable sessions, extra fixture cases, reset budgets, unseal, role escalation and `search_path` attacks. PUBLIC/unknown-role privileges are negative-tested.
3. Two simultaneous workers at source/global capacity one cannot exceed limits. Reclaim of the same BQ execution succeeds at capacity one, keeps the original hold/jobs/budgets and fences the old generation. Different aliases/revisions/rotated principals in the same project/location share capacity.
4. Fault injection at every DB commit/RPC/response boundary produces the crash-matrix outcome. Unknown holds survive gateway completion/failure, restart, lease expiry and deadline. No time-based release occurs. Metadata-only terminal observations after expiry can settle capacity without releasing rows.
5. Race sealing against new auxiliary SQL, queued callbacks, same-job replay and generation advance. Seal plus all-known-DONE is the only normal group release path; sealing zero admitted jobs is atomic.
6. Full actual Cube HTTP→JWT/body validation→queryRewrite→orchestration→actual pinned BigQuery subclass→actual SDK serialization→mocked provider path succeeds with real PostgreSQL. Assert main and refresh jobs share the session/group caps and request ID. Exercise Continue wait, repeated polls, cache expiry/eviction, Cube restart, worker reclaim, and late refresh after main data.
7. Actual image startup, static `/meta`, process health and local configuration probes make zero BigQuery/auth calls. Inherited discovery/stream/write/export/preaggregation/test-connection query paths are either explicitly zero-I/O static checks or fail closed with zero provider calls. Wrong/missing source context never reaches the SDK.
8. Actual-package WIF construction tests prove explicit Compute client propagation, no key/file fallback or ambient endpoint/proxy override, no source-write request fields, correct maximum bytes/timeout placement, and no automatic insert retry. This is still synthetic auth/provider evidence.
9. Metadata fixtures cover documented anonymous destination/default echoes, identity/principal/config mismatch, arbitrary `_fake` destination provenance, parameter casing/types, provider errors, terminal cancellation, partial/oversized rows/metadata and redaction. No raw query, parameters, rows, credential, destination or upstream error appears in logs/receipts.
10. Normal gateway result/REST/MCP path preserves exact decimal/date/tenant/access-group isolation, policy revocation and accepted-query matching. While a main/refresh job is queued, revoke the principal, remove/change its grant, add a second relevant tenant grant, change the policy head or withdraw the version: the next RPC is denied despite a live lease. For fixtures independently revoke the initiating author and fixed fixture data principal, or move the draft/catalog. Race a grant change between initial read and authority commit and prove that inputs used for the decision cannot be replaced with a newer certified stamp. Unknown freshness succeeds only when optional; required freshness denies it. Strict checkpoint mode and empty-result behavior remain honest.
11. Fixture test group has one explicit immutable total budget across all source-positive cases and zero submissions for negative cases. Re-running/reclaiming a case does not create extra source operations.
12. Independent final code review and exact-source full unit/contract/frontend/backend gates run through the coordinator. Passed, not-run, mocked-provider and actual-provider evidence are reported separately.

### Later explicitly authorized live/GKE acceptance

This design/code work cannot supply these results. Operator-run acceptance must identify the exact source/image/package/config and approvals, then establish: GKE WIF principal/negative key fallback; narrow IAM including no permanent writes; project/dataset location; ordinary anonymous metadata behavior; correct types/precision/date/DST; known-answer parity; tenant/grant isolation and revocation; actual maximum-bytes rejection; cancellation followed by job-DONE reconciliation; unknown-job hold visibility; restart recovery; real registry role/network/TLS boundaries; explicit optional/required freshness semantics; and the normal draft/test/publish/consumer journey. A bounded live smoke is not production qualification.

## 11. Operator prerequisites and limits

Requires an approved GKE target, PostgreSQL with required narrow roles/migrations and reliable committed durability, exact image digests, TLS/DNS/network policy, separate WIF execution/fixture/observer identities, approved source views and permission mappings, explicit byte/job budgets, fixture source isolation/known answers, and retention/backup/operator ownership decisions. Operators provide infrastructure/identities; rendered templates never create them by themselves.

No automatic release of unproven source jobs; no support for a restored older registry until reconciliation; no hard provider runtime guarantee; no arbitrary SQL, source writes, exports, preaggregations, schema discovery, arbitrary datasets, browser SSO changes, connector admin UI, generalized scheduler, or claim of hard SDK response-allocation bounds. Source scope/credentials are not product inputs. Live provider grants, queries, spend, deployment and production acceptance require their separate approvals.

## Primary-source checks used in this design

- Pinned Cube server public construction/listen and returned app: https://github.com/cube-js/cube/blob/d01994a71e64f2e8074ee04fefa3a0d9f369cc81/packages/cubejs-server/src/server.ts
- Pinned auth-before-context middleware and query request propagation: https://github.com/cube-js/cube/blob/d01994a71e64f2e8074ee04fefa3a0d9f369cc81/packages/cubejs-api-gateway/src/gateway.ts and request-parser.ts in the same directory.
- Pinned orchestrator request ID/refresh/cache behavior: https://github.com/cube-js/cube/blob/d01994a71e64f2e8074ee04fefa3a0d9f369cc81/packages/cubejs-query-orchestrator/src/orchestrator/QueryCache.ts
- Pinned bounded orchestrator cache lifecycle: https://github.com/cube-js/cube/blob/d01994a71e64f2e8074ee04fefa3a0d9f369cc81/packages/cubejs-server-core/src/core/OrchestratorStorage.ts
- Google job configuration, anonymous destination and default echoes, byte ceiling and best-effort timeout: https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/Job#JobConfigurationQuery
- Anonymous dataset behavior: https://docs.cloud.google.com/bigquery/docs/cached-results and https://docs.cloud.google.com/bigquery/docs/writing-results
- Exact-job metadata/result/cancel permissions: https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/jobs/get , https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/jobs/getQueryResults , https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/jobs/cancel
- GKE Workload Identity: https://docs.cloud.google.com/kubernetes-engine/docs/concepts/workload-identity
- Maintained Compute metadata credential client: https://docs.cloud.google.com/nodejs/docs/reference/google-auth-library/latest/google-auth-library/compute

Cube source was fetched read-only through the connected GitHub tool at the exact pinned commit. The existing isolated local package contains BigQuery driver 1.8.5, SDK 7.9.4, and google-auth-library 9.15.1; inspection of that package supports an API design, not immutable-image or live GKE authentication acceptance.
