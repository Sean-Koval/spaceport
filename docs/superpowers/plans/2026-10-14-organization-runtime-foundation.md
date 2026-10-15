# Organization runtime foundation implementation plan

Scope: [approved implementation boundary](../specs/2026-10-14-organization-runtime-foundation.md). Source baseline is the tree of private main `08330568c575b04f29cabf36d926e709e0da623a`; local worktree ancestry may differ and publication must preserve remote main ancestry.

## 1. Strict operator configuration

Owner: isolated configuration worktree. Add `gateway/pilot_config.py` and dedicated unit tests. Establish failing tests for the profile, trusted origins, identity settings, certificate-verified registry URL, fixed secret references, synthetic asset pinning and bounded safe file loading. Implement the minimum passing behavior. Publish the interface to runtime/provisioning owners before integration.

## 2. Shared-services runtime composition

Owner: isolated runtime worktree. Add `gateway/pilot_runtime.py` and dedicated composition tests. Connect validated configuration to the existing services, authenticator, REST/MCP transports, worker and retention loop. Test fail-closed startup, canonical discovery, resource closure and synthetic OIDC authentication without a real issuer. Keep the existing demo entry point unchanged.

## 3. Explicit identity provisioning

Owner: isolated bootstrap worktree. Add `gateway/pilot_bootstrap.py`, strict-mapping tests and genuine PostgreSQL tests where applicable. Validate the entire mapping before connecting. Reuse existing migrations and issuer-bound subject keys. Test user/service distinction, scoped synthetic grants, separated author/publisher accounts, immutable-asset mismatch, partially populated state and preservation of revocations on rerun. Never run against an organization database.

## 4. Operator handoff and roadmap

Owner: integrator. Add fictional reference configuration, a narrow edge routing template, command/dependency/secret-mount instructions, the readiness assessment and explicit remaining gates. Cover identity-provider compatibility, browser login, the credential matrix, conditional GCP topology, performance evidence and full stored-data retention. Link the roadmap and README without replacing historical evidence.

## 5. Integration and independent review

Cherry-pick reviewed slices into one integration worktree; resolve only shared-interface issues. Run focused tests, all available unit/contract tests, OpenAPI consistency and existing frontend checks when changed. Have an independent reviewer inspect the combined diff and targeted security boundaries. Record exact test scope and any unavailable genuine PostgreSQL/Cube/browser evidence. Publish a draft pull request only after local source checks and independent review. The root coordinator owns any hosted CI dispatch, accounting and subsequent monitoring. Deployment and merge remain separate decisions.
