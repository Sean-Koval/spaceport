# Configured source pilot and team product workflow

Design candidate, 15 October 2026. Prepared against commit `fe6055f`, tree `a3155cbfb8fc5c073a4c2d184ecd75d0b99d0024`; parent reports merged main `3fa6b2f` has the same tree. This document proposes implementation, not authorization to create cloud resources, credentials or grants, query organization sources, or deploy. No implementation accompanies it.

## Outcome and bounded scope

An operator can configure existing GCP/GKE/registry/IdP resources once, install an immutable reviewed product package, let an author test and a separate publisher publish it, and explicitly check the configured source through the ordinary governed execution path. The resulting product executes through genuine BigQuery or Domo serving adapters under the existing REST/MCP result contract. It must not stop at a structurally valid but permanently disabled catalog.

The first runnable source sample is a weekly account-usage report with DAILY account rows for a seven-day interval. This is deliberately not a new weekly grain or a weekly distinct-user metric. BigQuery and Domo implement the same business contract through different reviewed source paths. Neither defaults to synthetic revenue when configuration is missing.

The existing demo/v1 entrypoints remain unchanged. Browser authorization-code/PKCE, generic connector discovery, arbitrary SQL/model upload, ETL, new cloud projects/clusters, public exposure, production capacity and a broad frontend API are outside this slice. The initial deployment uses existing approved GKE, Cloud SQL, IdP and Secret Manager resources. Rendering never applies anything.

## 1. Files, immutable identities and schema

Implement strict bounded JSON models with unknown fields forbidden, duplicate keys rejected, no-follow reads, stable-file checks and credential-free errors. All reference digests are recomputed from retained bytes. An arbitrary boolean or operator label cannot supply capability or observed source evidence. Operator-owned allowlisted source configuration records approved serving intent, not a measured verification result.

### SourcePilotConfig version 1

Required fields:

- `schema_version: 1`, `profile: "reviewed-source-pilot"`.
- Existing canonical `public_origin`, OIDC issuer/JWKS/audience/client/algorithm configuration, registry host/database/CA reference and optional existing hash-only service-auth reference. Keep the existing RFC 9068 checks and exact `/mcp` audience.
- `product_catalog_ref`: bounded local package-index path plus exact digest.
- `source_catalog_ref`: bounded local source-descriptor index plus exact digest.
- `identity_mapping_ref`: separate private reviewed principal/policy/grant input. Not a model or template field.
- `runtime`: fixed supported worker count, process shutdown bounds, source-session limits and exact image identities. Start with one API and one worker/reconciler; no unproven HA claims.
- `probe_product_id` and `probe_principal_id` explicitly selected; no implicit revenue/north identifiers. Probes do not dispatch source queries.

Configuration describes the actual new runtime. Add an explicit production-only `Settings.source="reviewed_sources"` value in `gateway/auth/settings.py`, assembled only by the new validated source-pilot composition. Demo mode rejects it; the existing synthetic pilot still rejects it; neither old selector silently enables live data. The new factory requires the reviewed-source profile plus valid complete catalogs before sockets. Do not add a new selector to `pilot.json` while continuing to launch the old synthetic-only `gateway.pilot_runtime`, or falsely label a live catalog as `synthetic_postgres`. Employee token verification remains unchanged.

### ProductPackage version 1

Required identity: `package_id`, positive `revision`, immutable `package_digest` calculated over the following retained references and contents:

- `manifest`: the existing `Manifest` shape; public fields and schemas unchanged.
- `consumer_binding_ref` and `fixture_binding_ref`: exact binding IDs, revisions and content digests.
- `consumer_source_ref` and `fixture_source_ref`: exact connection/source IDs, revisions and descriptor digests; distinguish `consumer` and `fixture_test` contexts.
- `bundles`: retained model/security assets for Cube or retained statement/definition catalogs for Domo. The package index points to reviewed image assets, not arbitrary executable modules loaded from author input.
- `policy_seed_ref`: exact policy ID/revision/content digest.
- `fixture_catalog_ref`: exact contract/catalog digest, fixture principals and immutable known-answer, input-negative and permission-negative cases.
- `consumer_check`: an immutable finite normalized request and expected rows for an ordinary published product check. Generate the private CLI `SourceCheckPlan` after loading the installed package: it carries the actual package digest, source identity, manifest digest and public provenance. The package never hashes a derivative plan that hashes the package back, avoiding a circular identity. These observed checks are not an activation or permission switch.
- `fixture_equivalence_ref`: exact reviewed mapping between consumer and fixture semantics. Different dataset/statement/bundle identities must be individually pinned by this mapping, not ignored through a broad equality exclusion.

Canonical reference shapes are `AssetRef = {path, sha256}`, `BindingRef = {binding_id, revision, content_digest}`, and `SourceRef = {provider, source_id, revision, descriptor_digest, context}`. Paths are bounded relative paths beneath a trusted package root; no absolute paths, parent traversal, URL fetches or symlink resolution. Registry identity references use exact identifiers and revisions, never paths. Common catalog lookup uses `source_id` for both providers; any Domo `connection_id` is provider-specific connection metadata, not an alternative public lookup key. Source fixture/consumer refs must name distinct identities in the selected supported topology.

A package is a full reviewed template with a specific product ID and initial version. It is not a generic model-upload facility. Author metadata edits may retain a package's execution contract only when that adapter permits them; Domo pins owner/policy as well as the query version. Changing the source, output contract, policy, limits, fixture answers or exact Domo request/version requires a newly reviewed package/catalog revision. The old package remains retained for published versions and result replay.

For multiple products, use a bounded index (initial maximum 25 packages, 50 source descriptors); maintain maps keyed by immutable IDs/revisions/digests. Reject duplicate identities and conflicting reuse. The initial demonstrated workflow may use one product per connector.

### BigQuery source descriptor

Fields owned and finalized by the BigQuery implementation:

- `provider: "bigquery"`, `source_id`, positive `revision`, `context`, `capacity_pool_id`.
- `auth: {"mode":"gke-workload-identity","expected_principal":"<approved workload identity>"}`. No `keyFilename`, raw credentials, token URI, environment fallback or dummy secret reference.
- `billing_project`, `data_project`, `dataset`, exact allowlisted tables/views and `location`.
- Exact consumer/fixture Cube endpoint and retained bundle identity. Private HTTPS endpoints and distinct fixture identity are required.
- `max_jobs_per_execution` (at most 8), `per_job_maximum_bytes_billed`, `total_execution_bytes`, source/global job-concurrency bounds, and `job_timeout_ms` (at most 120000).
- Freshness mode explicitly represents a supported coherent producer checkpoint or `unknown`. Required freshness without a proven mechanism blocks; unknown is permitted only when the manifest explicitly accepts unknown freshness.

The first implementation has ONE supported GKE credential strategy, WIF. Existing-secret service-account support is a future explicit variant, not an ambient fallback. An expected-principal string is a configuration expectation, not proof of the runtime's actual identity.

Use the shared registry-backed source sessions/jobs proposed by the BigQuery designer. The serving Node process has a separate `bq_source_runtime` database role limited to reviewed ledger operations. Do not change the previous POSIX admission library, add a second ledger PVC, or give Node application policy/results/owner access.

### Domo source descriptor

Fields owned and finalized by the Domo implementation:

- Runtime adapter discriminator `domo-named-v2`; preserve offline `domo-named-v1` unchanged.
- Common `source_id`, positive `revision`, `provider: "domo"`, `context`; optional separately pinned provider `connection_id` identifies the underlying service-client connection.
- Fixed allowlisted API origin and exact verified tenant/org and source service-principal identity.
- `auth.mode: "oauth-client-credentials"`, `client_id_ref` and `client_secret_ref` to existing private mounted files. Secret formats/read limits are provider-specific, not the synthetic 43-character key reader.
- Exact dataset UUID allowlist, definition/schema/statement/catalog digests and bounded dispatch/concurrency policy. Do not accept qualification booleans as source guarantees.
- `auth_policy: "shared-service-identity"` explicitly disclosed. It does not inherit employee Domo PDP.
- Reviewed finite normalized request + current exact authorization-scope variants. No interpolation, dynamic date compiler, arbitrary filters/sort or discovery.
- Optional coherent hidden checkpoint. With no provider-coherent checkpoint, freshness is unknown; a required-freshness query fails closed.

Source capacity ownership is registry-stable: aliases, descriptor/binding revisions, credential rotation and cloned config must resolve to the same recorded `capacity_pool_id` for the same underlying bounded source authority. Do not create a new capacity pool implicitly to escape held/unknown work. Changing a pool assignment requires an explicit reviewed registry transition with old reservations accounted for. Fixture data authority uses its fixed approved fixture principal, but fixture admission/budget ownership remains the author actor, matching existing `held_source_capacity` behavior; otherwise an author could evade their cap through fixture identities.

A source binding must dispatch to the adapter identified by its retained immutable source descriptor and adapter revision. Never infer source type from a URL, table identifier, runtime environment variable or public request.

## 2. Employee access is separate from source access

1. Employees/agents authenticate to Spaceport using a gateway RFC 9068 access token. CLI `--token-file` reads that token into memory and sends it only to the canonical gateway HTTPS origin from the trusted local pilot/client configuration. An optional `--gateway` must normalize to that exact configured origin including port/TLS; it is never an arbitrary credential destination. All operation/review/receipt files pin the same origin and refuse cross-origin resume. The browser still uses its current in-memory session while PKCE remains a separate task.
2. The gateway resolves `(issuer, subject)` to a server-owned principal and current policy/tenant roles and entitlements. JWT group claims do not create grants.
3. Cube services authenticate signed gateway execution contexts, with separate consumer/fixture configuration.
4. The connector obtains source authority through its own WIF identity or Domo OAuth client, never the employee bearer. Source credentials do not appear in manifests, browser state, API responses, generated shell arguments or ordinary logs.

Generalize the new bootstrap mapping to explicit `policy_id`, `tenant_id`, `roles` and `entitlements`, keeping one unambiguous tenant scope per principal/policy. Roles use existing capability names; preserve author/publisher separation. Fixture identities are internal and cannot receive external service hashes or match OIDC users.

`source check` uses an ordinary mapped consumer with current execute/result authority for the exact published product and policy/tenant scope. It has no privileged verification capability and cannot query an unpublished manifest. Each real scope in a live test matrix uses its own authenticated token and current grants; do not let the CLI choose a claimed user/tenant to impersonate.

## 3. Bootstrap and run entrypoints

Proposed new entrypoints:

- `python -m gateway.source_pilot_bootstrap migrate`.
- `python -m gateway.source_pilot_bootstrap provision --plan-file <private-reviewed-plan>`.
- `uvicorn gateway.source_pilot_runtime:app_factory --factory ...`.
- `python -m gateway.source_pilot_runtime worker` and `retention`.
- BigQuery consumer/fixture service: `node cube/bigquery-runtime/server.cjs` with its exact per-context config.
- Source check: the ordinary gateway REST execution path called by the supported CLI. No new privileged runner, bypass route or source-verification owner kind.

`migrate` uses the existing owner-only role separation plus reviewed new source ledger migration. It does not create database roles, cloud identities or secrets. The operator creates required DB roles separately through an approved process; migration validates their actual attributes and grants.

`provision` imports the exact reviewed package catalog, source/policy/binding rows, principal mappings, fixture capabilities and initial grants. It has a canonical plan digest and one transaction/advisory lock. It is additive for new immutable package revisions; conflicts fail rather than overwrite. Replaying the same plan verifies original identities and never restores revoked principals/grants, rewinds policy heads, creates a draft or publishes. New plans may add explicitly named grants; they cannot implicitly reconcile a whole organization directory.

Runtime loads the bounded catalog instead of hardcoded `load_assets()` revenue/bookings. `ChoiceService.templates` receives approved package manifests. The fixture-catalog provider selects the exact product/binding/execution-contract, rather than one global catalog. Existing services remain the sole authorization and publication path.

API startup and health/readiness are configuration, migration, registry and local process checks only. No OAuth token acquisition, source metadata call, Cube warmup query, refresh job or paid read is hidden inside a readiness handler. BigQuery server startup must also prove source background paths cannot bypass admission.

## 4. CLI commands and precise side effects

Provide a supported Python module first (`python -m scripts.spaceport ...`); an installed `spaceport` console entrypoint can wrap the same code. All parsing failures use redacted errors, not argparse's echo of potentially sensitive values.

### Offline commands

- `pilot init --directory ...`: write nonsecret templates, no overwrite.
- `pilot validate --config ...`: parse/cross-check local config, package/source/bundle identities and references; no credential reads or network.
- `pilot render --config ... --output ...`: render the complete configured GKE application bundle and a separate activation overlay. Never apply or run cloud CLIs.
- `product validate --package ...`: validate exact manifest/binding/policy/fixture/source mapping and source-specific request coverage offline.

Validation reports unsupported input combinations, not invented compatibility. A missing secret version is a reference prerequisite, not a request to enter a secret value. Output separates `config_valid` from `source_unverified` and `deployment_unverified`.

### Explicit ordinary source check

`source check --gateway ... --product-id ... --version ... --plan-file ... --token-file ... --operation-file ... --receipt-file ...` selects an exact installed reviewed known-answer request for an ALREADY PUBLISHED product and submits it through the existing POST executions / authorized GET result endpoints. It uses the same source sessions, current policy, exact binding, type/row/byte/scan/concurrency bounds and revocation checks as every other consumer request. It cannot accept inline SQL or execute an arbitrary unpublished manifest.

Before first publication, `product test` executes only the separately approved isolated fixture context. Fixture success permits ordinary publication under existing controls; it is not live consumer-source verification. An operator-owned reviewed source allowlist and budget enable the selected serving path. There is no `verified=true` flag, publish-inactive state, privileged qualification permit or later activate API.

The first separately authorized live source check occurs after publication under narrowly scoped pilot consumer grants. The command is an explicit source read, with its operation/request/source bounds displayed in a safe local summary. Installing configuration or publishing does not itself run it. Human authorization for actual source I/O, costs and intended result access remains required for this work; a CLI flag alone cannot manufacture that authorization.

Persist the same canonical query/idempotency key durably before the first POST. The accepted-execution sidecar pins the origin and complete operation digest; normal resume uses GET only and may use a refreshed token because the server reauthorizes that execution. A missing sidecar requires explicitly reviewed `--retry-ambiguous` with the original credential fingerprint and exact key. A corrupt sidecar never permits fallback POST. Pending source checks return exit 4, `outcome_status=pending` and `observed=null`. Rerun with the same operation file and a new receipt output path to observe completion; exit 0 with a passed known answer is distinct from pending. A new run is a separate explicit command and operation identity.

The receipt distinguishes configured source/package/plan identities from observed existing REST result provenance, product/version/digest, schema, row comparisons and pass/fail/unknown outcomes. The current public `ResultEnvelope` exposes no provider-job/ledger evidence, actual source principal or source terminal proof; the CLI must not invent those fields or infer per-job cap enforcement from a successful result. Detailed source-control evidence stays in internal source/runtime verification. No new public ledger read surface is part of this slice. It never serves as an authorization switch. No raw provider errors, SQL, credentials or organization rows in ordinary receipts/logs. Requested data can go to a private output file. A test cannot claim unavailable Domo PDP/scan/cancellation guarantees from an operator assertion.

Offline `pilot validate` does not authenticate to providers. A future bounded metadata-only source inspection, if added, must also be explicitly invoked and never called by readiness or normal validation. No additional provider-specific metadata browser is needed for this slice.

### Author/test/review/publish through existing REST

- `product save --manifest ... --gateway ... --token-file ... --expected-revision N --receipt-file ...`: POST or exact PUT draft. Return immutable draft ID/revision/digest and UI link. No automatic retries or subsequent tests.
- `product test --draft-id ... --token-file ... --receipt-file ...`: explicit POST tests; fixture source I/O only. Does not query organization consumer data. If fixtures themselves use cloud APIs, that fixture source has its own explicit approved source identity, scope and budget.
- `product preview --draft-id ... --test-run-id ... --token-file ...`: GET exact fixture results. This pre-publication preview is read-only and clearly labeled isolated fixture results.
- `product review --draft-id ... --token-file ... --receipt-file ...`: GET current diff and its server-selected test run; report stale evidence and exact tested digest/revision.
- `product publish --draft-id ... --review-receipt ... --token-file ... --reason-file ...`: independent publisher session, reread current diff and evidence, require exact match to the reviewed receipt, then POST publish. Return publication receipt only after established success.
- `product query --product-id ... --version ... --request-file ... --token-file ... --operation-file ...`: explicit post-publication consumer run using POST executions and authorized GET/paging. This is the live preview, with source spend and result-retention implications stated before invocation.

All queries pin version and use a persisted idempotency key created before sending; resume uses the same body/key and exact receipt. A terminal failed request is not silently retried with a new key. No mutations replay automatically after network failure. Lost save/test responses are uncertain; reconcile using authorized reads without declaring someone else's concurrent run to be this command's receipt. Publication can be reconciled only with an authorized exact version/digest read; otherwise remains uncertain.

Requested query rows go to a specified private output file or an explicitly requested interactive display, never ordinary operational logs. Tokens are never in flags, shell examples, environment dumps, receipts or persisted operation files. Client endpoint redirects are refused and credential files use bounded no-follow checks.

## 5. Concrete weekly-account-usage example

This is fictional reusable fixture data, not a claim about the user's actual tables or metric.

- Product ID: `weekly-account-usage`; initial version `1.0.0`; owner `product-analytics`; domain `customer-success`; classification `internal`.
- Meaning: count of accepted, deduplicated usage events, by UTC calendar day and account, within an inclusive-start/exclusive-end reporting window. Source producer/approved definition owns deduplication semantics. Do not call this active users or add daily distinct-user counts into a weekly unique-user count.
- Inputs: `start` DATE, `end` DATE, nullable optional `account` STRING default null.
- Outputs: `day` DATE, `account` STRING, `usage_events` exact DECIMAL(38,0), serialized as a plain decimal string.
- Grain: `daily`; timezone `UTC`; interval `[start,end)`; no aggregation across accounts after policy filtering. `usage_events` units are `events`. The initial manifest has no optional filters or sorts (`filters={}`, sort fields empty/max_fields 0). Input audit redaction is start/end digest and account omit.
- Consumer source: an operator-specified BigQuery table/view such as `example-project.analytics.account_usage_daily`, OR one explicit Domo dataset UUID containing the same reviewed meaning. These are alternative deployment examples, not runtime failover sources.
- Hidden source columns: `tenant_id` and `access_group`; independent mandatory constraints are applied BEFORE aggregation. Account IDs are not global tenant IDs.
- Pilot limits: max 7 days, 100 rows, 65536 normalized bytes, 100-row pages, per-principal concurrency 1, deployment concurrency 2, 30-second execution deadline and up to 5 seconds synchronous wait. No silent truncation.
- BigQuery proposed per-operation budget: at most 4 admitted jobs, at most 104857600 bytes per job and 419430400 total reserved bytes across all subordinate jobs. These are configurable example caps, not a claimed monetary price or sufficient quota. Actual request-construction and provider tests must show every job path observes them.
- Domo: finite one-dispatch variants, zero automatic retries and configured shared dispatch/concurrency/quarantine bounds. No invented byte-scan or provider-cancellation promise.
- Freshness: the easiest own-table pilot sets `required=false`, `meaning=source_load`, max age 86400 seconds and displays `unknown` when no coherent producer checkpoint exists. A checkpoint-enabled package must instead prove rows and checkpoint are coherent; query time or later metadata is not a substitute. A required-freshness query remains blocked without that proof.

Fixed fixture window: start `2026-10-05`, end `2026-10-12`.

Fixture source rows (hidden scope columns included only here):

| tenant | group | day | account | usage_events |
| --- | --- | --- | --- | --- |
| north | core | 2026-10-05 | acct-a | 120 |
| north | core | 2026-10-06 | acct-a | 180 |
| north | special | 2026-10-05 | acct-b | 50 |
| south | core | 2026-10-05 | acct-a | 900 |

Expected checks:

1. North-all principal, entitlements `core,special`, null account: three rows, exact strings `120`, `180`, `50`; no south `900` row. Compare multisets unless an approved sort is explicitly part of the request.
2. North-core principal, entitlement `core`: two acct-a rows only.
3. North-all with account `acct-a`: the same two acct-a rows.
4. North-core with account `acct-b`: zero rows, freshness unknown. With optional freshness this is a valid result; a required-checkpoint package must instead exercise unknown-freshness rejection outside current publication fixture kinds.
5. A principal with no consumer grant: `NOT_FOUND_OR_FORBIDDEN`, zero source calls. A south principal accessing the north-only Domo product must also be rejected before source I/O.
6. Eight-day range and malformed/unknown fields: `INVALID_INPUT`, zero source calls.
7. Unknown Domo date/account/filter/sort combination: `INVALID_INPUT`, zero source calls even when it is otherwise a valid manifest request.

For Domo, the initial catalog explicitly retains the four positive request/scope pairs above, separately for consumer and fixture plans. Hashes include normalized product/version/request and exact authorization scope (including principal and policy revision). Fixture principals have separate variants. Domo does NOT accept arbitrary seven-day windows because the manifest's range bound alone does not create query variants. New employee scopes/policy revisions or versions require reviewed catalog additions. CLI prints the finite supported requests; it never interpolates SQL.

The author receives the reviewed manifest/template, edits allowed descriptive meaning/examples metadata, saves, runs fixtures, previews those fixture results, and sends the exact draft to the separate publisher. Publication requires exact current fixture proof and reviewed package/binding/policy consistency. It makes no claim of consumer-source verification. A later explicit consumer `source check` or `product query` runs the organization-source report through the same bounded path.

## 6. Complete render-only GKE installation output

Bring-existing prerequisites: project/cluster/location, WIF-enabled nodes as applicable, managed Secret Manager add-on, approved private networking/egress, private Cloud SQL with required minimal roles and CA, existing source/fixture datasets, IdP/client and exact secret versions. No namespace/IAM/credential/account/resource is actually created by rendering.

Output includes all application pieces needed after approved operator actions:

- API, worker/reconciler and retention Deployments, private Services and process probes.
- Separate suspended owner migration/provision Jobs. No init hook automatically runs them or submits source checks; source-check CLI is run separately after publication.
- Pinned Publisher image and same-origin TLS edge routing with SPA fallback, `/v1`, `/mcp` and `/.well-known/oauth-protected-resource/mcp`.
- For BigQuery: distinct consumer/fixture Cube Node workloads and private services, their signing/auth/config mounts and minimal source-ledger DB connections; coherent TLS handling using the selected reviewed topology. No public Cube ports or unguarded developer/SQL interface.
- For Domo: only the workload-specific existing source-credential refs actually needed by API fixture execution and worker consumer execution; no credential mount in Publisher/edge. A separate connector service is not required unless the Domo design selects one.
- ConfigMaps, CSI references, regular-file memory snapshots, resource requests/limits and controlled network policies. Numeric secret versions and immutable config/image identities throughout.
- Networking requires an explicit existing approved egress topology: named proxy/static private Google API endpoints, DNS and GKE metadata routes as actually supported. Native Kubernetes NetworkPolicy is not an FQDN allowlist. Never render internet-wide TCP 443 and call it domain-restricted. Source transports also pin exact approved hosts and reject redirects; neither host pinning nor NetworkPolicy alone substitutes for the approved network path. If the selected private route/proxy or metadata policy is unresolved, rendering reports an activation prerequisite rather than opening broad egress.
- A generated manifest inventory, prerequisite report, exact command/run order and separate `activation/` overlays.

Base review bundle has zero serving replicas/suspended Jobs. The explicit activation overlay enables the reviewed serving replicas; separately selected one-shot Job manifests perform migrate and provision in order. Source checking uses the ordinary published execution path afterward. There is no permanently dead zero-replica installation, and there is no single bulk-apply directory that accidentally creates grants or starts all Jobs. Activation overlays do not create source credentials, IAM grants or DB roles.

No cloud apply code is included. The operator inspects the generated bundle/overlay and executes the separately approved action in their chosen environment. Document concrete `kubectl` command order, but do not execute it in this work.

Only process/local-registry checks run before first publication. `/readyz` must not dispatch a hidden source query or remove all authoring endpoints before the first product. Source status reports `configured-unverified`, `observed pass for <exact revision/run>`, `observed failure`, or a bounded blocker. A prior observed pass is not current connection health, standing authority or a new execution grant.

Rollback is a controlled application image/config revision change, preserving immutable published definitions and registry ledger history. Do not automatically migrate the database down, delete source reservations, revoke/regrant identities, delete datasets/volumes or roll secret versions backward. Coordinate old/new runtime ledger-schema compatibility and source fencing; unresolved provider work remains reserved.

## 7. Module ownership and interfaces

### Setup/product-workflow implementer

Own the reviewed-source integration in existing `gateway/auth/settings.py` and new `gateway/source_pilot_config.py`, `gateway/product_packages.py`, `gateway/source_pilot_bootstrap.py`, `gateway/source_pilot_runtime.py`, `scripts/spaceport.py`, CLI HTTP/receipt helpers, new `infra/gke/source-pilot/` templates/renderer, weekly sample packages, and corresponding setup/CLI/GKE tests and runbook.

Compose common Services and the source-adapter factory supplied by connector owners; do not implement duplicate source security logic. Runtime factory receives validated `ProductCatalog`, `SourceCatalog`, Registry, Authenticator and a source-runtime registry. Bootstrap accepts an explicit approved `ProvisionPlan`; no side effects at import or normal startup.

### BigQuery implementer

Own `cube/bigquery-runtime/`, explicit WIF provider-auth construction, source session/job ledger integration, signed gateway context through actual pinned Cube server hooks, verified completion/reconciliation/cancellation semantics, BigQuery source descriptor validation and source-control evidence/known-answer cases. Coordinate source ledger migration/common ownership with Domo implementer before touching shared schema.

### Domo implementer

Own `domo-named-v2` binding/transport/auth/source checks, typed source preparation and adapter-specific binding/scope/normalization/replay/fixture-equivalence seams. Own the minimal common binding/runtime-dispatch changes needed by both connectors, with BQ coordination. Existing v1 Cube public persistence/contract stays readable; never disguise Domo plans as Cube queries.

### Shared coordination files: single owner before edits

`gateway/api/services.py`, `gateway/publishing/service.py`, `gateway/publishing/test_runner.py`, execution worker/service/results, registry models/migrations and runtime dispatch are shared integration points. Coordinator assigns one editor per file. Setup requests package/fixture providers through explicit interfaces rather than patching connector internals concurrently.

Common source-check client: load an installed fixed known-answer request, invoke existing execution/result APIs, compare authorized typed results, and write a redacted receipt. Setup owns this CLI wrapper; source owners supply internal adapter evidence, while the CLI uses only the existing public result/provenance contract and labels catalog source identities as configured. No new verification ledger or owner kind.

## 8. Bounded acceptance and proof levels

1. Offline init/validate/render run with sockets, credential reads and cloud subprocesses forbidden. Unknown profiles, source mismatches, mutable references, path escapes, duplicate keys and forged capability facts fail closed. No fallback to synthetic.
2. Two package fixtures cannot cross-wire source IDs, policies, templates, fixture catalogs or binding revisions. Original v1 assets/hashes and REST/MCP public schemas remain unchanged unless a deliberately reviewed additive field is strictly necessary.
3. Genuine local registry + source-runtime boundary tests exercise author save, actual fixture execution, exact diff, separate publisher and consumer execution for both BQ and Domo. Provider HTTP may be guarded synthetic transport, but actual production adapters/auth/normalization/ledger code must run. Label this offline integration, not live-provider acceptance.
4. Guarded actual pinned Cube/Google package tests show WIF construction and all source request serialization paths; Domo actual HTTP transport tests show token exchange and named query boundaries without live credentials.
5. CLI tests prove exact canonical-origin/token binding, cross-origin resume rejection, token secrecy, no redirects, exact immutable receipts, no fabricated ledger evidence, stale review refusal, same-key query recovery, uncertain mutation outcomes, denied actor/no source calls and explicit source-versus-fixture preview labeling.
6. GKE rendering supplies every selected application component and matches real runtime/module names. Structural/schema tests cover security context, separate mounts, active overlay, source ledger isolation and no automatic source probes. Live admission/mount/TLS/network tests remain NOT RUN until separately authorized.
7. Publish/test do not fabricate live-source evidence. Source checking runs only an ordinary exact published request under current consumer authority and approved source configuration/budgets. Fake SDK/HTTP results cannot be reported as live-provider observations. Required freshness and unsupported budget/PDP/provider-completion guarantees remain blockers or explicitly unavailable capabilities, never user-configurable success booleans.
8. Run existing regression suites plus both new connector/runtime/CLI/package cases. Real-provider verification, cloud apply and browser PKCE are separately reported; passing source tests does not close them.

## External references used for identity/resource design

- GKE Workload Identity Federation: https://docs.cloud.google.com/kubernetes-engine/docs/how-to/workload-identity
- BigQuery query permissions: https://docs.cloud.google.com/bigquery/docs/running-queries#required_permissions
- GKE Secret Manager add-on: https://docs.cloud.google.com/secret-manager/docs/secret-manager-managed-csi-component

WIF supplies source API credentials, not employee SSO. Secret Manager projections can refresh, but this application's bounded init snapshots require a rollout to use new bytes. BigQuery job creation permission belongs on the query project and read permission on the approved tables/views; authorized views may avoid underlying-table grants. Exact role/driver/API compatibility remains an authorized target qualification responsibility.

## Component review outcome

Independent architecture reviewer approved this setup/product-workflow component on 15 October 2026 after closing exact-origin token binding, configured-versus-observed receipt evidence, registry-stable capacity pools and author-owned fixture budgets. The additive `reviewed_sources` auth selector requires new-composition validation plus explicit old demo/pilot rejection regressions. This is design approval only. Parent must record implementation authorization and file ownership; shared source-ledger and source-dispatch integration retain their separate review gates.
