# Registry-backed BigQuery serving runtime v1

This is the new `cube-bigquery-v1` pilot runtime. It does not select, modify, or
reuse the separately tested `cube/bigquery/` POSIX-ledger candidate. It requires
the source-lifecycle migration and separately provisioned, mapped source roles.
A configured runtime is not evidence of live source verification.

## Entrypoints and dependencies

- `node cube/bigquery-runtime/server.cjs`: the actual maintained Cube 1.8.5
  programmatic HTTP server with the guarded BigQuery driver subclass.
- `node cube/bigquery-runtime/reconciler.cjs`: bounded metadata-only maintenance.
- Exact tested package versions: Cube server/core/BigQuery driver **1.8.5**,
  Google BigQuery SDK **7.9.4**, Google auth library **9.15.1**. The pinned Cube
  image is the deployment/acceptance target; local package tests are reported
  separately. `pg` is required; no production dependency resolution happens here.

Both entrypoints read `GATEWAY_BIGQUERY_RUNTIME_CONFIG_FILE`, a regular bounded
file under `/run/source/config/`. No configuration parsing queries BigQuery,
requests a token, searches ADC files, or discovers datasets. Database connections
are lazy, bounded and TLS-verified using the configured private CA.

Server envelope (no unknown fields):

- `schema_version: 1`
- `profile`: the exact `BigQuerySource` JSON descriptor
- `deployment_epoch`: the external, operator-owned registry/source epoch
- `bundle_ref`: `{digest, context, root: "/run/source/bundle"}`
- `signing_key_file`: regular file under `/run/source/secrets/`
- `ledger`: `{host, port, database, user, password_file, ca_file}`

The observer has the same profile, epoch and ledger fields, but `bundle_ref` has
only `digest` and `context`; `observer_auth` contains its own explicit
`gke-workload-identity` and `expected_principal`. It has no signing key or model
mount. The submitting principal in a retained job remains the execution or
fixture principal, not the observer principal.

The server requires `NODE_ENV=production` and `CUBEJS_DEV_MODE=false`. It pins
`CUBEJS_DB_TYPE=bigquery`; a conflicting value is rejected. Only those Cube
variables and `CUBEJS_EXTERNAL_DEFAULT=false` are accepted. The maintained server
reads numeric `PORT` (default 4000); zero requests an ephemeral test port. Unix
sockets, invalid ports and the unsupported `CUBEJS_PORT` variable are rejected.
The source descriptor, not ambient Cube variables, selects the project, location,
principal and budgets. The maintained server's dotenv-capable import is followed
by another environment check before construction.

Credential files, ADC, metadata-host, proxy, universe, endpoint, global TLS trust
and SDK-debug overrides fail closed. Explicit maintained `Compute` credentials
use the exact approved service-account email and the BigQuery scope; they are
passed through the actual SDK's `authClient` option. Metadata token requests and
auth refresh recheck forbidden environment settings. A linked GKE KSA/GSA and
independently reviewed IAM are prerequisites. There is no credential creation,
IAM setup, OAuth grant, source activation, or network fallback in this runtime.

## Immutable model bundle

The retained bundle contains exactly `security.json` and `model/`, with 1–16
ASCII-named `.yml` files. Each file is at most 65,536 bytes; symlinks, extra files,
JS/Python/Jinja models and contextual interpolation are rejected. The digest is
SHA-256 over UTF-8 `spaceport-cube-bigquery-bundle-v1\0`, followed by every file in
UTF-8 relative-path order, each framed as `path + NUL + ASCII byte length + NUL +
exact bytes`. Models use `model/name.yml` paths in this framing.

The loader reads each file once. It retains the exact checked security/model
bytes, revalidates their hash, and supplies an immutable Cube repository. Cube
never reopens a mutable mounted model path during compilation. Compiler identity
is bundle plus source-definition digest; orchestration/queue/cache identity also
contains exact scope, immutable session and worker generation. Cache storage is
in-memory with a bounded 32-entry, 120-second lifetime. Losing a cache never
creates another provider intent or resets a source budget.

## Execution and source ownership

The maintained JWT verifier enforces a distinct audience and exact signed schema.
The raw HTTP load query's canonical digest is checked before normalization and
must equal the DB-retained BQ `source_attempt.prepared_digest`; a newly signed
body fingerprint cannot replace the accepted preparation. BQ uses a query-only
source preparation fingerprint while its three-key persisted preparation stays
unchanged, and endpoint/bundle/source identities remain independently pinned.
`source_attempt.scope_digest` must equal Python's canonical authorization-scope
fingerprint of the outer principal, policy revision, tenant and constraints.
Retained schema enforcement independently injects tenant/access-group filters.
The complete request ID must be exactly `sessionUUID.gGENERATION`; missing,
repeated, comma-joined, suffixed or mismatched headers are rejected.

`PostgresLedger` exposes only fixed SQL functions. Every call checks the external
epoch in the same short transaction, and a new submission permit is returned only
after acknowledged COMMIT. No transaction spans a provider RPC. The driver:

1. Checks current exact DB authority, looks up the stable semantic key, and admits
   only if no intent exists. SQL serializes competing first admissions.
2. Uses the immutable first job ID/configuration returned by SQL. A losing caller,
   retry, new worker generation, cache eviction or restart receives no submit right.
3. Checks current generation/lease/deadline and protected authority before every
   normal SDK RPC, and again after responses before admitting rows.
4. Makes at most one insert for its acknowledged new-intent permit, with only
   reviewed fields, fixed full byte reservation, original timeout, standard SQL,
   positional parameters, query cache off, automatic retries off, and no destination.
5. Treats conflicts, insert errors, lost replies and later 404s as lookup-only
   unknown outcomes. They never prove absence and never refund capacity or bytes.
6. Independently matches job ID/project/location/principal, exact SQL and typed
   parameter fingerprints and immutable configuration. It records matching DONE
   metadata before fetching results. Errors are terminal source evidence but
   never successful result evidence.
7. Seals only its exact current-generation attempt on runtime failure through
   `source_seal_attempt`. A fenced predecessor cannot seal its successor. The gateway still owns candidate
   result sealing/normalization/authorization and computed completion. Adapter
   success alone is not permission to release a source hold.

Default echoes and response-only anonymous destinations have a separate explicit
metadata profile. Hidden dataset names alone are insufficient: original no-write
submission provenance, exact identity/configuration, billing project, input
separation and reviewed no-permanent-write IAM must also hold. Destination
metadata is never dereferenced, logged or reused in another request.

Output is single-page and all-or-nothing: at most 1,000 rows, 1 MiB normalized
values, and 64 KiB parsed metadata. These are **post-SDK parsed-object bounds**, not
wire/allocation bounds. A container/heap memory ceiling remains mandatory; a
hostile large response can terminate a process while its durable holds survive.
BigQuery job timeouts are best effort, not a proven source-lifetime bound.

All inherited discovery/stream/export/write/preaggregation paths fail closed.
`testConnection` is static, and `/livez`, startup and static `/meta` have no source
or token I/O. SQL/native/GraphQL/developer routes and extra ports are unavailable.
Only the private authenticated `/meta` and POST `/load` data paths are admitted.

## Metadata-only reconciliation

A pass closes/seals at most 32 authoritatively expired/terminal-owner groups,
claims at most 8 due exact known jobs, and runs at most 2 metadata calls concurrently
with 5-second RPC deadlines. SQL retains observation leases and exponential
backoff capped at 60 seconds. Unknown metadata, 404, deadline, revoked data scope
and cancellation acknowledgement never release work. Matched late DONE metadata
can be recorded after data authority expires; only SQL's computed settlement
releases a closed/sealed fully terminal group. The initial observer does not
request cancellation and exposes no query, insert, result-row or arbitrary-list
operation. It needs `bigquery.jobs.get`, not jobs.create or tables.getData.

## Verification boundaries

- `node --test tests/node/*.test.cjs`: portable unit/legacy regression tests.
- Actual-package probes live in `tests/runtime/bigquery-serving-*.test.cjs`.
  They exercise the real driver, explicit Compute propagation, SDK serialization,
  startup and static HTTP metadata compiler. Unit SQL-boundary probes are labeled
  as such; they are not substitutes for real registry lifecycle tests.
- `tests/runtime/bigquery-serving.test.cjs` is the full actual-image/real-registry
  fixture entrypoint. It must use a freshly bootstrapped disposable registry and
  only fake the final provider transport. Missing fixture/packages are failures,
  never skipped acceptance.

No live-provider, GKE, IAM, deployment, spend, cancellation terminality, restored
registry recovery, or production qualification is established by synthetic tests.
Unknown obligations cannot be cleared by elapsed time, profile aliases, new
credentials, epoch changes or an operator assertion. Same-epoch database rollback
requires separately reviewed fenced recovery and independent provider evidence.

### Required private fixture contract

The host runs `python -m tests.integration.source_runtime_fixture bootstrap
--secret-dir /run/secrets --output /evidence/source-runtime-fixture.json` only
with `GATEWAY_SOURCE_TEST_AUTHORIZATION=private-synthetic-source-only`. This
creates the new run-private `gateway_source_test` database, applies production
migrations, provisions distinct consumer/fixture/observer logins through the
production provisioner, and issues five accepted source sessions through the
real policy/audit/lifecycle path. It refuses existing output/database objects.
No cloud resources or provider credentials are created.

The Node test receives a readonly fixture directory and only the submitting,
observer, and signing secrets. Host-only admin/app/owner secrets are absent.
Its final provider boundary is synthetic, but JWT verification, HTTP requests,
compiler, orchestration, driver, SDK serialization, PostgreSQL role grants and
source lifecycle are real. A fresh child environment uses `PORT=0`; inherited
legacy Cube/PostgreSQL/native-SQL settings cannot enter it. Guards allow only the
currently bound HTTP port and exact private PostgreSQL endpoint, revoke closed
HTTP ports, and deny external sockets, DNS, authentication, and unallowlisted key
reads before importing Cube/Google packages.

The reviewed host wrapper accepts exactly these ordered one-shot controls:
`revoke_queued`, `reclaim`, `close_main`, `close_zero`. Each control targets its
fixed seeded case. Reclaim atomically advances the real owner lease and session
fence in the application transaction, retaining preparation, provider IDs and
original deadlines/configuration. ACK files are atomically published, bounded,
readonly safe JSON without leases or credentials. Missing/extra/reordered
controls and malformed evidence fail the gate. Only the closed-schema final
result/checkpoint marker is forwarded, never arbitrary child output.

The acceptance cases cover main plus refresh job budgeting, Continue wait,
repolling, cache eviction, process restart, current-authority revocation, late
metadata-only observation, generation reclaim, zero-job settlement, and held
unknown submission after restart/404 lookup. Full row key sets are checked;
Cube 1.8.5 adds the exact duplicate base time key alongside its daily key.

For local development only, `tests/integration/source_runtime_local.py` can be
explicitly passed to pytest using an authorized fresh disposable PostgreSQL
server and exact installed Cube/Google packages via `NODE_PATH`. It is not part
of default test discovery and does not replace the required pinned-image gate.
`tests/integration/test_source_runtime_serving.py` is portable helper/contract
coverage, including Python-generated minimal contract parity with Node.

### Installable image and isolated packaging gate

Build from repository root with `docker build --network none --platform
linux/amd64 -f cube/bigquery-runtime/Dockerfile -t YOUR_LOCAL_TAG .`. The base is
the exact already-reviewed Cube image digest; this Dockerfile performs no package
installation or acquisition. Its default UID/GID is 65532 and it supports a
read-only root filesystem with a bounded writable `/tmp`. The image entrypoint
removes only the upstream `CUBEJS_DOCKER_IMAGE_VERSION` and
`CUBEJS_DOCKER_IMAGE_TAG` metadata, then invokes Node. All actual credential,
endpoint, and runtime overrides remain subject to the fail-closed guards. Keep
that entrypoint; choose `/cube/bigquery-runtime/server.cjs` (default) or
`/cube/bigquery-runtime/reconciler.cjs` as the container argument.

The pinned image's dependency tree is distinct from a fresh local install of the
same Cube version. Upstream's exact release lock includes BigQuery 7.7.0,
Google auth 9.10.0/10.2.1, and pg 8.18.0, whereas the initial local reproduction
resolved BigQuery 7.9.4, auth 9.15.1, and pg 8.16.3. The no-network image smoke
captures the actual resolved Cube, BigQuery, root Compute auth, SDK auth, pg and
Node versions in a closed receipt. Its major-family checks are compatibility
screens within this exact immutable base, not permission to substitute alternate
packages or image digests. Strict SDK serialization, metadata and full serving
acceptance must still pass using the installed image tree.

The host runs `python -m tests.integration.source_runtime_image_gate --image
RUN_PRIVATE_IMAGE --run-label RUN_LABEL` for its freshly built
`gateway-cube-HEX16-bigquery:local` image. It preserves the image entrypoint and
uses no network, a read-only root, no capabilities, no-new-privileges, bounded
memory/CPU/PIDs, and only a 64 MiB temporary filesystem. It imports the actual
packages lazily and executes the real Cube startup/health/retained compiler
component test with a labeled fake read-only ledger, zero provider/auth I/O,
and natural shutdown. Output is bounded and only a validated exact package
inventory receipt is forwarded; arbitrary stdout is discarded. This packaging
probe does not replace the separate genuine PostgreSQL full-serving gate.

First-intent admission reserves six seconds of headroom from the lesser of the
remaining wall-clock deadline and the retained monotonic deadline. If no positive
provider timeout remains, it fails before admission. This is operational headroom,
not a total database-latency guarantee; PostgreSQL still rejects overlong intents.
Existing jobs retain their original timeout/configuration and are never refreshed.
