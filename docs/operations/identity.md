# Identity and deployment modes

The source implements verification, fresh identity/grant reads and shared HTTP/MCP authentication. The publisher and synthetic Compose topology also exist, but rendered-browser/composed acceptance, an identity-provider browser sign-in flow and verified Studio delegation remain open. Production remains blocked by the additional gates in the approved design. The [organization-runtime foundation](../superpowers/specs/2026-10-14-organization-runtime-foundation.md) wires the existing verifier into a separate synthetic-data OIDC profile; it is not a universal identity-provider compatibility claim.

## Internal interfaces

Import these exact types from `gateway.auth.principals`:

- `PrincipalStore(sessions)` uses the registry's async SQLAlchemy session factory and app-role credentials.
- `await PrincipalStore.get_current(principal_id) -> PrincipalGrants` reads active current grants without a cache. The optional keyword `session=uow.session` includes the read in a policy/audit transaction. It refreshes already-loaded ORM grant state, preserving normal autoflush, and does not commit the caller's transaction.
- `PrincipalGrants` has `principal_id` and immutable `grants: tuple[PolicyGrant, ...]`.
- Each `PolicyGrant` has `policy_id`, `tenant_id`, immutable `capabilities` and immutable `entitlements`. Entitlements map bounded identifiers to nonempty arrays of nonnull scalar values. Never combine capabilities from one scope with entitlements or the tenant from another. Select the product policy and the exact allowed tenant first; ambiguity must fail closed in the policy service.
- `capabilities_for_roles(roles)` translates server-managed roles using the table below. Unknown/duplicate roles or malformed grant state fail closed rather than creating default permissions.
- `await PrincipalStore.resolve_subject(subject, issuer=trusted_issuer)` resolves a user by the compound provisioning key described below.
- `await PrincipalStore.resolve_principal(principal_id)` resolves the active identity behind an opaque credential. It does not accept impersonation fields.

Import `Authenticator` from `gateway.auth.tokens`. `await Authenticator.authenticate(authorization: str | None) -> Principal` accepts only an Authorization string. The shared `Principal` contains `id`, raw verified `subject`, `service_client_id` and `kind`. Tenant, capabilities and entitlements never come from JWT role/tenant claims, forwarded headers, MCP arguments or client-supplied grant objects. Unknown identity and verification failures raise safe `UNAUTHENTICATED` errors with a request ID. `get_current` rejects inactive/missing principals with `NOT_FOUND_OR_FORBIDDEN`; unreadable or malformed permission state raises safe `UPSTREAM_UNAVAILABLE`.

Construct one authenticator per process/event loop and reuse its HTTP client. Call `await authenticator.close()` during application shutdown. An injected client is caller-owned. The application must call `validate_mode(settings)` before opening ingress/source connections; the authenticator also calls it during construction. `Settings.from_env()` parses only the documented operator environment variables and performs that validation.

| Server-managed role | Capabilities |
| --- | --- |
| consumer | discover, describe, execute, result |
| author | author |
| publisher | publish |
| operator | operate |
| policy_admin | policy_admin |
| audit_view | audit_view |

These are separate privileges. Author/publisher/operator rights do not imply consumer/data access. A grant can explicitly combine roles within its own policy and tenant. Authentication can succeed for an active principal with no grants; authorization must still reject unauthorized actions. The policy service enforces current scope and required audit writes. The bounded pilot bootstrap further requires distinct author/publisher principals and supports one synthetic policy/tenant grant per principal.

## Production access-token profile

Production requires one explicitly configured credential-free HTTPS issuer URL, fixed HTTPS JWKS URL, API audience and allowlisted OAuth client IDs. No automatic token-directed discovery occurs. Production source configuration may select `postgres` or `bigquery`; those names confer no live-source readiness or credentials. The only enabled access-token profile is `rfc9068`; generic JWT profiles and issuers that cannot distinguish access from ID tokens remain disabled.

The profile requires a signed `typ: at+jwt` (or `application/at+jwt`) header and `iss`, `aud`, `sub`, `exp`, `iat`, `client_id` and `jti`. Signature, exact issuer and audience are verified. Numeric dates are strict integer seconds with no clock skew allowance: `iat <= now < exp`; a present `nbf` cannot be in the future. The client ID must be operator-allowlisted. An ID token's ordinary `typ: JWT` is rejected even when its signature, issuer and audience are otherwise valid; `token_use: id` is rejected too. Token role/tenant/claimed-user fields do not contribute permissions. JSON duplicate members, embedded keys, token-selected `jku`/`x5u`/`x5c`, critical extensions and unsigned/symmetric algorithms are rejected.

RS256 is the default. Alternative asymmetric algorithms must be explicitly configured: RS384/512, PS256/384/512, ES256/384/512 or EdDSA. RSA public keys must be at least 2048 bits; EC curves must match the algorithm. Do not infer allowed algorithms from the token or enable shared HMAC secrets to accommodate a JWT. This is a deliberately narrow [RFC 9068 access-token profile](https://www.rfc-editor.org/rfc/rfc9068); operational provider compatibility remains an integration gate.

JWKS requests explicitly require `Accept-Encoding: identity`. Any nonidentity Content-Encoding is rejected before reading or decoding the body. Raw chunks are checked against the remaining 64 KiB budget before copying; HTTPX content decompression is never invoked. JWKS bodies are capped at 64 KiB and 20 uniquely identified public signing keys. Cache lifetime is at most 300 seconds. Each fetch has a two-second maximum total deadline as well as HTTP inactivity timeouts; redirects and environment-derived proxies are disabled. A cached unknown key can trigger one refresh, with a process-wide 30-second unknown-key refresh cooldown to bound arbitrary-key fetch amplification. Token-selected endpoints are never followed. Once the cache expires, an issuer outage fails authentication; stale keys are never used to rescue the failed refresh. Known cached keys remain usable only within their existing bounded lifetime. Providers must account for that bounded key-removal window.

## Authenticated user/client attribution and provisioning

An accepted, verifiable access token provides user/client authentication attribution. It does not establish a delegation grant, user–agent relationship, source-user entitlement or permission to act on another user's behalf. `sub` identifies the user and `client_id` identifies the agent/client application. These identities stay separate: a verified OAuth client never becomes an arbitrary named user. The returned `Principal.service_client_id` records the verified client ID for later audit. A service bearer key maps only to its stored scoped service principal and cannot be exchanged for a user identity by adding headers or parameters.

The reviewed-source mode [rejects unsupported explicit delegation markers](source-access-approvals.md); individual Domo permissions and a provider-qualified delegated mode remain separate requirements. Possession of an ordinary bearer token cannot reveal an unseen agent or prove a delegation chain.

The registry has a single unique `principals.subject` column. To avoid aliasing two issuers' identical subjects, provision OIDC users with `oidc_subject_key(issuer, subject)` from `gateway.auth.principals`. This helper uses domain-separated SHA-256 over an unambiguous issuer/subject pair and yields a bounded `oidc-v1-...` lookup key. Store that key in the row; returned `Principal.subject` remains the raw verified subject. Never manually use the raw subject as the stored OIDC lookup key. Demo and service subjects remain operator-owned synthetic/scoped labels. Policy/result reauthorization uses the resolved principal **ID**, not a comparison between a raw subject and the stored lookup key.

Only the registry owner/operator provisions principals and grant rows or changes/revokes them. The application role has no insert/update permission on these tables. An owner must review the specific policy, tenant, roles and entitlements before granting access. Do not give the application migration/owner credentials to simplify bootstrap. For a new issuer, explicitly provision its users with that issuer's key; changing the configured issuer alone does not adopt the previous issuer's accounts.

This is not verified Studio integration. No live OIDC requests, real user tokens, production credential creation or Studio account data were used. Task 11 must adapt its resource server to this authenticator and verify real HTTP audience/profile/delegation behavior. See [integration research](../research/integrations.md).

## Hash-only service/demo credential storage

`FileCredentialStore` reads an absolute, operator-selected external path. It never writes credentials. Mount this file read-only into the application, owned by the operator, in a private directory; use 0600 for operator replacement and a separate application identity/read-only mount. The implementation rejects symlinks at the file, non-regular files, group/world-writable files, unreadable/oversized files, in-place changes observed during a read, duplicate JSON members/digests, unknown fields, malformed hashes and more than 1,000 records. Each lookup reopens and validates the entire file; there is no credential-result cache. Replace the file atomically, rather than editing a live mounted file in place. For container packaging, prefer mounting the private containing directory read-only: a single-file bind mount can retain the old inode after host pathname replacement and hide revocation. Task 14 must prove replacement visibility and immediate revocation from inside the running application container, alongside read permission for its identity and inability to modify either the file or directory. Current unit tests prove ordinary filesystem rename/reopen behavior only; no container-mount result is claimed.

The JSON document has integer `version: 1` and `credentials`, an array of records with exactly these fields:

- `digest`: lowercase SHA-256 hex of the complete bearer token, including its prefix
- `principal_id`: an existing server-managed principal ID
- `kind`: service or demo
- `active`: a boolean

There is no plaintext/token field and no roles, tenant or entitlement override. SHA-256 here assumes an operator-generated token with at least 256 bits of cryptographic randomness; it is not a password-storage algorithm. The verifier accepts the exact format `dpg_svc_` or `dpg_demo_` followed by 43 base64url characters. Production accepts only service keys plus verified user OIDC tokens; demo accepts only demo keys. Credential kind and the current database principal kind must agree. Service principals must have an operator-provisioned service client ID. No production credential generator is provided.

Revoke a key by atomically replacing the hash file with the record inactive/removed. Revoke a principal or a grant through the operator-owned database path. Key/principal revocation is checked on the next authentication; grant revocation is checked on the next authorization/read. A previously authenticated long-running operation must still recheck current policy before execution/result release. Disabling a hash record is not a replacement for that reauthorization.

## Synthetic local demo bootstrap

The demo uses no issuer, real credentials or outbound identity calls. It accepts only `source=synthetic_postgres`, explicit numeric loopback binding, `public_ingress=false` and `trust_forwarded_headers=false`. Hostnames, wildcard binds, real BigQuery sources and any OIDC setting are rejected.

`bind_host` denotes the **published/public gateway ingress** binding, including an edge proxy's host port. It is not a claim about a private application-container listener. A future private Compose topology may have an internal listener reachable only on its private network, but Task 14 must verify the actual published port and edge routes against this loopback setting. Merely declaring `public_ingress=false` is not isolation evidence. The currently implemented guard remains strict; no public demo binding is allowed. A persistent demo UI banner remains a later UI requirement.

Generate random synthetic demo accounts locally with a new absolute output directory outside the repository:

```sh
uv run --locked python scripts/demo_accounts.py --output-dir /tmp/new-private-demo-accounts
```

Use an operator-controlled private parent directory. The generator rejects existing final paths, including dangling symlinks before resolution, preserves the outside-repository check and uses exclusive directory/file creation. It prints no tokens, uses a 0700 directory and 0600 files, and creates only:

1. `demo-tokens.json`: local bearer tokens for synthetic north/south consumers and separate author/publisher accounts. Never commit, send, log or paste them into manifests, examples or documentation. The UI must later keep its token in memory only.
2. `credential-hashes.json`: hash-only mapping; mount read-only and set `GATEWAY_CREDENTIAL_HASH_FILE` to the mounted absolute path.
3. `bootstrap.sql`: token-free synthetic principal/grant inserts in one transaction. Apply this file manually **as the registry owner/operator**, after creating the reviewed `revenue-access` policy. It is intentionally not idempotent and does not overwrite existing identities. The generator does not connect to the database and does not give the app grant-writing permission.

The policy must use the synthetic `north`/`south` tenant IDs and the documented `groups` entitlement, with independent row-constraint enforcement. Later seed/Compose tasks must coordinate these fixture names, the runtime credential mount and source-load watermark. Delete local token output when the demo is retired. No production credentials or private user data are generated.

## Exact dependency baseline and verification

Verified against official primary metadata on 13 October 2026:

| Dependency | Exact release | Python compatibility and source |
| --- | --- | --- |
| PyJWT with crypto extra | 2.15.1 | Python >=3.9; [official metadata](https://pypi.org/pypi/PyJWT/2.15.1/json), [verification API](https://pyjwt.readthedocs.io/en/stable/api.html) |
| cryptography | 50.0.2 | Python >=3.9 excluding 3.9.0/1; [official metadata](https://pypi.org/pypi/cryptography/50.0.2/json) |
| HTTPX | 0.28.1 | Python >=3.8; [official metadata](https://pypi.org/pypi/httpx/0.28.1/json), [timeout behavior](https://www.python-httpx.org/advanced/timeouts/) |

`pyproject.toml` pins these releases and `uv.lock` pins all transitive versions and artifact hashes. Unit tests use fixed synthetic local RSA keys, HTTP mocks and in-memory tokens. Identity-store tests require the genuinely migrated, explicitly authorized disposable PostgreSQL fixture; mocks/SQLite are not substitutes. Run:

```sh
uv sync --locked
uv run --locked pytest tests/unit/test_identity.py -q
uv run --locked pytest -m 'not postgres' -q
# Requires the disposable PostgreSQL harness described by Task 2:
uv run --locked pytest tests/integration/test_identity_store.py -m postgres -q
uv run --locked pytest -q
```

Real database tests are **NOT RUN locally** in the cloud implementation environment because neither PostgreSQL nor Docker is installed. The full local pytest invocation intentionally reports fixture setup errors instead of skipping or falsely passing those gates. A GitHub runner must execute the new identity-store cases through the shared authorized disposable PostgreSQL harness. No live issuer, Studio, ingress topology or production-readiness success is implied by deterministic unit tests.
