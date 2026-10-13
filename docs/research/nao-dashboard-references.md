# nao and Claude Dashboards: references for the Data Product Gateway

Research date: **13 October 2026**. Research only: no dependency installation, upstream code execution, product changes, deployment, or compatibility-test claims. This note compares official product documentation and inspected upstream source with the approved gateway [design](../superpowers/specs/2026-10-13-data-product-gateway.md), [implementation plan](../superpowers/plans/2026-10-13-data-product-gateway.md), and existing [publisher UI recommendation](publisher-ui.md).

## Recommendation

**Keep Cube Core + FastAPI + PostgreSQL + the small React publisher. Use nao and Claude Dashboards as references for a later, separate consumption/reporting experience.** They validate the value of turning an analysis into an inspectable, editable, shareable artifact. They do not remove the need for our immutable product contracts, permission filtering, tested-digest publication, per-principal results, or source-freshness evidence.

The lowest-maintenance path is to finish the approved V1, then evaluate one downstream report containing a few approved product results. Do not add dashboard authoring, another agent loop, raw SQL, a second semantic compiler, or a generated-code runtime to the publisher now. The approved V1 explicitly excludes those capabilities.

Three material caveats:

1. **nao is mixed-license.** Its root LICENSE carves out Enterprise-marked files from Apache 2.0; the README's shorter Apache statement is insufficient for reuse decisions.
2. **nao's chart pipeline is not a generic MCP-result renderer.** Its native query references recognize its SQL and semantic-query outputs. A gateway MCP result needs a deliberate downstream data-source adapter.
3. **nao's documented shared live-story behavior differs from our policy.** Scheduled refresh uses the owner's access, while our readers must be reauthorized themselves. Its last-good-data fallback is not our fail-closed freshness contract.

## Which references are likely meant?

### nao: high-confidence candidate, not a confirmed user identification

The likely project is [getnao/nao](https://github.com/getnao/nao), the open-source-core analytics agent, rather than the older nao IDE. It combines a Python context-building CLI, an agent/chat application, native charts, and dashboard/report-like **Stories**. Its [documentation index](https://docs.getnao.io/llms.txt) separates the Agent and IDE products.

Source inspection used the code-search snapshot **94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0**. The [release page](https://github.com/getnao/nao/releases/tag/v0.3.22) lists **v0.3.22, 7 October 2026**, as the latest release observed. Custom React stories appeared in [v0.3.20](https://github.com/getnao/nao/releases/tag/v0.3.20) on 5 October; v0.3.22 added custom-story support in MCP modes. This is active, recently changing application code, not evidence of a stable embeddable dashboard SDK. Do not equate the inspected development snapshot with a tested release or use `latest` for a trial.

### Anthropic: Claude Dashboards is now the strongest match

There is a specific new product, **Claude Dashboards**, announced **8 October 2026**, five days before this research. It connects to warehouses including BigQuery, Databricks and Snowflake, and other connectors such as Salesforce. This is a closer match than assuming the user means older generic Artifacts. [Official announcement](https://claude.com/resources/articles/dashboards-and-motion).

The [official getting-started guide](https://support.claude.com/en/articles/17454700-get-started-with-claude-dashboards) describes a Dashboards artifact template: Claude writes and runs a SQL query per chart, exposes that query and refresh time, supports conversational refinement, and starts private. It is beta on Pro, Max, Team and Enterprise; Enterprise owners must enable it. It is a hosted Claude product reference, not an identified open-source renderer or self-hostable distribution. No official reusable dashboard implementation or public dashboard-authoring API was established in this research.

The user's exact Anthropic reference remains unconfirmed. If they meant a particular demo, screenshot or repository, obtain that link before claiming visual parity or implementation compatibility. No clarification is needed to adopt the product-level lessons below.

## What nao actually supplies

| Area | Verified implementation | Relevance here |
| --- | --- | --- |
| Database integration | The [CLI README](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/cli/README.md) lists optional drivers for Postgres, BigQuery, Snowflake, DuckDB, ClickHouse, Databricks and others. Context sync can produce column descriptions, previews, profiling and query history. | Useful connector/context examples. Do not grant this app direct access to our source database or private Cube interfaces merely to obtain charts. |
| Business context | The CLI organizes databases, queries, docs, repositories, rules, tests and agent tools in a filesystem. [Provider extras](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/cli/pyproject.toml) include Notion and Confluence; Obsidian is documented too. | Borrow the discoverable meaning/examples pattern. Our authorized `describe_product` response should remain the source of truth; shared context dumps could disclose hidden products. |
| Executable semantics | [Semantic-layer documentation](https://docs.getnao.io/nao-agent/context-builder/semantic-layer) supports dbt MetricFlow manifests, compiled locally to SQL. Modes are semantics-first, semantics-only and disabled. Only MetricFlow is documented as the semantic engine. | This is genuine semantic execution, not just schema prompting. It is still a different stack from our selected Cube boundary; do not add MetricFlow or translate Cube models into it for this task. |
| External MCP client | [`agent/mcps/mcp.json`](https://docs.getnao.io/nao-agent/chat/capabilities/tools-mcps-skills) supports remote Streamable HTTP. Tools are discovered into per-tool schemas and invoked through `mcp_call`; remote OAuth can be per user. | A plausible future gateway consumer. Validate issuer/audience and actual user delegation; configuration support is not proof our authentication flow works. |
| MCP server | [nao MCP](https://docs.getnao.io/nao-agent/connectors/mcp) offers sub-agent mode (`ask_nao`, `get_nao_answer`) and context-layer mode (context browsing, `execute_sql`, story creation/update), plus chart/story management. | This is broader than our four-tool contract. Do not proxy it as the gateway's public API. Its agent loop and mutation tools would expand scope. |
| Charts | [`apps/shared/src/tools/display-chart.ts`](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/apps/shared/src/tools/display-chart.ts) validates query reference, chart type, axes, series, labels, bounds and table options. | Good schema-first visualization reference. A future gateway renderer should validate axes against the authorized output schema and preserve units/grain. |
| Classic stories | [`apps/shared/src/tools/story.ts`](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/apps/shared/src/tools/story.ts) defines versioned story operations and chart/table/grid/tab markup. [`story-rendering.tsx`](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/apps/frontend/src/components/story-rendering.tsx) separates markdown and structured blocks. | Prefer this constrained composition idea to arbitrary generated apps for an initial report consumer. Do not insert markdown/HTML execution into the publisher. |
| Custom apps and decks | [`apps/shared/src/story-app.ts`](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/apps/shared/src/story-app.ts) defines React/HTML apps, bounded bundles, allowed imports and the host/frame protocol. [`story-kit.ts`](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/apps/frontend/src/story-runtime/story-kit.ts) exports KPI/chart/table/narrative components and `Slide`/`Slides`. | Especially relevant to the longer-term report/deck workflow. These are internal application modules with host/runtime dependencies; do not assume an independently supported npm SDK. |
| Evaluation | CLI tests support prompt cases, expected SQL-result comparisons, tool-call assertions and repeated-run metrics. [CLI documentation](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/cli/README.md). | Later consumer-agent evals can assert use of the gateway and correct interpretation. They supplement, never replace, deterministic known-answer and permission-negative publication tests. |

nao's stack includes Fastify, Drizzle, tRPC, React/TanStack, and a Python service/CLI. Adopting the entire application would add another backend, authentication model, database schema, agent runtime and build system. Shared use of React or FastAPI does not make it a drop-in component. Sources: [README stack](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/README.md), [workspace package](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/package.json).

## License, paid boundary and self-hosting

- [The actual LICENSE](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/LICENSE) says most code is Apache 2.0, but files marked `@license Enterprise` have separate commercial terms. This carve-out was also verified at [v0.3.22](https://github.com/getnao/nao/blob/v0.3.22/LICENSE). Those terms condition production use on a valid Enterprise subscription and impose separate modification/distribution restrictions. Do not copy Enterprise code into this project or assume a root-license label clears a whole directory. Review exact files, notices and dependencies before any reuse.
- The [Enterprise feature matrix](https://docs.getnao.io/nao-agent/enterprise/overview) puts generic OIDC/Microsoft SSO, unlimited user groups, nao-managed row-level security, Redshift identity passthrough and white-labeling in the paid tier. OSS includes basic application roles and up to three groups. [`apps/backend/src/types/license.ts`](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/apps/backend/src/types/license.ts) also enumerates entitlements such as excluded columns and user budgets. This is a reason to retain our independent gateway policy enforcement, not to reproduce or bypass their licensing checks.
- [Pricing](https://getnao.io/pricing/) offers free self-hosting with your own model credentials, with Cloud and Enterprise priced by quote. [Docker instructions](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/README-dockerhub.md) support SQLite or PostgreSQL and mounted or Git context. Self-hosting the application does not make model inference free or automatically keep data inside the deployment. Ollama appears in the CLI's supported providers, but no local-inference/no-egress trial was performed.
- Self-hosted does not mean telemetry-free by default. [`cli/nao_core/tracking.py`](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/cli/nao_core/tracking.py) supplies a PostHog endpoint/default configuration and enables tracking in production unless disabled; set `POSTHOG_DISABLED=true` for an eventual isolated trial and inspect both backend and frontend too. [Langfuse tracing](https://docs.getnao.io/nao-agent/self-hosting/observability) is separately opt-in and can include full prompts/responses. Neither setting proves absence of outbound traffic.

## Security and correctness differences that matter

### 1. Result IDs are not automatically interoperable

[`apps/backend/src/queries/execute-sql.queries.ts`](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/apps/backend/src/queries/execute-sql.queries.ts) defines `QUERY_TOOL_NAMES` as `execute_sql` and `execute_semantic_query`. Native chart/story retrieval uses those stored query parts. A gateway `execution_id` returned by a generic `mcp_call` is not automatically a nao `query_id`.

**Implication:** MCP connectivity alone establishes a possible text/data consumer, not a working dashboard integration. Do not fabricate a nao SQL result, write directly to its internal tables, or expose a SQL endpoint to satisfy the renderer. Prefer our own small result-to-chart adapter, or an explicit upstream-supported data-source extension, if a later spike proves valuable.

### 2. Shared report data needs its own authorization design

The [Stories documentation](https://docs.getnao.io/nao-agent/chat/capabilities/stories#access-and-refresh-on-shared-stories) states that scheduled refresh uses the story owner's permissions and recipients see that data; a failed refresh retains the previous successful values with an error indicator. That may fit an intentionally shared report, but it does not match our gateway's per-principal result access, revocation and required-freshness rules.

By comparison, current [Claude artifact sharing guidance](https://support.claude.com/en/articles/9547008-share-artifacts) says connected-app artifacts use the viewer's own connections and inaccessible portions show errors. It also warns that shared storage can expose stored information. Borrow the viewer-bound access principle; do not infer that a static export remains revocable or safe merely because the live view is protected.

Our future reports must distinguish authorized live views from intentionally exported snapshots. Sharing a layout must not grant access to the author's rows. Exporting data creates a separate persistent disclosure that cannot be recalled by gateway revocation.

### 3. Generated code introduces a substantial new trust boundary

nao's custom-story implementation contains useful defenses:

- [`custom-story-frame.tsx`](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/apps/frontend/src/components/custom-story/custom-story-frame.tsx): `sandbox='allow-scripts'`, no same-origin allowance, no-referrer, a per-document channel and MessagePort, navigation detection, and user-gesture gates for clipboard/export.
- [`story-frame-document.ts`](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/apps/frontend/src/components/custom-story/story-frame-document.ts): restrictive CSP including `default-src 'none'` and `connect-src 'none'`; scripts are bounded to generated hashes/runtime, while font/map hosts are allowed separately.
- [`story-app.ts`](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/apps/shared/src/story-app.ts): import allowlist and 2 MiB bundle cap. [`story-app-build.ts`](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/apps/backend/src/services/story-app-build.ts) defines a bounded build timeout; the [build worker](https://github.com/getnao/nao/blob/94ae6c9844cbb1e4f9cc5a5276d951c195aa0fc0/apps/backend/src/services/story-app-build.worker.ts) rejects unsupported assets/import behavior and removes source-provided CSP/base elements before host assembly.

These are code observations, not a security audit or proof the system is safe. A gateway report must still keep tokens in its trusted host, validate every frame request, authorize each referenced result, bound resource use and reject stale-session replies. For the first reporting slice, avoid executable code entirely and render a validated chart/layout descriptor using reviewed components. The publisher remains plain-text rendering as already recommended.

### 4. Transparent SQL is a UX lesson, not a reason to expose SQL

Claude's query-inspection feature makes numbers explainable. Our equivalent should show **product name/version, definition, typed inputs, effective filters, units, grain, timezone, provenance, source watermark and warnings**. Keep Cube SQL, source credentials and unapproved internals private. Show both execution time and source freshness; a recently refreshed dashboard can still contain stale source data.

### 5. Charts must preserve the trusted result contract

Do not coerce decimal strings into JavaScript numbers and silently lose precision. Our existing fixtures deliberately include amounts beyond the safe integer range. Retain original values for tables/tooltips/export, and use an explicitly bounded display conversion only when safe. Do not silently sum unlike units or currencies, invent empty-result zeros, hide truncation/errors, or let UI filters weaken required row constraints.

nao's chart schema already exposes concepts such as `hide_total` for non-additive series, but that is a useful presentation hint, not proof that the underlying metric is valid. The gateway contract remains authoritative.

## Adopt / reference / defer / avoid

| Decision | Concrete action | Where it fits |
| --- | --- | --- |
| **Adopt now, within approved scope** | Make meaning, examples, provenance, limits and freshness easy to inspect; clearly separate saved, tested and published states. | Existing `gateway/contracts/{manifest,execution}.py`; planned `publisher/src/pages/{Product,Review}.tsx`. No dashboard dependency needed. |
| **Reference** | Use nao's chart schemas, classic block composition and story-kit components to inform a small future chart descriptor and report layout. | Upstream paths above; a separate consumer app, not `gateway/publishing` or the publisher editor. |
| **Reference** | Preserve a reviewable/versioned artifact and explain the number behind each chart, following both products' UX. | Downstream report metadata pins gateway product version/digest and execution evidence. Report edits do not republish products. |
| **Defer until V1 passes** | A read-only nao-as-MCP-client trial with synthetic data and verified identity, checking all four gateway tools. | Existing planned `/mcp`; no change to its fixed tool set. Prove connectivity separately from chart rendering. |
| **Defer** | A single report/deck consumer with a narrow data adapter; possibly MCP Apps presentation if supported by the chosen client. | Separate project/task. First prove a bar chart, a table and a KPI with the same approved result; no arbitrary code or scheduling. |
| **Defer** | Live refresh, multi-user report sharing, PDF/HTML exports, custom React/HTML stories and slide authoring. | Each needs separate authority, retention, redaction, precision and rendering acceptance tests. |
| **Avoid** | Wholesale nao fork, copying Enterprise files, swapping Cube for MetricFlow, embedding nao's authentication/policy stack, or replacing the React publisher with its BI app. | These create substantial maintenance/scope changes without completing the existing gateway. |
| **Avoid** | Raw SQL passthrough, author-owned shared result caches, stale-data fallback, source credentials in agents, direct writes to nao's query tables, and model-required default demo. | Conflicts with the approved trust boundaries or offline/account-free demonstration. |

## Smallest later experiment and its stopping condition

Only after V1's mandatory acceptance evidence exists and reporting scope is approved:

1. Use one synthetic `revenue` product and two principals. The consumer discovers/describes it, pins its version, queries it and resolves the opaque execution ID through `get_result`.
2. Map only a successful authorized `ResultEnvelope` to a fixed chart/KPI/table descriptor. Preserve the full precision and evidence metadata. Filter controls submit only approved typed inputs through the gateway.
3. Show the same product in a simple report view or deck-ready snapshot. Do not install a second semantic engine or call warehouses directly. If nao is used, first establish a supported result-source extension instead of assuming generic MCP output is chart-compatible.
4. Test empty/null/high-precision/DST cases; pending, expired, denied and stale results; logout, revocation and cross-principal access; report refresh after policy change; malicious text/links; denied outbound network. Exports, if approved, must identify snapshot time and truncation and be checked separately.
5. **Stop with a documented integration verdict**: either the existing contract supports the small consumer with a maintainable adapter, or list the exact missing renderer/identity contract and defer. A successful chart must not become an excuse to rewrite the gateway or claim production readiness.

## Verification limits

This work read source and documentation. It did not run nao, execute generated stories, connect a warehouse, test OAuth, audit dependencies exhaustively, or validate a production security claim. Product pages, development source and release notes can drift; recheck the selected immutable revision and exact file licenses before implementation. No Domo compatibility, authoritative Beast Mode migration, or existing Studio/Claude dashboard integration is established by these references.
