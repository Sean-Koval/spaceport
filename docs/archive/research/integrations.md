# Integration and SDK research

Verified 2026-10-13 against primary documentation, published package metadata, and tagged source. This is an implementation recommendation, not compatibility-test evidence. No source code, dependency pins, lockfiles, credentials, or external services were changed by this research. Scope follows the approved design and plan: separate Cube Core, Python gateway, four fixed MCP tools, synthetic default, BigQuery through Cube, Domo in Phase 1b.

## Decisions at a glance

| Choice | Decision and boundary |
| --- | --- |
| Official Python `mcp==2.3.0` | Adopt as the candidate for Task 11. Use `MCPServer`, authenticated Streamable HTTP, and existing gateway services. Pin after an actual compatibility spike. |
| Official TypeScript `@modelcontextprotocol/client==2.3.1` | Adopt for the independent client harness. This is the split v2 client package; do not install the old monolithic SDK expecting v2 APIs. |
| Cube Core `1.8.5` | Candidate for Task 5's compatibility/image-digest selection. Preserve the separate service. Do not mistake verified release existence for a verified Docker image or working stack. |
| Existing `httpx==0.28.1` | Keep for the small Cube REST adapter. Reuse a bounded async client; no additional Cube-specific Python framework is needed. |
| Existing Pydantic contracts | Reuse for REST and MCP schemas. Keep manifest-aware input checks in the shared service. No parallel manifest/schema framework. |
| Cube BigQuery driver | Adopt only through the existing adapter/profile boundary. Mock contracts now; live and production activation remain separate gates. |
| Official `pydomo==0.3.0.16` | Defer installation. Useful reference/client candidate for a later read-only Domo adapter, not a policy or semantic-equivalence solution. |
| Third-party FastMCP wrappers, automatic FastAPI-to-MCP exporters, direct BigQuery Python serving path | Avoid for this V1: they add a second integration surface or bypass the approved fixed-tool/Cube boundary. This is a fit decision, not a claim those projects are unsafe. |

## 1. Verified package and maintenance facts

- Python MCP 2.3.0 was released on 2026-10-02. PyPI identifies the official repository, MIT license, Python >=3.10, and trusted-publishing provenance. Its release notes require `httpx2>=2.10.0`; tagged metadata also requires Pydantic >=2.12.0 and lockstep `mcp-types`. The existing Python 3.12/Pydantic 2.14 baseline satisfies those declared floors, but the resolved ASGI dependency set still needs testing. Keep the existing HTTPX Cube adapter separate from MCP's HTTPX2 transport rather than performing an unrelated HTTP client migration. [PyPI](https://pypi.org/project/mcp/2.3.0/), [release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0), [tagged metadata](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/v2.3.0/pyproject.toml)
- TypeScript client 2.3.1 was released on 2026-10-05. Its tagged package declares Apache-2.0 and Node >=20. Lock the actual supported Node runtime with the harness; a minimum engine constraint is not an LTS recommendation. The root TypeScript documentation URL still describes v1, so follow its v2 documentation link or the tagged README. Preserve the v2 package's actual license instead of copying the older v1 MIT statement. [Release](https://github.com/modelcontextprotocol/typescript-sdk/releases/tag/v2.3.1), [tagged package](https://raw.githubusercontent.com/modelcontextprotocol/typescript-sdk/v2.3.1/packages/client/package.json), [documentation routing](https://ts.sdk.modelcontextprotocol.io/)
- Cube 1.8.5 was released on 2026-10-13; 1.8.4 on 2026-10-12 included an API-gateway member-expression SQL-evaluation injection guard. Use a release containing that fix, while continuing to reject arbitrary member-expression objects in this gateway. The repository license defaults to Apache-2.0 with package/header MIT exceptions. Image contents, notices, digest and transitive inventory remain Task 5/14 checks. [Releases](https://github.com/cube-js/cube/releases), [license boundary](https://github.com/cube-js/cube/blob/master/LICENSE)
- PyDomo 0.3.0.16 was published on 2025-11-12 under MIT. Its README still says 0.3.0, so use registry metadata when evaluating a pin. Official Domo documentation links this repository, but the older release and loose Python requirement do not demonstrate current Python 3.12 compatibility. Evaluate dependencies and maintenance again at Phase 1b. [PyPI](https://pypi.org/project/pydomo/), [official SDK link](https://www.domo.com/docs/s/article/360042936554)

## 2. MCP implementation seam

### Transport and lifecycle

Use `from mcp.server import MCPServer`. SDK v2 supports protocol 2026-07-28 and older revisions. The modern protocol is sessionless; `stateless_http=True` affects the older transport leg. Prefer modern clients and a stateless older-client compatibility path, since the approved four tools do not need server-to-client sampling, subscriptions, or resumable sessions. Record the actual negotiated revision for each client instead of claiming all revisions were tested. [Deployment behavior](https://py.sdk.modelcontextprotocol.io/run/deploy/), [migration guide](https://py.sdk.modelcontextprotocol.io/migration/)

Create the MCP ASGI app before startup, then enter its session manager through FastAPI's parent lifespan. Mounted applications do not automatically run their own lifespans. Keep the final endpoint exactly `/mcp`; mounting a default `/mcp` sub-app beneath `/mcp` can accidentally create `/mcp/mcp`. Route ordering and OAuth metadata discovery must be integration-tested. Use explicit Host and Origin allowlists, with proxy forwarding trusted only from the configured ingress. [ASGI integration](https://py.sdk.modelcontextprotocol.io/run/asgi/)

Implementation choices:

- Register exactly the approved four functions. Do not expose publisher/admin REST routes as tools.
- Disable unused subscriptions with the v2.3 option. Application `query_product`/`get_result` already supplies the persistent execution lifecycle; do not adopt a second experimental task framework.
- Enforce the gateway's 64 KiB request cap at the edge and application boundary. SDK defaults are not the product contract.
- Treat unsupported protocol negotiation, malformed JSON-RPC, Host/Origin rejection and application errors as distinct transport cases with sanitized responses.

### Authentication and identity

Implement the SDK's `TokenVerifier` as an adapter over Task 3 verification. Return `AccessToken` only after verification; keep `subject` separate from OAuth `client_id`. Resolve `(issuer, subject)` to the server-managed principal rather than trusting client capabilities, headers, or arguments. The SDK exposes the current access token through request-local context.

Set audience checking explicitly. `validate_token_resource` currently defaults effectively to false when omitted. Enable it for URL-bound audiences; for an issuer-specific API audience, verify that configured audience in the shared verifier instead. The SDK's HTTP resource-server layer supplies protected-resource metadata and authentication challenges; it does not replace OIDC verification or product authorization. In-memory `Client(server)` and stdio bypass HTTP authorization, so neither proves the authentication gate. [Official authorization integration](https://py.sdk.modelcontextprotocol.io/run/authorization/)

Required HTTP tests: absent/wrong/expired token, wrong audience, changed principal between calls, revoked policy on result fetch, actual OAuth metadata URL, unauthenticated metadata versus private routes, unknown arguments, and REST/MCP parity. Keep all four tool handlers thin enough that authorization occurs in the same services as REST.

### Output schemas and wire limits

Return existing Pydantic envelope models with explicit structured-output requirements. SDK v2 emits both structured content and a textual representation by default. Scalars/lists can acquire a `result` wrapper, whereas model outputs remain object-shaped. Snapshot schemas and wire envelopes so clients do not accidentally depend on a different wrapper. [Structured output](https://py.sdk.modelcontextprotocol.io/servers/structured-output/)

Important inferred interaction: a business envelope within 1 MiB can exceed 1 MiB after text duplication, JSON escaping and protocol metadata. Python 2.3.0's SSE client limit defaults to 1 MiB per event. Test maximal legal strings and metadata through both real clients; either define a bounded larger transport ceiling or deliberately format compact text alongside structured data. Do not silently reduce the approved business limit or disable all bounds. [SSE size option](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)

The SDK's OpenTelemetry API is a no-op without an installed/configured exporter. Avoid SaaS exporters and automatic instrumentation in the offline demo. Verify runtime egress; do not patch private SDK internals merely to remove no-op spans. [Tracing behavior](https://py.sdk.modelcontextprotocol.io/run/opentelemetry/)

## 3. Cube client and query compatibility

Keep a deliberately small typed adapter using the existing HTTPX pin and an operator-owned base URL. A reused client provides connection pooling. Set connection/read/pool timeouts and response-size limits separately from the overall execution deadline; pass per-execution authorization on each request rather than mutating a shared client's default headers. Never follow redirects to arbitrary destinations or fetch manifest links. [HTTPX client behavior](https://www.python-httpx.org/advanced/clients/), [existing pin metadata](https://pypi.org/project/httpx/0.28.1/)

Use only `/meta` and `/load` for the approved binding/execution path. Prefer POST for load to keep query parameters out of URLs and avoid URL-size limits. **Set `cache=must-revalidate` explicitly** for ordinary execution, or use a separately tested `no-cache` profile. `/load` defaults to `stale-if-slow`, which does not satisfy this project's freshness contract. Do not copy raw annotations, echoed queries, or upstream errors into the public result. [REST reference](https://docs.cube.dev/reference/core-data-apis/rest-api/reference)

HTTP 200 can contain the special pending error `Continue wait`. Retry the same canonical query/security context with bounded backoff, one execution deadline and correlated request IDs. This response does not establish cancellation. Cube's cache freshness also does not establish the business source-load watermark. [REST transport behavior](https://docs.cube.dev/reference/core-data-apis/rest-api)

The current REST reference documents an internal running-query cancellation endpoint. It is a possible later adapter optimization, not a new public MCP tool and not proof that BigQuery/Postgres work stopped. Before using it, test request-ID correlation, authorization, multi-query isolation and actual source cancellation for the pinned image.

Security context must be minted by the gateway and independently checked by Cube. Enforce mandatory predicates in Cube even when the gateway already adds them. Do not forward user access tokens to Cube. [Security context](https://docs.cube.dev/docs/data-modeling/access-control/context)

`contextToAppId` isolates compiled models only. `contextToOrchestratorId` is the relevant additional boundary for connections, queues and result caches. Tenant-only isolation does not itself prove safety for two principals with different row entitlements in the same tenant. Use correct mandatory predicates and adversarial cache tests; retain separate instances or fail closed if isolation cannot be demonstrated. Production defaults to a Cube Store cache/queue backend, so the no-Cube-Store demo must explicitly configure and verify the selected supported topology. [Configuration](https://docs.cube.dev/reference/configuration/config)

Contract-test the exact mapping from gateway filter operators to Cube operators, including null semantics. For half-open `[start,end)` intervals, test `afterOrOnDate` plus `beforeDate` with explicit timestamps/timezone and DST boundaries; do not assume two date-only `dateRange` values preserve the gateway's exclusive end. Use strings for numeric filter values where required by the Cube format, after domain validation. Never allow a client to send Cube's more expressive nested formulas or member objects. [Query syntax](https://docs.cube.dev/reference/core-data-apis/rest-api/query-format)

## 4. BigQuery profile: supported boundary and production gate

Configuration belongs to Cube/operator settings: project ID, explicit dataset location and secret reference. The documented read/query roles are BigQuery Data Viewer and BigQuery Job User. Pre-aggregation writes require broader permissions and remain disabled. Do not confuse Cube Cloud's documented OIDC federation setup with proof of a self-hosted deployment's credential path. [Cube BigQuery configuration](https://docs.cube.dev/admin/connect-to-data/data-sources/google-bigquery)

Tagged 1.8.5 source shows positional parameters and standard SQL for normal queries, integer wrapping, and a polling timeout that attempts `job.cancel()`. Its normal query path does not include `maximumBytesBilled` or `jobTimeoutMs`. Therefore source inspection does **not** establish a per-query cost ceiling or guaranteed runtime stop. Record those capabilities as unproven; do not invent an environment variable or claim gateway timeout solves them. Source-level limits would require an explicitly reviewed driver change/profile and tests. [Tagged driver](https://raw.githubusercontent.com/cube-js/cube/v1.8.5/packages/cubejs-bigquery-driver/src/BigQueryDriver.ts)

BigQuery offers query cost controls and daily quotas, but a row count cap is not a scan-cost bound. Production enablement must specify the billing model, actual enforcement mechanism and evidence. A mocked acceptance fixture cannot prove source enforcement. [Google cost controls](https://docs.cloud.google.com/bigquery/docs/best-practices-costs)

Schema consequences for the existing contract:

- NUMERIC supports precision 38 and scale 9. Full BIGNUMERIC supports approximately 76.8 digits with scale 38, exceeding the gateway's current precision-38 ceiling. Reject unsupported bindings unless a reviewed source projection proves a narrower exact contract; never cast to float or silently round.
- TIMESTAMP is an absolute instant; DATETIME is civil time. Require a reviewed timezone conversion instead of appending `Z` to timezone-free data.
- Include INT64 values beyond JavaScript's exact range, nulls, decimal exponents, nonfinite FLOAT64 values, and unsupported arrays/structs in mocked contract cases. The gateway's existing wire types remain authoritative. [Google type semantics](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/data-types)

## 5. Domo Phase 1b: concrete read-only research path

The official portal now distinguishes Platform OAuth APIs, Product APIs and App Framework APIs. Platform clients have scoped, expiring tokens; Product tokens expose their Domo user's broader UI capabilities. App-session APIs are a different context again. Keep these identity models separate; an API credential does not automatically represent the gateway's end user. [API families](https://www.domo.com/docs/portal/API-Reference/overview), [client-credentials authentication](https://www.domo.com/docs/portal/1845fc11bbe5d-api-authentication)

Verified endpoints worth a later authorized spike:

1. Platform dataset query: POST `/v1/datasets/query/execute/{dataset_id}` with a SQL string. Results include columns, metadata, positional rows, counts and a `fromcache` field. Only the operator-reviewed named-query adapter may construct that SQL; agents still provide typed parameters. The documentation does not establish prepared-parameter binding, PDP delegation semantics, cancellation or a source-cost bound. Treat these as gates. [Dataset query](https://www.domo.com/docs/portal/52fd0777839f4-query-a-data-set)
2. Platform chart definition: GET `/v1/cards/chart/{cardUrn}` returns calculated-field definitions and chart configuration. This is a useful read-only reconciliation source for authoritative card logic, not proof the dataset query endpoint executes those calculations. [Chart definition](https://www.domo.com/docs/portal/42d54af2095ac-get-chart-card-definition)
3. Product Beast Mode metadata: documented search and template-read endpoints expose expressions, modification/checksum information and card/dataset links. These require a distinct Product API credential and should be an offline/operator reconciliation workflow rather than a credential available to agent tools. [Beast Modes](https://www.domo.com/docs/portal/bff3ab39f7a6b-beast-modes)
4. Dataset metadata distinguishes `dataCurrentAt`, `updatedAt`, schema and PDP status/policies. Evaluate `dataCurrentAt` as potential freshness evidence only after establishing its exact meaning for the selected product and upstream refresh process. [Dataset metadata](https://www.domo.com/docs/portal/3b1e3a7d5f420-data-set-api)

The official PyDomo source confirms a thin dataset-query wrapper but also imports pandas, includes broad mutation methods, and exposes raw upstream text in some exceptions. Prefer a narrowly scoped adapter with explicit safe errors; do not hand the SDK object to tools. Installing PyDomo is optional, not a prerequisite to this research path. [SDK dataset implementation](https://raw.githubusercontent.com/domoinc/domo-python-sdk/master/pydomo/datasets/DataSetClient.py)

Required Phase 1b evidence: exact tenant/API/identity and scopes; two principals with different PDP entitlements; credential-owner/admin reach; revocation; card filters, Beast Modes, aggregation grain, date windows and timezone reconciliation; empty/null/decimal results; query limits/rate handling; stable freshness evidence; and drift detection. If equivalent authorization or semantics cannot be demonstrated, keep the product unavailable. Do not advertise a native Cube Domo driver, create an execution stub that returns success, or use a bulk export as a shortcut around the governed query.

## 6. Reusable schemas and next implementation checks

Use the current Pydantic models as the single gateway contract source. Generate and snapshot their JSON Schemas/OpenAPI, then expose matching MCP input/output schemas and UI types. Pydantic provides JSON Schema generation and distinguishes validation/serialization modes; test the mode appropriate to each wire boundary rather than hand-maintaining two schemas. [Pydantic JSON Schema](https://docs.pydantic.dev/latest/concepts/json_schema/)

The adapter capability profile should describe supported types/operators, date semantics, watermark evidence, enforceable source time/cost controls and cancellation evidence. Keep it operator-controlled. No new connector registry, ORM, policy engine or workflow framework is justified by these findings.

Acceptance additions for the existing task owners:

- Task 3/11: distinguish subject from client identity; explicit audience validation; real HTTP authorization tests.
- Task 5/6: select a security-fixed Cube image and digest; configure no stale fallback; verify cache/queue topology and same-tenant/different-entitlement isolation.
- Task 6/13: half-open date boundaries, lossless decimals/large integers, BIGNUMERIC rejection, pending 200 responses and unproven source limits.
- Task 11: parent ASGI lifespan, exact `/mcp` and discovery routes, fixed tools, negotiated protocol evidence, bounded near-limit wire output, and two real independent clients.
- Task 13: keep BigQuery mocked versus live evidence separate; retain Domo as unavailable until its identity/reconciliation gates pass.
- Task 14: capture lockfile/license inventory and real blocked-egress evidence. None of the release/documentation checks above substitutes for those runtime tests.
