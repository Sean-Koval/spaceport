# Offline BigQuery source controls

15 October 2026. **Executable offline source-control candidate; live and gateway activation remain disabled.** This is not a complete BigQuery serving connector. No credentials, Google project, paid query or cloud deployment was used. The original non-executable `gateway/adapters/bigquery.py` eligibility profile and live gate are unchanged.

## Shipped software and evidence boundary

`cube/bigquery/` retains a real subclass factory for the pinned Cube **1.8.5** BigQuery driver, a durable admission/seal engine, strict internal contracts and fictional reviewed model/binding assets. It has no HTTP service, source registration, gateway execution-context hook or gateway completion/release wiring.

The deterministic Node suite runs the production control implementation against real local files and real independent processes, including a SIGKILL case. Provider calls and the small upstream interface harness are simulated. The harness is explicitly **not** the packaged Cube class or its hydration implementation.

A separate mandatory image test, `tests/runtime/bigquery-package.test.js`, loads the actual pinned Cube superclass and installed Google SDK. It intercepts the SDK transport after request serialization, supplies synthetic provider responses, and forbids sockets, DNS, HTTP/fetch and credential-file reads. It checks the actual inherited query/hydration path and `jobs.insert` JSON, including top-level `configuration.jobTimeoutMs` and `configuration.query.maximumBytesBilled`. Missing/mismatched packages fail, never skip. An isolated local install from the official npm registry (`@cubejs-backend/bigquery-driver@1.8.5`, resolved Google SDK **7.9.4**, install scripts disabled) passed this guarded probe on 15 October 2026. The mandatory guarded immutable-image probe subsequently passed in [hosted run 18](../evidence/reviewed-connectors.md#hosted-gate-attempt-18-pass) on exact source `9c0f74e1dda3898f61b3b5cdd48d08fd3562daa3`. The npm-resolved patch version above is not a claim about the installed image patch version; the image probe asserts the supported SDK 7.x major. The script does not prove real provider behavior.

The actual SDK sends lowercase `positional` in query configuration. Its metadata helper reaches the intercepted transport with `jobs/<id>` (without a leading slash), while result reads use `/queries/<id>`. The fixture asserts these exact package boundaries. Synthetic metadata explicitly supplies uppercase `POSITIONAL`; that canonicalization is an assumed fixture behavior, **not evidence that Google accepts or normalizes the request**. The driver still rejects other metadata enum forms. Actual-provider qualification of this strict mapping, and provider-populated destinations, remains a live-activation prerequisite.

Failures emit only a fixed, closed checkpoint enum with `kind`, `checkpoint`, and `exit_status` fields. No error, request, SQL, credential, path or version is included in these receipts. Stages identify the last entered probe operation, not a proven root cause. The image harness pins the spec reporter so receipt prefixes survive the Node test runner unchanged. Full test output remains private to the diagnostic boundary.

The image harness runs this package test only with read-only candidate/test mounts and the existing bounded manual backend workflow. It does not add cloud egress or credentials.

## Internal API

All callers are reviewed server/operator code. These objects are not authorization received from a user, manifest, public request, arbitrary HTTP header or environment opt-in. The library does not verify signed capabilities or implement policy authorization.

- `initializeLedger(absoluteDirectory, {deploymentId, ledgerId, limits})`: explicit one-time offline preparation in a new directory beneath a private owner-controlled parent. Runtime never initializes missing storage.
- `new AdmissionLedger(absoluteDirectory, expectedIdentity)`: acquire exclusive storage and recover the complete journal. The expected identity must be pinned independently of the volume.
- `register(permit, nowMs)`: register one trusted execution or advance its fenced generation. Maximum authority horizon is 120 seconds. Returns a detached validated permit.
- `attempt(executionId, generation)`: return the exact current permit, rejecting unknown/fenced attempts.
- `admit(executionId, generation, semanticKey, firstConfig, nowMs)`: synchronously reserve before submission. Returns `{submit, job}`. `submit` is true exactly once for the semantic job key.
- `observe(executionId, jobKey, status)`: trusted driver-only observation (`running`, `cancel_requested`, `done`). The caller must independently verify provider identity/configuration before recording `done`. This method is not an attestation endpoint.
- `seal(executionId, generation)`: durably deny every future new-job admission, returning `{sealed, allJobsDone, jobs}`. Existing known jobs remain readable. Sealing cannot cancel a job or certify a lifetime.
- `status(executionId)`: detached bounded status/count/reserved-byte snapshot. It contains no source data or SQL.
- `close()`: stop local ledger use and release a healthy process lock. This does not cancel upstream work. Unknown jobs remain journaled. Poisoned storage retains its lock.
- `boundedDriverClass(UpstreamBigQueryDriver)`: construct the subclass using the explicitly supplied pinned base class. This is the offline dependency seam.
- `loadPinnedDriverClass()`: require the installed driver and exact version 1.8.5; fail if unavailable. It does not install packages or enable execution.
- `new Driver({source, ledger, scopeDigest})`: validated fixed source and immutable scope. No arbitrary constructor options are spread to the SDK. The subclass supplies explicit project/location/key reference, BigQuery-only scope, read-only/no-preaggregation/no-export intent and disabled automatic retry.

A permit contains execution UUID, generation, scope/query/binding/bundle/source digests, deadline, maximum jobs, per-job bytes and total bytes. Byte limits are canonical positive decimal strings bounded to signed int64. The stable source digest covers the configured project, dataset and location. The permitted execution identity cannot change its fixed budget or scope; higher generations do not reset accounting and deadlines can only tighten. Execution IDs remain stable across attempts.

The private future transport identity is `executionUUID.gGENERATION`. The library requires exact registration and matching scope/source digest; an arbitrary header cannot register itself. In a future serving integration, the gateway must bind the permit to its durable immutable execution and independently validate a signed exact identity. That integration is intentionally absent here.

## Durable storage and atomicity

The supported topology is **one Node process, one private local POSIX volume**. Windows, symlinked directories, non-private directories/files, unexpected owners, hard-linked state files, missing storage and identity mismatch fail closed. This is not a multipod or network-filesystem protocol.

Storage consists of:

- `manifest`: format, independently expected deployment/ledger UUID and fixed storage ceilings
- `journal`: length-framed, canonical JSON records with increasing sequence numbers and SHA-256 chaining
- `head`: exact committed sequence/hash/byte length
- `lock`: exclusive-create process lock, never automatically stolen

All mutations are synchronous: the single Node event loop gives admission, fencing, observation and sealing one total order, with no async yield inside a transaction. The write sequence is journal append → journal fsync → exclusive temporary head write/fsync → atomic rename → directory fsync → publish/acknowledge. Any write uncertainty poisons the instance. Recovery requires the full chain and head to match; it never silently truncates an incomplete tail or retries uncertain source submission.

The runtime also checks retained file identities, private modes, head/manifest bytes and journal nanosecond modification/change stamps before operations. Unexpected replacement, removal or mutation poisons the instance. A killed owner leaves a non-stealable lock; recovery requires an independently reviewed offline procedure, not deleting a lock to make a test pass. No automated recovery/repair CLI ships in this slice.

Ceilings are operator-fixed at preparation: journal at most 8 MiB, individual record at most 64 KiB, at most 1,000 executions and eight jobs per execution. Lower configured ceilings are enforced before append. Sequence count is also bounded. Unchanged observations do not append records; cancellation cannot regress to running and terminal status cannot regress. State is never automatically evicted or compacted. Reaching a ceiling blocks work. Recovery/compaction/retention and detection of a coordinated rollback to an older valid volume are not implemented. Restores or storage replacement require reconciliation and must not be presented as safe automatic restart.

## Logical jobs, budgets and provider evidence

The job key hashes stable query and parameter fingerprints plus source identity. It deliberately excludes remaining time and generation. The deterministic provider job ID additionally binds the stable execution and deployment/ledger identity.

First admission reserves the full per-job byte cap and persists submission intent. The retained configuration includes project/location, query/parameter hashes, byte cap and first timeout. Subsequent identical polls reuse **that first configuration and timeout** even as the deadline decreases. Nothing is refunded after provider errors, cancellation, success or unknown submission. Main data, inline checkpoint computation and any separate Cube refresh/auxiliary jobs share the same execution budget; different SQL consumes another job slot. V2 has no separate watermark load.

Every admitted submission sets `maximumBytesBilled`, a numeric SDK `jobTimeoutMs`, fixed location, deterministic job ID, positional standard SQL and disabled query cache. The time limit is the smaller of the configured request and then-remaining authority; later polls do not change it. Any initial creation error or conflict triggers lookup of the exact existing job, never a new ID or automatic resubmission. A missing or mismatched job remains unknown forever unless independently reconciled.

Metadata must match the job ID, project, location, exact SQL/parameter fingerprints, first timeout and byte cap. The driver uses an allowlist for execution configuration fields. In particular, destination tables, create/write disposition, scripts/sessions, connections, UDFs and unreviewed fields fail closed. **Provider-generated anonymous destination metadata is also currently rejected.** A later actual-provider qualification must distinguish any legitimate generated fields through a reviewed exact mapping; weakening checks to make a smoke pass is not permitted.

Only independently matched metadata in provider `DONE` state records terminal source work. `errorResult` is terminal but fails the result. Cancellation acknowledgement records intent only. All provider messages are replaced with a fixed safe error. The journal stores fingerprints and safe configuration/status, not SQL, parameters, results, credentials or raw errors.

Generation/deadline are checked before and after awaited metadata/result work. Each run also retains a monotonic deadline derived once from its initial remaining authority (at most 120 seconds), tightened from the original clock pair when authority shrinks; wall-clock rollback cannot extend polling. SDK calls are lazy and cannot begin after their local deadline check fails. Cancellation has a separate one-second best-effort response allowance. Neither local timeouts nor `jobTimeoutMs` prove source termination.

## Query/result restriction

The only serving entry point is the inherited reviewed query path through overridden `runQueryJob`. It accepts a bounded single SELECT generated from retained reviewed Cube models, string positional parameters, standard SQL and the expected integer-wrapper option. Its lexical SELECT/semicolon checks are defense in depth, **not an arbitrary SQL authorization mechanism**.

Streaming, broad schema discovery, metadata SQL, table/schema creation, upload/download/export, pre-aggregation and inherited storage extract helpers are disabled. The exact inspected BigQueryDriver and BaseDriver entry points are covered by negative tests. Startup connection checks are also disabled rather than issuing unbudgeted discovery. An actual deployment therefore still needs a reviewed supported startup and control integration.

`maxMetadataBytes` and `maxResultBytes` are **post-SDK parsed-object admission bounds**. They do not prove bounded allocation of provider response bytes by the Google SDK. Results use manual pagination with a limit-plus-one sentinel; an extra page, excessive rows/bytes, unsupported nested values or binary floating values fails with no partial release. Existing gateway exact decimal/date/freshness normalization remains a separate requirement. Actual SDK hydration and provider wire limits need distinct evidence.

## Retained semantic assets

`model/sales.yml` uses fictional `spaceport-fixture.reviewed_sales` tables. The source amount contract is NUMERIC; event/checkpoint values are TIMESTAMP. Revenue explicitly formats the exact SUM as STRING before crossing JavaScript. Actual BigQuery/Cube SQL and hydration are unverified.

The same query carries `WarehouseSales.source_loaded_at` as an explicit UTC RFC3339 STRING, using `FORMAT_TIMESTAMP(..., 'UTC')`. The checkpoint subquery groups by tenant, errors on duplicate checkpoint rows, and the outer left join errors for a missing/null checkpoint rather than multiplying or silently removing data. The producer must atomically publish source rows and that tenant checkpoint. Those source semantics are operator prerequisites, not facts established by naming a column.

`binding.json` uses the shared reviewed v2 schema; `security.json` matches its exact members, tenant/access roles and hidden checkpoint. `source.json` retains the fictional reviewed source identity. `assets.cjs` hashes framed filenames and bytes of a fixed allowlist covering the controls, digest algorithm, model/security/source. `bundle.json` and the binding pin that candidate digest. They contain no endpoint and are not registered in runtime catalogs. The old PostgreSQL bundle bytes and digest algorithm remain unchanged.

## Verification commands

```sh
node --test tests/node/bigquery-*.test.cjs
.venv/bin/python -m pytest tests/contract/test_bigquery_controls.py tests/contract/test_bigquery.py -q
```

Inside the separately approved immutable Cube image only:

```sh
GATEWAY_BIGQUERY_CONTROLS_DIR=/cube/conf/bigquery node --test --test-reporter=spec tests/runtime/bigquery-package.test.js
```

Required regressions cover real cross-process exclusion, SIGKILL/unknown intent, restart/corruption/missing storage, same-job replay, all-job budget, atomic seal/late admission, generation/deadline changes, source-write metadata rejection, provider mismatches, zero-call disabled paths, limits, redaction and unchanged live gate. The complete repository unit/contract and genuine PostgreSQL/Cube backend gates remain required for the integrated candidate.

## Later serving integration

A future reviewed gateway hook must bind permits to durable execution claims; an authenticated private control path must seal admission and independently observe every admitted job terminal before the adapter may return `Completed`. This is required because current gateway `Completed` releases its source reservation. No result arrival, cancelled HTTP wait, elapsed deadline or best-effort cancellation may substitute for that proof. Live-source lifetime capability fields remain unproven.

### Primary-source compatibility references

- [Pinned Cube BigQueryDriver](https://github.com/cube-js/cube/blob/d01994a71e64f2e8074ee04fefa3a0d9f369cc81/packages/cubejs-bigquery-driver/src/BigQueryDriver.ts)
- [Pinned Cube BaseDriver](https://github.com/cube-js/cube/blob/d01994a71e64f2e8074ee04fefa3a0d9f369cc81/packages/cubejs-base-driver/src/BaseDriver.ts)
- [Pinned query request propagation](https://github.com/cube-js/cube/blob/d01994a71e64f2e8074ee04fefa3a0d9f369cc81/packages/cubejs-query-orchestrator/src/orchestrator/QueryCache.ts)
- [Google SDK v7.7.0 job construction, the declared minimum dependency rather than confirmed image version](https://github.com/googleapis/nodejs-bigquery/blob/v7.7.0/src/bigquery.ts)
- [Google job byte-limit and best-effort timeout semantics](https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/Job)
