# Private synthetic demo operations

## Scope and current gates

This runbook describes a source-candidate implementation. No full-demo Docker, browser, MCP, offline or restore acceptance is claimed. Mandatory missing tests are BLOCKED and exit nonzero.

`verify_cube_runtime.sh` is a separate compatibility harness with online preparation; it is not the full offline demo. The complete demo layers `infra/compose.demo.yaml` over its base and test service definitions without changing the approved Cube verifier.

> Only synthetic PostgreSQL data and local demo identities are supported by this runtime entrypoint. Production OIDC, real customer data, cloud queries, remote deployment and public ingress require separate approval and verification.

## Acquisition versus offline operation

Supported target: operator-controlled Linux Docker Engine and Compose, linux/amd64. Mac and Windows Docker Desktop have not been validated for namespace-capture evidence. CPython 3.12.14, uv 0.12.23, Node 24.19.0 and npm 11.9.0 are fixed by project files; see [pinned versions](../compatibility/versions.md). Do not use unpinned fallback tools.

Preparation is the only phase that downloads anything: locked Python and npm artifacts, the matching Playwright Chromium and upstream images.

1. `uv sync --locked --dev`

   You should see: the locked Python environment installed, with no resolver changes.
2. `npm --prefix publisher ci --ignore-scripts --no-audit --no-fund`

   You should see: `publisher/node_modules` populated from the lock, no scripts run.
3. `(cd publisher && npm exec -- playwright install chromium)`

   You should see: the Chromium revision pinned by Playwright downloaded. Install the platform's browser system libraries separately if they are missing.

   > A failure here is a blocker, not permission to download during verification.
4. `make demo-prepare STATE_DIR=/absolute/new/private-state`

   You should see: the application and publisher images built, and their local image IDs, source-file hashes and lock hashes recorded under the state directory.
5. `make demo-up STATE_DIR=/absolute/new/private-state`

   You should see: the stack running, the UI at `http://127.0.0.1:8080`, and `/readyz` answering 503 until the first publication.

The publisher build layer installs npm 11.9.0 with scripts, audit and funding requests disabled, checks the tool version, then runs locked `npm ci`. Node 24.19.0 upstream bundles npm 11.17.0 ([release notes](https://nodejs.org/en/blog/release/v24.19.0), [tagged package metadata](https://github.com/nodejs/node/blob/v24.19.0/deps/npm/package.json)), so the builder must not assume the Node pin supplies the pinned npm. No host npm or permissions are changed. Actual image build acceptance remains required.

The recorded image IDs are local output identities, not vendor registry signatures. Dockerfiles separately pin official upstream image indexes. Builds abort if the source changes during preparation.

> Do not move a prepared source or state tree. After a source update, prepare a new isolated state.

`demo-up`, `verify-demo`, backup and restore perform no image pulls, builds or package installation; `--pull never` is explicit. `make demo-down` stops and removes the selected project's containers and network but retains volumes. No blanket prune exists.

## Topology, identities and secrets

Eight persistent runtime services: registry-db, demo-db, cube, cube-test, gateway, worker, retention and edge. Migration and bootstrap run as one-shot operator phases and are removed afterwards. Only edge publishes a host port, strictly on 127.0.0.1. The private application listener uses 0.0.0.0 inside its isolated network; this is not published ingress. Cube, SQL, PostgreSQL and developer ports never route through edge.

Gateway and worker run as non-root 65532, with read-only root filesystems, dropped capabilities and no-new-privileges. The gateway receives only app-role registry credentials, two scoped Cube signing references, an audit HMAC key, a cursor key and a hash-only auth directory. It never receives owner or admin credentials.

The retention process has the app role and a dedicated audit-retention role; only migration and bootstrap receive owner credentials. Synthetic source readers remain read-only and have a 25-second PostgreSQL statement timeout.

The state root is 0700 and owned by the invoking operator. Secret leaf files may be 0444 so non-root container identities can read individual read-only mounts; private host directory traversal protects them. The plaintext token directory is never mounted in a container.

A separate hash-only directory is mounted read-only, so an atomic file replacement is visible inside the running gateway. The auth replacement suite must show a 401 on the next request for the revoked key, continued access for another identity, and denied writes from the container.

> Never edit the hash file in place or expose token values in diagnostics.

Four external identities stay separate: demo-author, demo-publisher, demo-north, demo-south. Three credential-free fixture identities are server-owned; the negative identity has no grant. Bootstrap is idempotent only against its exact retained original configuration. It never resets changed grants, active flags or policy heads, and creates no draft, test evidence or published version.

## First publication and readiness

The UI is live while `/readyz` is 503. That is intentional: `/healthz` gates process startup, and readiness must not deadlock authoring.

1. As demo-author, load the revenue template, save a definition and run the isolated fixture source.

   You should see: the draft saved with a digest, and a test run with every fixture check passed.
2. As demo-publisher, review and publish the exact tested digest.

   You should see: the review screen confirm the published version and digest.
3. Call `/readyz`.

   You should see: 200. Readiness resolves the approved current product as demo-north, checks the exact Alembic head and app-role queries, and freshly authorizes live Cube metadata. A change to access can make it fail immediately.

The Playwright acceptance starts clean and can leave a later reviewed version after race and recovery cases. Known-answer tests pin 1.0.0; persistence additionally preserves the current head. Existing publications survive restart and are never reseeded to appear healthy.

Source-load freshness is the timestamp committed during source seeding, not query time or event recency. A demo older than 24 hours correctly fails freshness. A restored source retains its original watermark and may legitimately be stale.

> Do not change the watermark to hide stale data. Use a new synthetic stack for a fresh demonstration.

## Verification and evidence

`make verify-demo STATE_DIR=... PREREQUISITES_DIR=/absolute/retained-prerequisites` is 13-slot business verification, not final integrated acceptance. It omits the integrated `acceptance_security` slot. Default BigQuery live tests remain optional, NOTRUN, and do not count toward the synthetic demo.

In order, it:

1. Validates the separate compatibility receipt.
2. Runs a clean-first actual browser flow and requires its nonzero JUnit count with zero skipped, failed or error cases.
3. Checks real runtime dependencies, REST isolation, independent MCP clients, readiness and fault behavior, restart persistence, fresh-stack recovery, synthetic expiry, logging and auth revocation, and denied egress.

You should see: every slot reported with its evidence path. Missing modules, MCP mount, browser binaries, runtime or capture privileges return nonzero. No mock record satisfies an actual slot.

Reports live under the selected private state's evidence directory. `make evidence` verifies retained artifact hashes before summarizing. Reports identify actual or mocked classification, source and image versions, suite status and evidence paths.

Every accepted runtime slot must be unique, bind the exact source, application, edge and backend images (and integration mode where selected), include its exact JUnit file and complete collected testcase identities, and verify all evidence hashes.

Failure, error or skipped children are rejected even if a forged summary claims zero; totals and case identities must agree. Retained summaries also match the selected state's application and edge images.

Raw diagnostic files stay private and are never pasted automatically. Browser traces, screenshots and video are disabled for credential-bearing acceptance. Network browser evidence contains only approved origin and path metadata.

The expiry test uses an owner-only backdated fixture derived from an actual result and runs the real retention service; it does not claim 24 hours of elapsed runtime. App privileges and content immutability stay unchanged. The default 24-hour results and 30-day redacted audit retention require owner approval and encryption choices before production.

### Separate final acceptance modes

After pinned toolchain and locked Python setup, use a clean checkout and separate new private run roots:

```sh
umask 077
bash scripts/verify_gateway_integration.sh --backend /ABSOLUTE/PRIVATE_BACKEND_PARENT/run
bash scripts/verify_gateway_integration.sh --demo /ANOTHER_PRIVATE_DEMO_PARENT/run
```

The external parents must already exist, be owned by the operator and be mode 0700; each `run` child must not exist. Keep npm and browser caches and setup metadata outside run state, and clear ambient `PYTEST_ADDOPTS` and `PYTEST_PLUGINS`.

> Never reuse a prepared state path as the wrapper's run root.

The wrapper owns bounded preparation, evidence admission and exact-resource cleanup; it does not authorize privileged observation or system-library installation.

Both modes acquire locked MCP client dependencies before mandatory offline unit and contract execution. Backend mode records the complete current `postgres or cube` selection while genuine disposable backends are live. You should see: the full backend selection admitted against live disposable backends.

Demo mode invokes `verify_demo --integration` and requires 14 mandatory slots in each of two independent clean cycles. The second cycle reuses verified dependencies and image IDs without acquisition or restoring the first cycle's published database. Its narrower prerequisite receipt does not satisfy the separate complete-backend gate.

Final-source backend, composed demo and fresh repeat remain **NOT RUN**. The missing approved packet observer remains **BLOCKED** and nonzero even when independent business, security and operations suites succeed. Command documentation and source-side recorder coverage are not runtime acceptance. Verified cleanup, rendered-browser inspection and final independent review remain required.

### Required pre-offline backend receipt

Normal demo mode has 13 mandatory slots. `python -m scripts.verify_demo --integration ...` adds the fixed `acceptance_security` phase after consumer queries and before fault, auth and retention mutations, with its exact current collected e2e identities as a fourteenth required slot. Missing that phase cannot satisfy integrated acceptance.

`backend_compatibility` requires all cases collected from `tests/integration/test_registry.py -m postgres`, `tests/integration/test_cube_compatibility.py -m cube`, and the actual `test_operator_bootstrap_is_idempotent_and_never_publishes` case against the disposable registry fixture. Positive JUnit totals alone are insufficient: exact collected testcase identities, zero skips, errors or failures, artifact hashes, source inventory digest and the relevant pinned upstream reference and local image IDs must match.

To record the receipt:

1. Run `bash scripts/verify_cube_runtime.sh --demo-prerequisites STATE_DIR RECEIPTS_DIR` from the acquiring compatibility harness's authorized environment, while its disposable registry and Cube backends still exist. The demo wrapper selects this hook before backend cleanup.
2. It runs `python -m scripts.verify_demo --record-prerequisites --state-dir /absolute/prepared-state --prerequisites-dir /absolute/new-receipt`.

   You should see: the backends' inspected container, image and endpoint identities validated before and after the actual tests, and the receipt written to the new directory.

The recorder neither provisions backends nor acquires packages. Rerunning an old compatibility log cannot produce this receipt, and the hook's existence does not establish acceptance. Genuine final-source runtime execution remains **NOT RUN**.

The offline verifier only reads and copies this receipt and re-collects test identities without executing backend fixtures. Missing, stale, wrong-image or altered prerequisite evidence is rejected before the UI workflow. Retained evidence summaries repeat that binding check. No fabricated prerequisite record is supplied by this repository.

## Egress evidence and limits

The runtime uses an internal user-defined Docker network and `dns: [127.0.0.1]` for its DNS upstream. Docker's embedded resolver still resolves service names; external resolution must fail. Merely declaring internal networking or disabling telemetry is insufficient.

`observer_preflight()` performs no privileged action and reports `approved_scoped_observer_not_available`. No executable helper, sudo route or whole-host capture is installed or invoked. Startup, readiness and all business verification run unprivileged. Their results are retained even when the mandatory packet-observer slot is BLOCKED, so the aggregate stays incomplete instead of labeling narrow topology and socket checks as complete offline proof.

Source-only parser support uses exact selected namespaces and bridge, observed begin and end controls and DNS-attribution metadata. It rejects missing controls, forbidden attributed DNS, TCP or UDP traffic and capture loss, and admits supported lossless Linux loopback counter inequality rather than equating libpcap's platform-dependent receive and captured totals. These are synthetic parser regressions, not actual observation.

> No observer profile is approved. A manually supplied PASS cannot activate one.

The [scoped observer proposal](observer-proposal.md) states the additional assurance, engine, build and resolver checks, positive controls, drain and loss semantics, and the exact ephemeral action, data, path and privilege boundaries for a future bundled approval. It excludes unrelated runner control traffic and acquisition.

No host firewall or security changes or broader verifier elevation are authorized by these documents. Browser request-origin evidence proves requested assets and API calls are local; it does not establish host-wide browser socket silence.

## Backup and restore

```sh
make backup STATE_DIR=/absolute/source-state BACKUP_FILE=/absolute/new-backup.tar.gz
make restore STATE_DIR=/absolute/new-restored-state BACKUP_FILE=/absolute/new-backup.tar.gz DEMO_PORT=18080
```

Backup, in order:

1. Admits the exact stack, then quiesces edge, gateway, worker and retention. Recovery is registered before the first stop, including partial stops and timeouts.
2. Takes bounded registry-owner and source-admin custom-format dumps, preserving the actual source-load watermark.
3. Includes reviewed bundle, catalog, binding, policy and account configuration, source and image identities and SHA256 checksums. It contains synthetic results and authorization records but no generated password, token or key material.
4. Publishes the final archive atomically only after every component succeeds, then restarts only the services that were originally running and checks their state.

   You should see: a 0600 archive at `BACKUP_FILE`, and prior liveness and readiness recovered. A failed recovery is reported as BLOCKED, not as availability restored.

> At-rest encryption and off-host transfer are not implemented. Encrypt and store the archive privately under the operator's approved policy before any real deployment.

Restore, in order:

1. Refuses every existing state path, even an empty one, and requires the exact source and images already present.
2. Provisions fresh synthetic secrets and roles in new uniquely labeled volumes, persisting a provisional cleanup identity before any secret or image acquisition.
3. Restores both databases. Only the new source database may use pg_restore cleanup, because initialization seeded its own new schema. No working source environment is selected for overwrite.
4. Runs normal migration, bootstrap, liveness and readiness checks.

   You should see: the source watermark, published digest, evidence and policy data intact in the new state. Regenerated credentials and cursor keys mean old tokens and cursors are not recovery credentials.

A failure during preparation, pg_restore or restored startup attempts exact-label cleanup of only the fresh target, then records a final inventory. On verified empty cleanup, generated secret and token directories are removed.

If cleanup fails, the retained `cleanup-report.json` names the exact project and remaining IDs, or null where inventory was denied; use the retained cleanup identity and do not broaden targets.

Dumps stream to private bounded files, with child termination and reaping on stdout, stderr or time-budget excess. Normal command captures are bounded too. Archives admit each of exactly four regular raw USTAR headers before reading its payload; extension headers, duplicate or extra members, oversized declarations and expansion beyond the cumulative limit are rejected.

Dump and archive processing uses 64 KiB chunks, with at most 1 MiB each for configuration and checksum JSON. A unit memory regression exercises a 32 MiB output with less than 3 MiB Python traced peak; actual PostgreSQL process memory still needs runtime measurement.

> Never restore an untrusted archive. Checksums detect corruption, not a malicious authorized archive producer. The helper does not extract paths or execute supplied SQL outside the newly isolated synthetic databases.

## Shutdown, recovery and cleanup

Worker shutdown allows a bounded in-flight execution to finish. Crashes and timeouts can leave source reservations quarantined. This fail-closed state can exhaust capacity; elapsed gateway time is not proof of upstream completion. Independent reconciliation and source cancellation proof are not implemented.

> Do not force-release reservations as a convenience.

`demo-down` preserves data. To retire a disposable verification stack:

1. `python -m scripts.demo_runtime cleanup --state-dir ...`

   You should see: only the exact current-run-labeled container, network, volume and custom-image IDs removed, and an empty final inventory.

A removal error is ignored only after a successful independent inventory proves that exact resource absent; permission and daemon failures remain failures. Upstream shared images are never pruned. The older Cube harness's cleanup behavior remains its own acceptance gate.

## Verifier scope and bounded subprocesses

Mandatory collection, execution and receipt admission reject nonempty `PYTEST_ADDOPTS` and `PYTEST_PLUGINS` in either the ambient or the explicitly supplied environment. Child pytest processes receive `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`; the source-controlled fixed commands and configuration define the full case set. A subset selected through environment variables cannot become the expected set.

Suite stdout and stderr share a private 0600 log with a 16 MiB streaming limit and a fixed per-suite deadline. Collection uses a private temporary 4 MiB log and a 60-second deadline. Both use the integration runner's shared Linux descendant supervisor.

A timeout, excess output, interruption, orphaned descendant or failed descendant cleanup blocks admission. Raw output is never copied into the JSON receipt or console. Interruption, missing or malformed supervisor results, supervisor exceptions or unverified descendant cleanup stop the remaining dependent demo suites and mutations.

The final report retains a safe BLOCKED row; files potentially still writable by unverified descendants are not consumed or hashed. Ordinary failed tests may continue only after verified cleanup. These bounds cover process output; they are not a blanket disk quota on every file a test could create.

## Observability

Uvicorn access logging, PostgreSQL statement logging and Nginx access logging are disabled. The gateway formatter emits only timestamp, level and an allowlisted event code; it never interpolates arbitrary messages, arguments, exceptions or request and result bodies. Required redacted audit records remain in the registry with scoped reads.

This deliberately sacrifices raw stack traces in runtime logs. Diagnostic changes require redaction tests before use. Capture-canary tests must prove synthetic credentials, request canaries and raw large-decimal results do not appear in actual container logs.
