# Foundry-like open-source references for Data Product Gateway

Research date: 13 October 2026. Read-only inspection of public repositories, source files, repository metadata, and official documentation. No external project was installed or executed. No dependencies or gateway behavior were changed. This is a design/reference assessment, not a runtime benchmark, security audit, legal opinion, or proof of Foundry compatibility.

## Recommendation

**Yes: use these projects to sharpen the product, particularly catalog UX, explicit connector capabilities, and answer provenance. Keep Cube Core and the approved Python gateway architecture. Do not fork a whole Foundry alternative or import its code by default.**

The useful comparison is a governed business-data interface: discover a named concept, understand its contract, validate its binding, and obtain a permission-filtered answer with evidence. Our gateway already specifies most of that. Full ingestion, graph storage, writeback, workflow orchestration, and an application builder would materially expand V1.

The user did not provide a project link. Several unrelated projects use “open-source Palantir” language. OpenFoundry is the strongest name/capability match; Semantica is another credible recent referent. This research cannot establish which announcement the user saw.

## 1. Which project might they mean?

| Candidate | Verified identity and licensing | Fit for this gateway |
| --- | --- | --- |
| **OpenFoundry** | The historically circulated `DioCrafts/OpenFoundry` URL returned 404 through both web and GitHub API. The visible fork network points to [`u485349-coder/OpenFoundry`](https://github.com/u485349-coder/OpenFoundry). Its Rust/Svelte snapshot and later Go/React forks are materially different. | Closest broad Foundry-style feature reference. Repository, revision, and license must be specified, not just the product name. |
| **Semantica** | [`semantica-agi/semantica`](https://github.com/semantica-agi/semantica) is an actively maintained Python knowledge/context-graph platform. Actual [`LICENSE`](https://github.com/semantica-agi/semantica/blob/e247b94fbe4fc3a52bafae5997e48f1839e13b87/LICENSE) is MIT. Latest observed release is [v0.7.0, 22 September 2026](https://github.com/semantica-agi/semantica/releases/tag/v0.7.0); repository push activity was 12 October. | Best permissively licensed candidate for a later provenance or knowledge-graph experiment. It is not a replacement metric-serving engine. |
| **SPICE-Harvester / Spice OS** | [`ludia8888/SPICE-Harvester`](https://github.com/ludia8888/SPICE-Harvester) is Python/FastAPI/React and explicitly Foundry-inspired. Its [current README](https://github.com/ludia8888/SPICE-Harvester/blob/4b0a6529414132c0f04c1503062a79f1a3f27731/README.md) contains an open-source description but a **Proprietary** license badge. The complete tree has no license file; GitHub reports no detected license. | Useful source-visible comparison, especially connectors and lineage. Do not treat public visibility as permission to copy. |
| **Foundry Ontology Open** | [`cloudbadal007/foundry-ontology-open`](https://github.com/cloudbadal007/foundry-ontology-open) is a small Python ontology/SQLite/MCP library with an actual [MIT license](https://github.com/cloudbadal007/foundry-ontology-open/blob/b349cdc0f28fcadd60f6ab3fa669703d6f4d57ea/LICENSE). | Readable educational object/link model; too small and insufficiently governed to become our serving boundary. |
| **OSIRIS** | [`simplifaisoul/osiris`](https://github.com/simplifaisoul/osiris) describes an OSINT/geospatial dashboard rather than an enterprise metric catalog. | A substantially weaker match for this tool. Its maps and public intelligence feeds do not solve our product publishing, BigQuery, or Domo requirements. |

These are independent projects, not evidence that Palantir released the Foundry platform as open source. Palantir's [Ontology SDK documentation](https://www.palantir.com/docs/foundry/ontology-sdk/overview) explicitly describes applications using Foundry as their backend. An open SDK does not supply an independent Foundry backend.

## 2. OpenFoundry: inspect the revision, not the screenshots

### Older visible Rust snapshot

Inspected commit: [`37cf492b2cc8d2b5bb83c9a0d48979ecceb8ccda`](https://github.com/u485349-coder/OpenFoundry/tree/37cf492b2cc8d2b5bb83c9a0d48979ecceb8ccda).

- The README claims Apache 2.0, but the actual [`LICENSE`](https://github.com/u485349-coder/OpenFoundry/blob/37cf492b2cc8d2b5bb83c9a0d48979ecceb8ccda/LICENSE) is zero bytes, as is `ARCHITECTURE.md`. Resolve licensing/provenance before copying anything from this revision.
- [`services/data-connector/src/connectors/mod.rs`](https://github.com/u485349-coder/OpenFoundry/blob/37cf492b2cc8d2b5bb83c9a0d48979ecceb8ccda/services/data-connector/src/connectors/mod.rs) registers PostgreSQL, CSV, JSON, and REST modules. It is not evidence of a broad production connector catalog.
- [`services/data-connector/src/handlers/sync_ops.rs`](https://github.com/u485349-coder/OpenFoundry/blob/37cf492b2cc8d2b5bb83c9a0d48979ecceb8ccda/services/data-connector/src/handlers/sync_ops.rs) creates a sync job and immediately marks it completed; the code explicitly leaves real asynchronous synchronization for a later iteration. A successful endpoint response here does not establish data ingestion.
- The connection-list handler performs an unfiltered `SELECT` over connections. It does not itself implement our verified-principal, permission-filtered catalog contract. This is a source observation, not a complete deployment exploit assessment. [Handler](https://github.com/u485349-coder/OpenFoundry/blob/37cf492b2cc8d2b5bb83c9a0d48979ecceb8ccda/services/data-connector/src/handlers/connections.rs).

### Later Go/React fork inspected separately

Inspected [`CapTrix/openfoundry` commit `090e3f19b6b90fb77c23d779a318957c937067b2`](https://github.com/CapTrix/openfoundry/tree/090e3f19b6b90fb77c23d779a318957c937067b2). This is an illustrative surviving fork, not a claim that it is the authoritative successor. The other checked forks, `SAPSentinel/openfoundry` and `stevodevo1/openfoundry`, also report AGPL licensing; their code was not exhaustively compared.

This fork has a substantive [AGPL-3.0 license](https://github.com/CapTrix/openfoundry/blob/090e3f19b6b90fb77c23d779a318957c937067b2/LICENSE). Its [architecture](https://github.com/CapTrix/openfoundry/blob/090e3f19b6b90fb77c23d779a318957c937067b2/ARCHITECTURE.md) describes about 50 Go service directories, React, generated SDKs, and a large infrastructure stack. Adopting that platform would replace the project's deployment and maintenance model. No GitHub release was returned for this fork; its latest repository push was in May 2026. Test files and CI workflows exist, but were not executed or independently certified here.

Specific useful source references:

| Area | Actual source | Assessment |
| --- | --- | --- |
| Connector truthfulness | [`internal/adapters/capabilities.go`](https://github.com/CapTrix/openfoundry/blob/090e3f19b6b90fb77c23d779a318957c937067b2/services/connector-management-service/internal/adapters/capabilities.go) and [`skeleton_stubs_test.go`](https://github.com/CapTrix/openfoundry/blob/090e3f19b6b90fb77c23d779a318957c937067b2/services/connector-management-service/internal/adapters/skeleton_stubs_test.go) | Strong idea: model support explicitly. Excel, GraphQL, LDAP, and SFTP are explicitly tested as unimplemented scaffolds. Do not convert a connector logo into a support claim. |
| BigQuery | [`internal/adapters/bigquery/adapter.go`](https://github.com/CapTrix/openfoundry/blob/090e3f19b6b90fb77c23d779a318957c937067b2/services/connector-management-service/internal/adapters/bigquery/adapter.go) | Contains real REST discovery/query/pagination code. However, `BuildIngestSpec` describes a not-yet-wired integration placeholder; `StreamArrow` materializes results into one frame. Useful test-case reference, not a reason to replace Cube's driver or claim end-to-end sync. |
| Typed business bindings | [`proto/ontology/ontology.proto`](https://github.com/CapTrix/openfoundry/blob/090e3f19b6b90fb77c23d779a318957c937067b2/proto/ontology/ontology.proto) | Separates object type, property, link, dataset binding, version, field mapping, and dry-run results. Our original manifest/binding contracts can use the same general separation without taking on object storage/writeback. |
| Catalog discovery | [`apps/web/src/routes/search/SearchPage.tsx`](https://github.com/CapTrix/openfoundry/blob/090e3f19b6b90fb77c23d779a318957c937067b2/apps/web/src/routes/search/SearchPage.tsx) | Useful owner/type filters, marking badges, snippets, and explicit loading/empty/permission-denied states. Build a much smaller product-only UI. Do not copy its full console or imply its UI alone establishes server authorization. |
| Provenance/lineage | [`services/lineage-service/internal/openlineage/event.go`](https://github.com/CapTrix/openfoundry/blob/090e3f19b6b90fb77c23d779a318957c937067b2/services/lineage-service/internal/openlineage/event.go) | Distinguishes job, run, dataset, and input/output edges. It explicitly excludes column-level lineage. Reference the [OpenLineage standard](https://openlineage.io/docs/spec/object-model/) directly if adding interoperability later. |
| API/SDK consistency | [`docs/reference/api-sdk-mcp.md`](https://github.com/CapTrix/openfoundry/blob/090e3f19b6b90fb77c23d779a318957c937067b2/docs/reference/api-sdk-mcp.md) | Generated OpenAPI/clients plus drift checks are useful. For us, existing Pydantic contracts remain the source of truth. Automatically exposing every generated operation as an MCP tool would violate V1. |

The earlier Rust snapshot's placeholder behavior must not be attributed to every later fork. Conversely, the later fork's richer implementation does not fix the older repository or establish Palantir parity.

## 3. SPICE-Harvester: similar stack, different scope and unclear reuse rights

Inspected commit: [`4b0a6529414132c0f04c1503062a79f1a3f27731`](https://github.com/ludia8888/SPICE-Harvester/tree/4b0a6529414132c0f04c1503062a79f1a3f27731). Latest observed push: 6 April 2026; no GitHub releases returned.

Its [connector factory](https://github.com/ludia8888/SPICE-Harvester/blob/4b0a6529414132c0f04c1503062a79f1a3f27731/backend/data_connector/adapters/factory.py) enumerates Google Sheets, Snowflake, PostgreSQL, MySQL, SQL Server, and Oracle. BigQuery and Domo are not in this factory. Its [adapter contract](https://github.com/ludia8888/SPICE-Harvester/blob/4b0a6529414132c0f04c1503062a79f1a3f27731/backend/data_connector/adapters/base.py) distinguishes connection testing, public configuration, snapshot/incremental/CDC extraction, and change tokens. Those are useful ideas for a future ingestion product, but V1 is deliberately a serving gateway rather than an ingestion system.

A repository [connector-parity report](https://github.com/ludia8888/SPICE-Harvester/blob/4b0a6529414132c0f04c1503062a79f1a3f27731/foundry_connector_parity_report_2026-02-20.md) records passing focused tests alongside a failed live frontend end-to-end run and missing agent-runtime control-plane work. Treat that as author-reported historical evidence, not an independent test result. Its broad test-count/enterprise claims do not establish production readiness for our use case.

The present proprietary badge and missing license are sufficient to defer code extraction. A future licensing clarification would still leave the cost of Elasticsearch, Kafka, object storage, data versioning, workers, and writeback outside our approved scope.

## 4. Permissively licensed references worth retaining

### Semantica

Inspected commit: [`e247b94fbe4fc3a52bafae5997e48f1839e13b87`](https://github.com/semantica-agi/semantica/tree/e247b94fbe4fc3a52bafae5997e48f1839e13b87).

- [`semantica/provenance/schemas.py`](https://github.com/semantica-agi/semantica/blob/e247b94fbe4fc3a52bafae5997e48f1839e13b87/semantica/provenance/schemas.py) makes a valuable distinction between the previous version of a fact, a fact derived from another source, inputs used, and invalidation. Apply that conceptual distinction to product versions versus source/model lineage. It does not require adopting the library.
- [`semantica/ingest/bigquery_ingestor.py`](https://github.com/semantica-agi/semantica/blob/e247b94fbe4fc3a52bafae5997e48f1839e13b87/semantica/ingest/bigquery_ingestor.py) uses Google's SDK and can turn table/query results into graph-oriented documents. This is a different abstraction from our reviewed metric products; importing it would introduce a parallel BigQuery execution path.
- [`semantica_mcp/mcp/server.py`](https://github.com/semantica-agi/semantica/blob/e247b94fbe4fc3a52bafae5997e48f1839e13b87/semantica_mcp/mcp/server.py) is a stdio JSON-RPC server, while [`session.py`](https://github.com/semantica-agi/semantica/blob/e247b94fbe4fc3a52bafae5997e48f1839e13b87/semantica_mcp/mcp/session.py) maintains shared process-level graph/vector state. This inspected surface is not a substitute for our authenticated, user-bound HTTP service. That is a scope observation, not a claim all Semantica deployment modes lack security.
- Its [package metadata](https://github.com/semantica-agi/semantica/blob/e247b94fbe4fc3a52bafae5997e48f1839e13b87/pyproject.toml) pulls a substantial scientific/graph dependency set. Add it only if a real graph/provenance requirement justifies that footprint and passes a separate compatibility/security spike.

### Foundry Ontology Open

The small [MCP server](https://github.com/cloudbadal007/foundry-ontology-open/blob/b349cdc0f28fcadd60f6ab3fa669703d6f4d57ea/foundry_ontology/mcp/mcp_server.py) is useful for learning object discovery and descriptions. But it exposes action execution, uses a fixed `mcp-client` actor for writes, and returns all matching search results through `q.all()`. Do not adopt it as the gateway's authentication, bounded query, or read-only tool layer. Its MIT license makes selective reuse possible subject to notices and review; it does not make its behavior a fit.

## 5. What to take into our product

These are prioritized recommendations, not authorization to expand the implementation plan.

1. **Adopt the presentation pattern now:** product detail should make business meaning, owner, grain, units, freshness contract, version, and examples easy to scan. Add clear validation and permission states to the already-planned catalog/publisher UI.
2. **Adopt an explicit adapter capability view:** show supported types/filters, limit and cancellation evidence, freshness evidence, and separate “implemented,” “mock-verified,” and “live-verified” states. BigQuery configuration is not a green live-connection check. This extends existing adapter-contract thinking rather than adding a second connector registry.
3. **Reference lineage, keep implementation small:** present a permission-filtered chain from product version to binding/model digest, test evidence, execution, and watermark. Start with existing registry IDs and links; defer a graph database and full graph editor.
4. **Adopt contract drift checks:** derive REST/MCP schemas and UI types from the gateway contract models. Keep the four fixed tools and ensure they call the same service-level checks. No automatic whole-platform API-to-MCP exposure.
5. **Defer operational ontology/writeback:** object relationships, action types, branching pipelines, ETL, notebooks, dashboards, and orchestration become relevant only if the user later chooses that product expansion. Do not let the reference platform's breadth define our V1.

### “Pulling APIs” versus “pulling code”

- **Calling an API:** requires a running, versioned instance and explicit authentication/authorization mapping. None of these repositories supplies free access to Palantir's private systems or customers' data. A future adapter could call a selected instance through our existing gateway boundary; our principal, policy, limits, and freshness checks remain mandatory.
- **Reusing an SDK/library:** can save transport/model code but does not supply the remote service, trust model, or proven business semantics. For BigQuery, retain Cube's already-selected driver path. For Domo, retain the separate read-only adapter and PDP/Beast Mode/card reconciliation gates in `integrations.md`.
- **Copying or modifying source:** requires a specific repository/commit/file license and notice review. MIT/Apache components may be suitable after review; AGPL code carries materially different copyleft/source-offer obligations, including conditions for modified network-served software. Do not mix it into a proposed Apache-licensed gateway casually. Separate API use is a different engineering boundary, not an automatic legal exemption. SPICE's current rights ambiguity and the empty OpenFoundry root license are blockers to extraction until clarified.
- **Forking a whole platform:** inherits storage, migrations, operations, dependency, security, and upgrade responsibilities. It would be more work and risk than our focused V1 needs.

## Decision

**Proceed with the current build. Use Foundry-like projects as design/test references. Keep all external code imports deferred.** The best near-term payoff is a trustworthy product catalog and a visible explanation of where an answer came from, with truthful connector status. A full ontology platform is a later product decision.

If the user supplies the exact announcement/repository, recheck that specific current branch, license, and implementation before changing this assessment. No live-source integration, third-party CI pass, production security, or runtime performance was verified in this research.
