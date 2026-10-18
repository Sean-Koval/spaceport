# Proposed source-access security gate

Date: 15 October 2026. Status: design proposal for a separate security branch and review. No implementation or activation approval is granted by this document.

## 1. Outcome and boundary

Before organization activation, the reviewed-source runtime must require current, protected data-owner approval for each exact shared-identity, non-PII product and audience. Product authors, publishers, callers and provider runtimes cannot grant that approval. Existing authentication and product authorization remain necessary; approval adds another restriction and never expands those rights.

The connector wave can finish as a non-rollout draft with its own source, genuine database, actual-package and hosted-image evidence. That evidence does not satisfy this gate. Live credentials, IAM, source queries and deployment still need separate authorization and qualification.

The first security slice does not implement generic ReBAC, token exchange, agent delegation, per-user Domo REST access or PDP equivalence. It explicitly rejects unsupported delegated-source modes. The later delegated-user phase is described in section 9.

## 2. Existing controls and gaps

- The RFC9068 token profile checks signature, issuer, audience, subject, expiry, issue time, token ID and an allowlisted client ID. The returned principal retains the authenticated client ID. A client ID identifies a client; it does not establish a user-to-agent relationship or source-user entitlement.
- Current policy decisions restrict principal, policy revision, tenant, row constraints, visible fields and limits. Accepted source plans and current source-authority snapshots are protected. Result and cursor access is reauthorized.
- Source accounting retains ambiguous work across crashes, claims, deadlines and source changes. Metadata-only terminal observation is separate from data release.
- Product-level classification is metadata, not a per-field PII policy. Parameter audit redaction does not classify result fields. Domo's shared OAuth identity does not inherit an employee's Domo rights.

The bounded gate can enforce exact allowlists and revocation. It cannot prove that an operator's classification is true or that an upstream producer never puts personal data into an approved string field. Reviewed lineage, controlled source publication, data-owner authority and source permission qualification remain explicit trust prerequisites. An account identifier, hash or aggregate is not automatically non-personal or safe against small-group inference.

## 3. Protected approval content

Introduce a versioned internal approval model and registry record. Do not add public query arguments, new MCP tools, SQL upload or approval flags to the product manifest. Reject unknown keys, duplicate JSON names, non-exact integers and unbounded collections. Proposed limits: 64 KiB canonical approval content, at most 100 audience entries and 200 lineage/field entries, with tighter existing product limits still controlling output.

Each immutable approval revision contains:

1. A generated approval ID, revision, domain-separated canonical digest, fixed mode `shared_non_pii`, creation time and absolute expiry.
2. Exact product ID/version, canonical manifest digest and policy ID. Pin the consumer and fixture binding/source descriptor digests separately, model bundle or Domo statement-catalog/definition digests, fixture-catalog digest, and schema/normalization profile. Retained source identity includes the physical provider identity, not a display-name alias.
3. A complete typed inventory of exposed output fields, hidden scope/checkpoint fields, grouping keys and reviewed source-input lineage. Review runtime input/filter value domains as well: closed enums or narrow typed domains, never unrestricted free text in this slice. The exact manifest text, examples, defaults, enum values and fixture expected/evidence data are part of the review. Each entry declares its source/member identity, purpose and a closed classification: non-personal, personal, sensitive-personal or unknown. Only explicitly reviewed non-personal entries are permitted. A type or regex alone is not proof of that classification. Existing confidentiality labels remain separate.
4. Explicit bounded audience entries: principal ID and kind, authenticated client IDs where applicable, tenant, permitted group/row scope, visible-field subset and allowed actions. No wildcard principal, client, tenant or action. One unambiguous complete entry must cover the full requested tuple; never independently union groups, fields or capabilities across entries. Approval is intersected with current policy; an audience entry is not a grant. Fixture test actors and the fixed fixture data principal have distinct entries and context pins.
5. The data-owner review identifier, approving operator identity, evidence reference and evidence digest, reviewed purpose, and explicit attestations covering lineage, source publication control, shared-identity use and aggregate/inference risk. Evidence references are identifiers, not auto-fetched URLs. Never retain raw source samples, SQL, credentials or source/customer PII in the approval/audit record. Necessary opaque actor/client/subject identifiers remain governed security metadata with restricted access and retention; neither opacity nor hashing makes them anonymous. Keep this identity metadata out of provider-facing approval projections.

Evidence is an operator/data-owner attestation, not an automatically verified permission certificate. The trusted operator must verify the owner's authority and retain the underlying review through an approved organizational process. Publisher-controlled text cannot become an approval by naming an owner.

Avoid digest cycles: approval content points to existing immutable product/source assets. Do not set a source descriptor's existing `source_policy_digest` to an approval digest that itself includes that descriptor digest. Approval is a separate protected relationship. Builders may produce a review request but never create an active approval.

## 4. Registry, authority and revocation

Use a new migration, without changing historical migrations or legacy v1 assets. The approval content is insert-only. A separate protected state row records active/revoked state and a monotonic revision. Revocation is terminal for that approval ID; renewed approval creates a new reviewed revision/ID. Keep historical records while any execution, fixture, audit or held source intent references them.

Only the existing explicitly operator-owned provisioning authority may install or revoke approvals. It must use exact validated content in a durable transaction. App, publisher, runtime, observer and retention roles cannot insert, update, delete or reactivate approvals through direct SQL or privileged functions. No public route grants this authority. Ordinary migration/bootstrap must not approve packages automatically, revive revoked approvals or overwrite existing content. Synthetic test approvals are explicit fixture setup and cannot be exported as live qualification evidence.

Accepted consumer executions and fixture groups retain an immutable approval reference and digest atomically with their existing authority records. A new active approval must not silently resurrect an execution accepted under a revoked approval. Existing accepted work without an approval pin is denied data execution/release in the reviewed-source mode after rollout; its held source work remains reconcilable. This migration behavior must be tested, not handled by deleting holds.

Idempotent submission must authorize the current requesting principal/client against the retained accepted principal/client and approval before returning an old execution handle or status. Principal-scoped idempotency alone is insufficient. A mismatch denies without silently creating a second source execution. Fixture group creation and attempt reconstruction retain the original actor/client and approval pins. A later publisher is a separate action with its own approved audience, not an inherited author capability.

Extend the protected coherent source-authority snapshot with the exact approval content/state and actor/client audience projection. Derive authorization from that same snapshot. Current approval checks must independently derive the expected product/source pins from protected owner records; a caller-supplied approval ID or digest cannot select authority. Provider roles receive only the already reviewed boolean/digest projection, never the full audience or evidence record.

Approval revocation uses the existing source-capacity/product/draft/policy lock ordering with an explicitly documented approval lock position. Multi-product operations lock approval rows in a stable order. Review both the SQL and Python transaction paths for deadlocks and SQL three-valued logic. Direct NULL/malformed provider-function calls must deny.

Before implementation, the migration/service plan must enumerate the concrete lock graph for source creation, data checks, result release, publication and revocation. The proposed position is after existing mutable authorization/lifecycle locks, taking approval-state locks in sorted order before final mutation/release. No path may hold an approval-state lock and then acquire an earlier source-capacity, product, draft or policy lock. Any incompatible existing path must be reconciled in the reviewed plan rather than relying on this design paragraph as a lock-order proof.

In particular, trace `ExecutionQueue.complete`: its authorization precedes `_fence` and `_close_source`, which later acquire source pool/group/session locks. A blanket approval-state lock inside `PolicyService.authorize` would invert the proposed order. Use a preliminary evaluation that grants no release authority followed by a final locked recheck, or coherently reorder the transaction. The implementation plan must prove the selected approach across both Python and SQL paths before coding.

Revocation/expiry stops new data admission, Domo send authority and subsequent data/result RPCs. It does not release ambiguous holds or disable exact known-job metadata observation, late original Domo terminal proof, sealing or settlement. An observed terminal source response may settle capacity while rows remain forbidden. No source query is replayed to recover revoked work.

Check approval expiry against a fresh database clock in source admission/data functions and final release transactions. Comparing immutable snapshot JSON containing `expires_at` does not detect time passing. New attempt deadlines cannot exceed approval expiry; retaining the original deadline and expiry must never refresh either on replay. Direct NULL/missing expiry denies, and local-clock rollback cannot extend authority.

## 5. Enforcement points

Implement one shared internal approval evaluator and transaction-aware snapshot primitive, not separate ad hoc checks in each renderer or adapter. Preserve the public REST/MCP contracts.

- **Startup:** the reviewed-source application validates every configured package's static pins and required approval references before reading provider credentials or opening provider connections. It then checks active registry approvals before becoming ready to serve those products. Missing approval/schema/state fails closed. Readiness performs registry checks only, never source queries.
- **Offline tooling:** init/build/validate/render can create unapproved review artifacts without credentials. Their outputs must distinguish configured intent from approval and observed evidence. Activation/reference commands refuse an unapproved or expired package. Offline success cannot be promoted to an approval receipt.
- **Authoring and publication:** offline draft construction can remain available without authority. Server-side save/update accepts only exact approved manifest/package content under existing author permissions, preventing arbitrary unreviewed text/examples/defaults from being persisted as approved product metadata. Approval pins canonical content rather than a generated draft ID, so it can precede server save. Every product projection, preview, fixture execution and publication applies the same gate. `PublishingService._review`, `_scopes`, fixture preparation and final publish checks require review because some paths derive decisions directly instead of calling `PolicyService.authorize`.
- **Discovery and choices:** `CatalogService`, draft projections, binding/fixture choices and pagination recheck current approval and audience. Unauthorized products/fields are not disclosed through names, examples, schemas or differences. Existing generic denial behavior remains.
- **Admission and execution:** `ExecutionService`, the worker, `FixtureRunner` and `SourceLifecycle` retain and recheck exact approval pins. Validate actual input/filter values against reviewed domains before canonical-query persistence, audit processing or source dispatch. For fixtures, authorize both the author and fixed data principal; never substitute the consumer source. Every BQ admission/data RPC and Domo pre-send gate checks the protected current snapshot and current DB-clock expiry.
- **Results and caches:** `ResultService`, every cursor page and any cache hit require current approval for the retained accepted approval ID and exact product/source pins. Hold the relevant mutable approval state through the release audit transaction, following existing lifecycle locking. Include approval identity/state version in cache/cursor authority identities or prove an equivalent fresh gate occurs before every byte release. No stale positive authorization cache. Source caches may retain private rows, but cannot confer release authority.
- **Audit and errors:** allow/deny records identify approval revision/digest and decision reason without source values, raw SQL or evidence content. Client errors do not reveal whether an approval, source or another principal exists. Reject unsupported delegation configuration and request assertions rather than ignoring them.

Revalidate privately retained models at trust boundaries; frozen dataclasses are not proof against in-process mutation. Review copied snapshots and callbacks just as in the connector implementation.

Additional direct decision paths requiring explicit tests are `FixtureRunner._decision_for_case`, which calls `derive_decision`, and `ChoiceService._context`/`_authorize`, which use policy-scoped authorization without an exact product approval. Binding choices require an exact approved product/binding witness, not merely an author role.

For the reviewed shared-source mode, reject access tokens containing signed `act` or `may_act` claims instead of silently treating them as ordinary user tokens. Reject designated `On-Behalf-Of`/`X-On-Behalf-Of` request headers case-insensitively, including repeated forms, and reject `on_behalf_of`, actor-chain or delegated-source configuration/request fields under the closed schema. These fields create no authority. REST and MCP must behave identically. Existing unrelated spoofed identity headers remain untrusted and ignored according to their existing contract; do not broadly reinterpret them as delegation. Keep legacy authentication behavior outside the reviewed-source mode unchanged unless separately reviewed. This rejection contract does not detect an unseen agent possessing an ordinary valid user bearer token.

## 6. Suggested code ownership and sequencing

1. Shared contracts/evaluator and new registry migration; exact ACL, immutability and revocation tests first. Candidate modules: a dedicated `gateway/source_access.py` and registry repository/model additions.
2. Shared policy/publication/discovery/result hooks: `gateway/policy/service.py`, `gateway/publishing/service.py`, fixture runner, catalog/choices and `gateway/execution/results.py`. Preserve generic errors and wire snapshots.
3. Accepted-owner approval pins and source snapshots: `gateway/source_lifecycle.py`, queue/preparation transactions and the new SQL migration. Node/Domo consume the existing protected check interfaces where possible; no new unsigned control path.
4. Operator-only approval import/revoke workflow, semantic package validation and startup wiring. No automatic grant or activation. A production operator action is separately authorized; tests use disposable synthetic authority.
5. End-to-end and actual-package regression, independent review, then a separate draft PR. Do not mix this implementation into the connector evidence tree or reuse its receipt.

## 7. Required acceptance matrix

Use genuine disposable PostgreSQL for protected state/roles and fake provider boundaries for source data. Preserve actual Cube/compiler/SDK coverage. No live permissions or PDP claim follows from these tests.

- Missing, expired, revoked, malformed, duplicate, unknown-classification and PII approvals reject before credential/token/query I/O; every configured package is checked, not only the readiness sample.
- Personal/free-text input and filter values, unapproved manifest text/examples/defaults/enums and fixture evidence reject before persistence or dispatch. Test input validation before deny-audit processing so a rejected value cannot enter a raw audit field. Operational identity/audit identifiers remain governed sensitive metadata; this gate does not claim the entire application stores no personal information.
- Repin every editable package/source/binding/model/schema/statement field and prove a new valid digest cannot borrow an old approval. Include coherent model replacement and hidden/source-input field changes.
- App, publisher, provider runtime, observer and retention roles cannot install, alter, reactivate or delete approvals. Policy-admin capability alone is insufficient. Direct NULL/malformed JSON function calls deny.
- Same user with an unapproved client, another principal, tenant, group or source context cannot discover, preview, publish, query or receive cached/cursor results. Tightening current policy never becomes a broader approval.
- Audience entries `(group A, field X)` and `(group B, field Y)` cannot authorize `(group A, field Y)` through component-wise union. Ambiguous complete matches deny. Same-principal/different-client idempotent retries cannot obtain old handles or initiate a replacement execution.
- Revoke after acceptance, before claim, while queued in Cube, between source admission and RPC, during Domo response, after normalization, before result commit and between cursor pages. No forbidden rows are released. Exact terminal evidence still settles, and unknown work remains held.
- A replacement approval does not revive old accepted executions. Restart/reclaim does not switch approval ID, source identity or Domo send authority. Alias/revision/credential rotation does not bypass approval or capacity holds.
- Fixture author and fixture data-principal scopes are both checked; consumer and fixture identities remain isolated. Publisher tests cannot self-approve or relabel synthetic evidence as live.
- Unknown/expired approval DB state or unavailable policy store fails closed. Audit failure never permits release. No raw rows, SQL, credentials, private evidence or PII appear in exceptions/repr/logs.
- Advance real DB time through approval expiry without modifying approval JSON and prove queued admission, source data RPC and result/cursor release deny while exact terminal metadata observation still settles. Local clock rollback cannot revive authority.
- Actual REST and all four existing MCP tools exercise the same gate; standalone internal service callers cannot bypass it. Public schema snapshots and legacy regression matrices stay unchanged unless a separately reviewed compatibility change is essential.
- In the reviewed-source mode, signed `act`/`may_act`, designated on-behalf-of headers and unsupported delegated configuration deny through REST and MCP. A plain valid token still undergoes its exact current principal/client audience check; no test or documentation may claim it proves absence of an agent.

Document the unavoidable DB-check-to-network/HTTP-release race honestly. A completed revocation serializes subsequent authorization checks; it cannot retract already transmitted data or make network send and DB state atomic.

## 8. Release criteria

Independent design approval precedes implementation. Require exact-source unit/contract, genuine database, actual-package and hosted-image gates with verified cleanup, unchanged caps and no skips. Update runbooks to distinguish operator attestation, synthetic qualification and live source verification. Organization activation stays blocked until this slice passes and separately authorized source/IAM/network qualification is complete.

## 9. Later user-and-agent/source-entitlement phase

Define a separate authenticated user subject and agent actor chain, with trusted issuer, audience, expiry, revocation and bounded action/product/tenant scope. Evaluate the intersection of current user relationships, current agent delegation, product/PII policy and independently verified source-user entitlements. Relationship additions/removals and source entitlement changes must affect discovery, execution, cached results and cursor release.

Do not accept an `on_behalf_of` header, self-asserted subject, service account, allowlisted client ID or opaque provider token as proof of that intersection. The current bearer profile also cannot reliably identify every agent holding a valid user token; unsupported-mode rejection must not be described as such a detector.

Domo MCP user OAuth and the approved REST query endpoint require separate endpoint/token/permission contracts. Do not assume tokens or PDP semantics transfer between them. Before choosing a mechanism, verify the actual vendor-supported audience, token exchange/delegation, dataset/row/column permissions, revocation and cache behavior from official contracts and separately authorized qualification. If that proof is unavailable, delegated Domo serving remains unavailable rather than falling back to the shared service identity.

Later acceptance must include user/agent/tenant substitution, nested or overbroad actor chains, replay, stale/revoked delegation, relationship changes, source permission loss, field-policy changes, cache/cursor separation and confused-deputy attempts. This phase is a new reviewed design, not an extension inferred from the non-PII shared-source approval.
