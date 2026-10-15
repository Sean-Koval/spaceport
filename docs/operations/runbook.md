# Private synthetic demo operations

## Scope and current gates

This runbook describes a source-candidate implementation. No full-demo Docker/browser/MCP/offline or restore acceptance is claimed. Mandatory missing tests are BLOCKED/nonzero. The existing `verify_cube_runtime.sh` is a separate compatibility harness with online preparation; it is not the full offline demo. The complete demo layers `infra/compose.demo.yaml` over its base/test service definitions without changing the approved Cube verifier.

Only synthetic PostgreSQL data and local demo identities are supported by this runtime entrypoint. Production OIDC, real customer data, cloud queries, remote deployment and public ingress require separate approval and verification.

## Acquisition versus offline operation

Supported target: operator-controlled Linux Docker Engine/Compose, linux/amd64. Mac/Windows Docker Desktop has not been validated for namespace-capture evidence. CPython3.12.14, uv0.12.23, Node24.19.0 and npm11.9.0 are fixed by project files. Obtain locked Python/npm artifacts and the matching Playwright Chromium in preparation. Do not use unpinned fallback tools.

1. `uv sync --locked --dev`
2. `npm --prefix publisher ci --ignore-scripts --no-audit --no-fund`
3. `(cd publisher && npm exec -- playwright install chromium)`; install the platform-supported browser system libraries separately if missing. Failure is a blocker, not permission to download during verification.
4. `make demo-prepare STATE_DIR=/absolute/new/private-state`
5. `make demo-up STATE_DIR=/absolute/new/private-state`

The publisher build layer explicitly installs npm11.9.0 with scripts, audit and funding requests disabled before checking the tool version and running locked `npm ci`. Node24.19.0 upstream bundles npm11.17.0 ([release notes](https://nodejs.org/en/blog/release/v24.19.0), [tagged package metadata](https://github.com/nodejs/node/blob/v24.19.0/deps/npm/package.json)); the builder must not assume that its Node pin supplies the separately pinned npm version. This tool acquisition is confined to preparation, never offline verification. No host npm or permissions are changed. Actual image build acceptance remains required.

Preparation builds the application and publisher images and records their exact local Docker image IDs, source-file hashes and frozen lock hashes. These local output identities are not vendor registry signatures. Dockerfiles separately pin official upstream image indexes. Builds abort if the source changes during preparation. Do not move a prepared source/state tree; prepare a new isolated state after a source update.

`demo-up`, `verify-demo`, backup and restore perform no image pulls/builds/package installation. `--pull never` is explicit. `make demo-down` stops/removes the selected project's containers and network but retains volumes. No blanket prune exists.

## Topology, identities and secrets

Eight persistent runtime services: registry-db, demo-db, cube, cube-test, gateway, worker, retention and edge. Migration/bootstrap run as one-shot operator phases and are removed afterwards. Only edge publishes a host port, strictly127.0.0.1. The private application listener uses0.0.0.0 inside its isolated network; this is not published ingress. Cube/SQL/PostgreSQL/developer ports never route through edge.

Gateway/worker run as non-root65532, with read-only root filesystems, dropped capabilities and no-new-privileges. The gateway receives only app-role registry credentials, two scoped Cube signing references, an audit HMAC key, a cursor key and a hash-only auth directory. It never receives owner/admin credentials. The retention process has the app and dedicated audit-retention role; only migration/bootstrap receive owner credentials. Synthetic source readers remain read-only and have a25-second PostgreSQL statement timeout.

State root is0700 and owned by the invoking operator. Secret leaf files may be0444 so non-root container identities can read individual read-only mounts; private host directory traversal protects them. The plaintext token directory is never mounted in a container. A separate hash-only directory is mounted read-only, so atomic file replacement is visible inside the running gateway. The actual auth replacement suite must show next-request401 for the revoked key, continued access for another identity, and denied writes from the container. Never edit the hash file in place or expose token values in diagnostics.

Four external identities remain separate: demo-author, demo-publisher, demo-north, demo-south. Three credential-free fixture identities are server-owned; the negative identity has no grant. Bootstrap is idempotent only against its exact retained original configuration and never resets changed grants, active flags or policy heads. It creates no draft, test evidence or published version.

## First publication and readiness

The first UI is live while `/readyz` is503. That is intentional. `/healthz` gates process startup; readiness must not deadlock authoring. An author loads the revenue template, saves a definition and runs the actual isolated fixture source. A distinct publisher reviews/publishes the exact tested digest. Then readiness resolves the approved current product as demo-north, checks the exact Alembic head/app-role queries and freshly authorized live Cube metadata. Changes to access can make readiness fail immediately.

The Playwright acceptance starts clean and can leave a later reviewed version after race/recovery cases. Known-answer tests pin1.0.0; persistence additionally preserves the current head. Existing publications survive restart and never get reseeded to appear healthy.

Source-load freshness is the timestamp committed during source seeding. It is not query time or event recency. A demo older than24 hours correctly fails freshness; do not change the watermark to hide stale data. Use a new synthetic stack for a fresh demonstration. A restored source retains its original watermark and may legitimately be stale.

## Verification and evidence

Ordinary `make verify-demo STATE_DIR=... PREREQUISITES_DIR=/absolute/retained-prerequisites` is 13-slot business verification, not final integrated acceptance. It first validates the separate compatibility receipt, then runs a clean-first actual browser flow, requires its nonzero JUnit count with zero skipped/failed/error cases, then real runtime dependencies, REST isolation, independent MCP clients, readiness/fault checks, restart persistence, fresh-stack recovery, synthetic expiry, logging/auth-revocation and denied egress. It omits the integrated `acceptance_security` slot. Missing modules/MCP mount/browser binaries/runtime/capture privileges return nonzero. No mock record satisfies an actual slot. Default BigQuery live tests remain optional/NOTRUN and do not count toward the synthetic demo.

Reports live under the selected private state/evidence directory. `make evidence` verifies retained artifact hashes before summarizing. Reports identify actual/mocked classification, source/image versions, suite status and evidence paths. Every accepted runtime slot must be unique, bind the exact source/application/edge/backend images (and integration mode where selected), include its exact JUnit file and complete collected testcase identities, and verify all evidence hashes. Testcase failure/error/skipped children are rejected even if a forged summary claims zero; totals and case identities must agree. Retained summaries also match the selected state’s application/edge images. Raw diagnostic files stay private and are never pasted automatically. Browser traces/screenshots/video are disabled for credential-bearing acceptance. Network browser evidence contains only approved origin/path metadata.

The expiry test explicitly uses an owner-only backdated fixture derived from an actual result and runs the real retention service; it does not claim 24 hours of elapsed runtime. App privileges and content immutability stay unchanged. The default 24-hour result retention and 30-day retention for old unreferenced redacted audits require owner approval/encryption choices before production. Source group/session-referenced allow audits remain protected, even after approval revocation; immutable owner/publication/source records and their referenced audits have no general erasure path in this slice.

### Separate final acceptance modes

After pinned toolchain/locked Python setup, use a clean checkout and separate new private run roots:

```sh
umask 077
bash scripts/verify_gateway_integration.sh --backend /ABSOLUTE/PRIVATE_BACKEND_PARENT/run
bash scripts/verify_gateway_integration.sh --demo /ANOTHER_PRIVATE_DEMO_PARENT/run
```

The external parents must already exist, be owned by the operator and mode 0700; each `run` child must not exist. Never reuse a prepared state path as the wrapper's run root. Keep npm/browser caches and setup metadata outside run state, and clear ambient `PYTEST_ADDOPTS` and `PYTEST_PLUGINS`. The wrapper owns bounded preparation, evidence admission and exact-resource cleanup; it does not authorize privileged observation or system-library installation.

Both modes acquire locked MCP client dependencies before mandatory offline unit/contract execution. Backend mode records the complete current `postgres or cube` selection while genuine disposable backends are live. Demo mode invokes `verify_demo --integration` and requires 14 mandatory slots in each of two independent clean cycles. The second cycle reuses verified dependencies and image IDs without acquisition or restoring the first cycle's published database. Its narrower prerequisite receipt does not satisfy the separate complete-backend gate.

Final-source backend, composed demo and fresh repeat remain **NOT RUN**. The missing approved packet observer remains **BLOCKED/nonzero** even when independent business/security/operations suites succeed. Command documentation and source-side recorder coverage are not runtime acceptance. Verified cleanup, rendered-browser inspection and final independent review remain required.

### Required pre-offline backend receipt

Normal demo mode has 13 mandatory slots. `python -m scripts.verify_demo --integration ...` adds the fixed `acceptance_security` phase after consumer queries and before fault/auth/retention mutations, with its exact current collected e2e identities as a fourteenth required slot. Missing that phase cannot satisfy integrated acceptance. `backend_compatibility` requires all cases collected from `tests/integration/test_registry.py -m postgres`, `tests/integration/test_cube_compatibility.py -m cube`, and the actual `test_operator_bootstrap_is_idempotent_and_never_publishes` case against the disposable registry fixture. Positive JUnit totals alone are insufficient: exact collected testcase identities, zero skips/errors/failures, artifact hashes, source inventory digest and relevant pinned upstream reference/local image IDs must match.

The separate pre-offline recorder is `python -m scripts.verify_demo --record-prerequisites --state-dir /absolute/prepared-state --prerequisites-dir /absolute/new-receipt`. Invoke it only from the acquiring compatibility harness's authorized environment while its disposable registry and Cube backends still exist. It validates their inspected container/image/endpoint identities before and after the actual tests. It neither provisions backends nor acquires packages. The source-side acquiring hook now exists as `bash scripts/verify_cube_runtime.sh --demo-prerequisites STATE_DIR RECEIPTS_DIR`, selected by the demo wrapper before backend cleanup. Genuine final-source runtime execution remains **NOT RUN**. Merely rerunning an old compatibility log cannot produce this receipt; the hook's existence does not establish acceptance.

The offline verifier only reads/copies this receipt and re-collects test identities without executing backend fixtures. Missing, stale, wrong-image or altered prerequisite evidence is rejected before the UI workflow. Retained evidence summaries repeat that binding check. No fabricated prerequisite record is supplied by this repository.

## Egress evidence and limits

The runtime uses an internal user-defined Docker network and `dns: [127.0.0.1]` for its DNS upstream. Docker's embedded resolver still resolves service names; external resolution must fail. Merely declaring internal networking or disabling telemetry is insufficient.

The current `observer_preflight()` performs no privileged action and reports `approved_scoped_observer_not_available`. No executable helper, sudo route or whole-host capture is installed/invoked. Startup/readiness and all business verification run unprivileged. Their actual results are retained even when the mandatory packet-observer slot is BLOCKED, so the aggregate remains incomplete instead of labeling narrow topology/socket checks as complete offline proof.

Source-only parser support now uses exact selected namespaces/bridge, observed begin/end controls and DNS-attribution metadata. It rejects missing controls, forbidden attributed DNS/TCP/UDP and capture loss, and admits supported lossless Linux loopback counter inequality rather than equating libpcap's platform-dependent receive/captured totals. These are synthetic parser regressions, not actual observation. No observer profile is currently approved; a manually supplied PASS cannot activate one.

The [scoped observer proposal](observer-proposal.md) states the additional assurance, engine/build/resolver checks, positive controls, drain/loss semantics and exact ephemeral action/data/path/privilege boundaries for a future bundled approval. It excludes unrelated runner control traffic and acquisition. No host firewall/security changes or broader verifier elevation are authorized by these documents. Browser request-origin evidence proves requested assets/API calls are local; it does not establish host-wide browser socket silence.

## Backup and restore

```sh
make backup STATE_DIR=/absolute/source-state BACKUP_FILE=/absolute/new-backup.tar.gz
make restore STATE_DIR=/absolute/new-restored-state BACKUP_FILE=/absolute/new-backup.tar.gz DEMO_PORT=18080
```

Backup first admits the exact stack, then quiesces edge/gateway/worker/retention. It takes bounded registry-owner and source-admin custom-format dumps, preserving the actual source-load watermark. It includes reviewed bundle/catalog/binding/policy/account configuration, source/image identities and SHA256 checksums. It contains synthetic results and authorization records but no generated password/token/key material. It publishes the final archive atomically only after every component succeeds. Recovery is registered before the first stop, including partial-stop/timeouts; only originally running services are restarted, their state is checked, and prior liveness/readiness must recover. A failed recovery is explicitly BLOCKED, not reported as availability restored. Archive files are0600. At-rest encryption and off-host transfer are not implemented; encrypt/store privately under the operator's approved policy before any real deployment.

Restore refuses every existing state path, even an empty one. It requires the exact source/images already present, provisions fresh synthetic secrets/roles in new uniquely labeled volumes, restores both DBs, then runs normal migration/bootstrap/liveness/readiness checks. Only the new source DB may use pg_restore cleanup because initialization seeded its own new schema. No working source environment is selected for overwrite. Regenerated credentials/cursor keys mean old tokens/cursors are not recovery credentials. Source watermark, published digest, evidence and policy data must survive. Failure during preparation, pg_restore or restored startup attempts exact-label cleanup of only the fresh target, then records a final inventory. A provisional cleanup identity is persisted before any secret/image acquisition, so cleanup admission does not depend on successful startup. On verified empty cleanup, generated secret/token directories are removed. If cleanup fails, the retained cleanup-report.json names the exact project and remaining IDs, or null where inventory was denied; use the retained cleanup identity and do not broaden targets.

Dumps stream to private bounded files, with child termination/reaping on stdout, stderr or time-budget excess. Normal command captures are bounded too. Archives admit each of exactly four regular raw USTAR headers before reading its payload; extension headers, duplicate/extra members, oversized declarations and expansion beyond the cumulative limit are rejected. Dump/archive processing uses 64 KiB chunks, with at most 1 MiB each for configuration/checksum JSON. A unit memory regression exercises a 32 MiB output with less than 3 MiB Python traced peak; actual PostgreSQL process memory still needs runtime measurement.

Never restore an untrusted archive: checksums detect corruption, not a malicious authorized archive producer. The helper does not extract paths or execute supplied SQL outside the newly isolated synthetic DBs.

## Shutdown, recovery and cleanup

Worker shutdown allows a bounded in-flight execution to finish. Crashes/timeouts can leave source reservations quarantined. This fail-closed state can exhaust capacity; elapsed gateway time is not proof of upstream completion. Independent reconciliation/source cancellation proof is not implemented. Do not force-release reservations as a convenience.

`demo-down` preserves data. To retire a disposable verification stack, `python -m scripts.demo_runtime cleanup --state-dir ...` removes only exact current-run-labeled container/network/volume/custom-image IDs. A removal error is ignored only after a successful independent inventory proves that exact resource absent; permission/daemon failures remain failures. A bounded final inventory must be empty. Upstream shared images are never pruned. The separate older Cube harness's cleanup behavior remains its own acceptance gate.

## Verifier scope and bounded subprocesses

Mandatory collection, execution and receipt admission reject nonempty `PYTEST_ADDOPTS` and `PYTEST_PLUGINS` in either the ambient or explicitly supplied environment. Child pytest processes receive `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`; the source-controlled fixed commands and configuration define the full case set. A subset selected through environment variables cannot become the expected set.

Suite stdout and stderr share a private 0600 log with a 16 MiB streaming limit and a fixed per-suite deadline. Collection uses a private temporary 4 MiB log and a 60-second deadline. Both use the integration runner's shared Linux descendant supervisor. A timeout, excess output, interruption, orphaned descendant or failed descendant cleanup blocks admission. Raw output is never copied into the JSON receipt or console. Interruption, missing/malformed supervisor results, supervisor exceptions or unverified descendant cleanup stop the remaining dependent demo suites and mutations. The final report retains a safe BLOCKED row; files potentially still writable by unverified descendants are not consumed or hashed. Ordinary failed tests may continue only after verified cleanup. These bounds cover process output; they are not a blanket disk quota on every file a test could create.

## Observability

Uvicorn access logging, PostgreSQL statement logging and Nginx access logging are disabled. The gateway formatter emits only timestamp, level and an allowlisted event code; it never interpolates arbitrary messages/arguments/exceptions or request/result bodies. Required redacted audit records remain in the registry with scoped reads. This deliberately sacrifices raw stack traces in runtime logs. Diagnostic changes require redaction tests before use. Capture-canary tests must prove synthetic credentials, request canaries and raw large-decimal results do not appear in actual container logs.
