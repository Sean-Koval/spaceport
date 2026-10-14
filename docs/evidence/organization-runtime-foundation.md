# Organization runtime foundation: source-level evidence

Verified 14 October 2026. Scope is the [bounded OIDC/synthetic runtime foundation](../superpowers/specs/2026-10-14-organization-runtime-foundation.md), with GCP/GKE selected as the target. This is not live identity, managed-database, cloud deployment or organization-data acceptance.

## Source identity

The final local code/reference candidate was commit `fb7f6b0ff14afd32792e22f8ec99e8f008a37aec`, tree `01fd8915c8a86c06836ffca402a0c765a9e797c7`. Independent review approved that exact source. The subsequent evidence/status update changes documentation only. Local commit ancestry differs from private GitHub main; publication must preserve remote main ancestry and verify tree equality rather than claiming these local commits were pushed unchanged.

The source baseline is the tree of private main `08330568c575b04f29cabf36d926e709e0da623a`. The existing demo runtime, publisher, clients and OpenAPI/MCP snapshots are unchanged. The candidate adds pilot configuration, runtime, explicit provisioning, projected-file snapshots, inert GKE references and dedicated tests.

## Passed locally

- Complete unit/contract selection: **2,220 passed, 373 deselected**, 114.93 seconds, exit 0. Command: `.venv/bin/python -m pytest -m 'unit or contract' -q`.
- Focused pilot selection: **270 passed**. These are included in the 2,220 total, not additional tests. Independent review reran the 270 cases successfully.
- Frontend: **254 tests across 24 files passed** using Node 24.19.0 and the existing locked Vitest 5.0.4 dependency. The unchanged frontend also passed TypeScript checking, generated-client consistency, Vite production build and Prettier formatting checks.
- Python compilation and Git diff checks passed for the new modules. Changed-document relative links were checked; final documentation receives its own review.
- Fictional GKE configuration and identity examples pass through the actual configuration/provisioning contracts without opening connections.

Synthetic OIDC tests use signed test tokens and the real authenticator/ASGI/MCP middleware with mocked external I/O. They cover canonical public metadata, user/service distinctions, rejected credentials, exact Host authority, startup/shutdown state and resource cleanup. Filesystem tests cover the supported projected-file shape, mutation/escape/type/mode bounds and private regular-file snapshots. GKE tests inspect reference structure; they are not admission/schema or provider tests.

Review caught and corrected exact-authority normalization at the application/edge, missing initial author/publisher identities, signal-cleanup resource closure, serving-role registry timeouts and shutdown-grace accounting. Fixed serving-role SQL options are verified through URL parsing and the actual SQLAlchemy/Psycopg argument conversion; server-side session behavior is covered by separately marked genuine tests.

## Collected, not executed locally

The complete genuine PostgreSQL/Cube selection collects **346 cases**: the previous 329 plus 15 pilot provisioning cases and two session-option cases. Local execution has no authorized disposable PostgreSQL/Cube host, so collection is not a pass. Those new cases require the same guarded disposable backend and complete exact-source hosted verification as the existing backend suite.

No new live NGINX, Kubernetes schema/admission, CSI provider/mount, Cloud SQL, TLS socket, actual issuer/Studio, browser/composed, BigQuery or Domo evidence is claimed. No image was deployed, no credential or IAM/OAuth grant was created, and no warehouse query was executed. Public release and merge are separate decisions.

## Remaining release boundary

See the [organization-pilot assessment](../operations/organization-pilot.md), [GKE prerequisites](../../infra/gke/README.md), [identity guide](../operations/identity.md) and [production gates](../operations/production-gates.md). The new runtime still serves only the reviewed synthetic model; required external Cube/TLS/source topology is not supplied by the GKE reference. Browser OAuth/PKCE, actual identity-provider interoperability, bounded live-source execution, complete stored-input retention, operating visibility, restore and load evidence remain open.
