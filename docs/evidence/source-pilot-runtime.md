# Source pilot runtime: evidence boundary

15 October 2026. Scope: [source pilot design](../superpowers/specs/2026-10-15-source-pilot-runtime.md) and [implementation plan](../superpowers/plans/2026-10-15-source-pilot-runtime.md).

**In development. No final-source hosted acceptance yet.** The previous connector foundation was merged by approval into private main `3fa6b2f698fa406918d82173d4422166c059b84b`, tree `a3155cbfb8fc5c073a4c2d184ecd75d0b99d0024`. Its [run 18 receipt](reviewed-connectors.md#hosted-gate-attempt-18-pass) applies to that foundation only. The new pilot implementation requires its own review, exact-source gate and separate merge approval.

## What the checks establish

- Unit and contract tests cover strict catalogs and product packages, source request compilation, current-authority handoffs, bounded provider transports, response validation and private CLI state. They do not establish real provider permissions, cost behavior, PDP or freshness.
- A disposable PostgreSQL 17.11 instance built from the checksum-verified official release is available in the cloud development workspace. Real migration, privilege, concurrency and retained-state tests use fresh isolated clusters with synthetic credentials. This is actual database execution, but not the pinned hosted container gate.
- The BigQuery serving probe uses actual Cube 1.8.5 HTTP/compiler/driver, BigQuery SDK 7.9.4, Google auth 9.15.1 and PostgreSQL client 8.16.3 against the genuine disposable registry. Only the Google provider boundary is synthetic. The probe exercises authorization, retained models, job identity and lifecycle behavior; it does not query Google Cloud.
- The complete BigQuery package/provision → author → isolated fixtures → publish → describe → consumer query → worker → stored result path has now run through production services with actual Cube/SDK and PostgreSQL. Only the Google transport and exact-origin loopback TLS routing boundary are synthetic; live TLS, Google permissions and quotas remain unqualified.
- The required hosted gate builds the actual gateway Dockerfile and runs CLI/import checks with network disabled, then executes the genuine backend and source-runtime cases. Merely adding that gate does not mean it has passed.

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

## Remaining release gates

1. Independently review the complete final source tree. BigQuery gateway/Cube and Domo service/installed-API journeys have development evidence; final-source qualification remains required.
2. Preserve both complete sample-product journeys in the mandatory hosted selection, with synthetic provider boundaries clearly identified. The combined test image uses the pinned Cube base and the same prepared locked Python/package inputs; related image-ID labels are evidence references, not ancestry proofs.
3. Pass the exact-source hosted backend, actual gateway image and actual Cube source-runtime gates within the existing limits, with verified cleanup and a bounded receipt.
4. Obtain separate approval to merge this pilot wave.
5. Before organization use, implement the separately reviewed, revocable data-owner approval gate for explicit non-PII shared-source products and fail closed on unsupported delegated-source modes. Current user/client authentication and product ACLs do not prove employee Domo permissions, relationship-based delegation or PII safety.
6. Separately configure and verify operator-owned credentials/IAM, restricted source datasets, network egress, identity/PDP mapping, source definitions, quotas and freshness prerequisites. Live source requests, cloud resource creation and deployment are not authorized by the implementation work.

Browser SSO/PKCE, composed browser acceptance, the clean no-acquisition repeat, packet-observer assurance and production rollout remain separate gates. Domo MCP is not used for approved-statement serving; the direct REST transport and its shared integration-identity semantics are described in the [Domo design](../superpowers/specs/2026-10-15-domo-pilot-runtime.md). GCS remains the earlier offline exact-generation artifact slice.
