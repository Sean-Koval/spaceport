# Domo gateway pilot acceptance

`domo-named-v2` now has a configured gateway serving path through real registry,
policy, draft fixtures, publication, consumer queue/worker and result services.
The existing offline `domo-named-v1` remains unchanged. Local acceptance replaces
only the final Domo HTTP boundary; it does not establish actual Domo compatibility.

## Implemented boundary

- Immutable operator source, dataset, statement bytes and finite variants pin the
  normalized request, principal-derived scope and complete effective decision.
- Documented OAuth client credentials and exact SQL POST use actual HTTPX
  serialization. No query retry, redirect, environment proxy, token persistence
  or fallback response shape is permitted. `data` is not a read-only OAuth scope.
- Customer/user/client identity, a complete bounded JSON response, one sentinel
  row and 50 total wire columns are required. LONG-to-decimal conversion retains
  exact signed-64-bit JSON integers without floating-point intermediaries.
- One original in-memory send permit follows a committed PostgreSQL intent.
  Reclaim never grants a second POST. Unknown outcomes retain source capacity.
  Complete exact synchronous response evidence is recorded independently of
  current row authorization; its eight-field receipt is immutable.
- One monotonic cutoff spans token acquisition, gates, query, parsing and cleanup.
  Wall-clock rollback cannot renew it. The transport origin is a literal and
  detached private source/auth snapshots survive callbacks. Raw response bytes
  are privately retained and revalidated before evidence or semantic consumption.
- A separate immutable typed preparation row is tied to the exact logical owner,
  session, binding, finite statement and plan fingerprint. Session and preparation
  insertion share a transaction. Canonical TEXT and JSONB use separate typed binds.
- One fixture-run group covers the fixed positive-case roster and its combined
  budget. Each case requires its own exact context and sealed terminal session;
  whole-group settlement waits for closure and all materialized source work.
- Publication checks exact current fixture evidence and settled source ownership.
  Public descriptions use Domo's explicit grain/time mapping and expose only
  currently reviewed finite examples, ordinary product fields and public provenance.
- Optional unknown freshness is explicit. Required freshness needs an approved
  producer checkpoint in the same response. Fetch time, HTTP date and independent
  dataset metadata cannot establish row freshness.

## Genuine local evidence

The PostgreSQL suite uses isolated random local databases and roles, actual
migrations through `0006_source_preparations`, real owner provisioning and all
production gateway services. It never seeds a passed TestRun for the full flow.
HTTPX MockTransport provides bounded synthetic provider bytes only.

`tests/integration/test_domo_gateway.py` covers:

1. Author draft → four isolated fixture queries and three permission/input
   negatives → publish → real describe/source-check preflight → consumer Worker
   → ResultService. Exact integer counts, unknown freshness, source receipts,
   preparation counts and final capacity settlement are asserted.
2. A query HTTP 401 leaves an unknown intent and held capacity without results.
3. Revocation during a successful read retains terminal proof and releases no rows.

`tests/integration/test_domo_lifecycle_edges.py` covers one POST across worker
reclaim; late original response proof after generation change; immutable strict
receipts and wrong-nonce denial; original unused versus started permit proofs
following revocation; an actual expired database deadline; cross-provider retained
active caps; owner-bound preparation decoding and atomic SQL rollback.

Commands used on 2026-10-15:

```sh
.venv/bin/pytest -q -m 'not postgres and not cube and not e2e and not live_bigquery'
.venv/bin/python /tmp/spaceport-postgres-build/run-local-postgres.py -q \
  tests/integration/test_domo_gateway.py \
  tests/integration/test_domo_lifecycle_edges.py \
  tests/integration/test_domo_preparation_registry.py
```

The local runner creates a synthetic SCRAM-authenticated loopback PostgreSQL 17.11
cluster, supplies the existing explicit disposable-test authorization, and verifies
process shutdown and cluster removal. Repository CI may use its equivalent
`scripts/verify_registry_postgres.sh` isolated-stack path. No external database,
cloud resource, IAM grant or provider credential is touched. Raw logs are untracked.

## Operator-owned live qualification

No real Domo access has occurred. Before using an own-data source, the operator
must review the exact account/customer, dedicated shared identity, approved
shares/datasets, finite SQL, producer grain/decimal rules and optional checkpoint
contract. Fixture and consumer dataset/client/owner isolation is configuration
attestation; it is not measured IAM or PDP equivalence.

The first expressly authorized bounded source check runs through ordinary
published execution with the same policy, source session and bounds. Configuration
is never a `verified=true` override or a privileged qualification bypass.
Actual token audience, SQL dialect, complete-response shape and source semantics
remain live qualification prerequisites. Domo scan/cost caps, cancellation,
provider job status, provider lifetime bounds and per-employee PDP impersonation
remain unavailable guarantees. A client timeout does not cancel provider work.
