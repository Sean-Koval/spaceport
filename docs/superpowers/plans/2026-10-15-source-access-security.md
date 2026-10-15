# Spaceport Source-Access Approval Transaction Implementation Plan

This is a pre-code plan. Implementation begins only after independent approval of its ownership and transaction decisions.

**Goal:** Add an independently operator-approved, exact-package, shared-non-PII gate to reviewed BigQuery/Domo products without lending authority across clients, fixtures, accepted work or replacement approvals.

**Architecture:** Keep the existing deployment-capacity row as the conservative source-operation mutex. Retain immutable approval references in accepted owner records, derive source authority from those records, and run fresh database-clock approval checks as the last authorization phase of every data/release transaction. Metadata-only proof and settlement stay independent of active approval.

**Tech stack:** Python, Pydantic, SQLAlchemy async UoWs, Alembic, PostgreSQL PL/pgSQL; existing Node/Cube and Domo provider boundaries.

**Spec:** [`2026-10-15-source-access-security.md`](../specs/2026-10-15-source-access-security.md), SHA-256 `7b7f9d17cb23201e09c42cb162a7559fb437a0a7d8bdc00b29f87820a7f36edb`.

**Independent design review:** [`source-access-design-review.md`](../../evidence/source-access-design-review.md).

**Inspected baseline:** Spaceport integration commit `061eae2a8f0c3858005467cd883b8e7676c29812`; private connector draft PR 5 is a separate evidence/publication line. Security implementation branch is `feat/spaceport-source-approval`; commit `d766d92` adds a test-only CI-umask fix and reviewed documents, with no runtime difference from the inspected baseline. No runtime implementation is part of this plan.

## Global constraints

- Only `shared_non_pii`; this does not implement ReBAC, user-to-agent delegation or Domo per-user/PDP entitlement.
- Runtime source bindings `cube-bigquery-v1` and `domo-named-v2` require this approval gate. `cube-rest-v2` is reviewed generalized Postgres, not an approval-exempt legacy source: organization/production serving and activation fail closed in this slice pending separately qualified approval/source-lifecycle support. Preserve checked-in v1 and v2 synthetic regression contracts/assets through an explicit trusted synthetic composition; adapter name, missing dependency, caller JSON or a public mode flag cannot grant that exemption. Unknown profiles fail closed.
- Preserve public REST/MCP contracts and all four existing MCP tools. No caller approval IDs, extra public query fields, SQL upload or automatic approval.
- Canonical approval content is at most 65,536 UTF-8 bytes; at most 100 complete audience entries and 200 lineage/field entries. Only explicit `non-personal` classification passes. Exact integer/closed-key/duplicate-name validation is mandatory.
- One complete audience entry must cover the entire effective Decision. Do not union entries or modify an already-derived Decision to fit an approval. Current policy derives the Decision first; approval only allows or denies it. This is the smallest compatible intersection and preserves Domo's exact full-Decision variants.
- Unrestricted free text is unsupported in reviewed query inputs/filter values. Validate reviewed domains before canonical persistence, audit parameter processing, preparation or dispatch. Manifest text/defaults/enums/examples and fixture content are exact reviewed artifacts.
- Approval records contain no credentials, SQL, raw samples or source/customer PII. Identity/client identifiers are governed security metadata, not anonymized data. Provider projections contain no audience/owner evidence records.
- Migration creates no approval, provider role, credential or source grant. Existing held obligations are never deleted or released by migration, revocation, expiry or replacement approval.
- Physical source identity and immutable secret-version references can be pinned. Mutable secret aliases cannot prove the credential behind them is unchanged. Reviewed activation must require immutable version references where supported or an explicit operator-controlled rotation prerequisite; local tests cannot establish provider permission drift or source-user entitlement.
- No merge, deployment, live credentials, IAM/cloud changes or live source queries are authorized by this plan.

## Review focus

1. Same user, different authenticated client: idempotency must deny before returning an old handle and must not create a second execution. Test in Task 2 and Task 6.
2. Permission-negative fixtures: missing approval must produce failed configuration evidence, never a successful policy-negative check. Test in Tasks 2 and 4.
3. Expiry while a transaction is blocked: approval JSON does not change, but final SQL must use `clock_timestamp()` after the wait. Test in Tasks 1, 3 and 6.
4. Approval-denied completion with exact terminal proof: data is discarded while source settlement remains independently recoverable; no source query replay. Test in Tasks 3 and 6.
5. Multi-target publication/pagination: collect all earlier locks before any approval lock; no looping authorize/lock-more sequence. Test in Tasks 4 and 6.

## 1. Verified current call graph and why a policy-only hook is insufficient

The following are baseline locations, not claims that these paths already enforce source approval.

| Existing seam | Current behavior | Required change |
| --- | --- | --- |
| `gateway/execution/service.py::_submit` | Capacity, policy authorize/audit using raw request inputs, then `validate_query`, then queue admission | Move reviewed-domain validation before audit handling; split preliminary policy derivation from final gate; inspect idempotency before selecting a new approval |
| `gateway/execution/queue.py:182-222::admit` | Principal/product/version/key lookup; `_authorize` uses stored principal; inserts execution then authorization | Compare current complete Principal/client first; retain one immutable approval selected from protected product/decision; atomic owner-pin constraint |
| `queue.py:265-362::claim` | Capacity, source `_lock_authority`, `_authorize`, reserve group, then state/lease | Lock existing state/lease before source reservation; final approval after all lifecycle work, before claim commit |
| `queue.py:432-460::gate/save_prepared` | Authorize before `_fence` | Fence before final approval; preparation changes complete before final gate |
| `queue.py:501-528::complete` | `_authorize` before `_fence`, `_close_source`; `_release` closes source again | Preliminary decision only; fence, validate/close source, normalize into private memory; final approval/audit after all lifecycle locking; insert result/state; no later source close call |
| `gateway/execution/results.py:224-318::get` | Product/policy, version state, admitted Principal equality, execution state, result/cursor and final audit | Reviewed paths take capacity first; keep admitted identity equality; prelock lifecycle/cursor dependencies; final retained approval gate for every status/cache/page release |
| `gateway/source_lifecycle.py:351-358::_lock_authority` | Capacity → product → optional draft → policy | Preserve; do not hide approval-state locks here |
| `source_lifecycle.py:360-393::_snapshot/_decision` | Snapshot by caller-built authority spec; direct `derive_decision` | Owner-derived reviewed snapshot and pure approval predicate; source `spec` is an equality assertion, never an authority selector |
| `source_lifecycle.py:539-558::begin_fixture_run` | Uses run UUID as group owner before a persisted `TestRun` exists | Insert protected fixture invocation and case identities before group creation; invocation is the authoritative owner |
| `gateway/publishing/service.py:156-199::_scopes/_review` | Multiple policy gates and direct publish-or-author fallback | Batch preliminary derivation and lock acquisition; choose action once, then exact approval for that action; no approval-denial fallback |
| `gateway/publishing/test_runner.py:677-697::_decision_for_case` | Direct fixed-principal derivation/audit; missing policy Decision can count as permission-negative | Independently approve fixed case review before testing policy; final data gate only for non-null Decision; failed approval is not expected policy denial |
| `FixtureRunner._run_cases/evidence_stale_causes` | Final evidence write and later publication inspect fixed cases | Retain original invocation/attempt approval pins; current publisher gets independent audience check; stale original approval invalidates evidence |
| `gateway/api/catalog.py`, `drafts.py`, `choices.py` | Separate scans then final projection; choices use policy scope alone | Exact product/package witness for binding/fixture choices; final batch recheck including approvals before response/cursor issuance |

Historical migration contracts:

- `0003_execution_authority`: acceptance trigger supplies the DB acceptance/deadline instants and locks policy; `execution_authorizations` and preparation rows are immutable. Existing `_load` requires `deadline_at = created_at + execution_timeout_seconds`.
- `0004_publishing_fixtures`: immutable `fixture_attempts` contains fixed-principal snapshot but only author ID, not complete author/client identity. Attempt trigger locks current policy and overwrites timestamps. Existing fixture timeout equality must remain valid.
- `0005_source_lifecycle`: `source_lock_session` takes deployment capacity → pool → group → session. Group/session insertion checks retained owner, binding and audit. `source_check_context` checks owner/epoch/snapshot equality. `source_current_authority` is currently STABLE and contains no expiry predicate. Observation/seal/settlement intentionally work separately from data authorization.
- `0006_source_preparations`: insertion trigger directly locks session before checking Domo preparation. Replace this entry with capacity-first locking in 0007; do not append an approval lock before its existing session lock.
- `source_bq_observe` and `source_domo_observe` lock intents and call settlement; `source_expire_groups`/reconciliation iterate bounded sets. These already hold the same exclusive deployment-capacity row. That existing mutex prevents compliant source transactions from holding a later source lock while waiting on one another's capacity row; no generalized lock manager is needed.

## 2. Additive migration and exact ownership seam

Create only `migrations/versions/0007_source_access_approvals.py`, `down_revision='0006_source_preparations'`. Do not edit 0001–0006. Use `CREATE OR REPLACE` in 0007 for affected functions, preserving their signatures and narrow provider grants unless explicitly listed below. New objects are owned by the same separately provisioned non-elevated registry `owner` used by existing source provisioning. No fifth mandatory legacy role is introduced.

### 2.1 Immutable approval and terminal state

Create these owner-owned tables:

1. `gateway.source_access_approvals`
   - `id uuid PRIMARY KEY`, `revision integer NOT NULL CHECK(revision>0)`, optional `supersedes_id uuid REFERENCES ... (id)`.
   - `content jsonb`, `canonical_content text`, `content_digest char(64)`, `created_at timestamptz DEFAULT clock_timestamp()`, `expires_at timestamptz`, all NOT NULL.
   - Typed indexed pins: `product_id`, `product_version`, `manifest_digest`, `policy_id`, `consumer_binding_id`, `consumer_binding_digest`, `fixture_binding_id`, `fixture_binding_digest`, `fixture_catalog_digest`, `package_pins_digest`.
   - Composite FKs to existing immutable binding rows `(id,content_digest)`; approval canonical content additionally pins both source profiles `(source_id,revision,context,descriptor_digest)`, physical identities, model/statement definition/catalog, schema/normalization and complete input/field/lineage inventories. Verify profile existence and equality in the install guard; do not change historical profile uniqueness constraints.
   - Unique `(id,revision,content_digest)` supports exact accepted-reference FKs. Approval has no FK to a generated draft/version row: reviewed canonical content can be approved before server save. Product/version strings and manifest digest are exact, not aliases.
   - `expires_at > created_at`; only `mode='shared_non_pii'`. Renewal creates a new ID; optional predecessor requires revision exactly predecessor revision + 1 and identical product identity. A predecessor cannot be updated.
2. `gateway.source_access_approval_states`
   - `approval_id uuid PRIMARY KEY REFERENCES source_access_approvals(id)`, `state text CHECK(state IN ('active','revoked'))`, `state_revision integer`, `changed_at timestamptz`, `revocation_reason_code text NULL`.
   - Insert exactly `active,1` in the same operator transaction as immutable content. The only update is active/1 → revoked/2. Revocation is idempotent only by comparing the existing exact result, never by rewriting it. No delete/reactivation.
3. `gateway.source_access_admin_events`
   - Append-only `id`, approval exact reference, action (`install`,`revoke`), `operator_role` derived from `session_user`, safe review/evidence identifiers and digest, server-generated request identifier, reason code, DB timestamp. No free-form user source content.
   - Required durable receipt is committed with install/revoke. Audit/commit failure yields no success receipt.

The canonical payload has closed top-level keys `schema_version`, `mode`, `revision`, `supersedes_id`, `expires_at`, `product`, `bindings`, `sources`, `artifacts`, `lineage`, `input_domains`, `filter_domains`, `audiences`, `review`. Generated database ID, installed `created_at`, mutable state and digest are outside that payload. SQL assigns UUID and fresh DB creation time only on first install; `UNIQUE(content_digest)` makes exact re-import return the original immutable record/state. The full immutable record and its composite FK bind payload to generated identity. A successor changes its reviewed revision/predecessor payload and obtains a new ID; it cannot revive the old record.

Installation accepts one exact validated canonical document, not a bag of independent pin overrides. `source_approval_install(canonical_content text) RETURNS jsonb` is SECURITY INVOKER, owner-only, fixed `search_path=pg_catalog,gateway`. Revalidate closed JSON, duplicate keys, bounds, exact integer types, expiry, digest and all source/package relationships in SQL as well as Python. Use a small recursive canonical validator for the approval schema's supported types; decimal domain bounds, if needed, are canonical decimal strings, not binary floats. Digest is SHA-256 of `b'spaceport-source-approval-v1\0' + canonical_utf8`, matching the Python contract. The digest field is outside the digested payload. Duplicate-key detection occurs on JSON text before JSONB loses duplicate information. Exact replay returns the original record/state; a replay of revoked content never reactivates it.

`source_approval_revoke(approval_id uuid, expected_digest text, reason_code text) RETURNS jsonb` is also owner-only SECURITY INVOKER. Acquire deployment capacity, read immutable approval to obtain product/policy keys, lock its existing product row if present, then its policy head/revision, then approval state. There is no draft-row lock because an approval is canonical-content scoped and does not select a generated draft. No owner/state/source locks are needed for revocation because it changes none of those objects. Install uses the same C → optional product → policy prefix before inserting its privately new state row. Validate exact ID/digest; update state and append required administrative receipt. Do not enumerate/update executions, source groups, source sessions, jobs, sends, results or reservations. Revocation therefore cannot itself settle or replay anything.

Both functions call the existing durability guard (`synchronous_commit=on`, fsync/full_page_writes required). An owner could disable guards or alter ACLs; the database owner remains an explicit administrative trust boundary, as in the existing audit implementation.

### 2.2 Consumer accepted reference

Create `gateway.execution_source_authorities`:

- `execution_id varchar(128) PRIMARY KEY REFERENCES execution_authorizations(execution_id)`.
- Exact approval `(approval_id,approval_revision,approval_digest)` composite FK to approvals.
- `audience_entry_id`, `principal_snapshot`, `principal_digest`, `package_pins_digest`, `accepted_approval_expires_at`, `effective_deadline_at`, `created_at`; all immutable and non-null.
- Snapshot must equal the existing `execution_authorizations.principal_snapshot` exactly, including `service_client_id` for OIDC users; it is already the authenticated client field, despite its historical name. Service identities retain their exact service client. No new client headers are trusted.
- Effective deadline is DB-derived `least(executions.deadline_at, accepted_approval_expires_at)`; do not change the historical integer timeout equality in `executions` or `_load`.

Add `source_access_generation smallint NOT NULL DEFAULT 0` to `executions`, with no app insert/update privilege. In replacement `set_execution_acceptance`, set it to 1 for new reviewed-source bindings, 0 for legacy, and acquire deployment capacity before the existing policy lock for reviewed inserts. Existing rows remain 0. This is an admission-generation marker, not a caller-selectable feature flag.

An AFTER INSERT trigger on `execution_authorizations` calls private `source_accept_execution_authority(execution_id)` for reviewed bindings. It derives product/version/binding/source/manifest from the immutable execution graph, derives actor/client and effective Decision from the just-inserted authorization, and finds exactly one active approval/complete audience entry matching those values. No supplied approval ID is accepted. Multiple active records that completely match this request deny rather than selecting the newest. Installing a successor neither revokes nor rebinds the predecessor automatically; operators explicitly revoke when intended, and retained work continues to check its original record only. Under the retained deployment-capacity mutex, operator install/revoke cannot change selection before commit. It inserts the companion reference and never updates it.

A DEFERRABLE INITIALLY DEFERRED constraint trigger on newly inserted reviewed executions checks that authorization and companion both exist, match, are currently active and unexpired at fresh DB time, and preserve the exact Decision/inputs. It locks only the approval-state suffix at commit; it does not acquire lifecycle locks. Missing companion aborts the whole acceptance transaction. Companion creation rejects generation 0 rows, preventing retroactive approval of migrated work. Owner content INSERT/UPDATE/DELETE guards reject attempts to override or mutate references. Existing immutable execution/auth triggers remain in force.

### 2.3 Fixture invocation and attempt references

Create `gateway.fixture_source_invocations` and `gateway.fixture_source_invocation_cases`.

Invocation:

- `id uuid PRIMARY KEY` equals the server-created run ID; `product_id`, `draft_id`, `draft_digest` with exact FK `(draft_id,product_id,draft_digest) REFERENCES draft_revisions(id,product_id,content_digest)`; consumer and fixture binding `(id,digest)` FKs; catalog and execution-contract digests.
- Complete `actor_snapshot` plus its digest, exact approval FK, author audience entry ID, package pins digest, DB creation/expiry/effective-deadline instants, positive source roster and full catalog digest.
- Insert-only. Actor/client and source identity cannot later be reconstructed from the current caller or from a new matching approval.

Case rows:

- Primary key `(invocation_id,fixture_id)` with FK to invocation; exact fixed principal snapshot/digest, case kind/version/digest, fixed-principal audience entry ID and catalog digest.
- The exact immutable reviewed catalog is supplied from the trusted offline package and digest-checked against the selected approval. Cases, roster, inputs and expected/evidence contracts are generated from that catalog, not arbitrary caller-provided principal IDs. Maximum eight source-positive cases and existing catalog bounds remain unchanged.
- Approval selection is one approval covering the complete invoking author and every fixed case's independently reviewed identity/context. Do not combine different approvals opportunistically. The author and data-principal audience entries are distinct.

Provide `source_accept_fixture_invocation(draft_id text, actor_snapshot jsonb, catalog_canonical text, run_id uuid) RETURNS jsonb`, SECURITY DEFINER, granted only to app. Its target principal is the trusted app's authenticated actor; the database does not independently verify an OIDC signature. The function checks exact current principal/registry identity and package pins, locks capacity → product/draft/policy, derives approved case rows and timestamps, writes invocation+cases, and ends with the final approval-state check. It accepts no approval ID, candidate Decision, source profile override or fixture principal override. Any failure rolls back all invocation data. Consumer source configuration cannot substitute for fixture configuration.

For permission-negative cases, first require an active, exact case/identity/context review entry and reviewed fixture input content. This structural case-review proof grants no data access. Then test current policy; only its genuine `NOT_FOUND_OR_FORBIDDEN` can satisfy the negative expectation. If policy permits a Decision, that entire Decision must pass the normal complete-tuple gate before any source call. Missing approval/configuration is a failed fixture, not a policy-negative success.

Add nullable `source_invocation_id uuid` to `fixture_attempts`, app INSERT permitted, FK to invocation; legacy attempts retain NULL. The replacement fixture insertion guard requires invocation for reviewed bindings and validates exact invocation actor, client, draft/bindings/catalog/case/fixed-principal pins. Create immutable `fixture_attempt_source_authorities(attempt_id PRIMARY KEY REFERENCES fixture_attempts, invocation_id, fixture_id, approval_id/revision/digest, author_entry_id, data_entry_id, package_pins_digest, accepted_approval_expires_at, effective_deadline_at)` with composite FK `(invocation_id,fixture_id)` to case rows and approval FK. Populate in AFTER INSERT from invocation/case, never from supplied approval fields. Enforce one reviewed positive attempt per `(invocation_id,fixture_id)`. Effective deadline is minimum of original fixture deadline, invocation deadline and approval expiry; preserve 0004's original timeout equality.

A deferred new-attempt completeness/current-approval trigger makes attempt+authority atomic. An existing attempt lacking a reference cannot receive one later. Add a companion `fixture_test_source_authorities(test_run_id PRIMARY KEY REFERENCES test_runs, invocation_id UNIQUE REFERENCES fixture_source_invocations)` populated/validated for reviewed `TestRun` inserts; missing original invocation, missing/wrong attempts, replaced approval or incomplete roster rejects successful evidence. A failed/aborted invocation need not have a TestRun; its source group still holds and reconciles independently.

### 2.4 Atomic publication operation receipt

Create immutable `gateway.source_publication_operations` for the narrow publication/transition mutation seam. It is not a new source execution owner. Columns: generated `id uuid PRIMARY KEY`, `transaction_id xid8 NOT NULL` from `pg_current_xact_id()`, `action` (`publish`,`deprecate`,`withdraw`,`rollback`), `product_id`, exact target `version_id`, `draft_id`, `test_run_id`, `previous_version_id` when applicable, expected product/draft revisions, complete `actor_snapshot`/digest, publisher approval exact FK/entry, package pins digest, original fixture invocation/approval exact FK, `allow_audit_id`, bounded immutable `allow_audit_projection`/digest, request ID and DB creation time. The receipt validates and copies the safe audit fields while that event exists in the same transaction; the ID is deliberately not a foreign key, so ordinary 30-day event retention does not permanently pin it. The ordinary authorization audit action is always the existing capability `publish`, including deprecate/withdraw/rollback; the exact transition remains bound by receipt.action and publication_events.action. Do not expand the public/internal capability union. The audit must match this actor/client, capability, package/target, approval, current policy and request, and must originate in this same database transaction. The receipt binds every target checked by a replacement/rollback operation in one canonical bounded `target_pins` array; no independently unioned approvals.

`source_record_publication_operation(actor_snapshot jsonb, action text, product_id text, version_id text, draft_id text, test_run_id text, previous_version_id text, expected_product_revision integer, expected_draft_revision integer, allow_audit_id text, request_id text) RETURNS uuid` is SECURITY DEFINER, app-only, fixed search path. Optional identifiers are SQL NULL only in the documented action-specific shape. This is a complete checked entrypoint: it acquires C → all relevant sorted P/D/Q/V rows → sorted A itself. The service may already hold the earlier prefix, but must not enter an A phase before calling it. The function reconstructs exact package/test/invocation from these identifiers, validates full authenticated actor/client against current registry identity and policy, derives the unique current publisher approval (no supplied approval ID), verifies the original fixture approval and exact provisional audit, then inserts the receipt after its final A check. Historical fixture evidence without the new protected invocation/attempt linkage cannot acquire a valid receipt. Trusted app authentication remains the source of the actor snapshot; this is not database verification of a bearer token.

Add private nullable `source_operation_id uuid` FKs to `product_versions`, `version_states`, `products` and `publication_events`, with tightly scoped app insert/update columns matching their existing mutation roles. Legacy synthetic rows keep NULL. For reviewed mutations:

- New version insert requires a publish receipt for exactly that generated version/draft/TestRun and actor; matching initial VersionState, product current-version head and publication event carry that same receipt.
- Deprecate/withdraw requires a new receipt for exactly that version and action; the new state and its event carry it. Rollback additionally binds the previous/current target and product head change.
- Replacement guards on version insert/state/head and event write require receipt transaction ID equal to the current transaction and exact current target/action/actor. They are suffix-only: they read already locked rows and do not enter lifecycle lock helpers. Direct reviewed INSERT/UPDATE without linkage, a different target/client/action, or a prior transaction's receipt denies. Ordinary draft-head updates do not become publication operations, but reviewed current-version pointer changes require this linkage.
- `publication_events.source_operation_id` is unique for non-null values. A deferred constraint on receipt insertion requires exactly the expected event plus version/state/head final mutation set; an unused receipt cannot commit and cannot later be replayed. No mutable consumed flag is necessary. All approval/expiry assertions at this point are suffix-only and use fresh DB time.
- Add private nullable `created_transaction_id xid8` to `audit_events` without backfilling old events. A BEFORE INSERT trigger always assigns `pg_current_xact_id()`; app has no INSERT/UPDATE privilege on this marker. Recorder requires exact equality to its current transaction ID, so an old matching committed audit cannot authorize a new mutation. Timestamp proximity is not used. Test old-audit replay and caller marker override denial.
- `AuditWriter.append` returns the inserted audit ID (existing callers may ignore it). The service appends its exact provisional same-transaction allow audit, then calls the recorder as the final publication gate (no earlier A gate), performs the now-prelocked mutation, and commits together. A failed recorder rolls back that provisional allow audit; no success is released. The reviewed `UnitOfWork.insert_version` branch consumes this receipt and must not execute its legacy product/draft/policy lock-acquisition prelude.

App has SELECT only on receipt rows and EXECUTE on the one checked recorder; no receipt INSERT/UPDATE/DELETE. Provider/observer/retention have no access. The original fixture pin and this publisher operation remain separately attributable.

### 2.5 ACL and trigger inventory

All new tables and all new/replaced functions undergo the existing ACL-scrub pattern: remove PUBLIC and every non-owner inherited default grantee, not just the known four roles. Reapply only the allowlist. Inspect effective grants and role membership at startup/operator provisioning; role membership in owner or any source role is a hard failure.

- Owner: install/revoke/admin receipt and provisioning. Owner-only functions have no grant to app.
- App (including publisher/policy-admin requests): SELECT of protected accepted companion/invocation rows needed by trusted internal services; no INSERT/UPDATE/DELETE there. App receives only the narrow fixture invocation entrypoint and current approval/snapshot read-check functions. It cannot install/revoke/reactivate approvals. Full operator evidence content is not returned to app; the evaluator projection supplies package pins, bounded audience and approval metadata only.
- Runtime/observer/source_reader: no direct SELECT or mutations on any new table; no new approval function EXECUTE grants. Existing provider function signatures return only boolean/digest/expiry projection, never audience/owner review details.
- Audit retention: existing audit privileges only; no new approval/accepted-record reads or writes; no approval deletion when it deletes an ordinary audit event.
- New approval, companion, invocation, case, TestRun-link and publication receipt tables receive deny-update/delete triggers. Approval state receives one-way-transition trigger. Admin events receive append-only trigger. Exact approval checks on result/state/test inserts reference original owner pins; publication/transition checks use the current-transaction receipt and its original fixture linkage. New reviewed result/TestRun commit constraints perform suffix-only active/expiry checks; they never call lifecycle locking. Replacement `source_guard_owner_completion` still permits metadata-only failed/timed-out transitions and checks original approval only for successful data completion.
- Add nullable `source_approval_id`, `source_approval_revision`, `source_approval_digest`, `source_approval_state_revision`, `source_access_reason_code` columns to existing audit events, with strict bounded/coherent checks and explicit app INSERT columns. No FK from ordinary audit to approval: denied-event insertion must never wait on authorization locks. Audits use safe reference/reason metadata only, and no rejected input values are passed to hashing/redaction.

### 2.6 Retention without authorization-receipt starvation

Keep durable authorization receipts and ordinary audit-event retention distinct. Referenced authorization proof is deliberately retained beyond 30 days for as long as its protected source/approval history exists; do not claim that all audit data expires after 30 days. New publication receipts preserve only the validated immutable safe audit projection and ID, without an audit FK. Existing source group/session `allow_audit_id` FKs remain unchanged because those are already retained source-provenance obligations. Do not delete or repin those source references.

The current `RetentionSweep` blindly deletes the oldest 1,000 audit IDs and can repeatedly hit a protected FK. Replace its audit phase with owner-defined `gateway.prune_unreferenced_audits(before_at timestamptz, batch_size integer) RETURNS integer`, SECURITY DEFINER with fixed search path, granted only to the configured dedicated audit-retention role. It verifies `session_user` exactly, rejects null/invalid bounds, caps batch size at 1,000 and cutoff at the database's `statement_timestamp() - interval '30 days'`. In one DELETE statement, select eligible audit IDs ordered by creation/ID after excluding IDs referenced by `source_operation_groups.allow_audit_id` or `source_sessions.allow_audit_id`; return only deleted count. Filter protected IDs before LIMIT so they cannot starve later unprotected events. Add `ix_source_groups_allow_audit` on `source_operation_groups(allow_audit_id)` and `ix_source_sessions_allow_audit` on `source_sessions(allow_audit_id)` in 0007; retain the existing audit-retention created-at index. Assert these indexes and bounded query plans in genuine-PG qualification; this is a scoped exclusion fix, not a retention redesign. It acquires no source/capacity/approval locks, exposes no source rows, and grants no new SELECT on source/approval tables. Existing audit append-only age trigger remains enforced.

Owner A owns this new SQL function and `gateway/execution/retention.py` integration. Real-PG tests seed more than 1,000 old referenced events plus later old unreferenced events: one sweep deletes an eligible bounded batch, preserves every referenced event/approval/receipt/hold, and a second sweep progresses without FK failure. Test direct retention-role source/approval reads still deny, NULL/bounds misuse denies, and failed cleanup does not affect committed result retention.

## 3. Transaction protocol and lock proof

### 3.1 Lock phases

Use existing row locks, not a new global advisory lock or lock manager.

1. **C:** `gateway.capacity_locks['deployment'] FOR UPDATE`. Required first for reviewed-source admission, worker claim/gate/prepare/complete/fail, fixture work, final metadata/result release, approval install/revoke and source provisioning. No provider network I/O occurs while it is held.
2. **P/D/Q:** all needed product rows sorted by ID; draft rows sorted by ID; policy heads/revisions sorted by policy ID/revision. Pre-enumerate multi-target metadata operations while holding C, before evaluation. Existing immutable reads need no extra locks.
3. **V/E/L:** mutable version lifecycle, execution/fixture state and current lease rows, sorted by exact owner ID within each class, when needed. Fence the exact generation/token before cleanup. Retention/cursor dependencies that will be read for release are locked before approval as necessary.
4. **S:** existing source pool → group → session → intent locks, including nested seal/close/settle. These are already serialized by C. Existing reentrant nested settlement and bounded loops are safe under the common mutex and need not be redesigned. No approval check is embedded in metadata-only settlement.
5. **A:** approval-state rows in sorted approval ID order, using `FOR UPDATE` consistently, then fresh `clock_timestamp()` and all final pure assertions/audit. Approvals are immutable except for the state row. Never acquire a previously unheld C/P/D/Q/V/E/L/S lock after entering A.
6. Commit. Return a claim/permit/result/publication receipt only after confirmed commit. Failed/ambiguous commit returns no capability and is not automatically replayed.

SQL provider paths use C → S → A, skipping unneeded Python product/state locks. Their current-policy/identity comparisons remain a coherent snapshot read, as in 0005; approval revoke/install is serialized by C and A. Python release paths lock current P/D/Q/V/E/L as appropriate before A. Policy/admin operations with no source release retain the existing policy-lock semantics and must not call back into a source/capacity helper after taking policy locks. An operation mixing legacy and reviewed targets takes C first for the whole operation. Supported service/function entrypoints obey this graph; arbitrary adversarial direct SQL can deliberately deadlock its own transaction but cannot turn an aborted transaction into authority.

The C mutex is deliberately conservative: reviewed source admission and final metadata/result release serialize for their short DB transactions. They never encompass HTTP/provider work or user waits. Measure latency in qualification, but do not optimize this gate by weakening it in this change.

### 3.2 Preliminary and final contracts

`PolicyService.evaluate(principal: Principal, action: str, product: PinnedProduct, uow: UnitOfWork) -> Decision` (new internal method) derives/validates a Decision and current policy snapshot without an allow audit, approval lock, source permit or release right. `SourceAccessService.precheck(request: AccessRequest) -> str` validates immutable reviewed package/domain/tuple data, also without release authority. A denial uses existing rollback-then-safe-deny-audit behavior, omitting rejected values.

`SourceAccessService.finalize(uow, requests: tuple[AccessRequest,...]) -> tuple[AccessReceipt,...]` requires the caller's earlier phases finished, sorts/deduplicates retained or newly derived approval state IDs, calls SQL final assertions, and appends required allow audits in the same transaction. It does not call `_fence`, `_lock_authority`, policy locking, source materialization, normalization I/O or publication helpers. Returned receipt is an internal UoW-bound value; it is not reusable after commit and not a provider credential. Direct service calls require this same service; a missing reviewed dependency fails closed.

The existing public/internal `PolicyService.authorize` cannot silently become a preliminary gate. Retain existing behavior only for explicitly trusted synthetic regression products; for reviewed products, route to a complete one-operation gate only when its caller has finished the earlier lock phases, otherwise change the caller to explicit evaluate/finalize. Remove all reviewed call sites relying on authorize-before-lifecycle ordering. A static callsite test and transaction spies cover this inventory.

SQL `source_assert_owner_access(owner_kind text, owner_id text, action text) RETURNS jsonb` derives accepted approval, requester/client, product/source pins and effective Decision from protected owners. No approval ID/digest/client argument selects authority. It rejects SQL NULL, JSON null/malformed objects, missing references, inactive/expired state and incoherent owner relationships; only the exact allowed projection is returned. For source admission/data and running completion, the caller also enforces the retained effective execution deadline. For an already completed cached result/cursor, approval expiry and result/cursor retention expiry apply, but the original execution-duration deadline must not invalidate an otherwise valid stored result. A separate transient-product assertion supports authenticated catalog/author/publisher operations before an execution exists; its candidate package must be independently reconstructed from immutable binding/source rows and digest-exact approved content.

All time-sensitive assertions are VOLATILE, not STABLE. Use `clock_timestamp()` after acquiring A; use `IS TRUE`/`IS DISTINCT FROM` rather than nullable truthiness. No positive authorization cache. A fresh time-only assertion occurs after the final allow-audit flush immediately before transaction return; deferred owner/result/test constraints repeat the expiry/state assertion at commit where applicable. A lock prevents revocation changes, not the passage of time. Document that DB validation/commit and subsequent network/HTTP emission are not atomic.

### 3.3 Exact operation sequences

**Consumer admission and idempotency**

- Run bounded expiry maintenance in its own C-only transaction before admission, removing the current `_expire` call from inside already-authorized `admit`.
- Resolve immutable product/binding; take C/P/Q. Perform only bounded structural request/key parsing at this point; do not consult a new approval, canonicalize/persist input values or process them for audit yet.
- Look up the existing principal/product/version/key row before resolving a new approval. If found, load protected owner; compare the current full Principal to retained Principal first. Different client denies generically, returns no handle/status, and does not create another execution. For the matching requester, validate input/filter domains only against this retained approval before canonical comparison or audit parameter construction. Lock existing state; evaluate current policy and final-check the retained approval only. Replacement approval is irrelevant.
- For a new key, evaluate current policy and select the unique matching candidate approval; validate actual input/filter domains against that candidate before canonical persistence/comparison or any audit parameter construction. Require one complete approval tuple. Insert execution, authorization, state, companion and idempotency in one UoW. No caller approval fields. Finish all row creation/flush before A. Final approval/audit and deferred completeness check commit together. Preserve old idempotency key schema and do not add client to its primary key.

**Claim/reclaim and source materialization**

- C; resolve protected accepted owner; P/Q; lock existing E/L before reserve. Preliminary policy plus retained approval checks do not grant dispatch.
- Reserve/reuse the existing source group. Retain original owner approval, source identity, group expiry and original deadline. Lease generation may advance, approval may not.
- Perform claim state/lease changes and flush; final A gate; commit claim. Failed candidates roll back, then cleanup in a new C/fence/source-close transaction that never requires active approval.
- Consumer/fixture source materialization obtains C/P/D/Q/E/L/S, verifies exact plan and accepted owner, writes/reuses session and Domo preparation, then final A. Session reuse cannot move approval, source, plan, expiry, nonce or Domo send right.
- Replace `guard_source_preparation`'s direct session-first lock with capacity-first source locking. Its final validation must happen after all preparation/session work. Do not place a final approval lock in `_materialize` and subsequently call `save_source_prepared`.
- Group/session/preparation BEFORE INSERT guards perform structural/owner/fence checks and earlier-phase locking only. New DEFERRABLE INITIALLY DEFERRED AFTER INSERT constraint triggers `source_group_approval_complete`, `source_session_approval_complete` and `source_preparation_approval_complete` perform the final suffix-only approval assertion at commit. Explicit service finalization still occurs after the last preparation flush. This prevents a session-insert approval lock from preceding the preparation trigger's source lock. A direct SQL insert cannot commit without approval.
- Group/session insert guards derive expected reviewed authority from `execution_source_authorities` or fixture invocation/case/attempt references; supplied spec/snapshot/pins must equal these values. For new reviewed fixture groups, owner_id is the invocation ID. Legacy retained groups remain readable for metadata settlement only.

**Provider functions**

- Split the current private `source_check_context` responsibilities into structural/fence/snapshot validation and the final owner-approval assertion. Public provider signatures remain unchanged.
- `source_check_attempt`, `source_bq_lookup`, `source_bq_check_data`: lock current context and relevant existing intent, then final A immediately before their allowed return.
- `source_bq_admit`: structural context + source/intent/budget checks, determine existing/new intent, final A, then mutate only already-locked group/pool/intent or insert its new private intent. No subsequent lifecycle lock helper. Same semantic intent returns original job without extending deadline; a revoked/expired attempt does not get data/replay authority.
- `source_domo_arm_send`: structural context + all pool/group/session/send/budget checks, final A, then exact one-send mutation on already locked owner. The Python permit is issued only after commit. Recheck existing `source_check_attempt` immediately before the actual outbound call; a denial before I/O can use the original unused nonce to prove not-sent, without constructing another permit.
- All new attempt/group/session/job timeout limits are bounded by retained effective deadline and approval expiry. Never refresh expiry or original deadline during a poll, retry or generation change.
- `source_current_authority(spec)` preserves the old strict six-key legacy contract for compatibility. Add private reviewed owner snapshot reconstruction rather than adding caller-selected approval keys to this function. Reviewed group/session checks compare the owner-derived snapshot including safe approval ID/revision/digest/state/client-projection digest; the provider still gets only its existing safe wire context. Full audience/evidence stays in the registry.
- Keep `source_bq_observe`, reconcile-claim/check-observation, `source_domo_observe`, `source_domo_not_sent`, seal, close, status and settle free of active-approval/expiry requirements. They verify only exact existing intent/role/nonce/generation/evidence and old metadata contracts. They cannot create a new source job, send, rows or result handle.

**Completion and failure**

- C/P/Q; load protected owner; preliminary current policy; E/L fence; source candidate identity/proof validation; `_close_source(..., require_terminal=True)` and settlement BEFORE A.
- Normalize only into private process memory under the same retained product/Decision (no new upstream I/O). Validate result shape/row/byte limits and any explicitly approved output domains; non-PII content still depends on the owner attestation and controlled upstream publication, not a runtime personal-data detector; rejected rows never enter result/evidence/audit storage.
- Final retained-approval/result gate, required audit, then result insertion/state transition using already held dependencies. The source-mode `_release` branch must not call `_close_source` again after A. Existing source completion trigger becomes a suffix-only assertion: reads terminal status without locking earlier rows, then verifies exact retained approval for successful data completion.
- If final authorization/audit fails, the result transaction rolls back and returns no rows. Provider terminal metadata committed before completion remains durable. In the worker's existing failure/finally path, start a new C → exact E/L fence → source close/settle transaction with no approval gate. A stale worker cannot close a successor. If cleanup commit is uncertain, retain the hold; existing observer/expiry/terminal-proof workflows can recover it. Never report rollback closure as committed settlement.
- Fixture completion follows the same phases. Its already-separated `seal_source` transaction is retained. Final fixture checks/evidence require the original invocation and data-principal approvals; failure cleanup does not fabricate successful evidence.

**Result/status/cache/cursor release**

- C for reviewed binding, then P/Q/V/E; compare full current requester with accepted Principal. Load immutable result and lock any existing cursor/result dependencies needed for safe retention before A. Fresh policy derives current projection; no release if scope/variant is incompatible.
- Construct bounded response privately; final gate uses accepted approval, not current matching approval; append release audit; insert new cursor (new row only) and commit. No expiry maintenance, source status locking or policy reentry after A.
- Cache identity remains tied to immutable execution. This plan chooses the equivalent-fresh-gate option: public cursor format is unchanged, and every cursor resolves back to that execution plus its immutable approval; no cached allow can bypass final SQL. Test every page and cache hit after replacement/revocation/expiry.
- `wait_authorized` accepts an explicit trusted requester or the internal single-use wait authority's retained requester and checks it against the owner; it must not authorize only the loaded stored user. `view` remains unscoped internal maintenance only and is never transport-reachable.

**Drafts, fixtures, publication, discovery and choices**

- Read/resolve all needed immutable target IDs first, then C for any reviewed target, all P/D/Q/V locks, all necessary fixture/lifecycle inspection, and only then sorted A locks for the whole final operation.
- `_review` chooses publish-or-author from current policy before the approval check. Approval denial does not try the other action to find a looser entry.
- Save/update gates the exact candidate canonical content before persistence; approval uses manifest digest, not generated draft ID. Incoming unreviewed manifest strings do not enter deny audit.
- Publication/rollback includes target and displaced version, all relevant policy heads, original fixture invocation approval and the publisher's current approval. After earlier locks, final audit and `source_record_publication_operation` produce the exact current-transaction receipt; all reviewed version/state/head/event writes consume that linkage atomically. Publisher capability never substitutes for fixed data-principal scope. Any revoked original fixture approval makes evidence stale; a new approval requires a newly authorized invocation.
- `uow.insert_version`/publication trigger must not acquire a new product/draft/policy lock after A: caller prelocks them, and reviewed final insertion uses a suffix-only consistency assertion. Legacy function behavior is preserved in its legacy branch. The same rule covers successful TestRun insertion.
- Catalog/draft/choice scans can prefilter without returning bytes. Final page recheck batches the exact witnesses and all locks before A, rechecks cursor authority fingerprint, and only then commits response. Include approval ID/state revision/digest in the private discovery scope fingerprint or invalidate by the equivalent full final recheck; choose full recheck plus fingerprint inclusion for deterministic stale cursors.
- Binding/fixture choices require an exact currently approved manifest/package witness and action-specific audience. Policy scope alone cannot reveal connectors. Policy-only administrative choices may retain their current policy authorization, but cannot imply any source binding approval.

## 4. Rollout, compatibility and migration failure behavior

1. Stop reviewed-source gateway/worker admission for schema transition; existing known-job metadata reconcilers can resume after function migration. Do not silently upgrade a running old reviewed-source application against 0007.
2. Apply 0007 as existing owner in one durable migration transaction. If anything fails, it rolls back; do not partially grant functions. Preserve all old rows, counts, IDs, physical pools, epoch, nonce/intent evidence and holds byte-for-byte except schema additions/default generation marker.
3. Old accepted reviewed executions/attempts/groups have no source approval reference and fail all future data admission/RPC/release gates. Do not synthesize approvals, relink them to a new approval or delete them. Exact BQ metadata observation, original Domo terminal/not-sent proof, sealing, close and terminal settlement still work.
4. Checked-in legacy-v1 synthetic owners retain old authorization/timeout/public contracts. Existing checked-in cube-rest-v2 synthetic actual-package regressions also remain supported only by their explicitly injected trusted harness/demo composition, validated against exact retained synthetic assets and synthetic endpoints/registry identity. The generalized cube-rest-v2 adapter is not an organization-serving escape route: production/source-pilot configuration rejects it before credential/provider setup, and direct service calls without supported source approval/lifecycle deny. The synthetic capability is internal, unforgeable by serialization, and issued only by that validated synthetic factory; it is not a request/header/environment allow flag. Do not infer non-PII from model digest or adapter revision. Gate selection comes from protected binding plus trusted validated composition, never caller JSON; missing SourceAccessService denies all non-synthetic source serving.
5. Provision non-query source/binding assets independently of approval; owner explicitly imports reviewed approvals; only then may approved server metadata/package activation occur. Split existing source bootstrap if it currently persists a package before that proof. No ordinary bootstrap installs approval. Synthetic fixture installation is visibly test-only and cannot produce a live approval receipt.
6. Startup validates all configured package files and their immutable secret-version/physical pins first, reads required registry approvals second, and only then opens provider credentials/connections. Readiness consults registry only. One invalid/unapproved configured reviewed package blocks reviewed startup rather than being skipped because the sample package passes.
7. Metadata-only observer startup is a separate path: it validates the existing exact source profile, observer role/endpoint and retained job contracts, but does not require an active serving approval. Revoked/expired historical profiles and approvals must remain available for exact known-job reconciliation across restart/rotation. Do not delete them to make serving readiness pass; no observer path gains query or result-release authority.
8. Keep downgrade destructive rollback disabled. Application rollback is safe only with reviewed-source serving disabled; reverting to a pre-gate binary is not an approved way to restore serving. Restore/recovery must preserve all historical approval references and unresolved source obligations.

## 5. Interfaces and file ownership for safe implementation

Only one worker owns a file. Integration of shared hooks happens through the named owner, not simultaneous edits. Create distinct worktrees from the security branch. These partitions are sequential where interfaces depend on one another, not blanket permission to implement everything concurrently.

### A. Registry, contract and transaction owner

Exclusive files:

- New `migrations/versions/0007_source_access_approvals.py`.
- New `gateway/source_access_contracts.py` (strict approval/package/domain/audience models; canonical digest; no I/O).
- New `gateway/source_access.py` (shared evaluator, accepted-owner repository/snapshot, final SQL gate and lock-phase contract).
- New `gateway/registry/source_access_models.py`; edit `gateway/registry/models.py` only for new existing-table columns/import registration.
- `gateway/source_lifecycle.py`, `gateway/adapters/source_preparations.py`.
- `gateway/execution/queue.py`, `gateway/execution/worker.py`, `gateway/execution/retention.py` for transaction/cleanup protocol.
- `gateway/registry/repository.py` for reviewed insertion/finalization ordering; coordinate its call contract with B rather than overlapping edits.
- Registry/SQL tests in new `tests/integration/test_source_approval_registry.py`, `test_source_approval_transactions.py`, `test_source_approval_migration.py`; pure contracts in `tests/unit/test_source_approval_contracts.py`.

Publishes exact Python interfaces before B/C start:

- `ReviewedPackagePins` strict immutable model, `SourceApprovalDocument`, `AcceptedApprovalRef`, `AccessRequest`, `AccessReceipt`. `AcceptedApprovalRef` carries exact approval ID/revision/digest, audience-entry ID, complete retained Principal, package pins digest, original approval expiry and effective execution deadline. Add it as a private optional field to `Claim`/`AuthorizedExecution` (required for non-synthetic supported sources); do not change public ExecutionView or QueryRequest.
- `validate_source_approval(raw: bytes) -> SourceApprovalDocument`.
- `validate_reviewed_values(pins: ReviewedPackagePins, manifest: Manifest, request: QueryRequest) -> None` (safe bounded error, never audit processing).
- `evaluate_source_access(document: SourceApprovalDocument, principal: Principal, decision: Decision, *, action: str, context: str, pins: ReviewedPackagePins) -> str` returning the unique audience entry ID, never a changed Decision.
- `SourceAccessService.precheck(request: AccessRequest) -> str` and `finalize(uow: UnitOfWork, requests: tuple[AccessRequest,...]) -> tuple[AccessReceipt,...]`; `AccessRequest` explicitly distinguishes retained owner from transient approved package and contains the already-derived Decision/action/requester. No public constructors/routes accept it.
- `load_execution_access(uow, execution_id: str) -> AcceptedApprovalRef`; analogous fixture invocation/attempt accessors.
- `validate_registry_packages(registry, packages: tuple[ReviewedPackagePins,...]) -> None`, which performs no credential/network-source I/O.

### Frozen no-I/O contracts (safe to review separately)

`SourceAudienceEntry` has exact keys: `id`, `context` (`consumer` or `fixture_test`), `principal_id`, `principal_kind`, `client_ids`, `actions`, `tenant_id`, `row_constraints`, `visible_fields`, `fixture_case_ids`. Bounds are `client_ids` 1–100, `actions` 1–9 from the existing closed capability action set, `row_constraints` 1–20 with 1–100 scalar values each, `visible_fields` 1–50 and `fixture_case_ids` 0–8; every collection is duplicate-free; no wildcard IDs/actions. User/service `client_ids` are explicit non-null IDs. The single explicit null member is permitted only for an exact fixed data-principal fixture identity of kind `demo` in `fixture_test`, never for a consumer, invoking author or publisher. Consumer/author/publisher entries require an authenticated non-null client. `fixture_case_ids` is empty for consumer entries and names exact approved cases for fixture-specific review. Actor author/publisher entries remain distinct from fixed data-principal entries.

`audience_contains(entry: SourceAudienceEntry, principal: Principal, decision: Decision, *, action: str, context: str, fixture_case_id: str | None = None) -> bool` is pure and has no audit/DB/network side effects. Revalidate Principal, Decision and entry from fresh plain data at every predicate boundary, rejecting unknown fields and malformed/bypassed-validation instances. A pure predicate cannot distinguish a coherently manufactured valid value from identical legitimate plain data. Coherently changed entry/document bytes must instead fail the protected canonical approval-digest/record comparison in evaluate/finalize; Pydantic freezing is not provenance. Reject duplicate row members (including a member repeated with another operator), duplicate member/operator pairs, duplicate typed scalar values and cross-type equality collisions such as `true`/`1` or `1`/`1.0`; do not rely on Python set equality. It checks exact principal ID/kind/client, action/context/tenant, Decision principal equality, and visible-field subset. For rows, require the same member/operator set; equality values must match and every requested `in` value must be in the corresponding approved set. Reject missing/extra row dimensions; scalar type is part of equality (boolean is not integer). It neither unions entries nor changes Decision limits/fields/rows. `evaluate_source_access` returns the sole matching entry ID; zero or two matches deny. This predicate checks only the tuple. Package pin/domain/classification, canonical manifest and manifest-limit checks remain mandatory in `evaluate_source_access` before tuple matching; passing `audience_contains` alone never grants authority. Fixture structural review for an expected policy-negative case is a separate pure `reviewed_fixture_identity(entry, principal, case_id) -> bool`; it never grants data access or substitutes for `audience_contains` when a Decision exists.

`has_unsupported_delegation_claims(claims: dict[str, object]) -> bool` requires an exact dict with at most 256 exact-str keys, each 1–256 characters. Reject custom containers/subclasses before any iteration; validate the full key shape before returning a boolean. Values are not traversed, decoded, copied or represented. It reports presence of signed `act` or `may_act`, including null/empty values. Call only within the reviewed token profile after signature validation (early rejection is permitted but never early acceptance). `has_unsupported_delegation_headers(raw_headers: list[tuple[bytes,bytes]] | tuple[tuple[bytes,bytes],...]) -> bool` requires exact list/tuple of at most 256 exact two-tuples of exact bytes, with 1–128 ASCII-token-byte header names. Reject custom containers/subclasses before any iteration and validate the full shape before returning a boolean. Compare ASCII-lowercased names against exactly `on-behalf-of` and `x-on-behalf-of`; presence alone, including empty/repeated forms, rejects. Total header name-plus-value bytes must be at most 65,536, counted using `len` only. Values are not decoded, traversed, copied or represented; invalid shapes or aggregate overflow produce fixed safe ValueErrors with no supplied content. These helpers are predicates, do not mint identity/authority, and do not classify unrelated spoof headers or plain tokens as agents. Narrow review approval of these pure helpers does not freeze or authorize the approval-document schema, persistence APIs or runtime wiring.

### B. Policy, authoring, transport and release owner

Exclusive files:

- `gateway/policy/service.py`, `gateway/policy/audit.py`.
- `gateway/execution/service.py`, `gateway/execution/results.py`, `gateway/execution/cursors.py` if private authority handling needs it.
- `gateway/publishing/service.py`, `gateway/publishing/test_runner.py`, `gateway/publishing/models.py`.
- `gateway/api/catalog.py`, `drafts.py`, `choices.py`, `services.py`, `app.py`, `dependencies.py`, `transport.py`, `transactions.py` as needed for wiring and consistent rejection.
- `gateway/auth/tokens.py`, `gateway/auth/settings.py`, `gateway/mcp/server.py`.
- New unit/contract/security tests named `test_source_approval_*` in those respective directories, plus targeted legacy assertions.

Consumes A's published contract, does not edit migration/models/source lifecycle/queue. Requests queue changes from A. Explicit reviewed-mode signed `act`/`may_act`, case-insensitive repeated `On-Behalf-Of`/`X-On-Behalf-Of`, and closed-schema delegated configuration/request fields deny consistently. Keep unrelated ignored spoofable headers and legacy auth behavior unchanged. Plain valid tokens still require exact principal/client tuple and do not prove absence of an unseen agent.

### C. Operator tooling/startup owner

Exclusive files:

- New `gateway/source_access_operator.py`; new `scripts/source_access_approvals.py`.
- `gateway/source_pilot_config.py`, `source_pilot_assets.py`, `source_pilot_bootstrap.py`, `source_pilot_builder.py`, `source_pilot_runtime.py`, `source_pilot_samples.py`, `product_packages.py`, `reviewed_runtime.py`, and `runtime_config.py`/`runtime.py` only if these own the reviewed composition entrypoint.
- `scripts/spaceport.py`, `spaceport_setup.py`, `spaceport_files.py`; source-access runbook/examples/tests under `tests/unit/test_source_approval_tooling.py`, `tests/integration/test_source_approval_startup.py`.

Consumes A's contract and registry checks; invokes B's API factory dependency seam without editing B-owned files. Import/revoke are explicit owner-only commands. Build/validate can emit unapproved requests offline. No auto-import, auto-grant, live credential fetch or activation. CLI never prints full documents, audience, evidence or credential references on error.

### D. Qualification/review coordinator

Owns shared harness/workflow files and resolves integration edits after A/B/C: `tests/integration/conftest.py`, migration-pair receipts, source image-gate fixtures, `.github/workflows/*`, `scripts/verify_*`, and release documentation. Individual workers add isolated tests, not shared harness edits. Capture new exact-source/tree/hash receipts; connector PR 5 receipts do not qualify this gate.

## 6. Implementation tasks and acceptance gates

### Task 1: Canonical approval registry and ACLs (owner A)

- [ ] Write failing pure tests for duplicate keys, unknown keys/classification, 65,537-byte document, 101 audiences, 201 lineage entries, bool/fraction as integer, malformed/null expiry, crossed tuples and ambiguous complete matches. Assert no Decision mutation and no credential/sample/SQL content accepted.
- [ ] Run `pytest tests/unit/test_source_approval_contracts.py -q`; capture expected failures.
- [ ] Implement strict contracts and 0007 approval/state/admin objects, owner-only install/revoke and final VOLATILE assertion helpers.
- [ ] Write real-PG role/default-ACL tests: app, publisher-equivalent app, policy-admin-equivalent app, retention, source_reader, BQ consumer runtime, BQ fixture runtime, observer and Domo runtime cannot install/alter/delete/reactivate or call private functions. Hostile default ACL/membership setup must fail closed.
- [ ] Verify canonical digest parity between Python/SQL, idempotent import, terminal revoke, required receipt rollback, and `provolatile='v'` for time-sensitive functions.
- [ ] Commit only passing Task 1 files; independent review of the migration and SQL 3-valued-logic paths before acceptance-owner work.

### Task 2: Immutable consumer and fixture owners (owner A; B consumes)

- [ ] Failing PG tests: acceptance missing companion; forged companion approval/client/Decision; retroactive pin on generation 0; client-swapped idempotent key; invocation draft/catalog/source/actor/client/case substitution; missing fixed approval on permission-negative fixture; fresh replacement approval cannot relink existing owners.
- [ ] Implement companion/invocation/case/TestRun-link tables, exact FKs, insert-only generated pins, deferred completeness checks and explicit fixture admission function. Preserve original execution/fixture timeout equalities while exposing pinned effective deadlines.
- [ ] Update queue admission/load/idempotency and source owner reconstruction. For new pins derive from protected owners, never caller-provided approval IDs; for reuse require exact retained reference.
- [ ] Run owner/SQL tests and existing client identity/execution/fixture/retention tests; assert mismatched client returns no old execution ID, source/session/prepared/cursor count stays unchanged and no replacement execution was inserted. Test publication receipt absence, client/target/action/audit substitution, unused receipt, duplicate event and cross-transaction reuse directly in PostgreSQL, including replay of an old matching committed allow audit.
- [ ] Commit and publish typed interfaces to B/C; independently review accepted-owner FK/trigger and publication-operation linkage seams.

### Task 3: Ordered source lifecycle and result completion (owner A)

- [ ] Failing transaction spies and real-PG tests for claim/gate/preparation/complete ordering; block final approval to prove no source/policy/fence lock is first acquired afterward.
- [ ] Refactor preliminary authorization/fencing/source closure/final gate phases; replace 0006 session-first preparation guard in 0007. Split provider structural/final checks without changing public provider signatures.
- [ ] Preserve metadata-only observation/nonce proof/sealing/settlement across approval denial. Add completion cleanup that independently commits exact terminal settlement after result rollback, with stale-worker fences intact.
- [ ] Run existing source lifecycle, Domo lifecycle/preparation, pending replay and worker tests plus new approval races. Assert one Domo send maximum and zero new BQ intents under revoked authority.
- [ ] Commit; independent SQL/Python lock-table review before release hooks are claimed ready.

### Task 4: All application and transport release boundaries (owner B)

- [ ] Failing tests through REST, every current MCP tool and direct services: discovery/describe/draft save/update/diff/choices/test/evidence/publish/execute/wait/status/cache/cursor deny for missing/revoked/expired/wrong tuple approvals.
- [ ] Implement evaluate/finalize discipline and complete-tuple action-specific checks; batch final multi-target locks; preserve author/publisher/fixed-principal isolation.
- [ ] Move domain checks before input audit processing. Use safe approval reason metadata; test rejected values are absent from canonical query, audit hashing calls, preparation, logs, exception/repr, result and evidence.
- [ ] Implement explicit unsupported delegation rejection in reviewed mode, including repeated/case-variant headers. Legacy spoof-header/auth/public-schema snapshots remain unchanged.
- [ ] Run targeted REST/MCP/result/publishing/security regression; commit only when direct service bypass tests pass.

### Task 5: Explicit operator workflow and startup (owner C)

- [ ] Failing tests for every configured package, especially second/non-sample invalid package; assert zero credential reads/token requests/provider clients/source queries on missing/expired/malformed approval.
- [ ] Implement review-request build, owner-only import/revoke commands, registry-only readiness, immutable secret-version prerequisite and startup sequencing. Block generalized cube-rest-v2 organization activation/serving and unknown profiles; preserve exact synthetic regressions through the trusted validated factory only. Test source-pilot, reviewed_runtime and standalone service attempts to bypass via adapter/config selection. Separate source identity provisioning from approval and metadata activation.
- [ ] Test offline construction without approval/credentials remains possible but cannot be labeled active/qualified. No raw approval/evidence/secret content in stdout/stderr/errors.
- [ ] Run static pins and actual package/compiler/SDK tests, commit, and update runbooks distinguishing attestation, synthetic qualification and separately authorized live qualification.

### Task 6: Real PostgreSQL races, migration and whole-branch qualification (owner D)

Use the existing disposable loopback guard (`GATEWAY_TEST_POSTGRES_AUTHORIZATION=disposable-local-postgres-only`) and fresh non-elevated roles. Tests must run against real PostgreSQL, not SQLite/mocks. Use two or three independent connections, deterministic barriers/events and `pg_blocking_pids`/`pg_stat_activity` to verify actual waits. Do not infer races from arbitrary sleeps. Bounded `lock_timeout`/`statement_timeout` detect accidental hangs; deliberate barrier tests release barriers before those ceilings.

Required schedules, each with committed final-state and provider-call assertions:

1. **Admission wins vs revoke:** T1 holds C, inserts accepted owner/pins and reaches final A; T2 revoke demonstrably waits. T1 commit may return its accepted handle; T2 then revokes; next claim/data/result check denies. Reverse order: revoke commits first; admission writes nothing and returns no handle.
2. **Same-key different client concurrently:** two clients for one user submit same key/query. Exactly one accepted owner may exist; losing wrong client gets generic deny, no old handle and no extra dispatch. Same exact client reuses only original approval. New approval after revoke never revives the key.
3. **Claim/reclaim vs revoke:** pause before final A and after lease generation mutation; reverse both commit orders. No claim committed after revocation's serialization point can dispatch. Reclaimed owner retains expiry/source/approval and Domo nonce/send state.
4. **BQ admission/data vs revoke:** pause at provider admission and before get-results/data gate. Revocation first denies, intent counts/budgets unchanged. Admission first may create only the original intent; after revocation no resubmit or row retrieval. Known job metadata can still be observed to terminal and settle.
5. **Domo arm/pre-send/response vs revoke:** revoke before arm, between arm and immediate pre-send check, during fake response and before normalization/final result. Unused original permit may prove not-sent; started permit cannot. Completed original response can prove terminal while rows remain forbidden. No retry/token/query fallback or replacement send.
6. **Complete inversion regression:** T1 complete holds C and reaches E/L/S closure before A; T2 revoke waits at C. Deliberately pause at `_fence`, `_close_source`, final approval and result flush. Assert no observed lock cycle/deadlock, no post-A earlier helper acquisition, and no duplicate `_release` source close. Reverse ordering denies data and leaves terminal settlement committed independently or reconcilable.
7. **Result/cursor vs revoke/withdraw:** final release holds relevant lifecycle/A locks through audit commit. Revocation/withdrawal first prevents release; release-first transaction may produce that already-authorized page, but every later page denies. No claim that revocation retracts bytes already emitted.
8. **Expiry without JSON mutation:** install near-future expiry; use real `SELECT clock_timestamp()` and wait until DB time crosses it while another connection blocks the operation. Do not edit approval JSON or mock DB time. Queued claim, BQ/Domo data check, final result/cursor and final fixture evidence fail after wait; local clock rollback does not help. Observer/late terminal proof succeeds.
9. **Multi-target publication/choice final page:** opposite target/policy/audience order in concurrent operations; verify deterministic prelocks and sorted A order. Revoke one target approval mid-scan; final page/publication returns no partial sensitive projection/receipt. Reuse of original fixture evidence after replacement approval is stale.
10. **Audit/commit failures:** fail allow audit before/after result/cursor insertion and fail commit/connection acknowledgement. No row, handle, permit or success receipt is returned. The application never retries an uncertain Domo arm. Revocation audit failure leaves old state intact and reports no successful revocation.
11. **Migration with obligations:** start at 0006 with accepted/running/succeeded reviewed executions, pending BQ jobs, ambiguous Domo intent, successful fixture evidence and legacy executions. Upgrade twice; snapshot old obligation identities/counts and prove no destructive mutation. All unpinned reviewed data paths deny; exact original observation/proof/settlement work; ambiguous holds persist. Legacy regression works unchanged. No generation-0 backfill allowed.
12. **Direct SQL misuse:** NULL and malformed context/expiry/approval JSON, wrong owner/session/context, changed principal/client, new digest over different coherent model/source pins, alternate source aliases and caller approval IDs cannot select authority. Call functions as actual runtime/observer roles. Approval content/audiences/admin evidence are unreadable from provider roles. Test default ACL and membership inheritance, not only direct grants.
13. **Fixture independence:** invoking author/client, fixed positive principal and fixed negative principal each lose approval separately. Missing review never counts as negative policy success. Later publisher passes its own exact tuple and cannot rescue revoked original evidence. Swap consumer/fixture bindings and run IDs directly in SQL and deny.
14. **Domain and package repins:** mutate every independently editable package pin, model/statement definition, schema/normalization, catalog, exposed/hidden/grouping/source-input field, approved enum/default/example and source physical identity. Recompute all attacker-editable hashes; old approval still denies before provider I/O. Secret-alias-only mutation is explicitly not presented as credential-drift detection.

- [ ] First run new tests in isolation with expected red failures, then all applicable tests under `scripts/verify_registry_postgres.sh` and `scripts/verify_gateway_integration.sh` in the authorized disposable environment.
- [ ] Run actual Cube/compiler/SDK and hosted image source-runtime gates on the final security commit/tree, not the connector receipt; unchanged caps, no skips, cleanup verified. Document any unavailable gate as blocking, not passed.
- [ ] Independently review code and confirm no unlisted release/derived-decision/source-RPC path bypasses the shared gate. Verify historical migration files and legacy assets are byte-identical.
- [ ] Publish only a separate private draft PR and its exact-source evidence when authorized by the coordinator. Organization activation and merge remain blocked.

## 7. Plan self-review and handoff status

- Design requirements map to schema/ACLs (Section 2), transactions/revocation/expiry (Section 3), migration/startup (Section 4), ownership (Section 5) and tests (Section 6).
- The previous authorize-before-fence completion inversion is explicitly removed; approval locking is not placed inside a helper that can be followed by lifecycle locking.
- Existing global source-capacity serialization is retained; no unnecessary cross-session lock manager is proposed.
- Public contract and legacy timeout invariants are retained; reviewed accepted pins and effective deadlines use private companion records.
- Caller-controlled pins never select source approval authority. Trusted application authentication remains necessary; DB functions cannot prove an OIDC signature or detect an unseen agent using an ordinary user token.
- Remaining release boundary is honest: revocation serializes subsequent authorization transactions, but cannot make DB commit atomic with a later provider network send or HTTP response, or retract bytes already sent.

Status: independent reviewer has cleared the ownership/persistence design for implementation in staged reviewed partitions; parent confirmed the final raw-header bounds; document frozen for the exact-hash review receipt; includes the parent-requested generalized-Postgres fail-closed boundary and reviewer-requested approval-independent observer startup. Any disagreement about trigger ordering, accepted-owner identity, fixture negative-case semantics or the direct SQL boundary is a blocker to implementation, not an implementer choice.
