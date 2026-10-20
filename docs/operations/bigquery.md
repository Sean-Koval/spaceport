# BigQuery-through-Cube operational gate

Verified 13 October 2026. **Mock contracts: PASS. Live BigQuery: NOT RUN. Production: BLOCKED.** These are separate evidence levels; no Google project, dataset, credentials, query, paid account or cloud deployment was used. The default demonstration remains synthetic PostgreSQL with no external account.

An executable, offline-tested source-control library and separately retained fictional v2 assets are now described in [BigQuery source controls](bigquery-controls.md). That candidate has no serving/runtime integration and does not change this profile or live activation gate. Actual packaged-image and live evidence remain separate.

## Profile and runtime boundary

`BigQueryCubeProfile.validate(settings: SourceSettings) -> SourceCapabilities` is an internal operator-owned eligibility check. `select_adapter("postgresql")` preserves `CubeAdapter`; explicitly configured `bigquery` selects the non-executable profile. Missing BigQuery settings, production activation and `domo` fail with safe `INVALID_INPUT`. This is not a new public connector schema, direct SQL compiler, discovery endpoint or metadata registry.

The existing executable runtime/profile deployments remain PostgreSQL-only; the separately retained BigQuery candidate is not registered as a serving deployment. Passing configuration validation does **not** turn them into BigQuery assets. Mocks explicitly reuse their member shapes against `httpx.MockTransport`; they cannot establish BigQuery metadata, dialect, hydration or runtime compatibility. A runnable BigQuery deployment needs independently reviewed and retained driver/model/binding assets and their own bundle digest. No Cube configuration or shared contracts changed in Task 13.

`SourceSettings` requires the exact project/dataset, explicit query region, operator-reviewed expected dataset region, and a mounted `/run/secrets/<name>` reference. Location syntax and equality are checked; neither is live dataset-location verification. A reference does not establish that credentials exist or are appropriately scoped. The model rejects credential contents, unknown fields, source-writing pre-aggregations, exports, floating versions and attempted self-certification. It never reads a secret. Do not accept these settings from manifests, query inputs or public endpoints.

## Exact selected release configuration

The upstream `v1.8.5` annotated tag resolves to commit [`d01994a71e64f2e8074ee04fefa3a0d9f369cc81`](https://github.com/cube-js/cube/commit/d01994a71e64f2e8074ee04fefa3a0d9f369cc81). The selected image's immutable digests are retained in [the Cube compatibility record](../compatibility/cube.md); the guarded actual-image package probe passed in [run 18](../evidence/reviewed-connectors.md#hosted-gate-attempt-18-pass), while BigQuery serving integration and actual-provider qualification remain outstanding.

The exact-tag [environment implementation](https://github.com/cube-js/cube/blob/d01994a71e64f2e8074ee04fefa3a0d9f369cc81/packages/cubejs-backend-shared/src/env.ts) maps:

| Operator Cube setting | Purpose |
| --- | --- |
| `CUBEJS_DB_TYPE=bigquery` | Select the existing Cube BigQuery driver |
| `CUBEJS_DB_BQ_PROJECT_ID` | Approved query/billing project |
| `CUBEJS_DB_BQ_KEY_FILE` | Mounted, operator-managed service-account file |
| `CUBEJS_DB_BQ_LOCATION` | Explicit approved location |
| `CUBEJS_DB_POLL_TIMEOUT` | Poll budget, seconds or duration string |
| `CUBEJS_DB_POLL_MAX_INTERVAL` | Poll interval bound; tagged default five seconds |
| `CUBEJS_DB_QUERY_TIMEOUT` | Poll fallback; tagged default ten minutes |

These names agree with [Cube's official configuration documentation](https://docs.cube.dev/admin/connect-to-data/data-sources/google-bigquery). The documented Base64 credentials alternative is deliberately unused. No secrets belong in checked-in `.env` files. Current Cube Cloud OIDC instructions do not prove a self-hosted credential flow.

The tagged [driver](https://github.com/cube-js/cube/blob/d01994a71e64f2e8074ee04fefa3a0d9f369cc81/packages/cubejs-bigquery-driver/src/BigQueryDriver.ts) accepts `projectId`, `keyFilename`, `location`, `readOnly`, `pollTimeout` and `pollMaxInterval` configuration. Poll values are converted from seconds to milliseconds. Normal queries use positional parameters, standard SQL and integer wrapping. Their job request omits `maximumBytesBilled` and `jobTimeoutMs`; polling eventually attempts `job.cancel()`. This establishes neither an enforced scan-cost ceiling nor guaranteed termination. Do not invent a maximum-bytes environment variable, assume a constructor option reaches each job, or treat a gateway deadline as source-stop evidence.

Operator recommendation, pending a reviewed deployment: explicitly use `readOnly: true`, remove all model pre-aggregation definitions, disable scheduled refresh/build/export workflows, omit export buckets, and grant no source-write/storage privileges. These settings are required safeguards, not proof that the selected source runs safely. No source or IAM configuration is installed by this profile.

## Least-privilege read/query review

Google documents `bigquery.jobs.create` on the query project and `bigquery.tables.getData` on referenced tables/views. Standard role examples are BigQuery Job User on that project and BigQuery Data Viewer scoped to the approved dataset/tables. Authorized views can avoid granting underlying-table reach. Review whether startup/schema discovery needs additional metadata permissions; do not expand to project-wide Data Editor/Admin or Storage Object Admin to make a test pass. [Google query permissions](https://docs.cloud.google.com/bigquery/docs/running-queries#required_roles)

Cube lists Data Viewer/Job User for read/query access and broader write roles for pre-aggregations/exports. Those write paths remain disabled. The actual credential's project, dataset reach, tenant predicates, expiry/revocation, forbidden operations and metadata visibility all need authorized live evidence. [Cube role guidance](https://docs.cube.dev/admin/connect-to-data/data-sources/google-bigquery#prerequisites)

## Exact values, date filters and resource behavior

- Decimal wire values remain plain strings. `convert_numeric` defaults to decimal(38,9), preserves the text exactly, and rejects float/integer coercion, exponents, nonfinite values, precision overflow and excess fractional digits. A manifest's narrower scale is independently enforced by existing normalization.
- Full BIGNUMERIC exceeds precision 38 and is unavailable, including values that happen to fit. A narrower source projection requires its own review and binding; there is no bypass flag. DATETIME is civil time; scalar TIMESTAMP checks require an explicit offset and preserve microseconds. Timestamp helpers do not expand the reviewed daily DATE output contract. Arrays, structs, FLOAT64 and INT64 source bindings remain unavailable in this adapter revision. Large exact integers can be represented only through a separately reviewed decimal-string contract. [Google type semantics](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/data-types)
- Tagged [hydration](https://github.com/cube-js/cube/blob/d01994a71e64f2e8074ee04fefa3a0d9f369cc81/packages/cubejs-bigquery-driver/src/HydrationStream.ts) stringifies finite JavaScript numbers and Big values, unwraps string-valued wrappers and preserves nulls. **Inference:** numeric-looking metadata cannot prove the physical expression avoided floating-point loss; inspect reviewed source types/expressions and record actual REST values. Exponential hydration remains a compatibility gate because the gateway rejects exponent-form decimal strings rather than silently converting them.
- Existing Cube mapping uses explicit `afterOrOnDate` and `beforeDate` local midnights for `[start,end)`, with manifest IANA timezone, daily grain and mandatory tenant/access-group filters. Date equality and other reviewed comparisons reuse the same Cube mapper; null uses `notSet`. Contract tests do not prove physical BigQuery partition pruning or live DST behavior.
- Caps remain 1,000 rows and 1 MiB. The profile's `max_rows` specializes the existing Cube limit-plus-one sentinel; `max_bytes` is the private response admission bound exercised in mocks. Existing manifest/current-policy limits and full-envelope normalization still apply independently. A lower raw-byte cap is not a lower normalized-envelope policy. Over-limit responses fail `RESOURCE_LIMIT`, never return an unmarked partial result. Upstream errors are redacted, and unsupported cache evidence stays `unknown`.

Google explains that `LIMIT` does not reduce non-clustered table scan costs. On-demand `maximumBytesBilled` can reject an over-budget job before charging, but a local requested value is not enforcement. Daily quotas, estimates and partition pruning do not substitute for a verified per-execution ceiling under the actual billing model. [Google cost controls](https://docs.cloud.google.com/bigquery/docs/best-practices-costs#restrict-the-number-of-bytes-billed-per-query)

Even Google's `jobTimeoutMs` is a best-effort stop attempt, not a guaranteed lifetime bound. Queuing, retries, orphaned work, metadata/watermark queries and cancellation acknowledgement require separate evidence before source reservations can safely be released. [Google job configuration](https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/Job#JobConfiguration)

## Optional live smoke: currently blocked before any query

Run mocked contracts with:

```sh
UV_CACHE_DIR=$PWD/.superpowers/uv-cache uv run --locked --offline pytest tests/contract/test_bigquery.py -m contract -q
```

The separate optional live gate is:

```sh
uv run --locked --offline pytest tests/integration/test_bigquery_live.py -m live_bigquery -q
```

By default it reports **NOT RUN** before reading settings/authorization/credentials or opening a socket. `GATEWAY_BIGQUERY_LIVE=1` is only opt-in, never authorization. A separate `GATEWAY_BIGQUERY_LIVE_AUTHORIZATION_FILE` must contain exactly `settings` and `authorization`, with no keys or tokens. The one-shot approval identifies operation `one-read-only-cube-smoke`, authorization ID, matching project/dataset/region/secret reference, scoped-credential review/evidence, an exact positive USD spend ceiling and future RFC3339 expiry. Deterministic tests validate this boundary using fictional references only.

**Even complete approval is currently BLOCKED:** `require_live_source_bounds` fails before credential resolution/network access because the selected driver lacks proven spend/lifetime enforcement. There is no runnable live-query stub or direct SDK workaround. To enable a real smoke later, separately approve the project/dataset/region/credentials/spend and review an actual enforcing source mechanism, immutable BigQuery deployment/binding assets, safe redacted evidence capture and one fixed known-answer query. Do not remove this gate merely because configuration or mocks pass.

Keep mock and live reports in distinct evidence files, including exact gateway/image/model digests, date, authorization scope, observed row/type/date/watermark/error/limit behavior and actual source billing/lifetime evidence. Never publish credentials, SQL, tokens, raw upstream errors or unapproved data. A passing live smoke would still not automatically establish production readiness.

## Remaining activation and distribution gates

Live metadata/results, actual type hydration, least-privilege IAM, tenant/cache isolation, source cost/lifetime enforcement and cancellation are all unproven. Production additionally requires reviewed identity/policy, TLS/ingress/egress, secret management, retention/encryption, restore evidence and named operational ownership.

The exact-tag [BigQuery package metadata](https://github.com/cube-js/cube/blob/d01994a71e64f2e8074ee04fefa3a0d9f369cc81/packages/cubejs-bigquery-driver/package.json) declares version 1.8.5 and Apache-2.0; Google SDK dependencies are semver ranges. No SDK is installed or separately pinned by Task 13. Preserve vendor notices and inventory actual packaged transitive versions/licenses under the immutable image in Task 14. The original-code license/distribution gate remains open. See [integration research](../archive/research/integrations.md) and [performance/source-bound research](../archive/research/performance.md).
