# Performance and reliability reuse review

Reviewed 13 October 2026 against the approved design/implementation plan and the current local contracts/registry. Research only: no runtime configuration, package installation, application edits, live queries, or benchmarks were performed. Recommendations below are engineering judgments; official documentation establishes the underlying capabilities, not measured speedups for this project.

## Recommendation

**Add no new mandatory runtime dependency for performance in V1.** Reuse SQLAlchemy/psycopg, HTTPX, Pydantic, PostgreSQL transactions, and Python's existing profiling/async facilities. Prioritize bounded resource ownership and crash/revocation correctness before tuning throughput. Keep Cube separate, one memory-backed demo instance per isolated endpoint, no pre-aggregations/Cube Store, and no Redis/Celery/Kafka or hosted observability requirement.

The best immediate opportunities are:

1. Bound and reuse database/HTTP connections in long-lived processes.
2. Implement PostgreSQL job claiming, lease fencing, and source-capacity accounting together.
3. Separate the gateway deadline, HTTP inactivity timeout, Cube pending interval, and actual source execution timeout.
4. Verify Cube cache isolation using the exact selected release; never use a cache hit as permission evidence.
5. Measure validation and serialization before changing the existing strict contracts.

## Evidence from the repository

- `pyproject.toml` already pins SQLAlchemy 2.0.54, psycopg[binary] 3.3.6, HTTPX 0.28.1 and Pydantic 2.14.1. The working tree was active during inspection; this is a point-in-time observation.
- `gateway/registry/repository.py:110–130` creates an async engine with `NullPool`, a five-second connection timeout, hidden SQL parameters, and transaction-scoped sessions. Its error wrapper handles `DBAPIError`, not SQLAlchemy pool `TimeoutError`.
- `tests/integration/test_registry.py` and its fixture use separate `asyncio.run` calls. Changing that shared engine to reuse async connections without adjusting lifecycle ownership can cross event loops.
- `gateway/registry/models.py` already includes execution states, idempotency records, capacity-lock rows, lease generation/token fields, `source_reserved_until`, scoped result foreign keys, and retention indexes. Reuse these primitives rather than introducing another scheduler.
- `gateway/contracts/canonical.py` uses strict, frozen models, immutable nested collections, bounded JSON and RFC 8785 canonicalization. `ExecutionSpec` verifies its canonical hash. `ResultEnvelope` validates every row and the full canonical serialized byte count. These are deliberate correctness/security boundaries.
- The approved Task 7 caps are two running executions per principal, ten per deployment, ten pending per principal and 100 per deployment. The five-second synchronous wait does not terminate a job. Deadline is 120 seconds from acceptance, preserved across recovery.
- Tasks 5–6 explicitly require a real pinned Cube/PostgreSQL test, pending-response evidence, cache-isolation evidence and proven source timeout. Task 13 leaves live BigQuery gated. No recommendation here changes those requirements.

## Prioritized adoption and task mapping

### P0 · Reuse one bounded SQLAlchemy pool per long-lived process

**Adopt in Tasks 2, 7 and 10; verify in Tasks 14–15.** SQLAlchemy's async engine already supports `AsyncAdaptedQueuePool`; no `psycopg_pool` dependency is necessary. Keep an explicit `NullPool` profile for disposable/multiple-loop tests and short-lived commands. Own the runtime engine in application/worker startup and dispose it on shutdown. Use one `AsyncSession` per concurrent task, never a globally shared session. [SQLAlchemy pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html), [async lifecycle and concurrency](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html).

Suggested benchmark candidates, **not approved production sizing**: pool size 2 or 5 per process, `max_overflow=0`, checkout timeout around one second. Reserve connections for policy checks, audit writes, readiness and maintenance. Compute the deployment budget as the sum of every API/worker process's pool maxima, not one process's settings. The source database has a separate Cube pool budget. A ten-query execution cap does not imply ten registry transactions or ten physical source queries.

Keep registry transactions short and release them before awaiting Cube or sleeping. Do not hold a connection during the five-second API wait. Retain reset-on-return behavior; use transaction-local settings for request-specific state and never leave a principal's state on a reused connection. Evaluate `pool_pre_ping` against restart reliability and its extra round trip; it cannot replay a transaction interrupted midway.

**Required companion change:** normalize SQLAlchemy checkout `TimeoutError` to a safe service error. Existing `DBAPIError` handling alone will not catch it. Distinguish caller quota rejection (`RESOURCE_LIMIT`) from an unavailable registry (`UPSTREAM_UNAVAILABLE`). Do not retry a publication or audit transaction blindly after an ambiguous commit.

**Alternative, defer:** psycopg_pool can supply connections to SQLAlchemy using `NullPool`, `async_creator`, and `close_returns=True` in pool 3.3+. Choose exactly one pool owner. Its explicit waiting limits/statistics may be useful later, but are not enough reason to introduce a second pool now. [Official integration](https://www.psycopg.org/psycopg3/docs/advanced/pool.html).

**Evidence gate:** compare NullPool and bounded-pool candidates under 1/10/30 concurrent synthetic clients. Record connect count, checkout-wait p95, request p50/p95, maximum PostgreSQL backends, safe saturation responses, restart recovery and leaked sessions. Promote only with reproducible benefit and unchanged security/race tests.

### P0 · PostgreSQL durable worker with explicit ownership

**Adopt in Task 7; reuse Task 2 schema.** Use an ordered, bounded `SELECT … FOR UPDATE SKIP LOCKED` claim followed by lease/state updates within one transaction. PostgreSQL documents this as appropriate for competing queue consumers, while warning that skipped rows are not a generally consistent view. Use it for job claiming, never authoritative authorization/catalog enumeration. [PostgreSQL SELECT](https://www.postgresql.org/docs/current/sql-select.html).

Recommended algorithm:

1. Lock capacity records in a fixed global-to-principal order; respect the existing product/policy lock order wherever those locks are also needed. Recheck current identity, policy and withdrawal state.
2. Check pending/running limits and still-live source reservations transactionally; do not use a process-local semaphore as deployment-wide authority.
3. Claim an eligible job, increment its lease generation, assign an unguessable token, preserve its original deadline and record audit evidence before upstream work.
4. Commit the claim and release database locks before calling Cube.
5. Persist completion only with a compare-and-set against the current lease token/generation and permitted state. A stale worker must not overwrite a reclaimed or timed-out execution.
6. Reauthorize on each claim/reclaim/pending poll and before output release. Avoid TTL-cached positive permissions.

Lease expiry and upstream cancellation are different events. Preserve `source_reserved_until` while a source attempt might remain active. Recovery must not start duplicate physical work merely because the worker lease expired; poll the same pinned query/context when the selected Cube version's behavior supports it, or wait for completion/proven source-duration evidence. If retrying can create another source attempt, account for both until the first is known finished. A gateway process crash must not reset deadlines or quotas.

The approved deterministic 0.25/0.5/1/2-second bounded schedule is sufficient initially. A notification mechanism such as PostgreSQL LISTEN/NOTIFY can later reduce idle polling, but durable rows remain authoritative and polling/reconciliation must survive missed notifications. Defer it until idle-query rate matters.

**Evidence gate:** three real worker processes contend for jobs; assert 2/10 running and 10/100 pending caps. Kill a worker after claim, after sending upstream, and just before result commit. Observe recovery, stale-write rejection, preserved deadlines and reservations. Test audit outage and revocation while pending. A fake clock verifies scheduling, not source cancellation.

### P0 · One HTTPX client, bounded streams and one retry owner

**Adopt in Task 6; integrate in Tasks 7, 10 and 14.** Reuse an `httpx.AsyncClient` for the process lifecycle rather than constructing it inside each poll. Set finite connection and keepalive limits matching the actual worker/endpoint budget. Close streamed responses on every success/error/cancellation path. [HTTPX async usage](https://www.python-httpx.org/async/), [resource limits](https://www.python-httpx.org/advanced/resource-limits/).

Set connect/read/write/pool timeouts explicitly. A read timeout measures inactivity between chunks, not total elapsed request time. Wrap transport activity with the remaining execution budget using the existing Clock abstraction plus Python async cancellation; persisted UTC deadlines support recovery, while local elapsed timing should be monotonic. Never renew a job deadline when retrying. [HTTPX timeout semantics](https://www.python-httpx.org/advanced/timeouts/), [Python async timeouts](https://docs.python.org/3.12/library/asyncio-task.html#timeouts).

Current Cube documentation lists a ten-second pending long-poll interval, whereas HTTPX defaults to five seconds of inactivity. Verify the selected Cube release's actual behavior and set transport reads accordingly; an API caller's five-second wait is independent. Prefer the worker as the single retry authority. HTTPX transport retries cover connection errors/timeouts only; they are not application polling and do not cover every 503/read error. Avoid layered hidden retries or an unbounded generic retry decorator. [HTTPX transports](https://www.python-httpx.org/advanced/transports/), [Cube configuration](https://docs.cube.dev/reference/configuration/config).

Stream and count decoded response bytes before JSON parsing; Content-Length alone is insufficient. Bound raw upstream bytes independently from the one-MiB normalized envelope and test compression expansion. Keep a fixed operator-owned base URL, redirects disabled, and explicit proxy policy. `trust_env=False` is appropriate for the isolated demo client so environment proxy settings cannot silently reroute it; explicit certificate configuration is required if environment-provided certificate settings were previously needed. [HTTPX environment handling](https://www.python-httpx.org/environment_variables/).

**Evidence gate:** slow-drip body exceeds total deadline; oversized/chunked/compressed body is rejected; valid pending response survives its expected long poll; connection loss causes bounded retries; revoked access prevents the next poll; redirects never forward credentials; no sockets remain leased after cancellation. `MockTransport` is useful for deterministic errors, but validate actual socket reuse/timeout behavior against the local real stack.

### P0 · Source work must be bounded independently of output

**Adopt in Tasks 5–7; enforce BigQuery gate in Task 13.** A 1,001-row sentinel proves the 1,000-row output cap, not cheap computation. Aggregations, sorts and joins can scan large sources while returning one row. Keep allowed members/operators, maximum date spans and reviewed joins. Review the physical query plan of each approved source model; add source indexes only after measuring representative bounded queries.

Configure and verify a PostgreSQL source statement timeout using the approved operator/driver path, separately from gateway cancellation. Keep registry lock/statement timeouts shorter than user-visible waiting budgets. PostgreSQL's statement timeout acts at the server; lock timeout bounds lock acquisition. Session settings need deliberate scope, and timeout logging may include SQL, so test redaction at the database as well as the application. [PostgreSQL timeout documentation](https://www.postgresql.org/docs/current/runtime-config-client.html).

Cube's documented `CUBEJS_DB_QUERY_TIMEOUT` default is ten minutes, longer than the approved gateway deadline. A configured Cube timeout is not evidence that the selected driver cancels source work. Observe a deliberately slow synthetic query through Cube, inspect source activity until it stops, and record an upper bound including queued work/connection setup. A source reservation may be released only after this bound or verified completion. [Cube timeout setting](https://docs.cube.dev/reference/configuration/environment-variables).

For BigQuery, verify that the exact pinned Cube driver actually propagates source-side bounds such as `maximumBytesBilled`; do not infer support from the underlying Google SDK. Google warns that LIMIT does not reduce bytes read for non-clustered tables and documents maximum-bytes-billed as a pre-execution cap. Dry-run estimates and partition pruning are useful additional checks but do not replace a proven enforced cap. No direct BigQuery bypass, paid query or new credential use is recommended. [Google cost controls](https://docs.cloud.google.com/bigquery/docs/best-practices-costs), [Cube BigQuery driver setup](https://docs.cube.dev/admin/connect-to-data/data-sources/google-bigquery).

### P0 · Cache isolation before cache optimization

**Adopt in Tasks 5, 6 and 8.** Keep gateway results isolated by principal, policy revision, immutable product/version and canonical query. Fetch current permissions and withdrawal state before returning every page or idempotent replay. Positive authorization caching and cross-principal result deduplication are unsuitable for the approved immediate-revocation contract.

For Cube, `contextToAppId` partitions compiled models only; `contextToOrchestratorId` partitions connections, execution queues and query-result cache. Choose trusted, stable, bounded identities for isolation, and independently enforce mandatory row predicates. Never use request IDs/timestamps to force isolation: every unique orchestrator can create more pools/queues/cache state. If immutable model bundles are shared, do not create one compiler copy per request. [Cube multitenancy](https://docs.cube.dev/embedding/multitenancy).

The current docs allow a memory cache/queue, but production defaults to Cube Store and warn that replicated memory-only instances can create races and inconsistent caches. Preserve the approved no-Cube-Store demo by testing one Cube instance per isolated endpoint with explicitly selected memory storage and production authentication behavior. If that selected release cannot satisfy the requirements, report a compatibility blocker rather than enabling development mode or silently adding infrastructure. [Cube deployment](https://docs.cube.dev/cube-core/deployment).

Keep all pre-aggregations and scheduled refresh workloads out of this V1. Avoid turning on background renewal to mask source latency: the gateway must still prove watermark freshness and cannot silently release stale data. Prefer documented concurrency/pool/timeout settings over tuning internal orchestrator timeouts. Current docs caution that internal orchestrator tuning can destabilize a deployment. [Cube configuration reference](https://docs.cube.dev/reference/configuration/config).

**Evidence gate:** warm caches as north, then query south with identical query shapes; reverse the order; test two principals in one tenant with different entitlements; change policy and withdraw product while work is pending; replay old cursors/idempotency keys; restart Cube; remove context; submit hostile filters. Inspect both rows and discoverable metadata. Record memory, source connections and upstream query counts as scope count grows. Failure blocks acceptance, even if warm-query latency is excellent.

### P1 · Profile existing validation before optimizing it

**Adopt measurement in Tasks 1, 6, 10 and 15; defer contract rewrites.** Preserve strict scalar semantics, bounded shapes, deep immutability, revalidation, decimal strings and RFC 8785 digests. Do not use `model_construct`, `Any`, permissive coercion, or a different serializer to skip security checks for speed.

If future adapter schemas use `TypeAdapter`, instantiate adapters once at module/service scope. Benchmark direct JSON validation against the existing path using actual validators; Pydantic recommends the JSON path generally but notes exceptions with before/wrap validators. Its strict JSON handling also intentionally differs from strict Python-input handling for some types. Therefore adopting `model_validate_json` requires positive/negative parity tests, not just a faster microbenchmark. [Pydantic performance guidance](https://pydantic.dev/docs/validation/latest/concepts/performance/), [strict-mode semantics](https://pydantic.dev/docs/validation/latest/concepts/strict_mode/).

A bounded cache of a validated **immutable manifest keyed by its verified content digest** is a possible later optimization. It must not cache current-version resolution, lifecycle, permissions or authorization decisions. Because registry resolution currently revalidates stored content, first establish what corruption/tamper detection a cache would defer; retain digest/binding checks and explicit invalidation semantics. No such cache is needed without evidence that validation dominates.

Profile maximal allowed inputs and results using existing `cProfile`, `time.perf_counter_ns`, and `tracemalloc`; measure Python allocations separately from process RSS, which also includes native libraries. Include cumulative canonicalization/freeze/row-validation costs, rather than measuring only Pydantic object construction. [Python profiler](https://docs.python.org/3.12/library/profile.html), [allocation tracing](https://docs.python.org/3.12/library/tracemalloc.html).

### P1 · Cheap local observability and explicit performance evidence

**Adopt in Tasks 7, 14 and 15.** Start with redacted structured logs and aggregate measurements from the existing Python runtime. Measure:

- Accepted-to-start queue delay; execution duration; poll attempts and terminal reason.
- Registry checkout/transaction latency and pool saturation.
- Active and quarantined source reservations; lease recoveries and stale-write rejections.
- HTTP connection waits, transport timeouts and upstream response bytes.
- Normalization/validation time, normalized bytes, row count and independently evidenced cache status.
- Revocation/policy-change rejection counts, audit failures and readiness failures.

Use only bounded label dimensions such as operation, adapter, state and stable error class. Do not label metrics with principal IDs, raw product names, query values, SQL, cursor/token values or request IDs. Keep authorized correlation identifiers in redacted logs. Measurements must not replace required durable audit records or turn an audit failure into success.

**Optional later:** `prometheus-client` supports local metrics exposition without a SaaS dependency. Add it only when an actual local scrape consumer exists; authenticate/restrict its endpoint and test multiprocess aggregation if deploying multiple API workers. [Official Python client](https://prometheus.github.io/client_python/). Defer an OpenTelemetry collector, tracing backend, hosted APM/Logfire and a new dashboard stack until a diagnosed observability gap justifies them.

PostgreSQL `pg_stat_statements` is a useful optional operator diagnostic, but requires server configuration and exposes SQL-related information to privileged roles. Do not enable it as a hidden app-side optimization or grant monitoring privileges to the gateway. Use the synthetic environment and keep query texts out of user-facing evidence. [PostgreSQL extension](https://www.postgresql.org/docs/current/pgstatstatements.html).

## Benchmark/verification plan

No benchmark figures are claimed. These tests belong with the implementing owners and should use only disposable synthetic data. Record commit, hardware/container limits, pinned runtime/image digests, dataset size, workload seed, cold/warm state, command, duration and repeated-run variance. Keep correctness and performance reports separate.

| Scenario | What to compare/measure | Acceptance criterion |
| --- | --- | --- |
| Connection reuse | NullPool versus finite runtime pools; 1/10/30 clients | Bounded backends, no leaks, stable errors; choose a pool only with repeatable latency/connection-count benefit |
| Admission overload | More principals/jobs than 2/10 and 10/100 caps | Exact cross-process caps, bounded pending state, explicit limits, no hidden unbounded in-memory queue |
| Worker recovery | Three workers, kill/restart at each external boundary | No stale completion, no deadline extension, no reservation released on lease expiry alone |
| Pending transport | Actual Cube pending plus mocked transient failures | Valid long polls survive; total deadline holds; retry count bounded and auditable |
| Source timeout | Slow read-only synthetic fixture observed at source | Source stops within a recorded proven bound; gateway timeout alone is insufficient |
| Cache isolation | Cold/warm profiles, revocation, withdrawal, old cursor | No cross-scope rows/metadata and no old-permission result release |
| Response limits | 999/1,000/1,001 rows; just-below/above byte limit; compressed oversized body | Complete accepted envelopes only; explicit failures, bounded memory |
| Contract CPU | Maximum legal filters/date range/schema/result | All negative tests unchanged; report CPU/allocation cost before selecting a faster path |
| Soak/retention | Repeated bounded jobs, expiry, Cube restarts | No unbounded connection/cache/queue/RSS growth; retention/access races remain safe |
| Offline operation | Full demo with denied egress after dependency acquisition | No required external calls; logs/metrics do not leak canary secrets or row data |

Do not turn a noisy absolute timing threshold into a flaky CI gate. Start with invariant checks and comparative measurements. Establish service-level latency targets from the real stack before choosing an absolute performance budget. Any optimization must preserve all Task 15 security gates.

## Current release and license snapshot

These are current official project/registry observations on 13 October 2026. They are not a recommendation to upgrade every dependency. Retain the approved lockfile until a targeted change has compatibility evidence; inspect bundled/transitive notices before distribution.

| Component | Repository / observed release | License and disposition |
| --- | --- | --- |
| SQLAlchemy | Repository 2.0.54; current main release 2.1.4 | MIT. Keep the existing supported maintenance-line pin for this implementation; pooling already exists. [Current metadata](https://pypi.org/project/SQLAlchemy/), [pinned release](https://pypi.org/project/SQLAlchemy/2.0.54/) |
| psycopg | Repository/current 3.3.6 | LGPL-3.0-only per current metadata. Inventory binary wheel and bundled library notices; do not describe the whole Python stack as MIT. [Official metadata](https://pypi.org/project/psycopg/) |
| psycopg-pool | Current 3.3.3; not required | LGPL-3.0-only. Deferred alternative pool owner. [Official metadata](https://pypi.org/project/psycopg-pool/) |
| HTTPX | Repository/current 0.28.1 | BSD-3-Clause. Reuse; no migration to another HTTP client is justified. [Official metadata](https://pypi.org/project/httpx/) |
| Pydantic | Repository/current 2.14.1 | MIT. Reuse strict existing models. [Official metadata](https://pypi.org/project/pydantic/) |
| Cube Core | Latest release link resolved to v1.8.5; project runtime pin not yet established in inspected files | Repository default Apache-2.0 with package-level MIT exceptions. Verify the exact image/bundled packages, not only the root license. [Release](https://github.com/cube-js/cube/releases/tag/v1.8.5), [root license](https://github.com/cube-js/cube/blob/master/LICENSE) |
| PostgreSQL | Project's verified runtime: 17.11, pinned by image digest; no latest-release claim | PostgreSQL License. Retain the verified runtime; no server upgrade proposed here. [License](https://www.postgresql.org/about/licence/) |
| prometheus-client | Current 0.26.0; optional/deferred | Current SPDX expression is Apache-2.0 AND BSD-2-Clause. [Official metadata](https://pypi.org/project/prometheus-client/) |

## Defer or avoid

- **Defer:** psycopg_pool, PgBouncer, LISTEN/NOTIFY wakeups, a metrics exporter, manifest parse cache, larger compiler caches and alternative JSON libraries until their specific bottleneck is measured.
- **Defer beyond the approved V1:** pre-aggregations/Cube Store, Cube replicas, source-write credentials and live BigQuery performance testing. These require architecture/authorization review and separate correctness/cost evidence.
- **Avoid for this scope:** Redis/Celery/Kafka, generic agent orchestration, vector search, general SQL parsing/planning libraries, serverless queue replacements, hosted tracing by default, arbitrary user-supplied source queries and cross-principal result caches. They add trust or operational boundaries without addressing the demonstrated V1 needs.
- **Avoid misleading shortcuts:** setting the gateway timeout and claiming cancellation; using LIMIT as a cost ceiling; treating a Cube security-context field as proof of cache isolation; claiming exactly-once upstream execution from a PostgreSQL lease; bypassing strict validation to improve a benchmark; using dev mode to avoid testing the approved secure Cube configuration.

## Remaining evidence gaps

No full-stack performance, source cancellation, selected-image cache isolation, offline networking or production BigQuery cost propagation was established by this review. Tagged Cube driver source could not be opened through the research browser during this pass, so no release-specific driver cancellation claim is made. General docs and current release metadata are not substitutes for the real-stack Task 5/6/15 evidence. The existing implementation owner should integrate the small changes at the mapped tasks and retain all production gates.
