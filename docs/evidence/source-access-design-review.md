# Independent review: bounded shared-source approval gate

Date: 15 October 2026. Review type: design and local code-path inspection, not implementation qualification.

## Decision

PASS for the bounded architecture to advance to a separate, pre-code implementation plan. The five design findings below are addressed in the reviewed amended proposal. Code-ready approval remains blocked on the concrete ownership/trigger seams and SQL/Python lock graph. This review does not approve organization activation, deployment, credentials, IAM changes, source queries, a merge, delegated Domo access, or claims of relationship-based authorization.

Reviewed proposal: [source-access security proposal](../superpowers/specs/2026-10-15-source-access-security.md).

Verified proposal SHA-256: `7b7f9d17cb23201e09c42cb162a7559fb437a0a7d8bdc00b29f87820a7f36edb`.

Inspected candidate: local source `061eae2a8f0c3858005467cd883b8e7676c29812`, exact tree `9a10b21464743c978bf2e7489648074158eeb580`, published as private draft PR 5 source `c000037599fb9f0625b88e4c46fe05f4a2571798`. This review changed no runtime implementation or configuration.

## Findings and closure requirements

### 1. Approval audiences must be complete tuples, not independently unioned dimensions

The initial design bounded principal, client, tenant, groups, fields and actions but did not define how multiple audience entries combine. Separately unioning approved groups and fields could turn `(group A, field X)` plus `(group B, field Y)` into unauthorized `(group A, field Y)` access.

Required semantics: one unambiguous complete entry covers the whole derived actor/client/context/action/tenant/row/field tuple. Ambiguous matching entries deny. Any permitted intersection must preserve the entry's row-to-field relationship, remain within current policy, and feed the exact retained decision used for compilation. A Domo statement variant that cannot represent the resulting decision must fail closed rather than run a broader variant.

The amended proposal now specifies complete, unambiguous entries and forbids component-wise union. Implementation must preserve these semantics across author, fixture, publish, execution and result gates.

### 2. Expiry needs a fresh database-clock predicate, not only snapshot equality

Current provider authorization compares `source_current_authority(...)` against retained snapshots in `migrations/versions/0005_source_lifecycle.py:273-293`. Adding an immutable expiry timestamp to that JSON would not make the snapshot change when time passes.

Required semantics: every protected source admission/data function and release transaction checks `expires_at` against fresh database time. New attempt authority is capped by expiry. Replays cannot refresh the retained expiry or deadline. NULL/missing expiry, unavailable state and malformed function inputs deny. Expiry cannot block exact terminal metadata observation or release ambiguous source holds.

The amended proposal includes this explicitly. SQL functions must enforce it directly, including calls made by provider roles without Python policy helpers.

### 3. Pin accepted approval and client identity through idempotency and source reconstruction

`ExecutionQueue.admit` currently finds idempotency records by principal/product/version/key, loads the earlier execution and authorizes its stored principal before returning its view (`gateway/execution/queue.py:182-199`). A new client-scoped approval gate must compare the current requester to the retained accepted client before this reuse. In contrast, `ResultService.get` already compares the complete admitted principal to the current requester (`gateway/execution/results.py:249-261`); retain that protection.

Required semantics: immutable accepted approval ID/revision/digest plus authenticated actor/client pins are stored atomically with consumer authority and fixture invocation/attempt authority. Source SQL reconstructs expected pins from those protected owner records, not supplied IDs, request headers or a newly matching approval. A different client cannot receive the old handle/status or create an unrequested second execution through idempotent reuse. A new approval cannot revive work accepted under a revoked approval. A later publisher receives a separate current audience check.

The amended proposal includes these invariants. The migration plan still needs exact ownership, foreign keys and insertion/immutability trigger checks before implementation.

### 4. Non-PII source classification alone does not cover input and metadata persistence

`FieldSchema` permits unrestricted strings, and `validate_query` accepts them as inputs and filter operands (`gateway/contracts/manifest.py:114-140`; `gateway/contracts/query.py:45-79`). Accepted canonical queries retain these values. A local pure validation check confirmed that an unconstrained string accepts a synthetic email-shaped value. No provider, credentials or network was used for that check.

Required semantics: approve and enforce reviewed bounded input/filter domains before query persistence, audit processing or dispatch. Unrestricted free text is unsupported in this slice. Exact reviewed manifests include descriptions, examples, defaults, enum values, fixture inputs and expected/evidence content. Server-side save/update cannot persist arbitrary unreviewed content through a nominally approved product. Synthetic negative fixtures must not become permission to store real personal data.

The amended proposal adds these restrictions and keeps offline artifact construction separate from server approval. Classification remains owner attestation supported by controlled source publication, not a proof derived from metadata, regex, hashing or SQL/model digests. Necessary opaque security identifiers require separate governed handling; they are not automatically anonymous.

### 5. Unsupported delegation must have an explicit rejection contract

`Authenticator._oidc` verifies its current token profile but does not currently reject additional signed actor/delegation claims (`gateway/auth/tokens.py:189-206`). A token carrying such assertions must not silently become an ordinary shared-source user token.

Required semantics: enumerate unsupported signed actor/delegation assertions and designated request/config assertion fields for the bounded mode; reject them consistently at REST, MCP and direct configuration boundaries. Keep existing ignored spoofable identity headers distinct from supported authority. Do not claim this detects agents possessing ordinary valid user tokens, or that authenticated `client_id` establishes user-to-agent delegation or Domo source entitlement.

The final amended proposal explicitly rejects signed `act`/`may_act`, designated `On-Behalf-Of`/`X-On-Behalf-Of` headers including repeats and case variants, and closed-schema delegation fields in the reviewed-source mode. It preserves the legacy spoof-header contract and the unseen-agent limitation. This closes the design finding; implementation tests remain mandatory.

## Mandatory integration map

A hook only in `PolicyService.authorize` is insufficient. At minimum, the implementation plan must cover:

- `PublishingService._review` derives decisions directly (`gateway/publishing/service.py:163-199`), including author-or-publisher fallback.
- `FixtureRunner._decision_for_case` derives and audits fixed-data-principal decisions directly (`gateway/publishing/test_runner.py:677-697`). Both the invoking author/client and fixed fixture principal need exact approvals; a permission-negative fixture must not mistake a missing approval for a successful policy-negative test.
- `SourceLifecycle._decision` derives from protected snapshots (`gateway/source_lifecycle.py:368-393`), while provider SQL makes its own current-authority checks.
- `ChoiceService._context/_authorize` use policy-scope authorization rather than a product decision (`gateway/api/choices.py:21-37`). Binding/fixture choices require an exact approved product witness, including the final pagination recheck; an author scope alone cannot reveal an unapproved connector inventory.
- Execution submission, idempotent reuse, wait/status, worker claims, prepared-query reuse, provider pre-send/data RPCs, normalization/completion, result-cache fetches and every cursor page must retain and recheck the same accepted approval.
- Final publication, fixture evidence retrieval and catalog/draft projections are release boundaries even when no live source query occurs.

## Bounded acceptance tests

Use genuine disposable PostgreSQL roles/state, fake provider data boundaries and existing actual-package/compiler/SDK regression coverage. Synthetic tests are not live source or entitlement qualification.

1. Change each pinned artifact independently, including hidden scope/checkpoint fields, source-input lineage, physical identity, statement/model definitions, schema/normalization profile and fixture catalog. Recomputed coherent digests still cannot borrow an old approval.
2. Exercise missing, expired, revoked, malformed and unknown/PII classification approvals at startup for every configured package, before provider credential/token/query I/O. Readiness may read the registry only.
3. Exercise crossed audience tuples, ambiguous matches, wrong principal/client/tenant/group/action/context and policy tightening. Test both ordinary requests and direct service calls.
4. Reuse an idempotency key from another authenticated client for the same user. Deny before returning old handles/status; do not dispatch a second execution. Retry after approval replacement and prove the old accepted approval remains terminally revoked.
5. Inject synthetic disallowed free-text input/filter values and unapproved metadata/fixture values. Assert no accepted query, prepared state, result/cache, evidence, audit value, exception or log contains them.
6. Advance database time across approval expiry without changing the approval JSON. Deny queued admission, provider data RPCs and result/cursor release. Show local-clock rollback and restart cannot extend authority.
7. Revoke after acceptance, before claim, before source admission, between admission and RPC, during Domo response, after normalization, before result commit and between pages. Verify no post-revocation-authorized release and no unsafe replay. Exact original terminal evidence may settle capacity; uncertain work remains held.
8. Use separate app, publisher, provider runtime, observer and retention roles to attempt approval install/update/reactivation/deletion, function misuse and source authority substitution. Include NULL/malformed direct SQL calls and inherited privileges.
9. Exercise author and fixture data-principal independently, then a different publisher. Missing approval is not a successful permission-negative fixture, and neither publisher nor source-role access supplies approval authority.
10. Exercise explicit unsupported delegation claims/config/request assertions at REST and all four existing MCP tools. No fallback to shared credentials may turn a rejected delegated request into a successful query.
11. Test release-versus-revocation, multi-product publication and concurrent source admission in PostgreSQL under the documented SQL/Python lock graph. Inject audit/commit failures; no data or successful approval receipt is released.
12. Migrate with existing unpinned accepted executions and held source work. Data serving fails closed, but exact metadata-only reconciliation and settlement remain possible without deleting obligations or granting synthetic approvals.

## Remaining trust and release boundaries

- Owner authority and non-personal source content require organizational review and controlled upstream publication. Immutable application pins do not make a mutable upstream string value non-personal.
- Retain only approved non-PII product rows in source/result caches. Cache identity is not release authority; every delivery needs a fresh gate.
- Necessary security identity metadata is distinct from source/customer PII and must remain minimal, access-controlled and absent from provider-facing approval projections.
- A committed revocation serializes later authorization checks. It cannot retract already transmitted bytes or make a database commit atomic with a network send. Tests and claims must use that precise boundary.
- Product ACLs are not ReBAC; a shared Domo OAuth identity does not inherit employee PDP. This slice does not establish the user's requested agent relationship or per-user source entitlement. That phase remains gated on a separate reviewed design and verified provider contracts.
- Before implementation, finalize the exact approval ownership/trigger seams and lock-order graph, then run the acceptance matrix on a separate security branch. No existing connector receipt can qualify the new gate.
- The proposed approval-lock suffix is not already proven compatible with current helpers. In `ExecutionQueue.complete` (`gateway/execution/queue.py:501-514`), `_authorize` precedes `_fence` and `_close_source`; the latter acquires source pool/group/session locks. Simply acquiring approval locks inside `PolicyService.authorize` would violate that proposed order. The pre-code plan must reorder coherently or distinguish preliminary evaluation from the final locked, current release check. Every multi-product path needs the same analysis.
