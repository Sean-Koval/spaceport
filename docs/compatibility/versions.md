# Pinned compatibility baseline

Selected and verified on 13 October 2026. This is the Task 1 contract baseline, not a claim that the complete demo or any source integration works.

## Runtime and tools

| Component | Exact version | Evidence |
| --- | --- | --- |
| CPython | 3.12.14 | Installed interpreter; [official release](https://www.python.org/downloads/release/python-31214/) |
| uv | 0.12.23 | Installed `uv --version`; `tool.uv.required-version` requires this release |
| Pydantic | 2.14.1 | [Official PyPI metadata](https://pypi.org/pypi/pydantic/2.14.1/json), requires Python >=3.10 |
| rfc8785 | 0.1.4 | [Official PyPI metadata](https://pypi.org/pypi/rfc8785/0.1.4/json), requires Python >=3.8 |
| pytest | 9.1.1 | [Official PyPI metadata](https://pypi.org/pypi/pytest/9.1.1/json), requires Python >=3.10 |

The project requires Python 3.12.14 exactly. Runtime and test dependencies are exact pins; `uv.lock` also pins all transitive versions and artifact SHA-256 hashes using the official PyPI registry. The project is currently a source-only uv project (`package = false`), with no build/distribution backend. No original-code license has been selected; distribution remains blocked pending the owner's decision and the complete dependency/notice inventory.

### Transitive lock inventory

| Package | Exact version |
| --- | --- |
| annotated-types | 0.8.0 |
| colorama (Windows conditional) | 0.4.6 |
| iniconfig | 2.3.1 |
| packaging | 26.3 |
| pluggy | 1.6.0 |
| pydantic-core | 2.50.1 |
| pygments | 2.21.0 |
| typing-extensions | 4.16.0 |
| typing-inspection | 0.4.4 |

## Repeatable verification

From the repository root:

```sh
uv sync --locked
uv run pytest tests/unit/test_contracts.py -q
make test-unit
make test-contract
make test
```

The Make targets use `uv run --locked`; they reject a stale lock rather than resolving different dependencies. pytest registers `unit`, `contract`, `postgres`, `cube`, `e2e` and `live_bigquery`, with `--strict-markers --strict-config`. The Task 1 tests carry both `unit` and `contract` markers because they establish the shared service boundary directly.

If your sandbox's default uv cache is read-only, select a writable directory for that shell, for example:

```sh
export UV_CACHE_DIR="$PWD/.superpowers/uv-cache"
export UV_LINK_MODE=copy
```

These are environment overrides, not committed machine-specific configuration. `.superpowers/` is already ignored. Ordinary clones continue to use uv's normal cache location. The contract suite needs no network after the locked packages are installed. Named IANA timezones use Python's `zoneinfo` and the host's installed timezone database; deployment-image timezone compatibility is a later integration gate.

## Contract shapes and invariants

All domain/wire models forbid extra fields, use strict scalar types and are frozen. Nested maps are frozen dictionaries and arrays are immutable tuples in Python; JSON serialization produces ordinary objects and arrays. Registry storage should use `manifest.model_dump(mode="json")`, which roundtrips including its checked digest. Pydantic constructors and the validation functions must be used at trust boundaries; `model_construct` is not a validation API.

- Manifest schema version is `"1.0"`; product versions use a bounded semantic-version string. Lifecycle status is not a manifest field.
- `FieldSchema` declares `type`, `nullable`, `required`, optional `enum`, optional `default`, and decimal `precision`/`scale`. A supplied default has the explicit shape `{"value": ...}`. This distinguishes an absent default from a default whose value is null, without coercion or serialization ambiguity. `required` defaults to true; a declared default is applied before checking missing required inputs. Optional inputs without defaults remain absent.
- Field types are `string`, `integer`, `number`, `boolean`, `date`, `timestamp` and `decimal`. Dates are `YYYY-MM-DD`; timestamps have an explicit RFC 3339 offset. Decimal values are strings, with maximum precision 38 and declared scale; floats are rejected for decimal fields. Unsafe integers outside JCS's exact range must be expressed as decimal strings.
- Links have `label` and `url`, with credential-free HTTPS URLs. Validation never retrieves links or resolves secrets. Descriptions, aggregation definitions and example descriptions are inert prose. Recognizable credential formats and secret references are rejected in typed values and binding identifiers; this is not a claim that arbitrary secret strings can be recognized automatically.
- Filters are a map from output field names to `operators` and optional `max_values`. Operators are `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, `not_in` and `is_null`, restricted by field type. `is_null` requires a nullable field and no supplied value. `in`/`not_in` require a nonempty bounded value array. Query filters use `field`, `operator`, `value`; sort keys use `field` and `direction` (`asc` or `desc`). Manifest sort has `fields` and `max_fields`.
- `date_boundaries` is null for non-date products or declares `start_input`, `end_input`, and the literal `interval: "[start,end)"`. Date-based products require both nonnullable, required date inputs. Maximum range is 366 local calendar days, regardless of DST or UTC elapsed hours. `timezone` is a known IANA timezone.
- `freshness` has `required`, `max_age_seconds` and `meaning: "source_load"`. This measures a recorded source-load watermark, never a query-time timestamp or event recency.
- `units` maps output fields to `{unit, currency}`; `redaction` maps every input to `omit` or `digest`. Fixtures include ID/version, typed inputs, expected rows or a stable expected error, nonnegative decimal tolerance and optional recorded watermark. Fixture rows contain exactly their declared output fields. `INVALID_INPUT` fixtures may intentionally carry invalid inputs.
- `limits` is the coordinated addition to the shared Manifest field list: it defaults to the global caps and permits tightening only. Maximum rows are 1,000, maximum bytes 1,048,576, page size 200, per-principal concurrency two, deployment concurrency ten, date range 366 local days, synchronous wait five seconds, and execution deadline 120 seconds. Related caps must remain coherent; for example, tightening rows below 200 also requires tightening page size.
- `manifest_digest` hashes [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) canonical JSON excluding `content_digest`. Decimal domain values become lossless strings before canonicalization. Missing draft digests are computed; supplied digests must match. Changes to metadata affect the digest; object key ordering does not.
- `Principal` carries verified identity only. `Decision`, `ExecutionSpec` and `PreparedQuery` are internal types, never public request schemas. Mandatory row constraints have `member`, `operator` (`eq`/`in`) and nonnull `values`. Execution specs require an already normalized query pinned to the product version, exclude an idempotency key from canonical inputs, and verify the canonical hash. Prepared queries contain an operator-owned endpoint ID, model-bundle digest and bounded Cube query JSON. Binding resolution, policy authorization and full semantic-member validation belong to their later owners.
- `AdapterPoll` is `Pending(retry_after_seconds)` or `Completed(raw_rows, source_watermark, cache_status)`. Unknown upstream cache evidence defaults to `unknown`. Missing source-watermark evidence stays null.
- `ExecutionView` carries state, aware execution times and a safe structured error. `ResultEnvelope` adds `product_id`, `version`, `product_digest`, `typed_schema`, typed rows, units/grain, effective range/timezone, authorized provenance, total `row_count`, `truncated`, source watermark, freshness, cache status, warnings and optional cursor. Row count is the total unpaginated count and may exceed the current page's length. Failed, pending and expired envelopes contain no rows or cursor; truncation must be the boolean false. Provenance contains binding ID/revision, adapter revision, bundle digest and authorized metadata links.
- Result freshness has `status` (`fresh`, `unknown`, `stale`), `assessed_at`, `age_seconds` and `max_age_seconds`. A proven assessment must agree exactly with the recorded watermark and threshold. Unknown freshness cannot claim an age.

One MiB means 1,048,576 UTF-8 bytes of the entire canonical serialized normalized envelope, including metadata and all unpaginated rows. The ResultEnvelope contract enforces the global byte and row caps; normalization/execution must additionally enforce each product's tighter limits on the complete envelope before pagination. A page by itself cannot prove the size of omitted pages. No over-limit result is silently truncated.

## Boundaries still unverified

Task 1 tests only these pure contracts. Docker, PostgreSQL, Cube, HTTP transports, MCP SDK/protocol versions, browser/UI behavior, BigQuery, Domo, OIDC delegation, Studio and production operations are not verified by these tests. Docker is not installed in the implementation environment, so required live container suites remain NOT RUN. Later owners must add their exact compatible release pins and actual evidence here.

## Task 5 private synthetic runtime candidate

Full immutable image pins, operator bundle digests, upstream source observations and the mandatory test command are in [Cube compatibility](cube.md). The official metadata-only CI job verified Cube v1.8.5's index/platform digests. This is not yet a real-stack pass: actual image/version agreement, single-instance production-mode memory cache/queue, authenticated responses, cache isolation, pending behavior, source-side timeout, restart persistence and blocked egress remain PENDING the reviewed manual runtime job. PostgreSQL 17.11 and Nginx 1.30.5-trixie are pinned by official OCI index and selected linux/amd64 manifest digests. No original-code license or production deployment was authorized.

Task 5 adds no Python package or lock changes. Its local Node tests used v24.19.0; the Cube image's actual packaged Node and jsonwebtoken versions must be recorded by the runtime job. Tagged Cube Docker source refers to Node 24.21.0-trixie-slim, which is a source observation rather than an observed runtime version. Model digests cover config/security/model bytes and must change together with reviewed assets; catalog and Compose values are checked against recomputation.
