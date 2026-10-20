# Data Product Gateway design specification

Version 0.1 · 13 October 2026 · Written design for approval  
Working name only · Proposed behavior, not implemented or tested

## Decision and purpose

Build a self-hosted, open-source serving platform through which teams publish trusted data products and existing agents discover and query them on a verified principal’s behalf. Use Cube Core as a separately deployed upstream semantic engine. Build our own catalog, publishing workflow, policy enforcement, API and MCP interface. No vendor-hosted control plane or paid account is required for the demonstration.

Studio already provides chat agents, skills and hosted MCP connections using OAuth or API keys. This platform supplies governed data capabilities to that environment rather than replacing it. BigQuery is a primary data source, but some authoritative metrics and transformations remain in Domo Beast Modes and card logic. Salesforce, Zendesk, Jira and Confluence are future sources or contextual links, not new V1 connectors.

Success means a publisher can explain, test and release a reusable product once, and two agent clients can retrieve the same approved answer without receiving database credentials or unrestricted query access.

## Architectural choice

Three approaches were considered:

- **Own publisher around Cube Core, selected.** Reuse semantic execution while owning product contracts, discovery and governance. The cost is an explicit integration and compatibility boundary.
- **Build a new metric compiler.** Maximum flexibility, but joins, aggregation correctness and query optimization would dominate the project before users receive a useful product.
- **Adopt a hosted semantic platform wholesale.** Faster access to an established administration experience, but conflicts with the required independent, self-hosted product.

Cube’s repository defaults to Apache 2.0, with package-level MIT exceptions. Preserve upstream notices and review the actual dependency inventory before distribution. Recommend Apache 2.0 for our original code, subject to owner approval; no public release is authorized by this design. [Upstream license](https://github.com/cube-js/cube/blob/master/LICENSE).

## V1 boundary

V1 includes Docker Compose, a separate Cube Core service, a Python API/MCP service, PostgreSQL registry and audit storage, and a small React publisher UI. A second PostgreSQL database holds synthetic business data behind Cube. The default demonstration needs no external account, model provider or cloud service.

The UI has a permission-filtered catalog, product detail page, versioned definition editor, test results, and a publish/deprecate flow. Search is ordinary catalog text search; embeddings are unnecessary.

BigQuery uses Cube’s existing driver path. V1 includes configuration documentation and mocked adapter-contract tests; a live BigQuery smoke test is optional and must be reported separately. Domo is Phase 1b, subject to the gates below.

Excluded: ETL, general metric compilation, raw SQL tools, cross-product joins, arbitrary formulas, dashboards, chat-agent orchestration, workload scheduling, source writes, production credentials and public deployment. A product can reference an already reviewed Cube model containing joins; agents cannot invent joins. On-demand query processing is included, but recurring business workloads are not.

## Components and trust boundaries

The React UI and MCP clients call the Python gateway. The gateway owns authentication, authorization, catalog filtering, manifest validation, lifecycle changes, execution records and result normalization. A worker from the same Python service processes bounded on-demand execution jobs stored in PostgreSQL.

Cube receives only gateway-built queries and server-created, short-lived security context. It is private to the Compose network; clients cannot reach its REST, SQL, GraphQL or development interfaces. Configure production-mode authentication even in the synthetic stack. Semantic models are reviewed, versioned deployment assets, not executable code uploaded through the editor.

The gateway uses Cube’s REST `/cubejs-api/v1/meta` to validate bindings and `/cubejs-api/v1/load` for execution. Cube’s documented `Continue wait` response is a pending state, not success or failure. Exact release-specific behavior is an implementation compatibility test. [REST reference](https://docs.cube.dev/reference/core-data-apis/rest-api).

Registry and demo data use different database roles and databases. Only UI/gateway ingress is published; stateful services remain private. Pre-aggregations and Cube Store are outside the initial demo. Evaluate their required topology before claiming production-scale readiness. [Cube Core deployment guidance](https://docs.cube.dev/cube-core/deployment).

Pin exact images, SDKs, protocol versions and digests during implementation after compatibility checks. Do not choose invented or floating “latest” versions in a release.

## Product contract and registry

Each product has a stable ID and immutable published versions. A manifest contains:

- Name, plain-language definition, examples, owner, domain and documentation/source links; lifecycle status is separate registry metadata.
- Typed inputs and outputs, nullable fields, enums, defaults, permitted filters/operators and bounded sorting.
- Grain, units/currency, timezone, date boundaries, aggregation meaning and freshness requirements.
- An approved semantic-member binding or reviewed named-query binding, plus adapter and binding revisions.
- Policy references, data classification, parameter-redaction rules and test fixtures with expected answers and tolerances.
- Manifest schema version, product version, content digest and semantic-model bundle digest.

Credentials are external secret references configured by operators, never manifest values. Product descriptions and source links are data, not instructions an agent should execute.

Core records are Product, ProductVersion, DraftRevision, BindingRevision, PolicyRevision, TestRun, Execution, Result and AuditEvent. Store immutable manifests as validated JSON with searchable catalog fields. Tests identify the exact draft digest, binding digest, policy revision and fixture version they covered.

Published manifests, test evidence and binding revisions cannot be edited in place. Lifecycle status and the current-version pointer are separately audited registry state. Semantic bindings must remain resolvable against their pinned model bundle; unavailable or changed bundles produce BINDING_DRIFT rather than silently changing an answer. Supporting an older product version requires retaining its compatible bindings.

Publication atomically creates a version and advances the current-version pointer. A query without an explicit version resolves that pointer once and pins it. Deprecation permits existing use with a warning; withdrawal blocks new execution and result retrieval. Rollback changes the pointer to an existing compatible version and is audited. Access-policy changes take effect immediately and are independently versioned.

## Publishing workflow

1. An authorized author creates a draft from an approved binding and supplies its business definition, ownership, schemas, policy references and examples.
2. The editor validates structure, unknown fields, filter bounds and binding existence. It does not accept SQL, scripts or secrets.
3. Test runs execute known-answer cases and permission-negative cases in an isolated test context. Results display expected versus actual values, freshness evidence and errors.
4. A publisher reviews the definition, test evidence and version diff, then publishes that exact digest. Any subsequent edit invalidates prior test approval.
5. The product appears only to authorized consumers. Its detail page shows meaning, limits, freshness contract, version, provenance and examples.
6. Owners can prepare a new version, deprecate or withdraw a release. Every transition records the actor and reason.

Separate consumer, author, publisher and operator capabilities. Authors cannot change their own grants or attach a broader policy without policy-administration permission. Publisher rights do not automatically grant access to underlying data. Demo accounts can combine roles explicitly; production requires a documented approval model.

## MCP and query lifecycle

Expose four fixed tools:

- `search_products`: permission-filtered search with bounded pagination.
- `describe_product`: authorized contract, supported inputs, examples and provenance for a version.
- `query_product`: product ID, optional version, typed inputs, allowed filters and optional idempotency key.
- `get_result`: an opaque execution ID and optional pagination cursor.

No tool accepts SQL, source URLs, credentials, policy overrides, arbitrary Cube members or a claimed user ID. The REST API invokes the same authorization and execution service.

A request is authenticated, version-resolved, authorized and schema-validated before the adapter sees it. The gateway derives mandatory row constraints and builds a canonical query solely from the approved binding. Unknown inputs or attempts to weaken mandatory filters are rejected.

Execution progresses through accepted, running, succeeded, failed, timed_out or expired. A synchronous wait lasts at most five seconds, then returns the execution ID. The worker polls pending Cube responses within a 120-second execution deadline with bounded backoff. Idempotency is scoped to principal, product version and canonical inputs; conflicting reuse is rejected.

Initial defaults are 1,000 rows and 1 MiB per execution, 200 rows per page, two concurrent executions per principal, ten per deployment and a 366-day maximum date range for date-based products. Products may tighten these limits. Over-limit results fail explicitly rather than returning an unmarked partial answer. Cursors are opaque, expiring and bound to the requesting principal.

A result envelope includes execution ID/state, product ID/version/digest, typed schema, rows, units, grain, effective date range/timezone, authorized provenance, row count, truncation status, execution timestamps, source watermark, freshness assessment, cache status and warnings. Decimals are lossless strings with declared precision; dates and timestamps have explicit formats. Never equate “fetched now” with “source data current.”

Required freshness that cannot be proven returns FRESHNESS_UNKNOWN; an exceeded threshold returns STALE_DATA. No silent stale fallback. Other stable errors include UNAUTHENTICATED, NOT_FOUND_OR_FORBIDDEN, INVALID_INPUT, BINDING_DRIFT, UPSTREAM_UNAVAILABLE, RESOURCE_LIMIT and EXECUTION_TIMEOUT. User-visible errors contain a request ID and safe explanation, never raw SQL or upstream secrets.

Gateway timeouts stop waiting but do not prove upstream cancellation. Adapter validation must identify enforceable source-side time/cost limits; production use remains blocked where runaway work cannot be bounded.

## Identity and authorization

Local demo mode uses isolated synthetic identities and fixtures, binds only to loopback, and displays a persistent demo banner. Startup rejects demo identity mode with non-demo sources or public ingress configuration.

Production validates access tokens from a configured OIDC issuer, with audience, signature, allowed algorithms, expiry and bounded JWKS caching. Reject ID tokens used as API access tokens, wrong audiences, expired tokens and unknown keys. Unverifiable identity or policy state fails closed.

Map the verified subject to server-managed roles, tenant and data entitlements. Never trust agent-supplied identity fields, forwarded arbitrary headers or role lists in tool arguments. For delegated user access, Studio must supply a verifiable user-bound token accepted by the gateway. A service API key maps only to its scoped service principal; it cannot impersonate whoever the agent names.

Source service credentials have only the required read/query permissions. They do not inherit a user’s source permissions. Explicit gateway policies and enforced source predicates establish access. The gateway creates Cube security context from its authorization decision; Cube independently rejects missing context and enforces mandatory row constraints. [Security context](https://docs.cube.dev/docs/data-modeling/access-control/context).

Apply authorization to search, descriptions, examples, linked metadata, execution, results, pagination, caches and audit views. Unauthorized and nonexistent product IDs share the same outward error. Recheck current permissions before starting work and on every result fetch, including cached results; revoked access cannot retrieve previously created output.

V1 result storage is isolated by principal, policy revision, product version and canonical query. No cross-principal result-cache reuse. Verify Cube cache isolation under the actual row/tenant policy; disable unsafe caching or isolate instances when correctness cannot be demonstrated. A cache hit never substitutes for authorization.

## Source adapters and validation

The adapter contract covers binding validation, query construction, execution/polling, typed result conversion, freshness evidence and sanitized failures. Each adapter declares capabilities and rejects unsupported filter, type, date or limit behavior.

The Postgres-through-Cube adapter is the complete V1 reference path. Use deterministic fixtures containing two access groups, empty results, nulls, decimal values, date boundaries and a recorded source watermark.

BigQuery remains behind Cube. Validate parameter mapping, decimal/timestamp conversion, region configuration, scoped credentials and limit behavior with contract fixtures. Cube documents BigQuery connectivity and service-account permissions; no live success is implied by that documentation. Do not enable source-writing pre-aggregations in V1. [BigQuery configuration](https://docs.cube.dev/admin/connect-to-data/data-sources/google-bigquery).

Domo Phase 1b uses a separate, read-only gateway adapter for reviewed named queries, not a claimed native Cube Domo driver. Before enabling it, validate the actual API, tenant identity model, PDP behavior, service-account reach, supported query semantics and cost/rate limits. Reconcile each product against its authoritative card, Beast Mode or approved dataset query; do not assume those definitions already exist in BigQuery. If equivalent permissions or business logic cannot be demonstrated, that product remains unavailable.

## Audit and operations

Audit publication, policy changes, query decisions, result access and administrative actions. Record verified actor, service client, product/version, policy revision, decision, parameter names and keyed digest, timing, row count, adapter and correlation ID. Exclude tokens, credentials, raw results, SQL and unapproved parameter values. Sensitive diagnostic logs are disabled by default.

The application role cannot update audit events. This is not a claim of tamper-proof storage against database administrators. Default result retention is 24 hours and redacted audit retention is 30 days; operators must approve production retention and storage encryption. Execution retries cannot bypass audit recording; if the required audit write fails, reject the operation.

Provide health/readiness checks, migration checks, secret-redacted logs and backup/restore instructions. The synthetic demo must run with outbound network access blocked after images and dependencies are obtained. Disable telemetry and prove there are no runtime vendor-cloud calls rather than relying on configuration names alone.

## Acceptance and release gates

The implementation is accepted only with recorded evidence that:

1. A fresh Compose install can create, validate, publish, discover and query a synthetic product, then restart without losing registry state.
2. Two distinct agent clients use one unchanged published product through the fixed MCP contract and obtain the same reconciled answer. Record exact clients; untested Studio integration remains an explicit gap.
3. Known-answer tests cover totals, grain, nulls, decimal precision, date boundaries, empty results and freshness failures.
4. Two access profiles receive their allowed rows and cannot discover restricted metadata, query another scope, forge identity, fetch another principal’s result or bypass revocation through caches/cursors.
5. Draft edits, concurrent publication, binding drift, upstream outage, pending responses, timeouts and over-limit output behave as specified.
6. Network evidence shows no required SaaS calls. BigQuery mock tests and any optional live tests are reported separately.

Production readiness additionally requires verified OIDC delegation, policy review, source cost limits, cache-isolation tests, TLS, restricted ingress/egress, secrets management, restore testing and operational ownership. Domo requires its separate identity/PDP and reconciliation gates. A successful synthetic demo satisfies none of these automatically.

## Initial work packages and approval

Work divides into contract/registry, identity/policy, Cube/adapters, API/MCP execution, publisher UI, and integration/operations verification. These are ownership boundaries, not an implementation sequence or estimate.

Approve this written design, or request changes to its scope and contracts. The next artifact is a detailed implementation plan for review and execution-method selection. Implementation begins only after those approvals.

