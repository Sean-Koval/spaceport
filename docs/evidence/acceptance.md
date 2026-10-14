# Integrated acceptance evidence

Overall acceptance: **BLOCKED**. This is the requirement/evidence index, not a completed acceptance certificate. The [merged private-main baseline](private-main-baseline.md) passed its complete backend gate on 2026-10-14: 1,847 unit/contract tests, 294 genuine backend tests and verified cleanup. That receipt covers only its exact source tree. The composed application, rendered-browser journey and fresh no-acquisition repeat remain unverified; changed source requires new checks. Collection, synthetic XML or an existing `PASS` string is not execution evidence.

## Evidence classes

- **Unit/contract:** deterministic validation, normalization and mocked adapter behavior. Loopback sockets with service doubles prove wire compatibility only.
- **Real backend:** migrated disposable PostgreSQL; pinned Cube where marked `cube`. Cases that deliberately seed immutable TestRun/results or use an adapter double prove database/authorization behavior, not real fixture publication or live source completion.
- **Composed demo:** independent edge/API/worker/retention/Cube/databases, actual publisher browser and separate Python/TypeScript MCP clients. No manufactured publication evidence is admitted.
- **Runtime observation:** only the stable explicitly observed workflow interval. Later restart/restore/fault/rotation phases change namespaces and are separate operations evidence.

## Requirement-to-evidence matrix

Paths below are relative to the repository root.

| Requirement | Evidence sources | Required classification / current gate |
| --- | --- | --- |
| Fresh bootstrap is unpublished; author tests and publisher releases exact digest | `test_operations.py::test_operator_bootstrap_is_idempotent_and_never_publishes`; `tests/e2e/publisher.spec.ts`; `test_publishing.py::test_real_isolated_cube_fixture_run_publishes_exact_original_digest` | Real PostgreSQL, PostgreSQL + Cube, composed browser; baseline backend passed; composed browser NOT RUN |
| Discover/describe/examples/provenance agree across REST/MCP | `tests/e2e/test_acceptance.py::test_actual_catalog_examples_provenance_and_result_match_across_transports` for north/south | Composed; NOT RUN |
| One canonical request pins an execution across transports; changed input conflicts | `test_acceptance.py::test_actual_query_replay_preserves_pinned_execution_across_rest_and_mcp`; `test_execution_queue.py::test_product_pointer_changes_do_not_repin_accepted_work` | baseline real PostgreSQL passed; composed NOT RUN |
| Two independent clients reconcile one unchanged product for two profiles | All three tests in `tests/e2e/test_two_clients.py`, recorded publisher/product/result digests | Composed; NOT RUN |
| Totals/grain/nulls/large decimals/empty/DST/freshness | `tests/integration/test_cube_adapter_runtime.py`; known-answer/date/watermark tests in `test_cube_compatibility.py`; `tests/unit/test_normalization.py` | baseline unit and real Cube backend passed separately; no live-source or composed claim |
| Registry/current head/watermark/rows survive restart | `tests/integration/test_operations.py::test_actual_restart_persistence` | Composed operations; NOT RUN |
| Fresh restore preserves published head, rows and watermark | `tests/integration/test_operations.py::test_actual_backup_restore` | Composed operations; NOT RUN |
| Six authority profiles and nine surfaces | [Security matrix](security.md), adversarial tests and existing backend security cases | Surface-specific; no six-external-account claim |
| Edit/publish races, stale evidence, drift/outage, timeout and bounds | [Boundary mapping](security.md#failure-and-boundary-requirements) | baseline full-backend receipt passed; deliberately seeded/double cases retain their narrower meaning |
| Blocked runtime egress after preparation | `scripts/check_egress.py`, retained observer/network report and exact engine/container/network/source bindings | Stable observed window only; BLOCKED pending reviewed actual capture |
| Repeat clean cycle without reacquisition | Two fresh independent state/project/volume receipts using the same prepared image IDs/source | NOT RUN; unresolved acceptance gate |

Unqualified `test_operations.py`, `test_publishing.py`, `test_execution_queue.py` and `test_cube_compatibility.py` above are under `tests/integration/`. `test_acceptance.py` is under `tests/e2e/`.

After all serial browser cases the current head may be 1.0.2. Acceptance reads that authorized current description and submits its exact observed version/digest; it never assumes the initial 1.0.0 release remains current. Existing operations tests that deliberately query retained 1.0.0 remain a separate check.

Freshness is reassessed on each fetch. REST/MCP parity compares immutable result fields exactly and requires fresh assessments with monotonic assessment time; it does not require two fetch timestamps to be identical.

## Observed source-work commands

Local implementation workspace: Python 3.12.14, uv 0.12.23, already-installed locked dependencies; Docker and explicitly selected synthetic backend/stack environments absent. No acquisition by these commands.

| UTC | Actual command | Exit / result | Meaning |
| --- | --- | --- | --- |
| 2026-10-13 23:48:30 | `UV_CACHE_DIR=/tmp/data-product-gateway-uv-cache uv run --locked --offline pytest tests/unit/test_acceptance_evidence.py -q --tb=short` | 1; 10 passed, 14 failed | RED: forged testcase outcomes and PASS-only mandatory slots exposed; verifier-owner fixes required. |
| 2026-10-13 23:50:18 | `UV_CACHE_DIR=/tmp/data-product-gateway-uv-cache uv run --locked --offline pytest tests/e2e/test_acceptance.py tests/security/test_adversarial_matrix.py --collect-only -q` | 0; 15 collected | Collection only: 13 composed and 2 PostgreSQL cases. |
| 2026-10-13 23:55:22 | `UV_CACHE_DIR=/tmp/data-product-gateway-uv-cache uv run --locked --offline pytest tests/unit/test_acceptance_evidence.py -q --tb=short` | 0; 40 passed | Focused GREEN against verifier-owner changes, including new bound-slot negative controls. Verifier unit proof only. |
| 2026-10-13 23:50:25 | `UV_CACHE_DIR=/tmp/data-product-gateway-uv-cache uv run --locked --offline pytest tests/e2e/test_acceptance.py tests/security/test_adversarial_matrix.py -q --tb=short` | 1; 15 setup errors, zero skips | 13 lack selected demo state; 2 lack authorized disposable PostgreSQL URL. Runtime NOT RUN. |

The initial unoverridden uv invocation stopped before pytest because its cache was read-only; the explicit writable cache corrected that environment issue. There is no external run link for these local commands. They are authoring observations, not immutable final-build certification.

## Required runtime receipt and command record

The integration harness must explicitly run the new `-m e2e` cases after publication and before fault/authentication replacement, and the two `-m postgres` cases while the genuine disposable backend is available. Do not relabel absent-environment setup errors as skips.

For every actual attempt record:

1. Exact reviewed Git commit, source-inventory digest, clean checkout status, UTC start/end, command/arguments, exit status, counts and exact collected identities. Record all failed/error/skipped cases.
2. Actual Python/uv/Node/npm/Playwright/Chromium, Docker Engine/build/Compose versions, immutable image IDs/platforms. [Compatibility pins](../compatibility/versions.md) and [Cube references](../compatibility/cube.md) are inputs, not proof of what ran.
3. Unique mandatory slots, source/image-bound receipts, exact JUnit identities and child outcomes, full hash-checked evidence paths, prerequisites and positive observer controls.
4. Actual direct CI run URL and bounded safe console manifest. Ephemeral private files are not durable downloadable artifacts. Never upload credentials, dumps, packet payloads, arbitrary raw stdout or the complete state directory.
5. Exact cleanup outcome, restored/fresh-repeat resources before original image removal. Cleanup failure remains a blocker.

`make evidence` must reject absent, stale, partial, forged and double-only output. An integrity hash detects changes relative to a receipt; it does not authenticate a writer-controlled receipt. Trusted execution provenance and independent review remain necessary.

The ordinary `make verify-demo` / `make evidence` sequence covers 13-slot business verification and omits integrated `acceptance_security`; it cannot establish final Task15 acceptance. The separate final routes are `bash scripts/verify_gateway_integration.sh --backend NEW_PRIVATE_RUN_DIR` and `bash scripts/verify_gateway_integration.sh --demo ANOTHER_NEW_PRIVATE_RUN_DIR`, with the distinct nonexistent external run children and pinned setup described in the [runbook](../operations/runbook.md#separate-final-acceptance-modes). The wrapper includes locked/offline mandatory unit/contract execution. Complete backend execution remains a separate gate from demo prerequisites; demo requires all 14 integration slots in each of two clean cycles, with the second cycle reusing prepared dependencies/images without acquisition. Source-side prerequisite recording exists. The merged baseline backend passed as recorded above; demo/repeat remain **NOT RUN**, later source changes require new backend evidence, and the packet observer remains **BLOCKED/nonzero**. Verified cleanup, rendered-browser inspection and final independent review remain required. See [limitations](limitations.md).
