# Publisher

A same-origin React catalog, structured publisher and bounded single-product result view. The gateway remains authoritative for validation, access, evidence and lifecycle. Synthetic demo labeling is present on every route. No bearer or private query cache is persisted.

## Reproducible development

Use Node **24.19.0**, npm **11.9.0** (the exact verified runtime), then:

```sh
npm ci --ignore-scripts
npm run generate
npm test -- --run
npm run typecheck
npm run build
```

TypeScript 5.9.3 is intentionally selected instead of the latest 7.x: the locked openapi-typescript 7.13.0 generator declares TypeScript ^5.x support. All direct dependencies are exact versions; package-lock.json records transitive versions/integrity. `evidence/registry-metadata.txt` records official npm registry engine/peer/license checks; the lockfile is the installation authority. Generated types come only from `../docs/compatibility/openapi.json`. The structured form uses that same bundled local schema for shape assistance, never remote references or a second semantic validator.

`npm run dev` binds 127.0.0.1 and proxies `/v1` to 127.0.0.1:8000. The production build is `dist/`. Serve it at `/`, use SPA fallback for `/catalog`, `/products/*`, `/drafts/*`, and proxy same-origin `/v1` unchanged. Fixture-test requests can take up to the backend execution deadline; allow a 180-second proxy read timeout. There are no runtime CDN, telemetry, remote font, icon or schema requests.

## Actual browser acceptance

`npm run test:e2e` is a real-gateway gate, not a mocked browser smoke test. It fails explicitly if prerequisites are absent. Required environment:

- `GATEWAY_E2E_BASE_URL`: local same-origin publisher + real REST runtime, for example `http://127.0.0.1:8080`.
- `GATEWAY_E2E_TOKEN_FILE`: private file outside the repository containing the separately generated `demo-author`, `demo-publisher`, `demo-north`, and `demo-south` synthetic bearer values. Never print, attach, commit or record the file in a trace.
- Optional `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`: explicitly reviewed local browser binary. Otherwise install/use the browser revision pinned by Playwright 1.64.0.

The runtime must have real PostgreSQL registry/source and the reviewed Cube consumer and isolated-fixture deployments, approved revenue policy and bindings, fixed fixture principals/catalog, and server-managed demo identities. **Start clean with no revenue draft/publication.** The suite creates the draft via the structured template, runs actual fixtures, publishes via an independent publisher session, then verifies exact north/south scoped decimal rows, permission-negative evidence, narrow layout, keyboard actions, and that the browser makes no external requests. The first saved editor URL populates a checked immutable draft locator shared by subsequent serial cases; missing or malformed locators fail before route use. Two subsequent serial tests exercise real concurrent save/publication conflicts and a dropped response after a genuine publication commit; the final current version is 1.0.2 while 1.0.0 remains queryable. `/readyz` may correctly remain unavailable until the first publication. The template is the repository's inert synthetic fixture; bootstrap must use the same fixture contract. Ordinary restarts preserve existing data and are not clean-bootstrap acceptance runs.

```sh
GATEWAY_E2E_BASE_URL=http://127.0.0.1:8080 \
GATEWAY_E2E_TOKEN_FILE=/private/synthetic/demo-tokens.json \
npm run test:e2e
```

With `GATEWAY_OPERATIONS_EVIDENCE_DIR`, the suite writes `publisher-junit.xml`, `publisher-results.json`, and `publisher-network.json` there; otherwise reports use `publisher/test-results/`. Network evidence records only methods, origins, pathnames and resource types, never headers, bodies or query strings. Traces, videos and automatic screenshots are disabled because they can capture sign-in bearer values. A successful component suite is presentation evidence only. It is not real backend, denied-egress stack, production, accessibility or live-connector acceptance. The three browser cases are implemented but remain unrun here. Required browser cases for policy/binding/evidence drift, revocation/network projection, empty/expired/degraded results, lifecycle effects, full keyboard/zoom and screen-reader evaluation are not implemented in this suite and remain open acceptance work. Source-limit/offline runtime gates require separate actual-stack evidence.

## Data and authority boundaries

- Draft IDs are immutable revisions. A successful save reads and follows the receipt's new ID. A conflict never substitutes a new expected revision automatically.
- Full replacement requires `editable_full_definition`; projections remain read-only. Binding/policy values come only from approved choices, not arbitrary entry fields.
- Review uses the test run selected by the server diff and rereads the diff. Missing/mismatched/stale evidence or absent capability hints disables publication. Local edits block review of the unsaved definition.
- Tests are one synchronous POST. Reads and remounts do not start tests. Mutations are never automatically retried or replayed offline.
- Publication success requires its matching receipt or an authorized read of the exact version/digest. A lost response stays uncertain if that read cannot establish its outcome.
- A concrete access denial unmounts protected fields/results, cancels pending reads and discards session-local read caches. Explicit reload starts a new authorized view; old in-flight operations cannot restore it or navigate a newer view. Non-denial validation errors preserve edits.
- Lifecycle controls require an explicitly opened, authorized existing published/deprecated version matching the reviewed product and version. That target is read-only; sessions without consumer read access cannot dispatch lifecycle changes.
- Unknown freshness is a warning. Execution completion, fixture reference watermark and observed source-load watermark are distinct. Connector support notes make no live-health claim.
- Original components only; no Helix Foundry code or logos were copied.
