# Source lifecycle internal interface v1

Implementation contract for the reviewed 2026-10-15 source pilot. This is not a
public API. The source design remains authoritative for safety and acceptance.

## Files and ownership

- `gateway/source_lifecycle.py`: frozen `SourceSessionSpec`,
  `SourceAttemptContext`, `SourceStatus`, `DomoSendPermit`, and registry service.
- `migrations/versions/0005_source_lifecycle.py`: common source tables/functions;
  no source login creation and no unconditional new-role prerequisite.
- Common lifecycle rows use bounded internal SQL; Domo typed preparation has a concrete UUID session foreign key.
- `gateway/execution/queue.py`: stable source-aware claim/reclaim and completion.
- `0006_source_preparations.py`: Domo-owned typed preparation persistence.
- `cube/bigquery-runtime/postgres-ledger.cjs`: Node SQL adapter, not a second ledger.
- `tests/runtime/bigquery-serving.test.cjs`: full actual Cube/SDK/real-PG fake-provider test.
- `tests/integration/source_runtime_fixture.py`: synthetic DB bootstrap/seed helper.
- `tests/integration/test_source_lifecycle.py`: real PostgreSQL role/concurrency/crash tests.

## Context

`SourceAttemptContext.wire_context()` is a fresh JSON object with snake_case keys:
`group_id`, `session_id` (canonical UUIDs), `generation` (positive integer),
`authority_stamp`, `context_digest` (lowercase SHA-256), `deadline_at` (UTC RFC3339),
and the complete `SourceSessionSpec` projection. It has no lease token, raw
authority snapshot, SQL, parameters, credential or row values. Every function
checks exact context identity, its operator-owned source/profile/context mapping,
and the current independent authority/fence where required. Public input cannot
mint this object. The context digest is DB-retained, not a supplied assertion.

`SourceSessionSpec` fields are frozen in `gateway/source_lifecycle.py`.
`capacity_key` equals the descriptor's computed `capacity_pool_id`; a provisioned
profile mapping enforces this equality. BQ `prepared_digest` equals the canonical raw Cube query digest; Domo uses its complete typed preparation digest. `request_digest` always equals the accepted execution/fixture canonical hash. BQ pool identity hashes provider + billing
project + location. Domo hashes provider + customer. Neither contains a revision,
alias, dataset, product or credential identity.

## SQL surface

All functions are in schema `gateway`, SECURITY DEFINER with fixed
`search_path=pg_catalog,gateway`, PUBLIC execution revoked, typed bounded JSON
and strings, no dynamic SQL. Role names are operator configured; the function
mapping checks actual `session_user`, provider, immutable source revision and
consumer/fixture context. Provider roles receive no direct table grants.

Each ordinary call runs in its own short transaction; successful COMMIT must be
confirmed before a returned submission permit is used. On ambiguous commit,
reconnect and inspect the existing exact intent only. Never infer a new permit.

### Ordinary runtime

- `source_check_attempt(context jsonb) returns jsonb`: returns
  `{allowed:true, deadline_at, context_digest}` after exact authority, active
  owner/lease/generation/deadline and profile checks. Denial raises a fixed safe
  database error. Used immediately before every normal provider RPC and after
  responses before data admission. Does not create or renew authority.
- `source_bq_admit(context jsonb, semantic_key text, intent jsonb) returns jsonb`:
  checks open session/group and current authority under locks, reserves full
  counts/bytes and active-job capacity, records immutable intent, returns
  `{submit_once:boolean, job:<JobProjection>}`. `submit_once` is true ONLY for
  this transaction's newly inserted intent and after caller-confirmed COMMIT.
  Existing semantic keys require identical query/parameter/type/byte/source
  identity and return the ORIGINAL stored intent with false. A concurrent loser
  may have computed a different remaining timeout; its proposed timeout/config
  digest is discarded, never written or submitted. All subsequent operations
  use the returned first intent.
- `source_bq_lookup(context jsonb, semantic_key text) returns jsonb`: current
  authority check then null or the existing JobProjection, never a submission
  permit. Call before admission to recover the first timeout after restart.
- `source_bq_check_data(context jsonb, job_id text) returns jsonb`: same current
  checks plus exact owned job; returns JobProjection. It cannot admit a job.
- `source_bq_observe(job_id text, evidence jsonb) returns jsonb`: exact permitted
  profile/job and immutable digest checks; accepts bounded pending/running/DONE
  metadata evidence independently of current data authority. Returns source
  status. The trusted runtime validates provider metadata before this call.

`intent` exact required keys: `query_digest`, `parameters_digest`,
`parameter_types_digest`, `configuration_digest` (SHA-256),
`maximum_bytes_billed` (canonical positive decimal STRING), `job_timeout_ms`
(integer 1..120000). SQL compares bytes to fixed per-session cap, rejects a new
timeout exceeding current/original remaining authority (never silently mutates
it), and derives project/location/principal from the
provisioned descriptor. SQL derives a deterministic unpredictable job ID from
the retained external epoch, session nonce and semantic key. The first intent
is immutable; retries do not refresh timeout or refund bytes. The configuration
digest is SHA-256 over canonical `{project_id,location,principal,query_digest,
parameters_digest,parameter_types_digest,maximum_bytes_billed,job_timeout_ms,
submitted_destination:false,submitted_create_disposition:false,
submitted_write_disposition:false}`. Actual submitted SDK REST bytes must match
the returned first identity/configuration exactly.

`JobProjection` keys: `job_id`, `session_id`, `group_id`, `semantic_key`,
`project_id`, `location`, `principal`, `intent` (the safe exact object above),
`intent_digest`, `state`, `terminal_error`. No query text or parameter values.
`state` is `intent|pending|running|done`; `terminal_error` is null until DONE and
then boolean. `intent_digest` fingerprints the full safe job identity.

`evidence` exact keys: `intent_digest`, `state`, `terminal_error`.
The exact job lookup and serving metadata validator establish identity,
configuration and provider provenance before emitting this small observation.
It cannot move DONE backward. Error text is never an argument. Provider roles
are trusted for truthful provider observation, not permission to mutate policy.

### Reconciler

- `source_bq_reconcile_claim(max_jobs integer) returns jsonb`: at most 8 due
  known-job projections restricted to login mappings, each with a short
  observation scheduling lease and safe source descriptor. No arbitrary listing.
- `source_bq_check_observation(job_id text, observation_token text) returns jsonb`:
  exact known job and observation lease/profile check before metadata/cancel RPC.
  It grants no insert or getQueryResults authority.
- The same `source_bq_observe` accepts terminal late evidence even if data
  authority or the scheduling lease has elapsed. Cancellation acknowledgement
  never calls it with DONE unless independently observed provider metadata is DONE.

### Gateway/fixture lifecycle

- `source_seal_attempt(context jsonb, reason text) returns jsonb`: irreversible runtime seal with exact retained context and CURRENT session plus actual owner generation/token. Same-generation expired/revoked cleanup is allowed; stale workers cannot seal a reclaimed session. Runtime has no ID-only seal privilege.
- `source_seal_session(session_id uuid, reason text) returns jsonb`: app-only administrative seal; reasons are closed constants, not diagnostics. Takes capacity→pool→group→session.
- `source_close_group(group_id uuid, reason text) returns jsonb`: atomically closes
  roster materialization/admission and seals every materialized session in ID order.
- `source_status(session_id uuid) returns jsonb`: exact SourceStatus projection.
- `source_settle_group(group_id uuid) returns boolean`: derives settlement only
  when group closed and all existing sessions sealed and every intent terminal.
  No caller terminal/release boolean is accepted.
- `source_expire_groups(max_groups integer) returns integer`: at most 32 groups
  whose authoritative deadline elapsed; atomically closes/seals and derives settlement, including zero-intent groups. NULL/out-of-range limits are rejected.

Python `ExecutionQueue.source_attempt(claim, prepared, decision, session_spec)` delegates to `SourceLifecycle.consumer_attempt(queue,...)`. `SourceLifecycle` also supplies `seal_source(context,reason)`, `source_status(session_id)`,
fixture group begin/materialize/close wrappers, and exact prepared persistence
seams. Group/session creation stays gateway-authorized and audited, never Node.
Case completion uses sealed+terminal session; consumer completion also closes
its one-session group. Group capacity remains held across the entire fixture run.

### Domo

- `source_domo_arm_send(context jsonb, plan_fingerprint text, nonce_hash text,
  intent jsonb) returns jsonb`: exactly one committed send; fingerprint equals
  admitted prepared digest. Duplicate/unknown send never returns another permit.
- `source_domo_observe(session_id uuid, nonce text, evidence jsonb) returns jsonb`:
  exact intent-bound terminal success. Can settle after lease/deadline/revocation;
  cannot expose rows or send again.
- `source_domo_not_sent(context jsonb, nonce text) returns jsonb`: original current generation and exact nonce only, called exclusively by an unused in-memory permit before transport invocation. It may prove no I/O after deadline/revocation and seals that session; a started permit or restart cannot recreate this proof.

Python `domo_arm_send(context, plan_fingerprint, intent_metadata)` returns
`DomoSendPermit` only after confirmed COMMIT. `permit.start_io(plan_fingerprint)`
is mandatory immediately before the sole POST and is single-use. Permit repr is
redacted. `domo_observe_success(permit,evidence)` and `domo_observe_not_sent(permit)`
do not accept a replacement permit reconstructed from persisted state.

## Actual-image harness topology

The existing `verify_cube_runtime.sh` starts run-private `registry-db` before
its package probes; the later separate registry gate is not a dependency.
An explicit new test overlay/bootstrap creates a disposable `gateway_source_test`
database on THAT PostgreSQL, migrates it with the existing synthetic owner and
provisions distinct synthetic source roles. It does not reuse gateway/demo data.
Bootstrap-only admin/owner secrets are absent from runtime/probe processes.

The actual Cube image starts a second programmatic Cube HTTP server on an
ephemeral loopback port. Requests pass actual JWT/body/rewrite/compiler/queue,
real serving driver, real PostgreSQL functions and actual SDK serialization.
Only the final SDK provider request boundary is synthetic. Network guards permit
loopback HTTP and the exact private registry-db address/port; all auth/provider
network attempts fail the test. Existing internal private topology is retained,
no host port or external egress is added. Pin the source/SDK/image and emit
redacted phase receipts. New phase timeout is 180 seconds inside the existing
20-minute backend budget; no skipped tests or fake-ledger full-runtime claim.


## Production service and provisioning

`SourceLifecycle(registry,policy,sources,*,deployment_epoch,provider_registries)` uses the application registry for audited owner/session creation. `provider_registries` contains only explicit `('domo','consumer'|'fixture_test')` restricted registries. No app-role fallback can write provider terminal evidence. `provision_sources(connection,descriptors,role_mappings,*,deployment_epoch,max_active_operations=10)` runs in an explicit owner transaction and grants existing restricted logins only the exact mapped functions. It creates no login, key, credential, or cloud permission.

Fixture methods are `begin_fixture_run(authority,run_id)`, `check_fixture_group(uow,group,authority,case)`, `fixture_attempt(group,authority,case,attempt,prepared,current_decision,session_spec)`, and `close_group(group_id,reason)`. The durable invocation UUID and immutable positive-case roster precede all cases. The full suite shares one original deadline. Source case success is independently guarded by its exact owner's sealed/terminal session and that session's group-clamped deadline; a caller-supplied completion boolean cannot certify it. Consumer success additionally requires its group closed. Worker-attributed group closure occurs only after the queue checks the current lease and generation in the same transaction.

Observer mappings and metadata-only cloud permissions for old immutable profiles must remain available through credential/revision rotation until their jobs settle. Revoking data runtime privileges does not revoke this recovery authority. Same-epoch database rollback cannot be detected by PostgreSQL alone; restoration requires externally fencing old processes/credentials and operator reconciliation before a fresh epoch is activated. Held/unknown evidence is never retention-deleted by this pilot.


The effective global active-operation cap is the minimum of deployment settings and every retained BigQuery profile's max_global_active_jobs, counted across all provider pools. Both BigQuery job and Domo send admission enforce it under the same deployment lock. Adding a lower-cap profile permanently lowers this pilot's ceiling; increasing it requires separately reviewed reconciliation/migration. Profile aliases, credential rotation, a different pool or a different provider cannot escape the lower retained cap.

## Durability prerequisites

Every trusted source transaction explicitly pins synchronous_commit=on before checking the external epoch. Private SQL mutation helpers independently enforce that mode before source state changes. Source epoch/readiness and mutation checks reject PostgreSQL fsync=off or full_page_writes=off. The provisioning transaction checks these prerequisites too. This qualifies acknowledged local WAL durability only: storage/fsync truthfulness, failover replication guarantees and restoration rollback remain operator prerequisites. A database or role default synchronous_commit=off cannot silently weaken a committed provider permit.

Preexisting provider logins must be NOINHERIT, non-elevated and membership-free; provisioning rejects gateway schema CREATE, direct table rights or any executable gateway function outside that provider/context's exact allowlist. Re-provisioning compares retained descriptors/pools and never clears held evidence.
