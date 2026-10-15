# Reviewed connectors: evidence boundary

15 October 2026. Scope: [reviewed connector design](../superpowers/specs/2026-10-15-reviewed-connectors.md). This is a private implementation candidate, not live source or deployment approval.

## Candidate slices

- Reviewed PostgreSQL/Cube v2: a complete internal product path with versioned bindings, exact accepted-query replay, current authorization, same-query hidden committed checkpoint, isolated fixture bundles and explicit service composition. Real source producer atomicity and reviewed checkpoint uniqueness remain prerequisites.
- BigQuery: executable bounded driver and durable admission/seal library, tested offline. No gateway runtime selection, private control endpoint, worker attempt-context hook or source-release integration is enabled. Packaged SDK/Cube compatibility and live source evidence remain open.
- Domo: offline finite named-query selector, strict positional result checks and response-bound freshness evidence with mandatory authorization gates. No live transport/authentication or gateway serving path.
- GCS: offline immutable exact-generation artifact allowlist reader with bounded metadata/content, hash/type/version checks and authorization before read/release. No public route, default network transport or artifact policy inheritance.
- Shared hardening rejects signed-access metadata URLs and malformed timestamp offsets.

## Baseline and review

The starting source tree exactly matches private main `569cf9b2f06b991003461ee2717dcd4b85b530ad`, tree `a4e0d366cf222cf65920e22f51ca015f2ec47aa2`. Local ancestry differs, so publication must preserve remote ancestry and verify tree equality.

Before implementation, 2,221 unit/contract tests passed, 373 service-dependent cases deselected, in 109.93 seconds. Missing links to the existing locked Node dependency installation were repaired before that clean baseline; no dependency versions changed.

Independent architecture review required exact accepted-query comparison rather than only member allowlisting, correlated same-query checkpoint evidence rather than a later watermark, and a narrower BigQuery library scope rather than an unreviewed authenticated control plane.

Independent Domo/GCS code review caught and corrected statement-catalog substitution before source I/O, release after cleanup exceeded the deadline, and row values in result repr. The shared timestamp parser regression demonstrated malformed offsets such as `+00:60` were normalized; strict component validation now rejects them. These are source-level checks, not live source evidence.

## Verification status

Independent whole-source review approved local source `9542b742343906ccb86752280bcfec596860ae4d`, tree `dd1433f63880b98b468a3980cc6296435bafccae`, for a private draft PR and the required hosted gate. Subsequent changes in this evidence record and the BigQuery status wording are documentation-only.

- The unchanged frontend passed **254 tests across 24 files**, typecheck, generated-client consistency, production build and formatting checks with Node 24.19.0/npm 11.9.0.
- Complete genuine backend collection is **359 cases**, including 13 new reviewed-Cube cases. Collection is not execution.
- Reviewed v1 loaded assets, public OpenAPI/MCP snapshots, persisted PreparedQuery/migrations and all workflow files remain byte-identical to the starting tree.
- Changed-document relative links: **42 checked, zero missing** before final evidence wording. Shell syntax and Git diff checks passed.
- Final combined unit/contract run: **2,553 passed, 386 deselected**, 151.43 seconds (153.446 seconds wall-clock), exit 0. This stays within the unchanged 180-second source-test cap. Command: `.venv/bin/python -m pytest -m 'unit or contract' -q`. The code tested is the reviewed source above; only final evidence/status wording changed afterward.

The new genuine PostgreSQL/Cube cases and actual-image BigQuery package probe require the manual exact-source backend gate. The package probe denies credentials/auth/network and intercepts the provider boundary after real SDK request construction; it is not a live BigQuery query. Missing/mismatched packages fail rather than skip. Local Docker/package runtime is unavailable, so offline controls, source tests and collection cannot substitute for that run.

All workflows remain manual-only. No live BigQuery/Domo/GCS/organization-PostgreSQL query, credential grant, IAM change, paid source request, deployment or main merge is included. Rendered-browser/composed-demo acceptance and production qualification remain separate.
