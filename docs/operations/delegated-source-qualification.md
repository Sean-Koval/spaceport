# Spaceport delegated Domo access qualification

15 October 2026 · Operator procedure · Documentation only · No live qualification performed

This procedure defines the evidence needed before Spaceport may execute reviewed queries for a particular Domo user and agent. It complements section 9 of the [proposed source-access security design](../superpowers/specs/2026-10-15-source-access-security.md). The next security slice targets owner-approved shared-identity, non-PII products; it does not implement delegation or generic ReBAC, and organization activation remains gated. Completing this document grants no source access or rollout approval.

## 1 Select a candidate without assuming compatibility

- **MCP user OAuth:** Domo documents access scoped to the connected user's permissions. Its prerequisites include AI Chat or AI Library and the Use MCP grant. This establishes a candidate, not proof of endpoint-specific row, column, revocation or cache behavior. [Domo OAuth](https://www.domo.com/docs/s/article/MCP-Standard-OAuth)
- **MCP query contract:** The published SQL Query tool is described as natural-language querying. Display names and successful chat answers do not establish exact SQL execution. Capture the actual contract in step 4. [Domo MCP toolkit](https://www.domo.com/docs/s/article/Domo-MCP)
- **Direct REST:** Domo publishes POST `/v1/datasets/query/execute/{dataset_id}` with a required `sql` string. Its client-credentials flow exposes a `data` scope covering import and export, not a read-only scope. Exact-statement capability does not establish employee-specific PDP entitlements for a shared client. [DataSet API](https://github.com/DomoApps/domo-documentation-hub/blob/main/portal/API-Reference/Domo-APIs/DataSet-API.yaml), [Domo API Explorer](https://developer.beta.domo.com/explorer)
- **Manual access tokens:** Domo documents a separate developer-token connection. Do not substitute it silently when user OAuth fails; its identity, lifecycle and permissions need separate qualification. [Manual MCP connection](https://www.domo.com/docs/s/article/MCP-Manually-Connect)

**Unresolved:** actual MCP wire tool names/schemas; exact-statement support; user OAuth token audience, expiry, refresh and revocation semantics; supported delegated identity binding; endpoint-specific PDP/column behavior; propagation and caching. No verified MCP-token-to-REST compatibility exists in this review. Obtain an official contract for the chosen lane; do not invent exchange, introspection, revocation or identity endpoints.

## 2 Authorize a bounded operator run

Name the Domo administrator, data owner, identity/security reviewer and test operator. Record the approved sandbox instance, two test users, synthetic datasets, agent clients, permitted actions, query/rate budget, time window, evidence retention and cleanup owner. Define acceptable permission-change and revocation delays before testing. Missing values block execution.

Account creation, dataset upload, role/PDP changes, OAuth registration/consent, credential provisioning and revocation require separately authorized user/operator execution. Users enter credentials through their approved secure sign-in process. No credentials belong in chat, fixtures, source control or this receipt. Bootstrap may prepare configuration but cannot authorize an account, approve a product or activate a delegated connector.

This assignment performs none of those actions, no authenticated Domo requests, paid queries, IAM changes, cloud creation or deployment. Future MCP qualification needs a cost decision: Domo says tools using its models can incur token charges. [MCP consumption considerations](https://www.domo.com/docs/s/article/Connect-AI-Tools-to-Domo-Using-MCP)

**Gate A:** approved scope and accountable operators exist; otherwise stop at documentation.

## 3 Establish synthetic ground truth

Use two non-admin test users, `U_A` and `U_B`, with neither dataset ownership nor broad administrative bypass. Use two Spaceport tenants, `T_A` and `T_B`. These represent application tenants inside one test Domo instance; cross-instance isolation needs a separate approved test if required.

The operator provisions `D_MIX` with these four invented rows and no production-derived values:

| Row | Tenant | Group | Metric |
|---|---|---|---:|
| A1 | T_A | allow | 11 |
| A2 | T_A | deny | 12 |
| B1 | T_B | allow | 21 |
| B2 | T_B | deny | 22 |

Add a `restricted_marker` column containing non-personal sentinels such as `SYNTHETIC_DENY_A1`; treat it as a prohibited field to exercise PII controls without real PII. Add `D_DENIED`, inaccessible to both users, with a unique synthetic sentinel. The approved product exposes only row identifier and metric; tenant/group fields are reviewed hidden scope inputs.

Configure and independently verify source rights: `U_A` sees only A1; `U_B` sees only B1; neither sees `D_DENIED`. Record whether source-enforced column denial for `restricted_marker` actually exists on the selected endpoint. If unavailable, record that gap and stop any claim of column-permission parity. A separately reviewed source projection may become a new candidate; filtering prohibited source columns after retrieval is not equivalent proof.

Retain the administrator's intended matrix and each user's independently observed baseline. Use approved synthetic probes directly at the selected source endpoint to test denied rows/fields, separately from gateway filtering. A Spaceport predicate or mock cannot prove Domo enforcement. Distinguish absent rows due to source policy from authorization errors; never treat an unexplained empty answer as success.

**Gate B:** independent source baseline matches the intended matrix; no real PII or broad-access test identity.

## 4 Capture identity and transport evidence

For each user, after separately approved authorization, record the exact instance/resource URL, issuer, client, scopes, authenticated source-user binding, issuance/expiry behavior, refresh support and revocation mechanism from supported metadata and vendor documentation. Retain references and redacted observations, never tokens, cookies, authorization codes, secrets or raw authentication traces. Decode JWTs only where appropriate and validate their signature and claims; an opaque token requires supported evidence of its identity/audience, not a guessed claim format.

If MCP is selected, capture initialization negotiation, server identity/version and all paginated `tools/list` responses for both users. Save exact wire names, input schemas, optional output schemas, descriptions, annotations and a content digest. Allowlist only the reviewed read tool. A tool annotation alone is insufficient evidence of safety. MCP defines tool discovery and schemas; the actual Domo response remains necessary. [MCP tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)

Keep Spaceport's inbound audience separate from the downstream Domo audience. Verify that the accepted resource, user and client cannot be replaced by caller-supplied tenant, subject or on-behalf-of headers. Do not forward an inbound bearer token to Domo or reuse MCP credentials at the REST endpoint. Audience restriction and separate upstream credentials are explicit MCP security requirements; this does not prove Domo's implementation. [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)

Include missing, invalid, expired and wrong-audience credential tests at approved receivers, with no data returned. Use controlled test credentials; never transmit a real token to an unapproved host to test audience isolation. Record whether failure occurs at the gateway or source.

**Gate C:** supported identity/audience/lifecycle evidence and the actual transport contract are complete. Unknown means blocked.

## 5 Prove the reviewed statement contract

Create a versioned reviewed statement catalog pinning dataset, statement definition/digest, typed bounded parameters, row predicate, explicit output columns, ordering, limits and normalization schema. Keep synthetic SQL in the controlled catalog; operational audit records retain its identifier/digest, not SQL or data values. No arbitrary SQL, natural-language query, dataset identifier or unbounded text is accepted from a caller.

The operator must demonstrate that the selected provider operation executes that exact approved statement with validated parameters, without an intervening model rewriting its meaning or selecting another dataset. Test extra fields, alternate dataset IDs, expanded predicates, wildcard columns and changed limits; all must reject before source dispatch. Require source-contract evidence plus an observable request/result match, not repeated coincidentally correct answers.

If the only MCP tool converts natural language into SQL without a supported exact-statement guarantee, **this connector lane fails**. Do not fabricate an argument named `sql`. Direct REST remains a separate candidate requiring its own per-user token/permission proof; a working shared service token cannot fill the gap.

**Gate D:** deterministic statement execution and exact package pins are demonstrated for the selected user-aware lane.

## 6 Test the user and agent conjunction

The later design must authorize the intersection of current user relationships, authenticated agent delegation, product/field policy and independently verified source-user entitlements for one complete user/agent/client/tenant/product/action/scope tuple. A client allowlist or possession of a user bearer token does not establish delegation. Do not implement a general relationship service as part of this procedure.

Run the matrix through discovery, execution, results, cache hits and every cursor page. Use independent agent identities `A_A` and `A_B`, each delegated only by its matching user. Record expected/observed decisions and provider-call counts.

| Case | Required result |
|---|---|
| U_A/A_A/T_A and U_B/A_B/T_B with all rights current | Only A1 and B1 respectively; approved columns only |
| User relationship removed; other conditions remain valid | Deny |
| Agent delegation missing, revoked or expired; user still entitled | Deny |
| Source access absent; Spaceport policy still permits | No forbidden data; no alternate credential |
| Product or field policy denies; source still permits | Deny before dispatch for forbidden request |
| Swap user, agent, authenticated client, tenant, source credential or dataset | Deny substituted tuple; no foreign metadata or handles |
| Combine group permission from one grant with field permission from another | Deny unless one complete authorized tuple covers both |
| Resume an accepted operation/receipt/handle, cache entry or cursor under a different user or client | Deny the unauthorized resume; no automatic replacement POST after the failed resume |
| Self-asserted subject, nested/overbroad actor chain or forged delegation | Deny |

Prove that prohibited sentinels cannot escape through user-visible fields, previews, counts, errors, summaries or tracing. Keep synthetic input/expected-denial evidence restricted; no real PII belongs in any fixture or log. Broad source access never overrides a narrower Spaceport field policy. Unknown classification denies. Retained operational identities are access-controlled security metadata, not anonymous data.

## 7 Measure changes and revocation

For each test, establish a fresh baseline, warm relevant caches and retain a cursor and queued request. The authorized operator changes one control at a time: dataset permission, PDP/group membership, source column permission where supported, Spaceport relationship, agent delegation, product/field policy and OAuth grant. Test natural token expiry separately. Record the exact change acknowledgement time, last successful observation, first denial, subsequent observations and clock uncertainty at both source and gateway.

A bare idempotency-key string is principal-scoped: independently authorized users may use the same string. It does not identify another user’s operation. Test accepted authority and handles, rather than inferring ownership from that string.

Probe old sessions/tokens, new sessions, permitted refresh, queued dispatch, in-flight completion, cached results and retained cursors on the pre-approved cadence through the defined observation window. Report propagation as an interval between observations, not an invented exact latency. A refresh token, restored permission or replacement grant must not revive an execution bound to revoked authority. Test source/policy unavailability and malformed or missing evidence; all data release fails closed. Record outgoing identity selections to prove denials never trigger shared/service-account fallback.

Compare every measured interval with the agreed freshness bound. An unknown or excessive source delay blocks rollout until a supported control closes the gap. Spaceport must recheck current authority at admission, dispatch and release; it must not claim revocation retracts data already transmitted or makes database checks atomic with network sends. Preserve existing holds and permit only exact known-work terminal observation/settlement after revocation; never replay a query to recover forbidden work.

**Gate E:** all denial cases pass; source/gateway/cache revocation behavior is measured and acceptable; no shared-identity fallback occurs.

## 8 Close with evidence and a decision

The operator submits a bounded receipt containing: authorization reference; environment and synthetic fixture/catalog digests; redacted identity/audience evidence; captured tool/schema digests; reviewed statement/package pins; complete matrix outcomes; permission-change timestamps/intervals; fallback/provider-call checks; log/fixture secret-and-PII scan results; unresolved gaps; and cleanup evidence. Keep actual schemas in restricted evidence if their metadata is sensitive. Synthetic results must be labelled synthetic and cannot become live source or PII approval evidence.

Data owner and security reviewer independently record **qualified candidate**, **failed**, or **blocked**, with reasons and expiry/requalification triggers. Tool/schema, issuer/audience, permission semantics, statement or identity-model changes invalidate relevant evidence. Operators revoke temporary access and restore/remove fixtures under the separately approved cleanup plan.

Qualification is an input to a later reviewed delegated-source design and implementation. Organization activation, real-data access and deployment remain separately authorized. Until all gates and that later release review pass, delegated Domo serving stays unavailable; the current reviewed shared-source mode continues rejecting unsupported delegation.
