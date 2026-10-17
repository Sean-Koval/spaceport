# Private Cube synthetic compatibility boundary

Task 5 candidate, 13 October 2026. Exact image metadata and offline asset/security checks are verified; the real runtime gate is **PENDING CI**. Configuration alone is not evidence of working production authentication, cache isolation, pending responses, source cancellation or egress blocking. This is an isolated synthetic reference, not a production deployment or completed gateway/UI.

## Immutable vendor image selection

| Image | OCI index digest | Selected linux/amd64 manifest |
| --- | --- | --- |
| cubejs/cube:v1.8.5 | sha256:47c298a6cc725b4840d42587ee6164b45fa195c34ddc9f9da8c98b63847684d3 | sha256:eb5cd9cfb257df0f9cf6dfab3aef394c3958f8f5faa0e55fdbd1ae49afb52b8c |
| postgres:17.11-bookworm | sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826 | sha256:66aafa11cf15800a3c94763f7e11d1e7b2e37e5e84bc4ce027cb6e1e4bfaf4df |
| nginx:1.30.5-trixie | sha256:9bf97bd7714f5e24c1ccd545ecb9eb5435cb6d109c97cebb15e7e455e0239edb | sha256:6e2a172ae5503c0df64cd3294b57c86394ced402016a8cc45436086fa51f5d02 |

The Cube metadata-only [actual CI run](https://github.com/Sean-Koval/spaceport/actions/runs/38447405398) read the tag once, captured the full index, then selected child manifests from that immutable index. Its linux/arm64 child is sha256:4196dafdf31d3f33955b85f2797dd8294efb667771ac37a865103abc9278e1c4; arm64 runtime compatibility is not tested. PostgreSQL and Nginx pins come from [official PostgreSQL image metadata](https://github.com/docker-library/repo-info/blob/master/repos/postgres/remote/17.11-bookworm.md) and [official Nginx image metadata](https://github.com/docker-library/repo-info/blob/master/repos/nginx/remote/1.30.5-trixie.md). The runtime script rechecks each selected platform digest and the actual packaged Cube version. No floating tag is sufficient to run this stack.

Cube 1.8.5 contains 1.8.4's member-expression SQL-evaluation guard. The private API additionally rejects expression objects and unapproved members before Cube normalization. Tagged source, not general current documentation, was inspected for [memory cache/queue support](https://github.com/cube-js/cube/blob/v1.8.5/packages/cubejs-query-orchestrator/src/orchestrator/QueryOrchestrator.ts), [cache modes](https://github.com/cube-js/cube/blob/v1.8.5/packages/cubejs-query-orchestrator/src/orchestrator/QueryCache.ts), [authentication/API scopes](https://github.com/cube-js/cube/blob/v1.8.5/packages/cubejs-api-gateway/src/gateway.ts) and [PostgreSQL statement timeout](https://github.com/cube-js/cube/blob/v1.8.5/packages/cubejs-postgres-driver/src/PostgresDriver.ts). Those source observations are not runtime acceptance.

The upstream Cube Docker package declares Apache-2.0; the repository has package/header MIT exceptions. PostgreSQL uses the PostgreSQL License. Nginx has its own upstream notices. The unmodified vendor images are preserved, with no license file removal. Their bundled/transitive notice inventory remains a Task 14 distribution gate. Original code is now licensed under Apache-2.0 ([LICENSE](../../LICENSE)); distribution of images and bundles remains blocked.

## Model bundles and operator catalog contract

`cube/bundles.json` is an operator-owned deployment catalog, never a manifest field or a client input. It declares distinct consumer and fixture-test endpoints, trusted private URLs, immutable bundle digests and mounted secret references. Task 6 owns the Python `CubeEndpoint` type and `BundleCatalog` implementation:

- `BundleCatalog.resolve(bundle_digest: str) -> CubeEndpoint` selects exactly one retained compatible operator deployment. Unknown, missing or changed bundles raise safe `BINDING_DRIFT`.
- `CubeEndpoint` carries the internal endpoint ID, trusted URL, bundle digest and secret reference. Request/manifest values cannot supply an endpoint URL or a signing credential.
- Consumer operations resolve only the consumer bundle/context. Publisher fixture execution resolves the separate fixture-test bundle/context; this does not confer consumer data access.
- Multiple deployments cannot silently replace a catalog digest. Keeping an older product usable requires retaining its compatible model bundle and binding.

Run `uv run --locked --offline python scripts/cube_bundle_digest.py` to recompute both digests. The digest is SHA-256 over exact bytes of `cube.js`, `security.js`, and the two sorted model filenames. Each file has a UTF-8 path, NUL, decimal byte length, NUL, then content. Both source model directories canonically mount as `model/`. Secrets, URLs and query-time values are excluded. Cube repeats this calculation at startup and refuses a changed bundle. The offline test checks catalog versus every deployed byte; any model/config change requires an intentionally regenerated digest and reviewed binding revision.

## Independent Cube enforcement

Only the private `/cubejs-api/v1/meta` and `/cubejs-api/v1/load` paths are accepted with gateway-signed HS256 context. The image's bundled `jsonwebtoken` verifier enforces algorithm, issuer `dpg-gateway`, audience `dpg-cube`, expiry and maximum age. `cube/security.js` independently checks the complete context shape, principal/tenant, positive policy revision, exact model digest, endpoint context and maximum 60-second lifetime. Missing, expired, future, malformed and mismatched context fails closed. Authentication/path/query/cache rejection uses the pinned release's exported `CubejsHandlerError` with HTTP 403 and the fixed message `Invalid gateway context`; it includes no original verifier error. The image gate exercises the actual Express/ApiGateway auth wrapper and requires that status/body, including rejection of missing/expired contexts. Context keys use the coordinated Decision names `principal_id`, `tenant_id`, `policy_revision` and `row_constraints`; clients cannot provide a Decision through the public gateway.

Every Orders query is ANDed with an exact tenant equality plus a mandatory access-group equality/in-list predicate, independently of client filters. An optional mandatory segment constraint permits same-tenant narrower entitlements. Watermark queries independently constrain their tenant. Cross-cube queries, joins, expressions, unapproved members/operators, dateRange shorthand, unsafe offsets and excessive limits are rejected. A 1,001-row limit is only the later gateway's over-limit sentinel; it does not prove cheap computation or permit returning an unmarked partial result.

Compiled model scope is the immutable bundle. Orchestrator scope is a stable SHA-256 of bundle, endpoint context, principal, tenant, policy revision and canonically sorted mandatory entitlements. Expiry/request IDs do not create new pools. Tests warm actual responses across both tenant orders and same-tenant entitlement changes. A cache failure blocks this gate; no unsafe profile is retained on the basis of configuration alone. Scope-growth/memory/connection soak remains Task 15 evidence.

Model refresh keys revalidate the committed source-load watermark, with a one-second refresh-key read interval. This avoids the release's default rolling ten-second refresh key changing during a bounded pending query. It does not set a query-time business watermark; actual pending/cache behavior still requires the real gate.

Load requests must explicitly use `cache: "must-revalidate"` or `cache: "no-cache"`. Missing, stale-if-slow and stale-while-revalidate profiles are denied before normalization. Tests use must-revalidate. Upstream absence of explicit cache-hit evidence must remain `unknown` in the eventual result envelope. The gateway's own cache remains off except principal-isolated idempotent replay.

## Private synthetic topology and source bounds

The Compose files are JSON, a valid YAML subset. Single-instance consumer and test Cube endpoints use an explicitly selected memory cache/queue, `devServer=false`, telemetry off and scheduled refresh off. There is no Cube Store, pre-aggregation, replica, live BigQuery or Domo service. Native SQL/GraphQL/development ingress is denied and native SQL/WebSocket ports are disabled. Only `edge` publishes `127.0.0.1:8080`. Its health route is available; all application paths return 503 until the later gateway/UI wiring task. Cube/database/probe ports are unpublished. Every runtime attachment uses one `internal: true` network.

Registry and source data use separate PostgreSQL instances, databases, volumes, randomly generated administrator passwords and distinct roles. File-backed Compose secrets retain host-file permissions. `scripts/cube_runtime_secrets.py` checks that the runner owns both the temporary and secret directories with mode 0700 before exclusively creating individual disposable 0444 files. Host traversal stays private while PostgreSQL UID999 can read its exact per-service read-only bind mounts after the official entrypoint drops privileges. The gate tests actual UID999 reads against the pinned image with no network/capabilities, then requires both real initializers to produce their expected non-elevated roles; it prints no contents. No Compose secret uid/gid/mode override or wider service mount is relied upon. Registry owner/app/audit-retention/source-reader roles are separate; no schema or grant writes are entrusted to the app. Demo source roles have SELECT only, no DDL/source-write/superuser/database-create/role-create/replication/RLS-bypass capabilities and no cross-schema privileges. The fixture-only observer has pg_read_all_stats solely for observing synthetic source work; that authority is never granted to the application or source readers.

The selected PostgreSQL driver sets server-side statement_timeout through its supported `executionTimeout: 25` configuration; source roles also default to 25 seconds and five-second lock timeouts. Real tests independently observe direct source SQLSTATE 57014 and actual Cube source activity stopping. Gateway/client waiting time is not proof of source cancellation. The 12-second fixture observes the release's real HTTP 200 Continue wait before eventual success; the separate 90-second fixture must be stopped at the source bound. Their functions/views exist only in the reviewed fixture schema, with separate role and bundle.

The five reference orders preserve nulls and numeric(38,2) amounts, including the unsafe-integer-size decimal. DST boundary orders live separately in the fixture schema and cannot change reference totals. Seed commits once and records actual transaction time in a persistent per-tenant watermark. Repeated queries/restarts must preserve that watermark. `fixtures/watermark.json` is only the deterministic unit/contract reference; it is never substituted for the running source-load time. Synthetic account IDs/grants match the separate local demo generator; no token/password belongs in these fixtures.

## Reproducible mandatory gate

From an exact reviewed source checkout, obtain pinned Python/dependencies with `uv sync --locked --dev`, then run:

```sh
bash scripts/verify_cube_runtime.sh
```

The script generates only disposable random local synthetic secrets in a private temporary directory, pre-pulls immutable vendor images, builds a disposable probe from the already-installed locked Python/package set without build-network access, starts the private stack with bounded readiness, and runs:

1. Actual pinned-image UID999 secret-read checks and both database initializers; actual-image Node security-context and HTTP auth-wrapper tests; selected outbound-negative socket probes from both Cube instances.
2. All `cube` cases inside the prepared private probe, requiring real authentication/rows/cache/pending/source-stop/roles/topology evidence with zero skips.
3. Database/Cube restart with retained volumes, then post-restart row and watermark comparisons.
4. The unchanged registry verifier and its bare full-project pytest with both genuine backends available.

For step 4 only, the CI operator obtains fixed-service private bridge IPv4s from exact current Compose project/run labels and healthy container/network inspection. The host test fixture re-inspects those exact identities and rejects stale/ambiguous/widened metadata; it accepts no arbitrary URL or published Cube port. This host-to-private-network operator verification is not publicly exposed application ingress. The preparation runner needs internet to obtain dependencies/images; runtime egress proof is collected from isolated Cube/probe processes. The separate disposable registry test harness has its already-authorized ephemeral loopback test port and is not part of the default demonstration topology.

Cleanup removes only this run's containers, network, synthetic volumes, local probe image and secrets, including an always-run CI fallback keyed by the run label. No registry login, repository secret, production credential, cache/artifact upload, paid service or deployment is used. `verify-cube.yml` is manual-only, contents:read, standard ubuntu-24.04, pinned actions and a maximum 20-minute job; the controller must review and dispatch it within the authorized cumulative allowance.

The script captures sanitized actual `/meta`, `/load`, pending, source-stop, watermark and topology evidence locally and prints only selected synthetic evidence. No guessed upstream fixture is presented as a release response. Before actual CI, all such evidence remains missing. A missing Docker daemon/stack is a blocker, never a PASS or skip.
