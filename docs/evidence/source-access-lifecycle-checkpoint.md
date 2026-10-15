# Retained consumer source lifecycle checkpoint

This checkpoint extends the reviewed consumer owner companion and finalizer in
`8aff542`. It does not qualify fixture owners, publication receipts, live provider
access, or a complete Domo worker run.

## Protected boundaries

- The six-key legacy authority specification remains closed. A new owner-derived
  snapshot resolves the original execution companion, full principal/client,
  exact approval triple, package pins, and accepted deadline. Caller-provided
  specifications are equality assertions, not approval selectors.
- New group, session, and typed preparation inserts have deferred approval
  completeness checks. A reclaimed session generation also checks original
  approval at deferred completion. Historical rows are not backfilled.
- Provider data functions retain capacity/source ordering and take the approval
  suffix only after relevant intent locks. Existing intent replay receives the
  same final active/expiry check as a new admission. Provider functions do not
  acquire Python product or execution-prefix locks after source locks.
- Group/session audit references must name an allow from the current transaction
  carrying the original safe approval fields. These provisional rows roll back
  if later source work, final authorization, or deferred commit checks fail.
- Consumer materialization uses freshly reloaded immutable owner fields and the
  queue's fresh current policy, claim-integrity and deadline helpers. All source
  and preparation work is flushed before the shared finalizer.
- Seal, status, close, observer reconciliation and exact terminal-proof settlement
  remain approval-independent. An unknown remote job keeps its capacity hold
  after revocation or expiry; no query replay can free that hold.

## Evidence

The new genuine PostgreSQL lane first failed seven intended checks: four revoked
provider release operations, absent original approval snapshot, expiry between
finalization and commit, and expiry while blocked on an existing intent. Its
metadata-only revoked unknown-job settlement control passed. All eight disposable
migration identity checks and orderly cluster cleanup completed.

The corrected lane passed 84 genuine PostgreSQL cases (eight lifecycle plus
76 common registry/owner/finalizer/final-clock cases), with 84 migration identity
rechecks and normal cleanup. The focused source/audit/Domo-unit lane passed 128
cases. These are author-run receipts; independent review is separate.

The Python consumer materialization hook additionally depends on the frozen queue
helpers from `7a4e17a`; combined queue integration must pass before execution
readiness is claimed. SQL-only runner v3 has SHA-256
`62d6bcb0e398670530e745821ef141c73ef7bfe113d62c84888830010e5193e1`.
This is finite synthetic loopback SQL evidence. It makes no detached-process,
Node, crash-suite, cloud, credential, or provider-I/O claim.

## Structural policy-negative evidence helper

`source_current_decision_or_denial` is owner-only and inert. It validates current
identity, all active grants, complete current policy digest/shape/coherence, and
product limits before returning SQL NULL for an ordinary missing/ambiguous scope,
missing capability, or absent required entitlement. Present empty/malformed
entitlements and invalid policy remain errors. The original strict
`source_current_decision` delegates and raises on NULL. No audit, source state,
permission, or negative-fixture proof is created by calling this helper alone.

Twelve genuine SQL checks cover positive Decisions, valid unused scalar values,
three ordinary negatives, and seven malformed/unavailable cases. They passed
with twelve migration rechecks and normal cleanup after the missing helper was
observed RED. The fixture owner must independently recompute this predicate in
its protected final transaction before accepting negative evidence.

## Review corrections

Independent review identified a legacy snapshot comparison in session reclaim
and a shorter session deadline missed after an intent-lock wait. The correction
uses the retained owner snapshot for active reclaim, checks group/session time
and the current lease/fence after the final approval suffix, and rechecks a new
BigQuery job's timeout budget after any approval wait. Consumer materialization
also uses queue `7f43361`'s clock-only tail guard after its finalizer.

The active reclaim control first failed, while revoked reclaim denied. Three
additional blocked-lock cases independently failed for session expiry, lease
expiry, and a stale new-job timeout budget. The corrected fourteen-case lifecycle
lane passed with fourteen migration rechecks and normal SQL-only cleanup.

## Finite Domo SQL qualification

An explicit synthetic Domo package now supplies separate consumer/fixture source
pins and immutable credential-version references without configuring or reading
credentials. The app role admits the original consumer companion and materializes
its exact typed preparation and session. Five SQL cases cover active arm, revoked
arm, unknown-send retention after revocation/expiry, exact nonce/terminal proof
settlement, deferred preparation rollback, and a shorter deadline after an A-lock
wait. No OAuth, HTTP provider, or worker transport is invoked.

The combined lifecycle (19) and transaction (46) lane passed 65 genuine PostgreSQL
cases with 65 migration identity rechecks and normal SQL-only cleanup. The deferred
expiry tests explicitly prove they reached successful common finalization before
waiting for database expiry and failing commit.
