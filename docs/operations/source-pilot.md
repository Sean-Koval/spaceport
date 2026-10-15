# Reviewed-source pilot: setup and product workflow

**Organization activation is blocked pending the separately reviewed data-access security gate.** This connector candidate does not implement per-user Domo entitlement intersection, a user–agent delegation relationship model, or a complete PII field policy. Current product labels and shared source credentials are not proof of those controls. The next bounded slice must enforce revocable data-owner approval for explicitly reviewed non-PII shared-identity products; ultimate per-user access remains a separate provider-qualified mode.

The deployment and source-check commands below are a reference for after that security gate and separate operator authorization, not current rollout instructions.

This is an operator-run, bounded pilot for BigQuery or Domo. The commands below
are explicit actions, not an automatic installer. Offline commands never obtain
source tokens, inspect tables, run queries, create grants, or apply cloud changes.
A configuration receipt establishes configured intent. A passed fixture run
establishes isolated fixture evidence. Publication alone does not verify a live
source. Only a completed explicit consumer check supplies observed result evidence.

## 1. Prerequisites and identities

Bring an existing private GKE cluster/namespace, image registry, PostgreSQL registry,
IdP, trusted TLS certificates, and an approved network topology. An operator must
separately authorize deployment, existing-secret mounting, IAM/database grants,
source-data sharing, and the first paid/data query. These steps are not performed
by `pilot init`, `build`, `validate`, or `render`.

Three identities have different jobs:

- Employees sign in to **Spaceport** with an RFC 9068 bearer token from the configured
  issuer/audience. The CLI reads a private token file. Separate author and publisher
  principals have explicit per-policy tenant/group grants. Browser PKCE is a separate gate.
- BigQuery uses a linked GSA through GKE Workload Identity. Consumer and fixture
  runtimes have distinct GSAs, KSAs, input datasets/tables, Cube endpoints and bundles.
  Source JSON accepts no service-account key, credential JSON, token URI, ambient ADC
  file or endpoint override. Give submitting identities only the reviewed billing
  project job-creation permissions and read access to the exact input tables.
  The separate metadata observer GSA/DB login must have no `jobs.create` or
  `tables.getData`; it observes exact retained jobs with `jobs.get` and any separately
  reviewed cancellation permission. It cannot run user queries.
- Domo uses two separately approved OAuth service identities and isolated dataset
  UUIDs. Configure mounted client-ID/secret references, the client-ID hash, expected
  customer/user identity and exact dataset-definition digest. The API receives only
  fixture credentials; the worker receives only consumer credentials. Shared Domo
  service identity does not inherit employee PDP. Unproved PDP, scan-byte and remote
  cancellation guarantees remain unavailable.

The registry requires preexisting owner, app, audit-retention and legacy source-reader
logins. Source-only runtime/observer logins are additional prerequisites for explicit
source provisioning, not legacy migration prerequisites. Bootstrap creates no roles,
passwords or provider grants. Its source functions grant only the reviewed registry
capabilities to these preexisting, non-elevated logins. Runtime and observer logins
cannot alias app/owner/retention or cross provider/context capability partitions.
One BQ metadata observer login may cover both explicitly mapped contexts.

## 2. Installed CLI and private files

The gateway image contains `scripts` and the package builders. With the source
checkout's locked environment, enter a private working directory while retaining
the explicit reviewed module path:

```sh
SPACEPORT_REPO="$(pwd)"
. "$SPACEPORT_REPO/.venv/bin/activate"
mkdir -m 0700 pilot-work
cd pilot-work
export PYTHONPATH="$SPACEPORT_REPO"
python -m scripts.spaceport --help
```

After an operator has built and published an approved gateway image, use its
immutable digest and the operator UID/GID. This source change does not publish images:

```sh
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD:/work" -w /work \
  REGISTRY/spaceport@sha256:REVIEWED_DIGEST -m scripts.spaceport --help
```

The container command assumes the current directory is the private working
directory. The reviewed gateway image sets its fixed `/app` module path, so the
private working directory does not change imports. In the commands below, use the locked checkout
Python invocation or replace that prefix with this image invocation.

Build/tag recipes from the repository root (operator-controlled local builds):

```sh
docker build -f gateway/Dockerfile -t REGISTRY/spaceport:REVIEWED_TAG .
docker build -f publisher/Dockerfile -t REGISTRY/spaceport-publisher:REVIEWED_TAG .
# BigQuery only; use the reviewed runtime recipe from this source revision:
docker build -f cube/bigquery-runtime/Dockerfile -t REGISTRY/spaceport-bigquery:REVIEWED_TAG .
```

Publishing these tags with `docker push` is a separate operator-authorized registry
write. Capture the registry-returned immutable image digests, review them, and put
those digests in the deployment plan. An ephemeral CI smoke image is not a published
pilot image. Do not substitute a mutable tag in rendered workloads.

Create `pilot-work` with mode `0700`. Client configuration, token, operation,
acceptance sidecars and receipts must be owned by that process UID. Use `0600`
files; the client configuration's immediate parent must also be private. A container
running as the image's default UID 65532 cannot read arbitrary host-owned private
CLI files. Service ConfigMap/Secret mounts follow a separate read-only trust boundary.
Never paste a token into flags, shell history, a browser form, source JSON or logs.

## 3. Build and inspect the fixed product package

```sh
python -m scripts.spaceport pilot init --provider domo --directory input
# Or: --provider bigquery
# Edit input/setup-input.json locally with the approved descriptors and identities.
python -m scripts.spaceport pilot build --input-file input/setup-input.json --directory package
python -m scripts.spaceport pilot validate --config package/pilot.json
```

The template is fictional `weekly-account-usage` version `1.0.0`, owned by the
configured team. It returns `day: date`, `account: string`, and `usage_events:
decimal(38,0)` as an exact decimal string. Its grain is **daily UTC**, over the
seven-day half-open window `[2026-10-05, 2026-10-12)`. It sums producer-preaggregated
account/day event counts; each account belongs to one access group. BigQuery's
static source schema is `row_key`, `tenant_id`, `access_group`, `event_date`,
`account_id`, `usage_events`. Domo uses the corresponding reviewed daily dataset
columns. No schema discovery or arbitrary SQL interface exists.

The four finite Domo choices are north/all groups, north/core, north/all with
account `acct-a`, and north/core with account `acct-b` (empty). Fixed fixture
answers include acct-a counts `120` and `180`, and special-group acct-b count `50`.
The isolated dataset must also exercise the reviewed south-scope exclusion. Test
cases include denied capability, denied south tenant and an invalid date window.
All known answers and all required negative cases must pass under current policy.

`consumer_expected_rows` is the bounded, typed known answer for the operator's
consumer dataset. It may differ from fixture rows. It is private local input,
repinned into package/index/check-plan digests, and never printed by offline CLI.
If it contains organizational data, review the later deployment audience before
uploading the generated package ConfigMaps. Source-check result files are private.
Changing dates, metric semantics, grain, product version or request shape requires
new reviewed source/bundle/catalog bytes; editing presentation metadata cannot
silently expand the finite source catalog.

Default request limits are 100 rows, 65,536 result bytes, page size 100, seven days,
30 seconds, one operation per principal and two per deployment. BQ descriptors
also set explicit job count and byte budgets. Domo has finite send/concurrency
limits, not a promised scan-byte cap. Freshness is optional and `unknown` without
an atomic source-load checkpoint; no timestamp is invented from query completion.

Generated files pin source IDs/revisions/digests, policy seeds, immutable bindings,
fixture equivalence, retained model/statement catalogs and the check recipe.
`manifest.json` is the author input. `source-check.json` is a derivative plan for the
same exact published manifest/public provenance; it is not an authorization flag.

## 4. Render, then separately authorize deployment

Edit `package/deployment.example.json` into an operator-reviewed deployment plan.
Replace placeholder image digests, public CA file/hash, existing KSA names, exact
version-named Secret names/keys, private network CIDRs and network-review reference.
For BQ, also pin runtime/observer database logins, metadata-only observer GSA and
GKE metadata route. Gateway/Cube signing-key references must select the same Secret
version/key for their exact context; observers receive no signing key.

```sh
python -m scripts.spaceport pilot render --config package/pilot.json \
  --deployment-file package/deployment.json --directory rendered
```

Inspect `rendered/base.json`, `activation.json` and `render-status.json`. Base
Deployments have zero replicas. The complete activation bundle has one replica per
service; it is a usable next step after prerequisites and bootstrap. Migration and
provision CronJobs stay suspended in both files and have no automatic retry.
No Namespace, ServiceAccount, IAM binding, Secret, public Ingress or cloud apply is
created by rendering. `render-status.json` lists exact private Cube origins/services.
Existing private DNS and trusted certificates must route each origin correctly.

Native Kubernetes NetworkPolicy enforces packet CIDRs/ports, not hostname allowlists.
The approved network-level egress gateway/FQDN policy/private Google route must
already exist. Setting `HTTP_PROXY` is not a substitute: provider transports ignore
or reject ambient proxy/endpoint configuration. Choose the GKE metadata route that
matches the actual dataplane. No universal TCP/443 egress is emitted.

All source files and Secrets use per-file read-only `subPath` mounts. Generated
ConfigMaps have immutable content-derived names and bounded payloads. No parent
mount may shadow another file. Secret names include an explicit version. Rotation
requires a new reviewed version name and pod rollout; mounted subPath files do not
hot-reload. Retain old descriptors, source ledger and unresolved obligations.

After separate operator authorization, an example execution sequence is:

```sh
kubectl -n NAMESPACE apply -f rendered/base.json
kubectl -n NAMESPACE create job source-migrate-UNIQUE --from=cronjob/spaceport-migrate
# Wait for successful completion; investigate redacted failure before proceeding.
kubectl -n NAMESPACE create job source-provision-UNIQUE --from=cronjob/spaceport-provision
# Wait for successful completion, then explicitly authorize activation.
kubectl -n NAMESPACE apply -f rendered/activation.json
```

For an existing mounted operator environment, equivalent explicit commands are:

```sh
export GATEWAY_SOURCE_PILOT_CONFIG_FILE=/run/source/packages/pilot.json
python -m gateway.source_pilot_bootstrap plan
python -m gateway.source_pilot_bootstrap migrate
python -m gateway.source_pilot_bootstrap provision --expected-plan-digest REVIEWED_PLAN_DIGEST
```

Provisioning appends exact policy/identity/binding/source seeds. Replay does not
restore revoked grants, reactivate identities, recreate deleted identities or rewind
policy heads. Source capacity is tied to stable physical provider identity across
aliases, revisions and secret rotation. Startup/readiness use local assets and
registry state only. They perform no OAuth, metadata, table or paid query.

## 5. Author, fixture-test, review and publish

Create private `client.json` with exactly
`{"schema_version":1,"gateway_origin":"https://YOUR_CANONICAL_ORIGIN"}`.
An optional `--gateway` must equal this origin, including TLS/port. Obtain the
employee bearer token through the approved IdP flow and store it privately in
`author.token`; source credentials never enter this flow.

```sh
python -m scripts.spaceport product save --client-config client.json --token-file author.token \
  --manifest package/manifest.json --expected-revision 0 --receipt-file save.json
python -m scripts.spaceport product test --client-config client.json --token-file author.token \
  --draft-id DRAFT_ID --receipt-file test.json
python -m scripts.spaceport product preview --client-config client.json --token-file author.token \
  --draft-id DRAFT_ID --test-run-id TEST_RUN_ID --receipt-file preview.json
python -m scripts.spaceport product review --client-config client.json --token-file publisher.token \
  --draft-id DRAFT_ID --receipt-file review.json
python -m scripts.spaceport product publish --client-config client.json --token-file publisher.token \
  --draft-id DRAFT_ID --review-receipt review.json --reason-file reason.txt --receipt-file publish.json
```

Use actual IDs from private receipts. A save update also supplies `--draft-id` and
the current expected revision. The test POST has no body. Publish sends only the
reviewed digest, expected revision and reason. The server atomically rechecks current
exact-draft evidence; the client does not pretend to pin a chosen test-run ID across
a concurrent retest. The existing publisher UI/API/MCP share these same contracts.
Publication enables only the installed binding/policy path, never table browsing.

## 6. Explicit bounded consumer check and safe resume

A consumer with the configured tenant/group scope can now run the exact published
product. This is the first consumer-source check and needs separate authorization.
The preceding deployed product test also queries a real isolated fixture source and
may incur provider cost; authorize that fixture query action separately before test.
Offline validation and synthetic acceptance authorize neither live query action.

```sh
python -m scripts.spaceport source check --client-config client.json --token-file consumer.token \
  --plan-file package/source-check.json --operation-file check-operation.json \
  --receipt-file check-1.json --result-file check-result.json
```

The CLI describes the immutable published version before POST and compares exact
product/public provenance pins, then checks them again after completion. It never
submits source SQL. Exit `4` and `outcome_status=pending` mean accepted/running;
`observed=null` is not a pass. Run the **same command with the same operation file**
and a fresh receipt filename (for example `check-2.json`) to finish it. Once the
accepted execution sidecar exists, resume is GET-only and current credentials may
be refreshed; the server reauthorizes the known execution. Results are emitted only
when complete, preserving page identity and the latest freshness assessment.

Exit `0` for a completed source check means the typed returned rows matched the
configured known answer. It does not certify IAM, Domo PDP, source principal,
per-job caps, provider terminal state or cost from fields the public API does not
expose. Exit `1` is observed failure; `2` is unavailable/invalid input; `3` is an
uncertain mutation. If a POST response was lost before an accepted sidecar exists,
retry requires explicit `--retry-ambiguous`, the same private operation file and the
exact original token fingerprint. A refreshed token cannot safely repeat that POST
because idempotency is principal-scoped and this API has no principal-discovery
protocol. Corrupt/orphan files never trigger fallback POST. Do not delete operation
files or invent a new key to recover uncertainty.

## 7. Timeouts, unresolved provider work and read-only diagnosis

Logical execution failure, lease expiry, HTTP timeout and cancel acknowledgement
are not proof that provider work stopped. BigQuery observers reconcile exact known
job identities using retained source metadata. Domo has no documented equivalent
job reconciliation/cancel proof for this path: an ambiguous query may retain its
physical source slot indefinitely. Creating a new profile, rotating credentials or
resetting a ledger is not a recovery strategy.

Use normal authorized result/status GETs first. Deeper ledger diagnosis is an
operator-only registry operation, separate from public CLI receipts. With an already
approved read-capable operator connection, the following bounded read-only query
shows metadata for one known execution/test run. Do not print authority snapshots,
query plans, credentials or result rows into operator logs.

```sql
BEGIN READ ONLY;
SET LOCAL statement_timeout = '2s';
SELECT g.id, g.owner_kind, g.owner_id, g.source_id, g.source_revision,
       g.context, g.deadline_at, g.closed, g.settled_at,
       s.id AS session_id, s.sealed, s.seal_reason,
       d.state AS domo_state, b.job_id, b.state AS bigquery_state
FROM gateway.source_operation_groups AS g
LEFT JOIN gateway.source_sessions AS s ON s.group_id = g.id
LEFT JOIN gateway.domo_source_sends AS d ON d.session_id = s.id
LEFT JOIN gateway.bigquery_source_jobs AS b ON b.session_id = s.id
WHERE g.owner_id = :'known_execution_or_test_run_id'
ORDER BY g.id, s.id, b.job_id
LIMIT 25;
ROLLBACK;
```

A restart preserves registry obligations. Scale workloads down to stop new local
work, but retain registry state and observer recovery capability. Restoring a registry
backup or changing a deployment epoch requires separately reviewed fencing and
reconciliation; bootstrap rejects an unexplained epoch mismatch. Do not infer terminal
proof from a human status label or directly update ledger rows to release capacity.

See the [source lifecycle durability prerequisites](../specs/source-lifecycle-interface.md#durability-prerequisites)
for synchronous WAL settings, provider-role restrictions and the externally fenced
restore/reconciliation boundary. These registry checks do not establish storage or
failover guarantees by themselves.
