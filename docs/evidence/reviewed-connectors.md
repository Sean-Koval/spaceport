# Reviewed connectors: evidence boundary

15 October 2026. Scope: [reviewed connector design](../design-records/2026-10-15-reviewed-connectors.md). This is a private implementation candidate, not live source or deployment approval.

## Candidate slices

- Reviewed PostgreSQL/Cube v2: a complete internal product path with versioned bindings, exact accepted-query replay, current authorization, same-query hidden committed checkpoint, isolated fixture bundles and explicit service composition. Real source producer atomicity and reviewed checkpoint uniqueness remain prerequisites.
- BigQuery: executable bounded driver and durable admission/seal library, tested offline. No gateway runtime selection, private control endpoint, worker attempt-context hook or source-release integration is enabled. The guarded actual-image package probe passed in run 18; real provider behavior and live source evidence remain open.
- Domo: offline finite named-query selector, strict positional result checks and response-bound freshness evidence with mandatory authorization gates. No live transport/authentication or gateway serving path.
- GCS: offline immutable exact-generation artifact allowlist reader with bounded metadata/content, hash/type/version checks and authorization before read/release. No public route, default network transport or artifact policy inheritance.
- Shared hardening rejects signed-access metadata URLs and malformed timestamp offsets.

## Baseline and review

The starting source tree exactly matches private main `569cf9b2f06b991003461ee2717dcd4b85b530ad`, tree `a4e0d366cf222cf65920e22f51ca015f2ec47aa2`. Local ancestry differs, so publication must preserve remote ancestry and verify tree equality.

Before implementation, 2,221 unit/contract tests passed, 373 service-dependent cases deselected, in 109.93 seconds. Missing links to the existing locked Node dependency installation were repaired before that clean baseline; no dependency versions changed.

Independent architecture review required exact accepted-query comparison rather than only member allowlisting, correlated same-query checkpoint evidence rather than a later watermark, and a narrower BigQuery library scope rather than an unreviewed authenticated control plane.

Independent Domo/GCS code review caught and corrected statement-catalog substitution before source I/O, release after cleanup exceeded the deadline, and row values in result repr. The shared timestamp parser regression demonstrated malformed offsets such as `+00:60` were normalized; strict component validation now rejects them. These are source-level checks, not live source evidence.

## Verification status

Independent whole-source review approved local source `9542b742343906ccb86752280bcfec596860ae4d`, tree `dd1433f63880b98b468a3980cc6296435bafccae`, for a private draft PR and the required hosted gate. Subsequent changes through local `660e55a373e73f3304b4f7de0dd4ee03cd246262` were documentation-only. The later probe/diagnostic corrections are described below.

- The unchanged frontend passed **254 tests across 24 files**, typecheck, generated-client consistency, production build and formatting checks with Node 24.19.0/npm 11.9.0.
- Complete genuine backend collection is **359 cases**, including 13 new reviewed-Cube cases. Collection is not execution.
- Reviewed v1 loaded assets, public OpenAPI/MCP snapshots, persisted PreparedQuery/migrations and all workflow files remain byte-identical to the starting tree.
- Changed-document relative links: **42 checked, zero missing** before final evidence wording. Shell syntax and Git diff checks passed.
- Final combined unit/contract run: **2,553 passed, 386 deselected**, 151.43 seconds (153.446 seconds wall-clock), exit 0. This stays within the unchanged 180-second source-test cap. Command: `.venv/bin/python -m pytest -m 'unit or contract' -q`. The code tested is the reviewed source above; only final evidence/status wording changed through local `660e55a` afterward.

The new genuine PostgreSQL/Cube cases and actual-image BigQuery package probe require the manual exact-source backend gate. The package probe denies credentials/auth/network and intercepts the provider boundary after real SDK request construction; it is not a live BigQuery query. Missing/mismatched packages fail rather than skip. Local Docker/immutable-image runtime is unavailable, so offline controls, source tests and collection cannot substitute for that run.

All workflows remain manual-only. No live BigQuery/Domo/GCS/organization-PostgreSQL query, credential grant, IAM change, paid source request, deployment or main merge is included. Rendered-browser/composed-demo acceptance and production qualification remain separate.

## Hosted gate attempt 16

[Run 16](https://github.com/Sean-Koval/spaceport/actions/runs/38682007973) checked out exact remote source `78d14431de4398c947e8dccbd71039e77f296ed1`, tree `9eae93bd464e119188075cab59ffd7a5ae2bf8e9`, using the unchanged manual workflow on main `569cf9b2f06b991003461ee2717dcd4b85b530ad`. The source phase passed all **2,553 unit/contract tests**. Backend setup exited before an admitted genuine-backend result, so this attempt provides **no PostgreSQL/Cube or packaged BigQuery acceptance**. Exact registered cleanup passed.

The bounded receipt marked failure detail invalid. Investigation reproduced a reporting defect: the shell can emit the new `cube-bigquery-package` checkpoint, but the strict diagnostic parser did not allow that enum. Such a marker clears all diagnostic markers by design. This reproduction explains how information could be lost; it does not establish the underlying failed substage. Raw private logs, credentials and exceptions are not published. The job ran from 04:32:39 to 04:36:31 UTC (232 seconds, four rounded Ubuntu job minutes).

### Probe and diagnostic corrections

The exact shell marker reproduction now has a strict allowlisted checkpoint and shell-vocabulary regression; fixed Node substages retain the same closed marker schema and limits. The new probe uses the spec reporter because TAP prefixes otherwise obscure those markers. No private exception content is admitted.

A separate isolated official npm installation of Cube driver **1.8.5** with SDK **7.9.4** reproduced two probe-boundary assumptions: serialized request mode is lowercase `positional`, and the SDK metadata request path is slashless `jobs/<id>`. Correcting those test assertions/stubs produced a passing guarded local package probe, independently repeated. Provider metadata canonicalization remains a synthetic fixture assumption. Driver and ledger controls are unchanged. This is neither immutable-image nor live-provider evidence, and the lost run16 receipt cannot establish that these were its exact failure causes.

Independent review cleared the integrated correction code at local `e62a4e5` and this evidence wording. Final full source verification on that code: **2,577 passed, 386 deselected in 154.98 seconds**, exit 0, using `.venv/bin/python -m pytest -m 'unit or contract' -q`. The unchanged source cap remains 180 seconds. No workflow, driver/ledger or gateway implementation changed in these corrections; immutable-image/backend acceptance at that point still awaited the next exact-source hosted gate.

## Hosted gate attempt 17

[Run 17](https://github.com/Sean-Koval/spaceport/actions/runs/38683762465) checked out exact source `3086fe45b91bfed8cfaf2b85dab1c50b1cb1f74f`, tree `ce3e51537d761122c9de8103d1b8d7e03e4f2a6f`. The unit/contract phase reached its unchanged **180-second timeout**, before backend setup or the package probe could execute. Exact cleanup passed. This attempt supplies no new backend/package acceptance. The job ran from 04:52:39 to 04:56:15 UTC (216 seconds, four rounded Ubuntu job minutes).

A subsequent unchanged local duration run again passed **2,577 tests**, 386 deselected, in **152.13 seconds**. Measured topology contract fixtures repeatedly started Python solely to serve synthetic Docker responses; those test stand-ins are the targeted optimization. Actual topology/restart verification, negative cases, security limits and runtime timeouts remain required.

The independently reviewed correction replaces only test-side command stand-ins with static validated responses. All 43 original topology cases and assertions remain, plus nine fidelity regressions for exact command ordering, before/after restart state, repeated-fixture isolation and unknown/inexact commands. The real extracted shell, inline Python, timeout wrappers and two admission passes remain exercised. Focused comparison improved from **152 passed in 72.98 seconds** to **161 passed in 32.78 seconds**. The independent reviewer passed all **52 topology cases in 9.14 seconds**. Product/runtime/workflow code and limits are unchanged.

Final full unit/contract verification on correction code local `084cf98`: **2,586 passed, 386 deselected in 115.10 seconds**, exit 0. Command: `.venv/bin/python -m pytest -m 'unit or contract' -q`. This recovers approximately 37 seconds against the immediately preceding unchanged local duration run, while retaining the 180-second source cap. Hosted backend/package acceptance at that point was still pending.

## Hosted gate attempt 18: PASS

[Run 18](https://github.com/Sean-Koval/spaceport/actions/runs/38685215504) passed on exact source **`9c0f74e1dda3898f61b3b5cdd48d08fd3562daa3`**, tree **`c6b95f9be057b7ca4662b68a92aea0fb7faad6cb`**, using the unchanged manual workflow on main `569cf9b2f06b991003461ee2717dcd4b85b530ad`.

- **2,586 unit/contract tests admitted**, source phase 111.34 seconds.
- **359 genuine backend cases admitted**, including the 13 reviewed PostgreSQL/Cube cases; backend phase 586.71 seconds.
- The mandatory guarded actual-image BigQuery package probe completed before backend admission. It verifies the installed package/serialization/hydration boundary with synthetic provider responses and denied authentication/network, not live BigQuery behavior.
- Exact registered cleanup and the always-cleanup step both passed.
- Job start/end: 05:08:59–05:21:17 UTC, 738 seconds, 13 rounded Ubuntu job minutes.
- Integration receipt SHA-256: `66e36bf953fc29a70fbdfd3ba0c8ce739677502e492d42f7079badf6be127e12`. Cleanup receipt SHA-256: `3101a9452b0f1ce8e15075e7ca9b73d9b04d2c4e781786e7f880de81827388ab`.

This final evidence/status update is documentation-only after that exact tested source. It does not change runtime, tests, workflows, dependencies or model bundles. The gate establishes the disposable backend path, not organization-source qualification, browser acceptance or production approval. BigQuery serving/lifetime integration and actual-provider behavior, Domo live transport/PDP semantics, GCS artifact policy/serving integration, operator atomic producer prerequisites, and deployment SSO/PKCE remain separate gates. Main merge still requires approval.
