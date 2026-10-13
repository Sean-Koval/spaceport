# Data Product Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-hosted, account-free synthetic demonstration in which publishers release governed data products and two independent agent clients discover and query the same immutable contract.

**Architecture:** A Python gateway and PostgreSQL registry own identity, policy, publishing, audit, durable execution and result access. A private, separately deployed Cube Core executes reviewed semantic models against synthetic PostgreSQL data; a React publisher and a fixed four-tool MCP interface share the gateway services. BigQuery receives contract tests and configuration documentation; Domo remains disabled pending its separate gates.

**Tech Stack:** Python, FastAPI, Pydantic, SQLAlchemy, Alembic, psycopg, HTTPX, PyJWT, official MCP SDKs, PostgreSQL, Cube Core, React, TypeScript, Vite, Vitest, Playwright, pytest and Docker Compose. Exact compatible versions, image digests and protocol revision are selected and locked in Tasks 1, 5 and 11, not guessed here.

**Spec:** [Approved design](../specs/2026-10-13-data-product-gateway.md), approved 13 October 2026. Original Library attachment: `libfile_00f39001b7448191bc72de9e7a7e60d8`. The adjacent spec is byte-identical to the source, SHA-256 `670156876b59c82c262b6f98b1d12905bec6d41f32a1db0d92c94935517d4895`; distribute both attachments together. Executors must read both documents.

## Global Constraints

The following requirements are copied verbatim from the approved spec. All tasks inherit them.

- “No vendor-hosted control plane or paid account is required for the demonstration.”
- “The default demonstration needs no external account, model provider or cloud service.”
- “Excluded: ETL, general metric compilation, raw SQL tools, cross-product joins, arbitrary formulas, dashboards, chat-agent orchestration, workload scheduling, source writes, production credentials and public deployment.”
- “Semantic models are reviewed, versioned deployment assets, not executable code uploaded through the editor.”
- “Pin exact images, SDKs, protocol versions and digests during implementation after compatibility checks. Do not choose invented or floating ‘latest’ versions in a release.”
- “Credentials are external secret references configured by operators, never manifest values.”
- “Published manifests, test evidence and binding revisions cannot be edited in place.”
- “Access-policy changes take effect immediately and are independently versioned.”
- “A synchronous wait lasts at most five seconds, then returns the execution ID.”
- “The worker polls pending Cube responses within a 120-second execution deadline with bounded backoff.”
- “Initial defaults are 1,000 rows and 1 MiB per execution, 200 rows per page, two concurrent executions per principal, ten per deployment and a 366-day maximum date range for date-based products.”
- “Over-limit results fail explicitly rather than returning an unmarked partial answer.”
- “Default result retention is 24 hours and redacted audit retention is 30 days; operators must approve production retention and storage encryption.”
- “The synthetic demo must run with outbound network access blocked after images and dependencies are obtained.”
- “A successful synthetic demo satisfies none of these automatically.” [Production-readiness gates]

## Review Focus

These five failure classes need explicit tests beyond the happy-path product story; their owning tasks include them below.

1. Two publishers race while a draft or policy changes: only the exact tested digest and current policy may publish; no partial pointer update. Tasks 2 and 9.
2. Revocation occurs after enqueue, success, or cursor issuance: neither a worker nor a result/cache/cursor may bypass the new decision. Tasks 4, 7 and 8.
3. DST boundaries, nulls, zero rows and high-precision decimals: preserve declared meaning and freshness evidence without coercion or invented timestamps. Task 6.
4. Worker death or audit failure surrounds an upstream request: bounded recovery must preserve concurrency accounting and fail closed without claiming upstream cancellation. Tasks 4 and 7.
5. Malicious descriptions, metadata links, tokens and oversized payloads: treat metadata as inert text, never fetch supplied links, reject forged identity and keep secrets out of errors/logs. Tasks 1, 3, 10 and 12.

---

## Approval, scope and execution

This plan is for review. No product code, repository, dependency installation, deployment or test execution has been performed by preparing it. Implementation starts only after plan approval and execution-method selection. Recommend **subagent-driven** execution because policy, Cube enforcement, publication evidence and asynchronous result access require independent interface/security review.

Use a new, isolated project workspace selected at execution time. Do not use an unrelated saved environment or assume an offline computer is available. Create an isolated worktree if starting inside an existing repository. No public repository, release, production credential, live BigQuery query or license selection is authorized by this plan. Apache 2.0 for original code remains an owner decision; preserve upstream notices and mark distribution blocked until decided.

The work packages form one dependent vertical slice, not six independent products. Keep one plan so a reader can follow the common security contract. Treat each task below as a separate acceptance/review gate; split into standalone subsystem plans only if scope changes to independently deployable products.

## File map and ownership

All paths below are relative to the future project root. Every listed product file is **Create** unless a task explicitly says **Modify**. Empty package markers may be created beside Python modules; no generated clients are edited by hand.

- `gateway/contracts/{manifest,identity,query,execution,errors}.py`: strict wire/domain types; `gateway/contracts/canonical.py`: canonical bytes/digests; `gateway/contracts/clock.py`: injectable time.
- `gateway/registry/{models,repository}.py`, `migrations/versions/0001_registry.py`: immutable records, version resolution and transactions.
- `gateway/auth/{settings,tokens,principals}.py`: deployment-mode checks and verified identities.
- `gateway/policy/{service,audit}.py`: current policy decisions and mandatory audit writes.
- `gateway/adapters/{base,cube,normalize,bigquery}.py`: binding validation, constrained query building, REST polling and source conversion.
- `gateway/execution/{service,queue,worker,results,cursors,retention}.py`: durable jobs, capacity, deadlines and principal-bound output.
- `gateway/publishing/{service,test_runner}.py`: draft validation, isolated test evidence and lifecycle transitions.
- `gateway/api/{app,products,publishing,executions,admin}.py`, `gateway/mcp/server.py`: thin transports, never independent policy implementations.
- `publisher/src/api.ts`, `publisher/src/{auth,App}.tsx`, `publisher/src/pages/{Catalog,Product,Editor,Review}.tsx`: publisher UI.
- `cube/{cube.js,model/orders.yml,model/watermark.yml}`, `infra/{compose.yaml,compose.test.yaml,postgres/init.sql,edge.conf}`: private runtime and reviewed demo models.
- `fixtures/{orders.csv,watermark.json,products/revenue.json,policies.json,accounts.json,adapter/bigquery.json}`: synthetic, deterministic examples. Tokens and passwords never belong here.
- `clients/{python_agent.py,typescript_agent.ts}`: two separate deterministic agent-client implementations using official SDKs; no model service.
- `tests/{unit,contract,integration,e2e,security}/`, `scripts/`, `docs/{operations,compatibility,evidence}/`: verification, repeatable operations and evidence.
- `pyproject.toml`, `uv.lock`, `publisher/package.json`, `publisher/package-lock.json`, `clients/package.json`, `clients/package-lock.json`, `Makefile`, `.env.example`, `.gitignore`, `README.md`, `THIRD_PARTY_NOTICES.md`: locked development/runtime entry points and documentation.

Only the owning task changes its files. Task 1 owns contracts; later schema changes require an explicit coordinated contract patch and regression test. Tasks 5 and 14 own Compose at different times, never concurrently. The integration owner resolves dependency locks and transport wiring after each reviewed merge.

## Shared interface decisions

Task 1 defines the following names and fields so parallel workers do not invent competing interfaces. All Python service operations are `async`; pure validation/conversion functions are synchronous. Service methods raise `GatewayError(code, request_id, safe_message)`; transports redact details.

- `Principal`: `id`, `subject`, `service_client_id`, `kind` (`user|service|demo`). Tenant, roles and entitlements come from server-managed records, not JWT role claims or request arguments.
- `Manifest`: `schema_version`, `product_id`, `version`, `name`, `description`, `examples`, `owner`, `domain`, `links`, `inputs`, `outputs`, `filters`, `sort`, `grain`, `units`, `timezone`, `date_boundaries`, `aggregation`, `freshness`, `binding_id`, `binding_revision`, `adapter_revision`, `policy_id`, `classification`, `redaction`, `fixtures`, `content_digest`, `model_bundle_digest`. Status lives elsewhere. Reject unknown fields recursively. A digest is SHA-256 of RFC 8785 canonical JSON excluding `content_digest`; represent decimals as strings before canonicalization.
- `QueryRequest`: `product_id`, optional `version`, `inputs`, `filters`, `sort`, optional `idempotency_key`. Filters and sort name manifest fields only. Date ranges are half-open `[start,end)` in the declared IANA timezone, with at most 366 local calendar days. Manifests must declare those boundaries explicitly.
- `PinnedProduct`: immutable `manifest`, `binding_digest`, `version_id`. `Decision`: `principal_id`, `policy_revision`, `tenant_id`, `row_constraints`, `visible_fields`, `limits`; caller cannot construct one through an external endpoint.
- `ExecutionSpec`: `principal_id`, `pinned_product`, `canonical_query`, `canonical_hash`, `policy_revision`, `context` (`consumer|fixture_test`), `request_id`. Its internal creation is validated; it is never accepted directly from clients.
- `AdapterPoll`: `Pending(retry_after_seconds)` or `Completed(raw_rows,source_watermark,cache_status)`; `PreparedQuery`: operator-selected endpoint ID, pinned bundle digest and JSON Cube query. Source URLs are never request or manifest values.
- `ExecutionView`: `execution_id`, `state`, `accepted_at`, optional `started_at`, `completed_at` and `error`; states are `accepted|running|succeeded|failed|timed_out|expired`. `ResultEnvelope` adds product/version/digest, typed schema, rows, units, grain, effective range/timezone, authorized provenance, row count, `truncated`, source watermark, freshness assessment, cache status, warnings and optional next cursor. Cache status is `hit|miss|unknown`; absence of upstream evidence means `unknown`.
- Stable errors: the spec's `UNAUTHENTICATED`, `NOT_FOUND_OR_FORBIDDEN`, `INVALID_INPUT`, `BINDING_DRIFT`, `UPSTREAM_UNAVAILABLE`, `RESOURCE_LIMIT`, `EXECUTION_TIMEOUT`, `FRESHNESS_UNKNOWN`, `STALE_DATA`; add `CONFLICT` only for idempotency/optimistic-write conflicts. Expired results are state `expired`, without rows.
- `Clock.now() -> datetime` and `Clock.sleep(seconds: float) -> Awaitable[None]` make five-second waits, deadlines and expiry deterministic in unit tests.
- Supporting types are defined by their owners: Task 2's `UnitOfWork` exposes `session: AsyncSession`, `lock_product(product_id: str) -> ProductRow`, `lock_draft(draft_id: str) -> DraftRevision` and `lock_policy(policy_id: str) -> PolicyRevision`; Task 4's `AuditEventInput` carries its listed redacted fields; Task 6's `CubeEndpoint` holds an internal endpoint ID, trusted URL, bundle digest and secret reference; Task 8's `RetentionCounts` contains deleted result/audit counts; Task 9's `VersionDiff` contains before/after digest plus changed field paths and values filtered by access. The corresponding modules export these exact names.

Implementation choices above refine unspecified details; they do not widen source or tool permissions. Default query/result caching in the gateway is off except idempotent replay; results remain stored and isolated. Cube caching must pass actual tenant/principal isolation tests or be disabled by a verified supported mechanism. A release cannot silently retain an unsafe cache.

The reference `revenue` product is daily grain, USD decimal(38,2), `America/New_York`, summed order amounts. Seed north orders `(n1,2024-03-09T12:00:00Z,A,10.10)`, `(n2,2024-03-10T07:00:00Z,null,20.20)`, `(n3,2024-03-11T12:00:00Z,A,9007199254740993.01)` and south orders `(s1,2024-03-09T12:00:00Z,A,100.00)`, `(s2,2024-03-10T07:00:00Z,B,0.01)`. The three-day north total is `9007199254741023.31`; south is `100.01`; an out-of-range query returns no rows. Add separate exact-boundary fixtures for DST assertions. Unit/contract fixtures record watermark `2024-03-12T00:00:00Z` against clock `2024-03-12T01:00:00Z`; max age is 24 hours. The running demo records its actual source-load watermark once during seed commit, never at query time, and describes freshness as source-load freshness rather than event recency.

## Dependency order and test evidence

Task 1 → Tasks 2 and 5 in parallel → Task 3 → Task 4. Task 6 follows 5 and can run alongside 3–4. Task 7 joins 2/4/6; Tasks 8 and 9 follow 7 (9 also requires 8 for fixture-result handling). Task 10 follows 8/9. Tasks 11 and 12 can run in parallel after 10, owning distinct directories. Task 13 follows 6 and can run alongside 7–12. Task 14 follows 11/12/13. Task 15 is the final integrated gate.

Use pytest markers `unit`, `contract`, `postgres`, `cube`, `e2e`, `live_bigquery`. A mocked HTTP response proves only adapter behavior. A `postgres` test uses real migrated PostgreSQL; a `cube` test uses the pinned real Cube image and demo database. Never substitute SQLite or mocks for database constraints, locking, Cube context or cache isolation. Docker-unavailable runs record those suites as **NOT RUN**, not PASS. `make verify-demo` must fail when mandatory Docker evidence is missing. All commands below are future implementation instructions.

### Task 1: Define strict contracts and reproducible test entry points

**Files:** Create the five contract modules, `canonical.py` and `clock.py` above; `tests/unit/test_contracts.py`; `tests/conftest.py`; `pyproject.toml`; `uv.lock`; `Makefile`; `.gitignore`; `docs/compatibility/versions.md`.

**Interfaces:** Consumes the spec. Produces every shared type above, `validate_manifest(raw: dict) -> Manifest`, `manifest_digest(manifest: Manifest) -> str`, `validate_query(manifest: Manifest, request: QueryRequest) -> QueryRequest`; fixtures `clock`, `valid_manifest`, `north_principal`.

- [ ] **Step 1: Write failing contract tests**, including these assertions (helpers create one explicit valid fixture):
  ```python
  assert manifest_digest(m) == manifest_digest(reordered(m))
  assert manifest_digest(m) != manifest_digest(with_description(m, "Changed"))
  assert reject_manifest({**valid_manifest, "sql": "select 1"}) == "INVALID_INPUT"
  assert reject_query(m, inputs={"claimed_user_id": "admin"}) == "INVALID_INPUT"
  assert reject_range(m, "2024-01-01", "2025-01-02") == "INVALID_INPUT"
  ```
  Add named cases for nested unknown fields, invalid defaults/enums/nullability, secret values, SQL/scripts in binding fields, unsupported operators, unsafe link schemes and product limits above global caps. Descriptions containing instruction-like text stay inert text; do not ban ordinary business prose.
- [ ] **Step 2: Establish and run the test entry point.** Select supported Python and dependency releases from official metadata, record exact versions, then create the minimum package/pytest configuration and lockfile. Run `uv run pytest tests/unit/test_contracts.py -q`; expect missing-contract/test assertion failures, not an unavailable interpreter. No floating dependencies.
- [ ] **Step 3: Implement the declared contract functions** with Pydantic strict models, bounded request collections and canonical JSON. Define 1 MiB as 1,048,576 UTF-8 bytes of the complete unpaginated normalized result; limits may tighten only. Never fetch metadata links or resolve arbitrary secret references during validation.
- [ ] **Step 4: Run** `uv run pytest tests/unit/test_contracts.py -q`; expect all cases PASS. Add `make test-unit` and `make test-contract` wrappers with registered markers and fail-on-unknown-markers configuration.
- [ ] **Step 5: Commit** only these files: `git add gateway/contracts tests/conftest.py tests/unit/test_contracts.py pyproject.toml uv.lock Makefile .gitignore docs/compatibility/versions.md && git commit -m "feat: define strict product and execution contracts"`.

### Task 2: Build the transactional immutable registry

**Files:** Create `gateway/registry/{models,repository}.py`; `migrations/{env.py,versions/0001_registry.py}`; `alembic.ini`; `tests/integration/{conftest.py,test_registry.py}`.

**Interfaces:** Consumes Task 1 models. Produces `Registry.resolve(product_id: str, version: str | None) -> PinnedProduct`, `Registry.save_draft(product_id: str, manifest: Manifest, expected_revision: int, actor_id: str) -> DraftRevision`, `Registry.transaction() -> AsyncContextManager[UnitOfWork]`; UnitOfWork exposes row-locking reads and inserts used by Tasks 4, 7–9.

- [ ] **Step 1: Write failing real-Postgres tests**:
  ```python
  assert sorted(await publish_race(registry, same_draft)) == ["CONFLICT", "committed"]
  assert await stored_version_digest(registry, "revenue", "1.0.0") == tested_digest
  assert await mutate_published_json_as_app_role() == "permission_denied"
  assert await current_pointer_after_aborted_transaction() == previous_version_id
  ```
  Also assert binding/test-evidence immutability and that a default-version lookup remains pinned after a pointer change. The publication-race helper exercises the repository transaction primitive; Task 9 adds policy-aware publication.
- [ ] **Step 2: Provide a disposable PostgreSQL fixture and run** `uv run pytest tests/integration/test_registry.py -m postgres -q`; expect missing migration/repository failures. The fixture requires an explicitly test-only database URL, creates a per-run database, and drops only that database. Task 5 supplies the final pinned container; this task can use an independently started official PostgreSQL container and records its exact digest. Never point tests at user data.
- [ ] **Step 3: Implement registry and migration** for Product, ProductVersion, DraftRevision, BindingRevision, PolicyRevision, TestRun, Execution, Result and AuditEvent, plus server-managed principal grants, execution leases, result cursor records and idempotency uniqueness. Separate migration owner, app, audit-retention and source-reader roles. App may append immutable rows and change separately stored lifecycle/pointers; constraints/triggers deny updating immutable content and audit rows. Store publication uniqueness and revision checks in PostgreSQL, not Python locks.
- [ ] **Step 4: Run** the Task 2 command plus `uv run alembic upgrade head` twice on the disposable database; expect PASS and no second-run changes. Record migration head and role privileges.
- [ ] **Step 5: Commit** `gateway/registry`, `migrations`, `alembic.ini` and the two Task 2 test files with message `feat: add immutable transactional product registry`.

### Task 3: Authenticate principals and guard deployment modes

**Files:** Create `gateway/auth/{settings,tokens,principals}.py`; `tests/unit/test_identity.py`; `tests/integration/test_identity_store.py`; `.env.example`; `scripts/demo_accounts.py`; `docs/operations/identity.md`.

**Interfaces:** Consumes Task 2 grants. Produces `Authenticator.authenticate(authorization: str | None) -> Principal`, `PrincipalStore.get_current(principal_id: str) -> PrincipalGrants`, `validate_mode(settings: Settings) -> None`. `PrincipalGrants` contains tenant, capabilities and entitlements; no transport accepts it.

- [ ] **Step 1: Write failing tests**:
  ```python
  assert await auth_code(expired_access_token) == "UNAUTHENTICATED"
  assert await auth_code(valid_signature_id_token) == "UNAUTHENTICATED"
  assert await auth_code(wrong_audience_token) == "UNAUTHENTICATED"
  assert reject_mode(mode="demo", source="bigquery")
  assert reject_mode(mode="demo", bind_host="0.0.0.0")
  ```
  Parametrize unknown keys, wrong issuer/algorithm, forged role headers, revoked API keys, unavailable identity store and a JWT-supplied `jku` URL. Assert no fetch follows the token's URL. Service keys resolve only to their stored service principal.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_identity.py tests/integration/test_identity_store.py -q`; expect missing authentication/mode validation failures.
- [ ] **Step 3: Implement authentication and settings.** Verify signature/issuer/audience/expiry and an explicit provider access-token profile, not merely generic JWT validity. Default allowed algorithm RS256; configure alternatives explicitly. JWKS cache max five minutes, request timeout two seconds, one bounded refresh for an unknown key; never trust token-selected endpoints. OIDC profiles that cannot distinguish access from ID tokens remain disabled. Demo-account generation emits random local-only synthetic tokens outside the repository; store API-key hashes, never plaintext. Tokens stay out of manifest/examples/logs. The UI later keeps its bearer token in memory only.
- [ ] **Step 4: Run** both suites; expect PASS. Document user-bound delegation versus scoped service identity, the token profile, revocation path, issuer outage behavior and why this is not verified Studio integration.
- [ ] **Step 5: Commit** the Task 3 files with message `feat: verify principals and isolate demo identity mode`.

### Task 4: Enforce current policies and fail-closed audit

**Files:** Create `gateway/policy/{service,audit}.py`; `tests/unit/test_policy.py`; `tests/integration/test_audit.py`; `tests/security/test_policy_revocation.py`.

**Interfaces:** Consumes PrincipalStore and Registry transactions. Produces `PolicyService.authorize(principal: Principal, action: str, product: PinnedProduct, uow: UnitOfWork) -> Decision`, `PolicyService.change_policy(actor: Principal, policy_id: str, rules: dict, reason: str) -> int`, `AuditWriter.append(uow: UnitOfWork, event: AuditEventInput) -> None`. Actions are `discover|describe|execute|result|author|publish|policy_admin|operate|audit_view`.

- [ ] **Step 1: Write failing tests**:
  ```python
  assert await decision(author, "policy_admin") == "NOT_FOUND_OR_FORBIDDEN"
  assert await decision(publisher_without_data_grant, "execute") == "NOT_FOUND_OR_FORBIDDEN"
  assert await decision(after_revocation, "result") == "NOT_FOUND_OR_FORBIDDEN"
  assert await audited_action_when_audit_insert_fails() == "rejected"
  assert await append_then_update_audit_as_app_role() == "permission_denied"
  ```
  Assert hidden/nonexistent products have the same outward error, tenant constraints cannot be removed, and raw parameters/results/tokens/SQL never appear in serialized audit events.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_policy.py tests/integration/test_audit.py tests/security/test_policy_revocation.py -q`; expect missing enforcement/audit failures.
- [ ] **Step 3: Implement current-policy lookup and audit.** Read current grants/policy for each decision; no authorization cache. Store verified actor/service client, product/version, policy revision, decision, parameter names plus operator-keyed HMAC digest, timing, row count, adapter and correlation ID. Audit decisions and mutations in the same transaction; write the required pre-upstream decision before sending a query. Audit failure suppresses result release and publication. Policy administration is separate from author/publisher roles and records actor/reason.
- [ ] **Step 4: Run** all three suites; expect PASS, including database rollback and redaction assertions. State clearly that administrators with database-owner privileges can still alter storage.
- [ ] **Step 5: Commit** the Task 4 files with message `feat: enforce current policy and mandatory redacted audit`.

### Task 5: Prove the private Cube and synthetic data boundary

**Files:** Create `cube/{cube.js,model/orders.yml,model/watermark.yml}`; `infra/{compose.yaml,compose.test.yaml,postgres/init.sql,edge.conf}`; `fixtures/{orders.csv,watermark.json,accounts.json,policies.json}`; `tests/integration/test_cube_compatibility.py`; `docs/compatibility/cube.md`. Modify `docs/compatibility/versions.md` only after Task 1 merges.

**Interfaces:** Consumes Decision field names. Produces an operator-owned `BundleCatalog.resolve(bundle_digest: str) -> CubeEndpoint` contract, `/cubejs-api/v1/meta` and `/cubejs-api/v1/load` compatibility fixtures, and a pinned model-bundle SHA-256. Endpoint credentials stay in mounted secrets. Cube security context contains gateway-signed principal, tenant and mandatory entitlement constraints with a short expiry.

- [ ] **Step 1: Write failing real-Cube tests**:
  ```python
  assert await cube_without_security_context() == "denied"
  assert await cube_rows(tenant="north") == expected_north_rows
  assert await cube_rows(tenant="south") == expected_south_rows
  assert externally_published_services() == {"edge"}
  assert await cold_bounded_slow_fixture_query() == "observed_continue_wait_then_success"
  ```
  Add hostile client filters, cross-tenant cache warming, expired context, missing context, SQL/GraphQL/dev ingress and post-restart data checks. Store actual release response shapes, not guessed fixtures.
- [ ] **Step 2: Run** `uv run pytest tests/integration/test_cube_compatibility.py -m cube -q`; expect unavailable-stack/configuration failures until the private runtime exists. An unavailable Docker daemon is a blocker, not a failing product assertion.
- [ ] **Step 3: Implement and pin the runtime.** Verify supported Cube/PostgreSQL images from official releases, pull once, record exact digests and notices. Use separate registry/demo databases and roles; source credentials are read-only. Cube independently imposes trusted row predicates and rejects missing context in production mode. Use no pre-aggregations/Cube Store. A test-only Cube endpoint reads a separate seeded fixture schema with its own role; include a reviewed bounded-delay fixture to observe actual pending responses. Only the edge service publishes `127.0.0.1`; later API/UI services attach behind it. Disable telemetry using verified release settings.
- [ ] **Step 4: Run** the real-Cube suite after `docker compose -f infra/compose.yaml -f infra/compose.test.yaml up -d --wait registry-db demo-db cube cube-test`; expect PASS. The test inspects the full Compose port configuration; edge routing is activated when API/UI exist. Verify cache isolation in actual responses; disable caching using a tested supported setting if it fails, otherwise stop this gate. Capture `/meta`, pending/load, role and port evidence with secrets removed.
- [ ] **Step 5: Commit** Task 5 files with message `feat: isolate Cube and seed deterministic demo data`.

### Task 6: Implement the constrained Cube adapter and result conversion

**Files:** Create `gateway/adapters/{base,cube,normalize}.py`; `gateway/adapters/bundles.py`; `tests/contract/test_cube_adapter.py`; `tests/unit/test_normalization.py`; `fixtures/products/revenue.json`.

**Interfaces:** Implements BundleCatalog from Task 5. Produces `CubeAdapter.validate_binding(manifest: Manifest) -> None`, `CubeAdapter.prepare(product: PinnedProduct, request: QueryRequest, decision: Decision) -> PreparedQuery`, `CubeAdapter.poll(query: PreparedQuery, decision: Decision) -> AdapterPoll`, `normalize(product: PinnedProduct, completed: Completed, execution: ExecutionView) -> ResultEnvelope`. `SourceCapabilities` declares supported operators/types/date semantics, row cap and proven source-side time/cost bounds.

- [ ] **Step 1: Write failing tests**:
  ```python
  assert prepared.query["limit"] == 1001  # detect overflow, never silently truncate
  assert mandatory_constraints_survive_user_filters(prepared)
  assert poll_recorded_continue_wait().kind == "pending"
  assert normalized_decimal("9007199254740993.01") == "9007199254740993.01"
  assert normalize_with_missing_watermark().code == "FRESHNESS_UNKNOWN"
  assert normalize_with_old_watermark().code == "STALE_DATA"
  ```
  Add exact-grain totals, nullable dimensions, empty rows with independently recorded watermark, invalid decimal precision, UTC timestamp formatting, DST start/end boundaries, 366/367-day inputs, 1,001 rows, 1,048,577-byte envelopes, changed bundle, unavailable retained bundle and safe upstream errors.
- [ ] **Step 2: Run** `uv run pytest tests/contract/test_cube_adapter.py tests/unit/test_normalization.py -q`; expect missing adapter/conversion failures.
- [ ] **Step 3: Implement the declared interfaces.** Build only approved member/filter/sort identifiers from bindings; no concatenated user SQL. Validate `/meta` plus operator-attested bundle digest; unchanged metadata alone cannot prove unchanged business logic. Request effective row limit plus one, count complete normalized bytes, and fail over-limit output. Bound HTTP response bytes/time independently. Poll the same canonical load query/context. A watermark comes from the reviewed watermark model, never fetch time. Compare decimals with Decimal, return declared precision as strings, and mark `truncated=false` only on complete success. Missing compatible endpoints produce BINDING_DRIFT.
- [ ] **Step 4: Run** both suites and Task 5's real-Cube suite; expect PASS. Classify contract fixtures as mocked; actual model/driver behavior as Docker evidence. Verify PostgreSQL statement-time limits in Task 5's real stack; otherwise mark production execution blocked.
- [ ] **Step 5: Commit** Task 6 files with message `feat: add constrained Cube adapter and lossless result envelopes`.

### Task 7: Add durable execution, idempotency and bounded worker recovery

**Files:** Create `gateway/execution/{service,queue,worker}.py`; `tests/unit/test_execution_clock.py`; `tests/integration/test_execution_queue.py`; `tests/security/test_execution_reauthorization.py`.

**Interfaces:** Consumes Registry, PolicyService, CubeAdapter and Clock. Produces `ExecutionService.submit(principal: Principal, request: QueryRequest) -> ExecutionView`, internal `submit_fixture(principal: Principal, spec: ExecutionSpec, capability: FixtureCapability) -> ExecutionView`, `wait(execution_id: str, seconds: float = 5) -> ExecutionView`, `Worker.run_once() -> bool`. Define `FixtureCapability` in `service.py` as a server-issued, non-serializable handle tied to an approved fixture run and isolated endpoint. Only Task 9's authorized test runner can obtain it; it is never a public argument or route.

- [ ] **Step 1: Write failing tests**:
  ```python
  assert await submit_twice_same_key_and_input() == (same_id, same_id)
  assert await submit_same_key_changed_input() == "CONFLICT"
  assert await concurrent_running_counts() == {"per_principal_max": 2, "deployment_max": 10}
  assert (await wait_with_pending_cube()).elapsed <= 5
  assert (await advance_clock(120)).state == "timed_out"
  assert await revoked_before_worker_claim() == "denied_before_upstream"
  ```
  Also test three worker processes racing, audit outage before upstream, worker death after submission/before completion, late upstream completion, pointer changes after acceptance, and a policy change while pending that prevents further polling/output release.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_execution_clock.py tests/integration/test_execution_queue.py tests/security/test_execution_reauthorization.py -q`; expect missing execution-state/capacity failures.
- [ ] **Step 3: Implement PostgreSQL-backed claims and execution.** Pin version once; authorize/validate before adapter construction. Scope idempotency by principal/product/version plus key; compare canonical inputs/filters/sort and persist under uniqueness constraints. Store 120-second deadline from acceptance, use bounded 0.25/0.5/1/2-second backoff capped by remaining time, and lease-based crash recovery. Count claimed running executions transactionally using database-level capacity locks; cap pending jobs at 10 per principal and 100 deployment-wide with RESOURCE_LIMIT. Reauthorize on each claim/reclaim and pending poll. Do not release a slot because the five-second wait ends. At deadline stop polling and record timeout with unknown upstream cancellation; quarantine any source-capacity reservation until confirmed completion or its proven maximum source duration. A lease expiring alone cannot release source capacity. Recovered read-only attempts remain audited and share the original deadline.
- [ ] **Step 4: Run** all Task 7 suites; expect PASS with fake-clock timing and real PostgreSQL race/lease assertions. Replay actual Cube pending fixture from Task 5 in contract coverage; do not claim fake-clock tests are real cancellation tests.
- [ ] **Step 5: Commit** Task 7 files with message `feat: add durable bounded execution and idempotent submission`.

### Task 8: Secure results, pagination and expiry

**Files:** Create `gateway/execution/{results,cursors,retention}.py`; `tests/integration/test_results.py`; `tests/security/test_result_isolation.py`.

**Interfaces:** Produces `ResultService.get(principal: Principal, execution_id: str, cursor: str | None = None) -> ResultEnvelope | ExecutionView`, `CursorStore.issue(principal_id: str, execution_id: str, policy_revision: int, offset: int, expires_at: datetime) -> str`, `RetentionSweep.run_once(now: datetime) -> RetentionCounts`.

- [ ] **Step 1: Write failing tests**:
  ```python
  assert len((await first_page()).rows) == 200
  assert await other_principal_fetch() == "NOT_FOUND_OR_FORBIDDEN"
  assert await revoked_principal_with_old_cursor() == "NOT_FOUND_OR_FORBIDDEN"
  assert (await fetch_after_hours(24)).state == "expired"
  assert await withdraw_then_fetch_existing_result() == "NOT_FOUND_OR_FORBIDDEN"
  ```
  Add tampered cursor, changed policy revision, expired cursor, guessed execution ID, cached/idempotent replay after revocation and audit failure before response. No error may leak another result's existence or content.
- [ ] **Step 2: Run** `uv run pytest tests/integration/test_results.py tests/security/test_result_isolation.py -q`; expect missing result-access/isolation failures.
- [ ] **Step 3: Implement stored results and opaque cursors.** Isolate by principal, policy revision, version and canonical query; random server-stored cursor tokens expire no later than result retention and bind all those fields. Reauthorize and check current withdrawal state on every page, including succeeded/idempotent results; a policy-revision mismatch requires a new query rather than reusing old output. Record result access before releasing bytes. Sweep expired result bodies after 24 hours; a dedicated maintenance role prunes redacted audits after 30 days, without granting the app audit-update/delete rights.
- [ ] **Step 4: Run** both suites; expect PASS, including concurrent retention/access and cursor replay. `row_count` is total complete result count; per-page rows never imply truncation of the execution.
- [ ] **Step 5: Commit** Task 8 files with message `feat: isolate paginated results and enforce retention`.

### Task 9: Implement tested-digest publication and lifecycle

**Files:** Create `gateway/publishing/{service,test_runner}.py`; `tests/integration/test_publishing.py`; `tests/security/test_publisher_roles.py`; `fixtures/products/revenue.tests.json`.

**Interfaces:** Produces `PublishingService.save_draft(actor: Principal, manifest: Manifest, expected_revision: int) -> DraftRevision`, `test(actor: Principal, draft_id: str) -> TestRun`, `publish(actor: Principal, draft_id: str, tested_digest: str, expected_revision: int, reason: str) -> PinnedProduct`, `transition(actor: Principal, product_id: str, version: str, action: Literal["deprecate","withdraw","rollback"], reason: str) -> None`, `diff(actor: Principal, draft_id: str) -> VersionDiff`.

- [ ] **Step 1: Write failing tests**:
  ```python
  assert await edit_after_green_tests_then_publish() == "CONFLICT"
  assert await change_binding_or_policy_after_tests() == "CONFLICT"
  assert sorted(await simultaneous_publications()) == ["CONFLICT", "committed"]
  assert await author_attach_broader_policy() == "NOT_FOUND_OR_FORBIDDEN"
  assert await rollback_to_unavailable_bundle() == "BINDING_DRIFT"
  ```
  Test persisted expected-versus-actual values/tolerances, positive and permission-negative fixtures, author/publisher separation, deprecation warning, withdrawal blocking new/fetched output, immutable TestRun and audited actor/reason on every transition.
- [ ] **Step 2: Run** `uv run pytest tests/integration/test_publishing.py tests/security/test_publisher_roles.py -q`; expect missing workflow and digest-gate failures.
- [ ] **Step 3: Implement publishing and isolated tests.** Execute only approved fixture cases through the internal fixture entry point, fixed test principals and Task 5's separate fixture schema/endpoint. Authors cannot choose credentials, principals or arbitrary policy overrides. Record exact draft/binding digest, policy revision and fixture version with evidence. Compare strings/nulls/dates exactly and decimals with explicitly declared absolute/relative tolerances. Publication locks the draft/current pointer and rechecks all evidence/policy/binding state in one transaction. Existing compatible versions support rollback; never mutate published manifests. Keep lifecycle state separately audited.
- [ ] **Step 4: Run** both suites plus Task 2's publication race test; expect PASS, including a changed policy during publication. No successful publish without current successful negative-permission fixtures.
- [ ] **Step 5: Commit** Task 9 files with message `feat: gate publication on immutable test evidence`.

### Task 10: Expose shared REST services and permission-filtered discovery

**Files:** Create `gateway/api/{app,products,publishing,executions,admin}.py`; `tests/contract/test_rest.py`; `tests/security/test_transport_inputs.py`; `docs/compatibility/openapi.json`.

**Interfaces:** `create_app(services: Services) -> FastAPI`. Routes: `GET /v1/products`; `GET /v1/products/{id}` with optional version; `POST /v1/executions`; `GET /v1/executions/{id}` with optional cursor; `POST /v1/drafts`; `GET|PUT /v1/drafts/{id}`; `POST /v1/drafts/{id}/tests`; `GET /v1/drafts/{id}/tests/{run_id}`; `GET /v1/drafts/{id}/diff`; `POST /v1/drafts/{id}/publish`; `POST /v1/products/{id}/lifecycle`; `GET /v1/bindings`; `GET /v1/policies`; `PUT /v1/policies/{id}`; `GET /v1/audit`; `GET /healthz`; `GET /readyz`. Bindings/policies return only attachable approved choices; policy PUT requires policy_admin. `Services` is an explicit dataclass of the Task 3–9 service instances.

- [ ] **Step 1: Write failing transport tests**:
  ```python
  assert "restricted-revenue" not in await search_as(north_consumer)
  assert await unknown_product_error() == await forbidden_product_error(exclude_request_id=True)
  assert await query_with_sql_source_url_or_claimed_user() == (422, "INVALID_INPUT")
  assert await oversized_request_body() == (413, "RESOURCE_LIMIT")
  assert await hidden_provenance_or_example() == "not_disclosed"
  ```
  Assert audit view authorization, zero outgoing HTTP calls for description links, fixed safe upstream error text plus request ID, and identical outcomes for REST/service decisions.
- [ ] **Step 2: Run** `uv run pytest tests/contract/test_rest.py tests/security/test_transport_inputs.py -q`; expect missing routes/transport guards.
- [ ] **Step 3: Implement thin route handlers.** Discovery filters before pagination/counts, including metadata and examples; text search is bounded to 256 characters, pages to 50 products, request bodies to 64 KiB. Use opaque principal/policy-bound catalog cursors. Serialize only authorized fields; never log raw bodies/tokens. Reject unknown arguments. Map unauthenticated→401, hidden/not-found→404, invalid→422, conflict→409, limit→413/429, upstream→503; completed execution failures retain their safe stable codes in ExecutionView. Pin OpenAPI output for UI generation. Readiness requires correct migration head, policy/registry availability and compatible Cube binding, not merely a listening process.
- [ ] **Step 4: Run** both suites; expect PASS and an approved OpenAPI snapshot. Extend Task 5's edge configuration only through its owner if route wiring needs adjustment.
- [ ] **Step 5: Commit** Task 10 files with message `feat: expose authorized catalog and gateway REST API`.

### Task 11: Add the fixed MCP contract and two independent clients

**Files:** Create `gateway/mcp/server.py`; `clients/{python_agent.py,typescript_agent.ts,package.json,package-lock.json}`; `tests/contract/test_mcp.py`; `tests/e2e/test_two_clients.py`; `docs/compatibility/mcp.md`. Modify `docs/compatibility/versions.md` and gateway app mounting through a coordinated patch.

**Interfaces:** `create_mcp_server(services: Services) -> MCPServer` mounts authenticated Streamable HTTP at `/mcp`. Tools only: `search_products(query: str = "", cursor: str | None = None, limit: int = 50)`, `describe_product(product_id: str, version: str | None = None)`, `query_product(request: QueryRequest)`, `get_result(execution_id: str, cursor: str | None = None)`.

- [ ] **Step 1: Write failing tests**:
  ```python
  assert set(await tools_list()) == {"search_products", "describe_product", "query_product", "get_result"}
  assert await forged_tool_identity_or_cube_member() == "INVALID_INPUT"
  assert await rest_and_mcp_normalized_answers() == "equal"
  assert await python_and_typescript_agent_answers() == "equal_product_digest_and_rows"
  ```
  Add per-request token verification, session/connection identity reuse rejection, Origin checks, missing auth, unsupported protocol negotiation and bounded tool pagination.
- [ ] **Step 2: Run** `uv run pytest tests/contract/test_mcp.py -q`; expect missing MCP server/contract failures.
- [ ] **Step 3: Implement MCP using an officially supported SDK/protocol combination.** Pin exact Python/TypeScript SDKs, protocol revision and lockfiles after compatibility checks. Reuse gateway services directly, not HTTP proxy logic with a second policy system. Validate auth on every request; a session ID is not identity. Clients deterministically search, describe, query, poll and compare the same product under the same authorized account; they are two independent agent-client harnesses, with no LLM or Studio dependency. Also run each with the two distinct access profiles and assert the appropriate different row scopes.
- [ ] **Step 4: Run** `uv run pytest tests/contract/test_mcp.py tests/e2e/test_two_clients.py -q` against the real stack; expect PASS. Record exact client implementations, versions, transport and tested behaviors. Label Studio integration **NOT TESTED** until separately verified.
- [ ] **Step 5: Commit** Task 11 files and coordinated mounting/version changes with message `feat: expose fixed MCP tools and interoperable demo clients`.

### Task 12: Build the publisher UI against the real contract

**Files:** Create `publisher/{package.json,package-lock.json,index.html,vite.config.ts,tsconfig.json,playwright.config.ts}`; `publisher/src/api.ts`; `publisher/src/{auth,App}.tsx`; `publisher/src/pages/{Catalog,Product,Editor,Review}.tsx`; `publisher/src/generated/api.ts`; `publisher/src/styles.css`; `publisher/src/tests/{publisher,metadata}.test.tsx`; `tests/e2e/publisher.spec.ts`.

**Interfaces:** Consumes Task 10 OpenAPI, never database/Cube access. `api.ts` exports typed `GatewayClient` operations corresponding exactly to REST routes. Generated types come from the locked OpenAPI generator. `auth.tsx` supplies an in-memory bearer and clears it on logout/401.

- [ ] **Step 1: Write failing UI tests**:
  ```typescript
  expect(screen.getByText(/synthetic demo/i)).toBeVisible();
  expect(screen.queryByText("restricted-revenue")).toBeNull();
  expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();
  expect(document.querySelector("script[data-injected]")).toBeNull();
  ```
  Add edit-invalidates-green-evidence, expected/actual fixture display, version diff, publish conflict requiring reload, role-specific controls, freshness/provenance detail, keyboard navigation and loading/error/empty states. Hostile HTML/Markdown descriptions and `javascript:` links never execute.
- [ ] **Step 2: Lock the UI toolchain and run** `npm --prefix publisher test -- --run`; expect component/behavior failures before implementation. Record official supported React/TypeScript/Vite/runtime versions; do not accept floating ranges in the final lock/install path.
- [ ] **Step 3: Implement catalog, detail, versioned editor and review pages.** Use ordinary text rendering and validated external HTTPS links with safe target attributes. Show semantics, limits, version, source watermark/freshness, permitted examples and warnings. Editor offers approved bindings/policies only, blocks unknown fields and never executes code. Review requires current digest evidence, diff and transition reason. Persistent demo banner appears on every route; controls reflect permissions but the server remains authoritative.
- [ ] **Step 4: Run** `npm --prefix publisher test -- --run`, `npm --prefix publisher run build`, and `npm --prefix publisher run test:e2e` (locked Playwright script with testDir `../tests/e2e`); expect PASS. Verify the complete author→test→publisher→consumer flow in the browser against the real backend, plus a narrow/mobile viewport and keyboard-only use.
- [ ] **Step 5: Commit** Task 12 files with message `feat: build versioned data product publisher UI`.

### Task 13: Add BigQuery contract coverage and explicit Domo gates

**Files:** Create `gateway/adapters/bigquery.py`; `fixtures/adapter/bigquery.json`; `tests/contract/test_bigquery.py`; `tests/integration/test_bigquery_live.py`; `docs/operations/{bigquery,domo-phase-1b}.md`.

**Interfaces:** `BigQueryCubeProfile.validate(settings: SourceSettings) -> SourceCapabilities` specializes the same Cube adapter; it is not a direct BigQuery compiler. `SourceSettings` contains operator-owned region and secret reference, never credentials supplied by a manifest. Source selection registers only PostgreSQL and explicitly configured BigQuery; `domo` remains unavailable.

- [ ] **Step 1: Write failing mocked-contract tests**:
  ```python
  assert convert_numeric("12345678901234567890.123456789") == "12345678901234567890.123456789"
  assert invalid_region_or_timestamp_fixture().code == "INVALID_INPUT"
  assert unsupported_cost_bound_profile().production_enabled is False
  assert select_adapter("domo").code == "INVALID_INPUT"
  ```
  Cover date/filter mapping, decimal and timestamp semantics, nulls, wrong region, missing credential reference, maximum bytes/rows, unsupported source limit capability and upstream error redaction.
- [ ] **Step 2: Run** `uv run pytest tests/contract/test_bigquery.py -m contract -q`; expect missing profile/mapping failures. No external credentials or live network calls.
- [ ] **Step 3: Implement profile validation and operations documentation.** Verify the selected Cube release's BigQuery driver configuration and least-privilege query/read permissions from official documentation. Disable source-writing pre-aggregations. Record whether actual source-side time/cost limits are enforceable through this driver; lack of proof blocks production activation. Provide an opt-in live smoke test requiring explicit project/dataset/region authorization, scoped credentials and a spend ceiling, never enabled by default. Domo documentation lists actual API, tenant/PDP, service reach, query semantics, cost/rate and authoritative card/Beast Mode reconciliation gates; create no Domo execution stub pretending success.
- [ ] **Step 4: Run** the mocked suite; expect PASS and `live_bigquery` **NOT RUN** without separate authorization. Keep live test reports in a separate evidence file even if later executed.
- [ ] **Step 5: Commit** Task 13 files with message `test: cover BigQuery contracts and document deferred Domo gates`.

### Task 14: Package operations and prove offline persistence

**Files:** Modify `infra/{compose.yaml,compose.test.yaml,edge.conf}`, `gateway/api/app.py` logging wiring, `docs/compatibility/versions.md` and `Makefile`; create `gateway/{Dockerfile,logging.py}`; `publisher/Dockerfile`; `scripts/{verify_demo.py,check_egress.py,backup.sh,restore.sh}`; `tests/integration/test_operations.py`; `docs/operations/{runbook,production-gates,licensing}.md`; `THIRD_PARTY_NOTICES.md`; `README.md`.

**Interfaces:** `make demo-up`, `make demo-down`, `make verify-demo`, `make backup`, `make restore`, `make evidence` are documented entry points. `verify_demo.py` emits machine-readable suite name, actual/mocked/live classification, versions, status and evidence paths; missing mandatory suites cause nonzero exit.

- [ ] **Step 1: Write failing operations tests**:
  ```python
  assert await restart_preserves_published_digest() == original_digest
  assert await restore_into_clean_stack_preserves_registry() is True
  assert await demo_with_denied_external_egress() == "complete"
  assert runtime_required_vendor_connections() == []
  assert log_capture_contains_canary_secret_or_raw_result() is False
  ```
  Test incorrect migration head, DB/Cube outage readiness, backup failure, expired-result sweep and denied external DNS/TCP/UDP attempts across every runtime container and browser asset path.
- [ ] **Step 2: Run** `uv run pytest tests/integration/test_operations.py -q`; expect missing operational entry points or egress/restore evidence.
- [ ] **Step 3: Package services and runbooks.** Add pinned gateway/worker/UI builds, local static assets/fonts and health/readiness probes. Keep only edge loopback ingress; mount secrets separately, disable telemetry and block container outbound traffic with a documented platform-supported test harness. Record deny counters/network capture as well as successful workflow execution; setting telemetry flags alone is insufficient. Back up registry plus required model-bundle/secret-reference configuration; restore to a fresh disposable stack without overwriting a working environment. Document retention roles, encryption/TLS/ingress/egress/secrets/ownership requirements and source-timeout limitations. Generate dependency/license inventory and preserve upstream notices; leave original-code license and public distribution pending owner approval.
- [ ] **Step 4: Run** `make verify-demo` and the restore exercise with dependencies/images already present; expect PASS with network evidence and original published digest after restart/restore. Production checklist remains blocked until its external approvals/tests are complete.
- [ ] **Step 5: Commit** Task 14 files with message `chore: package offline demo and verified operational runbook`.

### Task 15: Run integrated security and acceptance gates

**Files:** Create `tests/e2e/test_acceptance.py`; `tests/security/test_adversarial_matrix.py`; `docs/evidence/{acceptance.md,security.md,limitations.md}`. Modify only tests/documentation or narrowly reviewed fixes in the owning module.

**Interfaces:** Consumes every prior task. Produces a reproducible acceptance report with actual commands, timestamps, exit status, environment, pinned artifacts, observed outputs and explicit untested gates. `make evidence` gathers reports without copying tokens/raw private data.

- [ ] **Step 1: Write failing acceptance/security tests**:
  ```python
  assert await fresh_install_publish_discover_query_restart() == "same_published_digest"
  assert await two_agent_clients_reconcile() == "same_authorized_answer"
  assert await cross_profile_attack_matrix() == "all_denied"
  assert await concurrent_edit_publish_drift_outage_timeout_limit_matrix() == "expected_stable_outcomes"
  assert evidence_has_real_postgres_real_cube_and_blocked_egress() is True
  ```
  Matrix axes include anonymous/consumer/author/publisher/operator/service identities; search/description/examples/provenance/query/result/cursor/cache/audit surfaces; forged identity, revocation and withdrawal; request/row/byte/date/concurrency bounds. Prove source-side timeout separately from gateway timeout.
- [ ] **Step 2: Run** `uv run pytest tests/e2e/test_acceptance.py tests/security/test_adversarial_matrix.py -q`; expect missing scenarios/evidence or real defects, never manufacture a failure by weakening a finished implementation.
- [ ] **Step 3: Complete the acceptance harness and fix discovered failures** in their owning modules using a regression test before each fix. A fresh reviewer checks complete interface/authentication/policy/worker/Cube/UI behavior and the diff after all tasks merge. Resolve critical/high security findings before recording acceptance; scope changes return for approval.
- [ ] **Step 4: Run** `make test-unit test-contract verify-demo evidence` from a clean checkout, with fresh demo volumes, then repeat without outbound access. Expect all mandatory suites PASS and no undisclosed skips. Record BigQuery mocked PASS separately from optional live NOT RUN; Studio NOT TESTED; Domo NOT IMPLEMENTED; production NOT APPROVED. Inspect the browser flow and final artifacts rather than relying solely on unit assertions.
- [ ] **Step 5: Commit** evidence/tests and reviewed fixes with message `test: verify end-to-end governed data product demo`. Hand over source, startup instructions, demo accounts generation, evidence and remaining gates. Do not push publicly, choose a license or deploy as a side effect.

## Coverage and review result

- Purpose/OSS/self-hosted boundary → Tasks 1, 5, 11, 14; no paid account/model dependency; licensing remains explicit.
- Product contract, registry and immutable bindings → Tasks 1, 2, 6, 9.
- Publisher workflow and separate roles → Tasks 3, 4, 9, 12.
- Fixed MCP, REST parity and two clients → Tasks 10, 11, 15.
- Limits, pending work, idempotency, typed/fresh output and retention → Tasks 6–8, 14.
- Identity, immediate policy, row isolation, caches and audit fail-closed → Tasks 3–8, 10–11, 15.
- PostgreSQL/Cube reference, BigQuery contracts and Domo deferral → Tasks 5–6, 13.
- Restart/restore, no runtime SaaS, secret-redacted operations → Tasks 5, 14–15.
- Acceptance gates 1–6 → Tasks 9–15, with real Docker evidence distinguished from mocks.

Self-review checked requirement coverage, exact interface names, test ownership, dependency order, task proportions and the five Review Focus cases. No approved V1 requirement is intentionally omitted. Production OIDC delegation/Studio interoperability, source cost controls, real BigQuery, Domo, production operations approval and original-code licensing are explicitly gated rather than represented as completed.

## Review and execution choice

Please review this plan alongside the approved design. Does it capture the intended implementation?

- **Subagent-driven, recommended:** a fresh implementer and reviewer for each task, followed by whole-branch review. Appropriate for these security and interface boundaries.
- **Native:** one implementer executes the tasks in sequence, followed by an independent whole-branch review. Lower coordination overhead, with independent review deferred until the end.

Implementation remains pending the plan review and this choice.
