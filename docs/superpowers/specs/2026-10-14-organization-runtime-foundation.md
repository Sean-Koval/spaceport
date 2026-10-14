# Organization runtime foundation

## Outcome and boundary

Provide an explicit, independently testable runtime that connects the existing user-bound OIDC verifier, trusted HTTPS transport configuration, TLS registry connection and operator-managed identity mappings to the shared application services. This is the first organization-deployment foundation, not permission to deploy or proof that organization data is supported.

The selected slice retains the reviewed synthetic revenue model and isolated synthetic fixture model. Its profile is named `oidc-synthetic-pilot`. Real BigQuery execution remains blocked by the existing source-bound gate; Domo remains absent. Browser OAuth authorization-code/PKCE login, vendor-specific identity compatibility, a cloud provisioning module, performance improvements and live credentials are outside this slice.

GCP/GKE was explicitly selected during the review. Include inert GKE workload/reference manifests and a tested projected-file snapshot helper; do not create a cluster, database, service account, IAM binding, OAuth grant or secret version. Identity-provider selection remains open.

## Configuration and trust

- Use a separate `gateway.pilot_runtime` entry point. Preserve the loopback-only demo runtime and its defaults.
- Require a bounded, strict operator JSON configuration selected through an absolute `GATEWAY_PILOT_CONFIG_FILE` path. Reject unknown/duplicate fields, unsafe file types, mutation during read and invalid settings before opening a source, identity or registry connection.
- Require a canonical HTTPS public origin. Derive the REST origin, exact Host allowlist, MCP resource URL and protected-resource metadata from it. Do not accept request-selected URLs, wildcard hosts, bearer forwarding or forwarded-header identity.
- Configure the existing RFC 9068 verifier with an explicit trusted issuer, resource audience, JWKS endpoint, client IDs and asymmetric algorithms. Retain its bounded key cache, refresh policy, timeout and fail-closed behavior. This profile does not imply compatibility with every identity provider.
- Configure registry host, port and database with mandatory certificate/hostname validation (`verify-full`) and a mounted CA file. Retain fixed, separate owner, app, audit-retention and source-reader database roles and the migration privilege checks.
- Reference mounted secrets, never inline values. Application and worker processes must not receive owner credentials. No command generates or stores credentials.
- For GKE projected inputs, an allowlisted init helper materializes one stable supported projection generation into regular private files in memory-backed storage. Runtime mounts are read-only. Config/key/CA rotation requires a controlled rollout; no hot-rotation or immediate hash-file revocation claim is made.
- Configure separate HTTPS Cube endpoints while retaining the checked-in synthetic model/binding identities and separate signing-key references. Configured endpoints are operator trust decisions, not discovered from tokens, manifests or requests.

## Provisioning and authorization

An explicit owner-only command applies existing migrations; a separate owner-only command imports a reviewed identity mapping into an empty registry. Normal runtime startup never migrates, grants permissions, publishes a product or manufactures test evidence.

User identities bind the configured issuer and exact subject using the existing issuer-bound subject key. Service principals are explicitly distinct and may use only an already provisioned hash-only service credential file. JWT group/role claims do not create or broaden grants. Each approved mapping names synthetic tenant and policy-scoped roles; groups remain tenant-bound. Separate author and publisher principals are required. Internal fixture identities have no external credentials.

The first provision installs the reviewed policy/bindings and mappings without publishing. Repeating it verifies immutable identity and reviewed-asset compatibility and must not restore revoked principals/grants or overwrite operator changes. Divergent or partially populated state fails closed.

## Runtime and edge

REST and MCP share the same service container, authenticator and current policy checks. Worker and retention commands use the same configuration. A proxy reference routes the exact OAuth protected-resource discovery path to the gateway, rather than the frontend fallback. TLS termination, certificates, network boundaries and secret delivery remain operator-owned deployment prerequisites.

## Acceptance

1. Invalid configuration, inline secrets, unsafe URLs/files, missing required mounted material and TLS downgrade attempts fail before network use, with sanitized errors.
2. The composed application uses the configured production authenticator and canonical HTTPS origin/resource; demo tokens cannot authenticate in this profile.
3. A synthetic signed access token reaches the intended principal through the existing verifier, while wrong issuer/audience/client/profile, service impersonation and inactive mappings fail closed. Tests do not contact a real identity provider.
4. Explicit provisioning separates users/services and author/publisher roles, rejects ambiguous mappings and proves safe rerun behavior. Genuine database assertions remain separately labeled from mocked unit coverage.
5. Startup does not provision or publish. Every service retains existing contract, policy, freshness, idempotency, reservation and source-activation protections.
6. Configuration examples contain fictional domains/subjects and references only. Documentation explicitly distinguishes this foundation from browser login, live source, composed-browser, cloud deployment and production acceptance.
7. Independent review and source-level checks cover the exact combined tree. Genuine backend/hosted runs are dispatched only by the coordinator against the exact reviewed commit; no deployment or merge is implied.
