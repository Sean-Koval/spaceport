# Organization runtime foundation: source and backend evidence

Verified through 15 October 2026. Scope is the [bounded OIDC/synthetic runtime foundation](../superpowers/specs/2026-10-14-organization-runtime-foundation.md), with GCP/GKE selected as the target. This is not live identity, managed-database, cloud deployment or organization-data acceptance.

## Source identity

The initial local code/reference candidate was commit `fb7f6b0ff14afd32792e22f8ec99e8f008a37aec`, tree `01fd8915c8a86c06836ffca402a0c765a9e797c7`. Independent review approved that exact source. A documentation-only update was published as private GitHub commit `4f61557dd20c84b285ad7d8051cc7a0eb393b47f`, tree `33675e5e3f1bf4bbd254616ba355eb9f40f8fdd7`, in the then-draft [PR 3](https://github.com/Sean-Koval/spaceport/pull/3). Local commit ancestry differs from private GitHub main; publication preserves remote ancestry and verifies tree equality rather than claiming the local commits were pushed unchanged. The subsequent test-portability corrections below leave runtime code unchanged.

The source baseline is the tree of private main `08330568c575b04f29cabf36d926e709e0da623a`. The existing demo runtime, publisher, clients and OpenAPI/MCP snapshots are unchanged. The candidate adds pilot configuration, runtime, explicit provisioning, projected-file snapshots, inert GKE references and dedicated tests.

## Passed locally

- Complete unit/contract selection: **2,220 passed, 373 deselected**, 114.93 seconds, exit 0. Command: `.venv/bin/python -m pytest -m 'unit or contract' -q`.
- Focused pilot selection: **270 passed**. These are included in the 2,220 total, not additional tests. Independent review reran the 270 cases successfully.
- Frontend: **254 tests across 24 files passed** using Node 24.19.0 and the existing locked Vitest 5.0.4 dependency. The unchanged frontend also passed TypeScript checking, generated-client consistency, Vite production build and Prettier formatting checks.
- Python compilation and Git diff checks passed for the new modules. Changed-document relative links were checked; final documentation receives its own review.
- Fictional GKE configuration and identity examples pass through the actual configuration/provisioning contracts without opening connections.

Synthetic OIDC tests use signed test tokens and the real authenticator/ASGI/MCP middleware with mocked external I/O. They cover canonical public metadata, user/service distinctions, rejected credentials, exact Host authority, startup/shutdown state and resource cleanup. Filesystem tests cover the supported projected-file shape, mutation/escape/type/mode bounds and private regular-file snapshots. GKE tests inspect reference structure; they are not admission/schema or provider tests.

Review caught and corrected exact-authority normalization at the application/edge, missing initial author/publisher identities, signal-cleanup resource closure, serving-role registry timeouts and shutdown-grace accounting. Fixed serving-role SQL options are verified through URL parsing and the actual SQLAlchemy/Psycopg argument conversion; server-side session behavior is covered by separately marked genuine tests.

## Hosted attempt and portable certificate fixture

[Hosted run 38647209205](https://github.com/Sean-Koval/spaceport/actions/runs/38647209205) verified the exact published source/tree above and finished **BLOCKED**, with **cleanup PASS**. Its unit phase failed because a test assumed Python's default operating-system CA-file path was present; the standalone CI Python returned `None`. Genuine backend tests were not reached. The bounded receipt identifies `test_ca_requires_parseable_certificate_and_no_private_key` and a `TypeError` at the test's `Path(defaults.cafile)` construction.

The test now extracts a public PEM certificate from the existing locked `certifi` package, with no network access, new dependency, private credential or runtime trust change. A parameterized regression reproduces missing system CA paths; the original implementation fails that case. After correction, **104 configuration tests** and the complete selection of **2,221 unit/contract tests** passed locally, with **373 deselected**, in 108.84 seconds. The additional regression is included in that total. Independent review also verified the certificate cases with socket and process-launch audit guards. The malformed-certificate and private-key rejection assertions remain active.

[Hosted run 38648522667](https://github.com/Sean-Koval/spaceport/actions/runs/38648522667) verified corrected source `1f11a8b84e21279a76221fa6a88f9ba01499f07b`, tree `e6903bf7ad0df003d09e9da731217cf026104d9e`, and admitted **2,221 unit/contract tests PASS**. Its backend process completed all 346 cases but the suite remained **BLOCKED**: a new test assertion used a literal percent sign in `LIKE 'demo-%'` while its helper supplied an empty parameter tuple, causing Psycopg placeholder parsing to raise `ProgrammingError`. **Cleanup passed.** A completed-case counter is not a backend pass.

That assertion now binds the pattern through `LIKE %s`. Offline reproduction with the pinned Psycopg parser fails for the original query and preserves the `demo-%` parameter with the corrected query. All 32 statically evaluable SQL helper calls in the eight new test files were checked for binding compatibility; the failing assertion was the only new SQL literal-percent case. Focused bootstrap/configuration tests passed **190 cases** locally. Runtime code, database policy and dependencies remain unchanged. The final assertion correction passed the hosted verification below.

## Hosted final-source pass and merge

[Run 38650608566](https://github.com/Sean-Koval/spaceport/actions/runs/38650608566), completed 14 October 2026, verified source `c060dc23c97cae0123d075ce5d78767baf9773d9`, tree `76611787d8ee0c39478f6377bbb62dca3092e6e8`. It admitted **2,221 unit/contract tests PASS** and **all 346 genuine PostgreSQL/Cube backend cases PASS**. Exact registered cleanup passed, and the always-cleanup check exited 0. No artifacts were uploaded. The workflow executed from `08330568c575b04f29cabf36d926e709e0da623a` in explicit backend mode; this is disposable-backend evidence, not an organization deployment or composed-demo pass.

[PR 3](https://github.com/Sean-Koval/spaceport/pull/3) merged on 15 October 2026 as `a4e3c4fb36a60effd66a72a30fea6fbbabc44540`. The remote merge commit has the exact tested tree above, and `main` was read back at that commit. All four workflows remain manual-only; no workflow run was reported for the merge commit. This receipt covers the tested source and its tree-identical merge. The subsequent documentation-only status update has a different tree and was not executed by that run.

## Collected, not executed locally

The complete genuine PostgreSQL/Cube selection collects **346 cases**: the previous 329 plus 15 pilot provisioning cases and two session-option cases. Local execution has no authorized disposable PostgreSQL/Cube host, so collection is not a pass. The complete selection subsequently passed in the hosted run above; it was not executed locally.

No new live NGINX, Kubernetes schema/admission, CSI provider/mount, Cloud SQL, TLS socket, actual issuer/Studio, browser/composed, BigQuery or Domo evidence is claimed. No image was deployed, no credential or IAM/OAuth grant was created, and no warehouse query was executed. The source merge does not authorize deployment, paid queries, credential grants or public release.

## Remaining release boundary

See the [organization-pilot assessment](../operations/organization-pilot.md), [GKE prerequisites](../../infra/gke/README.md), [identity guide](../operations/identity.md) and [production gates](../operations/production-gates.md). The new runtime still serves only the reviewed synthetic model; required external Cube/TLS/source topology is not supplied by the GKE reference. Browser OAuth/PKCE, actual identity-provider interoperability, bounded live-source execution, complete stored-input retention, operating visibility, restore and load evidence remain open.
