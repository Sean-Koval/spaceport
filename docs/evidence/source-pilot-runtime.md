# Source pilot runtime: evidence boundary

15 October 2026. Scope: [source pilot design](../design-records/2026-10-15-source-pilot-runtime.md) and [implementation plan](../archive/plans/2026-10-15-source-pilot-runtime.md).

**Exact-source backend acceptance passed in run 22. PR 5 is merged; organization activation remains blocked.** The previous connector foundation was merged by approval into private main `3fa6b2f698fa406918d82173d4422166c059b84b`, tree `a3155cbfb8fc5c073a4c2d184ecd75d0b99d0024`. Its [run 18 receipt](reviewed-connectors.md#hosted-gate-attempt-18-pass) applies to that foundation only. The pilot has its own reviewed exact-source backend receipt below and has merged by user approval; the additional security/live qualification gates remain required.

## What the checks establish

- Unit and contract tests cover strict catalogs and product packages, source request compilation, current-authority handoffs, bounded provider transports, response validation and private CLI state. They do not establish real provider permissions, cost behavior, PDP or freshness.
- A disposable PostgreSQL 17.11 instance built from the checksum-verified official release is available in the cloud development workspace. Real migration, privilege, concurrency and retained-state tests use fresh isolated clusters with synthetic credentials. This is actual database execution, but not the pinned hosted container gate.
- The BigQuery serving probe uses actual Cube 1.8.5 HTTP/compiler/driver, BigQuery SDK 7.9.4, Google auth 9.15.1 and PostgreSQL client 8.16.3 against the genuine disposable registry. Only the Google provider boundary is synthetic. The probe exercises authorization, retained models, job identity and lifecycle behavior; it does not query Google Cloud.
- The complete BigQuery package/provision → author → isolated fixtures → publish → describe → consumer query → worker → stored result path has now run through production services with actual Cube/SDK and PostgreSQL. Only the Google transport and exact-origin loopback TLS routing boundary are synthetic; live TLS, Google permissions and quotas remain unqualified.
- The required hosted gate builds the actual gateway Dockerfile and runs CLI/import checks with network disabled, then executes the genuine backend and source-runtime cases. Run 22 passed that gate at its exact recorded source revision.

## Intermediate observations

These are development observations, not a final-build certificate. Later integration changes require revalidation.

- Integrated source `a81c86d`: 41 focused Domo dispatch, sample builder, offline setup and GKE renderer tests passed; 118 REST/MCP/result parity tests passed.
- The unchanged publisher passed 254 tests in 24 files, TypeScript checking, generated-client consistency and a production build against that integration tree. No rendered-browser acceptance is implied.
- Integrated source `719b0d0`: 17 genuine PostgreSQL lifecycle cases passed in 15.44 seconds. All 17 disposable databases preserved table/index identities across a second owner migration; restricted-role checks and process/cluster cleanup passed. These tests include deliberately seeded accepted owners and are not the complete publication journey. Fourteen focused Python serving/migration/import checks and 23 Node configuration/contract/ledger/security checks also passed.
- Integrated source `79a6a3f`: 31 combined lifecycle, owner-bootstrap and typed-preparation checks passed in 20.56 seconds, including 22 disposable database migration/replay checks through `0006_source_preparations`; cleanup passed.
- Integrated source `3399d70`: the full actual Cube/SDK/PostgreSQL source-runtime probe passed in 9.40 seconds with verified child/cluster cleanup. It used the integrated host control wrapper directly. Provider responses remained synthetic; no immutable Docker image or live Google access is implied.
- Integrated source `cb177f9`: 21 genuine PostgreSQL source lifecycle/queue tests passed in 19.25 seconds with 21 migration identity rechecks and verified cleanup. Separately, 62 existing queue/result regressions passed in 68.73 seconds; one Cube-dependent case was explicitly excluded from that local command.
- Integrated source `6737809`: the mandatory `-m 'unit or contract'` selection passed 2,949 cases, with 455 deselected, in 127.98 seconds. A collection audit identified 40 new portable tests awaiting explicit selection markers; this intermediate result does not claim those cases were covered.
- Integrated source `ecdc7b7`: 37 genuine PostgreSQL Domo workflow, lifecycle and owner-bootstrap cases passed in 47.76 seconds, with 37 migration identity rechecks and verified cleanup. The three Domo journey cases invoke production services directly, with only the Domo HTTP boundary simulated; installed ASGI routing remains a separate required test. The actual Cube/SDK/PostgreSQL probe was then repeated with both Python and Node durable-commit changes and passed in 10.03 seconds.
- Integrated source `e31dc2e`: the actual configured ASGI/OIDC/REST/MCP/worker composition passed two genuine PostgreSQL tests in 7.99 seconds, with two migration rechecks and verified cleanup. Identity and Domo HTTP boundaries were synthetic; browser and live source access remain unverified.
- Earlier integrated source selection passed 2,901 unit/contract-compatible cases, with 386 service-dependent cases deselected, in 122.16 seconds. This count predates subsequent runtime integration and is not the final mandatory source-phase receipt.
- The initial real PostgreSQL publishing/queue/results selection passed 91 cases but had two Cube setup errors because that local command did not start Cube. Those errors are not skips or backend acceptance.

- Integrated source `d2e25b9`: the complete BigQuery gateway journey passed in 7.87 seconds with migration replay and verified child/cluster cleanup. Ten jobs reached DONE, five sessions were sealed, two groups settled and no capacity remained held. An independent reviewer repeated the same scenario in 7.49 seconds. The source watermark remains unknown, rather than inferring freshness from successful execution.
- Integrated source `7ebbbe7`: the mandatory unit/contract selection passed 3,187 tests, with 433 deselected, in 138.87 seconds. This includes the mandatory 27-case Node serving-control inventory. Subsequent narrow harness fixes require final revalidation.

## Release-gate status

1. Reviewed connector source and bounded corrections now have exact-source backend qualification from run 22. Further runtime changes, including the security branch, require their own review and qualification.
2. Preserve both complete sample-product journeys in the mandatory hosted selection, with synthetic provider boundaries clearly identified. The combined test image uses the pinned Cube base and the same prepared locked Python/package inputs; related image-ID labels are evidence references, not ancestry proofs.
3. **Satisfied for source `2d3cd1e`:** exact-source hosted backend, actual gateway image and Cube source-runtime gates passed within existing limits, with verified cleanup and the run 22 receipt.
4. **Satisfied:** the user approved merging this verified pilot on 15 October; PR 5 merged normally at `27bb504f9a673267fd070a4e562b698388ded7e5`, preserving documentation tree `360388495eb947d523436550f152dd91734919b6`.
5. Before organization use, implement the separately reviewed, revocable data-owner approval gate for explicit non-PII shared-source products and fail closed on unsupported delegated-source modes. Current user/client authentication and product ACLs do not prove employee Domo permissions, relationship-based delegation or PII safety.
6. Separately configure and verify operator-owned credentials/IAM, restricted source datasets, network egress, identity/PDP mapping, source definitions, quotas and freshness prerequisites. Live source requests, cloud resource creation and deployment are not authorized by the implementation work.

Browser SSO/PKCE, composed browser acceptance, the clean no-acquisition repeat, packet-observer assurance and production rollout remain separate gates. Domo MCP is not used for approved-statement serving; the direct REST transport and its shared integration-identity semantics are described in the [Domo design](../design-records/2026-10-15-domo-pilot-runtime.md). GCS remains the earlier offline exact-generation artifact slice.

## Hosted attempt 19: blocked before backend

[Run 19](https://github.com/Sean-Koval/spaceport/actions/runs/38771001473) used trusted workflow main `3fa6b2f698fa406918d82173d4422166c059b84b` and exact candidate source `c000037599fb9f0625b88e4c46fe05f4a2571798`, tree `9a10b21464743c978bf2e7489648074158eeb580`. The unit/contract phase failed the private-parent negative fixture in `test_source_cli.py`; no backend or image acceptance was reached. Exact cleanup passed. Actual job duration was 190 seconds (four rounded standard Ubuntu minutes).

The failure was reproduced under the workflow's secure `umask 077`: requesting a directory mode of `0755` through `mkdir` actually produced `0700`, so the runtime correctly accepted the supposedly unsafe fixture. The reviewed test-only correction explicitly sets `0755` and asserts the actual mode. No runtime permission check, security control, timeout or test selection was weakened. All 62 CLI tests and the complete 3,213-case unit/contract selection then passed under `umask 077` (137.05 seconds; 433 service-dependent cases deselected). A new exact-source hosted run remains required.

The trusted main workflow still uses its older `domo: NOT IMPLEMENTED` summary label. For this candidate, Domo serving code and synthetic tests exist; live Domo access remains **not run**. The stale workflow label is not evidence about the candidate's implementation or live permissions.

## Hosted attempt 20: image prerequisites passed, complete backend blocked

[Run 20](https://github.com/Sean-Koval/spaceport/actions/runs/38772272838) used trusted workflow main `3fa6b2f698fa406918d82173d4422166c059b84b` and source `5c3b17bd6edb4fbcc348a9a976d8645d70f39c10`, tree `c1abbffd45db4362942cd61e72565983288df9dc`.

- All 3,213 mandatory unit/contract cases passed and their exact selection was admitted.
- The sequential harness reached full backend execution after the required gateway image build, no-network CLI/import checks, BigQuery image build/smoke and actual source-runtime probe returned success. This is separate evidence from the combined gateway journey, which did not pass.
- The first reported failure was the combined BigQuery image journey. Its original sanitized assertion did not expose a sufficiently specific safe failure stage; the root cause is unproven. The backend subsequently reached its unchanged 570-second limit with 331 of 406 cases completed. Completed count is not a passing-test count.
- Exact registered cleanup passed. Actual job duration was 989 seconds (17 rounded standard Ubuntu minutes). No cap, source cost control, authorization check or test identity was relaxed.

A separate local PostgreSQL diagnostic on the same runtime ran all 349 non-Cube PostgreSQL cases successfully in 460.87 seconds, with 257/257 second-upgrade identity checks and verified cluster cleanup. The 57 Cube-tagged backend cases were explicitly outside that diagnostic. Measurements identified about 0.45 seconds of repeated interpreter/import overhead per normal migration pair. The next reviewed candidate preserves every fresh database/role set and both genuine upgrades while investigating one serial, bounded migration process; hosted performance remains a separate gate.

## Reviewed correction candidate after run 20

The closed journey-stage/error receipt is now retained through both the combined image journey and an earlier exact-image import smoke. Only fixed enumerated stages/errors can reach diagnostics; malformed output, extra fields and arbitrary exception text remain a generic failure. The 15-second helper and 20-second outer smoke bounds are unchanged. Real subprocess regression tests cover both valid and hostile failure output. This is improved evidence for the still-unproven hosted journey failure, not a claim that its cause has been fixed. A local genuine PostgreSQL/Cube/compiler/SDK gateway journey plus runtime probe passed again (2 tests, 16.76 seconds); its synthetic provider boundary and different local package versions do not replace the exact-image hosted gate.

The independently reviewed migration optimization retains every fresh database and role set, both real owner upgrades, fresh/disposed connections and independent operator CLI process coverage. A serial session worker amortizes interpreter imports only. Closed request/receipt matching, bounded output, worker deadlines, parent-death/EOF handling, and the existing supervisor's exact descendant cleanup are tested. No schema template, cached migration result, transaction reuse, selection reduction or timeout increase is used.

Fresh verification of that five-file optimization completed with:

- 349 PostgreSQL cases passed in 306.69 seconds (310.724 seconds shell wall), versus the earlier 460.87-second diagnostic. The 154.18-second reduction is about 33.5%; it is local diagnostic evidence, not full hosted backend acceptance.
- All 349 ordered PostgreSQL identities and all 406 ordered PostgreSQL-or-Cube identities exactly matched the preserved baseline.
- The integration evidence accumulator reported unchanged table/index identities for 257 second upgrades and the same four permission negatives. The separately imported security fixtures also passed but are not included in that accumulator's count.
- 3,265 unit/contract cases passed in 142.64 seconds under `umask 077`, inside the unchanged 180-second source cap. This is the optimization-only source tree; combined candidate verification is recorded separately.
- 96 focused migration tests passed; independent review also exercised real pytest duplicate-module import and single-finalizer behavior.
- The fresh run verified its PostgreSQL process stopped and its exact generated cluster directory was removed. Logs from the earlier interrupted local run were incomplete; their cleanup is inconclusive and is not covered by the fresh receipt.

The complete 406-case hosted backend and exact-image combined journey still require a new exact-source run within the existing 570-second backend and 20-minute job caps.

Combined correction-tree verification: 3,334 mandatory unit/contract tests passed in 144.20 seconds under `umask 077` (433 service-dependent cases deselected). Its ordered 349/406 backend identities remained identical, and its local genuine PostgreSQL/Cube/compiler/SDK journey plus runtime probe passed 2 tests in 17.61 seconds with fresh-run cleanup verified. Documentation relative links, shell syntax and whitespace checks passed. These receipts qualify the local correction tree only; exact-source hosted acceptance is still pending.

## Hosted attempt 21: complete backend selection, Cube child startup failure

[Run 21](https://github.com/Sean-Koval/spaceport/actions/runs/38789716504) used trusted workflow main `3fa6b2f698fa406918d82173d4422166c059b84b` and exact source `95d244e141e714fd4551a178bddc13219b8991f0`, tree `acd8a890edbe970b212404ef26bffa526a9d1d30`.

- All 3,334 mandatory unit/contract cases passed and were admitted. That phase took 140.21 seconds.
- The backend progress receipt reached all 406 collected cases and exited normally with failure, without a backend timeout. Completion count is not a passing-test count; the allowlisted report exposes the first failure, not the total failure count.
- The first reported failure is now localized to the combined image's `image_cube_startup` stage with fixed error category `assertion`. The exact-image import smoke and earlier sequential prerequisites succeeded. The Node child did not provide accepted readiness; its underlying cause still requires reproduction or narrower safe diagnostics. No raw child error or credential-bearing output is released.
- Exact registered cleanup and the independent always-cleanup attempt both passed. The summary step intentionally returned failure for the blocked integration.
- Actual job time was 18:56:09–19:11:36 UTC, 927 seconds, or 16 rounded standard Ubuntu minutes.

This confirms that the optimization fits the complete backend selection inside the existing caps for this run. It does not establish complete backend, combined image journey, live provider or organization acceptance.

### Reproduced startup defect and bounded correction

The test-only Python launcher rebuilt Node's environment without `HOME`, although the pinned image explicitly uses `HOME=/tmp/spaceport-no-adc` and runs as UID 65532. A local Linux user-namespace reproduction mapped only the real Node child to that UID: Cube 1.8.5's import failed at `os.homedir()` without the fixed home. The complete genuine gateway/Cube/compiler/SDK/PostgreSQL journey then failed at the same bounded readiness assertion. Restoring the fixed synthetic home made that full journey pass (1 test, 8.33 seconds, verified cleanup).

The correction sets that constant in the test child; it never inherits the caller's home or credential environment. Production credential handling, permissions, cost limits, timeouts and configuration are unchanged. Two portable environment regressions were observed failing before the change, then passed; 191 focused Python checks and 13 actual Node startup/guard/server checks passed. The ordinary-UID full journey and source-runtime probe also passed (2 tests, 17.15 seconds, verified cleanup). Mandatory tests gain no dependency on Linux user namespaces.

This reproduces a concrete defect consistent with run 21's safe stage receipt. The hosted Node exception was not exposed, and exact-image acceptance remains pending a new reviewed-source run.

Independent review repeated the actual UID/GID 65532 case: the unmodified source failed the complete journey (1.66 seconds), while the corrected source passed (7.85 seconds); both disposable clusters cleaned up. Independent fixed-output probes also confirmed `uv_os_homedir` for both Node home lookup and guarded Cube import without the fixed home, and success with it. The reviewer reran all 191 focused Python checks successfully.

The combined correction tree passed all 3,336 mandatory unit/contract tests in 137.38 seconds under `umask 077`, with 433 service-dependent cases deselected. The production/runtime, migration, workflow, public-contract, client and publisher components remain unchanged from run 21; only the test child, its regression coverage and this evidence changed. Hosted exact-source acceptance is still required.

## Hosted attempt 22: exact-source backend pass

[Run 22](https://github.com/Sean-Koval/spaceport/actions/runs/38800064164/job/116646465060) executed trusted main workflow `3fa6b2f698fa406918d82173d4422166c059b84b` against source `2d3cd1e4319f36a70bb95379f6713584abc5c4f2`, tree `c4317c34c4f3c7b3030ca5c0287c73e537006ee8`.

- All **3,336 mandatory unit/contract cases** passed and were admitted. Source phase: 152.20 seconds.
- All **406 backend cases** passed. The mandatory sequential gate includes the actual gateway image build, network-disabled installed CLI/import smoke, BigQuery runtime image and probe, and the BigQuery/Domo synthetic-provider product journeys. Both source and backend phases reported exit zero with no timeout.
- Backend compatibility orchestration took 744.68 seconds including its sequential image/build/runtime prerequisites; this is not the separately bounded pytest-process duration. No timeout or cost cap was increased.
- Exact registered cleanup passed. The independent always-cleanup attempt returned zero; the final allowlisted summary was PASS.
- Job time: 20:12:32–20:28:11 UTC, 939 seconds, or **16 rounded standard Ubuntu minutes**. Project ledger after this run: 303 completed, zero reserved.
- Integration receipt SHA-256: `f37c1c6e1d072d9c19ebe61743f0ad8bb5ef1d20c5e0f409373fe41fc2c3eaa6`. Cleanup receipt SHA-256: `c21724cd3a54f37fac23570a2e4a4f3d0e15facf9a5f059c00c5946ce074fcb2`.

Provider transport/identity responses in the journeys remain synthetic. No live BigQuery/Domo queries, cloud deployment, organization credential setup, browser acceptance or source entitlement qualification occurred. The trusted main workflow's old `domo: NOT IMPLEMENTED` label remains stale: candidate direct-REST serving and synthetic journey tests exist and passed, while live Domo access was not run.

This receipt qualifies the connector candidate's exact tested source. It does not qualify the separately changing owner-approval security branch. PR 5 subsequently merged by user approval; the operator guide continues to block organization activation until the additional security and live-provider gates are satisfied.

This later documentation-only update was not rerun in CI. Its runtime, tests, workflows, migrations, clients and publisher are unchanged; the acceptance receipt remains bound to source `2d3cd1e4319f36a70bb95379f6713584abc5c4f2` and tree `c4317c34c4f3c7b3030ca5c0287c73e537006ee8`, not to a newly tested documentation head.
