# Explicit source-access approvals

Status: local implementation and synthetic qualification work. Organization activation, live identity/permission qualification, deployment and merge remain separate gates. Shared non-personal approval is not per-user source entitlement or delegated access.

## What is reviewed

The owner reviews exact manifests, bindings, physical sources, credential configuration, models/statements, fixture catalog, hidden/grouping/checkpoint/source-input inventory, bounded input/filter domains, audiences and purpose. Tooling derives structural inventory but never guesses a classification, owner, evidence or audience. The complete source-input lineage and upstream publication controls remain an owner review responsibility; matching hashes do not prove mutable upstream data is non-personal.

Full approval documents are private operator files. They contain governed audience and review metadata, never source samples, SQL or credentials. Keep them outside serving/provider package directories. The application reads only the closed registry runtime projection; provider functions receive the narrower protected decision/digest projection.

## Offline preparation

`python -m scripts.spaceport pilot init`, `build`, `validate` and `render` remain offline. Their output is unapproved configured intent, not active authority or observed source qualification. No builder or bootstrap installs an approval.

Create an unapproved structural review request:

```sh
python -m scripts.spaceport approval review-request \
  --config /private/package/pilot.json --package-id weekly-account-usage-bigquery \
  --revision 1 --output /private/review/request.json
```

The owner supplies the complete closed `SourceApprovalDocument` schema after independent review. Do not treat a request's inventory as classification evidence. Canonicalize that explicit private document without importing it:

```sh
python -m scripts.spaceport approval canonicalize \
  --approval-file /private/review/owner-reviewed.json \
  --output /private/review/approval.canonical.json
```

Canonical approval JSON is duplicate-free and at most 65,536 bytes. Input documents must be owner-only regular files; output directories must be owner-only. Commands do not follow symlinks or overwrite existing outputs. Unknown keys, noncanonical approval bytes, arbitrary text domains and unsupported classifications deny with fixed safe diagnostics.

## Explicit owner import and terminal revocation

Install only after separately authorized source/profile/binding provisioning. Import uses the existing owner registry login, exact package validation and the owner-only database function. It creates no provider identity, credential, grant or deployment.

```sh
python -m scripts.spaceport approval import \
  --config /private/package/pilot.json --package-id weekly-account-usage-bigquery --revision 1 \
  --approval-file /private/review/approval.canonical.json \
  --receipt-file /private/review/import-receipt.json
```

Receipts contain only immutable identifiers/digests and state, and are emitted after acknowledged commit. Exact import replay returns the original record; it never reactivates revoked approval. A database or receipt-write failure produces no success claim and may report an uncertain outcome. Inspect the protected registry before any manual retry; commands never retry automatically.

Revocation requires the exact installed ID and digest from the receipt and a bounded reason code:

```sh
python -m scripts.spaceport approval revoke --config /private/package/pilot.json \
  --approval-id APPROVAL_UUID --expected-digest APPROVAL_SHA256 \
  --reason-code owner_review_ended --receipt-file /private/review/revoke-receipt.json
```

Revocation is terminal for that identity. Replacement approval does not revive old accepted work. Revocation changes no source obligations, held capacity or provider jobs; exact terminal observation and settlement remain independent.

For reviewed sources, submitting an idempotent replay after the original execution deadline is forbidden because its execution authority has expired. A separately authorized result-status request may still return the original execution ID with `timed_out` / `EXECUTION_TIMEOUT`, while the retained source approval and result access remain valid. This does not admit a new attempt or release uncertain provider capacity. Checked-in legacy synthetic behavior is unchanged.

## Minimal serving references and startup

Produce a reference only after verifying the exact installed approval is currently active at database time:

```sh
python -m scripts.spaceport approval reference \
  --config /private/package/pilot.json --package-id weekly-account-usage-bigquery --revision 1 \
  --approval-file /private/review/approval.canonical.json --approval-id APPROVAL_UUID \
  --output /private/review/runtime-ref.json
```

The reference command uses the existing app registry login; import/revoke use the
owner login. Supply the respective role files in an explicitly authorized operator
environment. The owner-only bootstrap pod is not automatically suitable for
reference verification, and no command creates or widens these credentials.

Copy the returned closed reference into `pilot.json`'s `source_approval_refs` array. Each entry has exactly `package_id`, `revision`, `approval_id`, `approval_revision` and `approval_digest`. Here `revision` is the package revision; `approval_revision` is the separate immutable approval revision. No full approval file path, audience, owner evidence or reusable active-state flag belongs in serving configuration. The existing config renderer therefore needs no new approval-file mount, ConfigMap or Secret.

Startup checks every configured package and reference, including non-probe packages. All local source/binding/model/fixture pins and required immutable credential versions are validated before registry authentication. Registry credentials are necessarily needed to read the protected closed projection. The application compares its exact reference, manifest, complete inventory, domains and source/credential pins, then performs one coherent all-package active/expiry check before any provider credential, token, client or source I/O. Readiness rechecks only registry state. A reference file, rendered config or prior successful readiness check is never current release authority.

Domo serving requires both explicit immutable numeric Secret Manager version references in the approved source descriptor. Offline templates deliberately omit them. Aliases, `latest`, zero and leading-zero numeric versions deny. Operators must separately guarantee the mount-to-version mapping and verify actual runtime identity and least-privilege source permissions; a configured digest does not prove mounted bytes or upstream entitlement. BigQuery pins expected ADC/workload identity without inventing a credential-secret version. Internal signing-key rotation is separate from upstream source identity.

This slice does not provide ReBAC, token exchange, on-behalf-of delegation, per-user Domo permissions or a PII classifier. Reviewed mode rejects supported explicit delegation markers rather than granting delegated authority. An ordinary valid bearer token does not reveal whether an unseen agent possesses it. Owner attestations and digest matches cannot establish that future upstream rows remain non-personal; live identity, source permissions, publication control and inference-risk qualification remain separate requirements.

## Synthetic compatibility boundary

Only the exact checked-in v1/v2 package, bindings, fixture catalog, policy, accounts and endpoint catalog can receive the factory-issued in-process synthetic compatibility capability. The registry must retain its original fixed synthetic endpoint and exact engine, pool and session bindings. The capability cannot come from JSON, environment flags, adapter names or an ordinary standalone policy service; altered manifests and endpoint overrides fail closed.

The legacy and reviewed factories are explicitly wired and tested with their actual policy service and a genuine disposable registry. Existing tests that create product variants, replace private endpoint URLs or compose organization OIDC synthetic pilots cannot reuse this capability. They must migrate to supported BigQuery/Domo packages with explicit owner-installed approvals or receive separately reviewed support. No generalized production Postgres approval path is enabled by this slice.

Selected local accepted-owner, lifecycle, fixture, publication and release checks have passed independent review; see the [integration receipts](../evidence/source-access-integration-progress.md) for exact checkpoints and limits. Full branch acceptance, exact-source actual-package/compiler/SDK/hosted-image checks and separately authorized live qualification remain required before rollout.


## Upgrade, renewal and recovery

Before applying migration `0007`, stop reviewed admission and use the existing registry owner migration path. Preserve historical records, configured source profiles, deployment epoch and unresolved provider holds. Do not backfill missing approvals or canonical witnesses, or restore serving with a pre-gate binary. Metadata-only observers remain approval-independent; they may settle only exact retained obligations.

For a changed package or expired approval, obtain a new explicit owner review, import that approval, verify its minimal reference, and perform the required newly authorized fixture/publication workflow. Replacement approval never relinks accepted work or revives old fixture evidence. After a failed or uncertain operation, inspect committed protected state before any manual retry. Do not repair publication counters or receipts by hand, or clear unknown provider holds.

## Organization qualification checklist

Before separately authorizing activation, retain evidence for all of these gates:

- Passing exact-source/tree acceptance receipts, including every required backend partition and actual runtime/image lane.
- Observed runtime provider identity and least-privilege permissions, independently of configured identity strings or digests.
- Exact numeric secret-version-to-mount mapping for Domo, verified without recording credential values.
- Consumer and fixture source isolation, including source permissions and controlled upstream publication.
- Explicit owner evidence that all reachable inputs, outputs and inference risks satisfy the supported non-personal classification and approved purpose/audience.
- A tested owner revocation and recovery procedure that preserves unknown obligations and rejects stale references.

These checks do not create user delegation, individual Domo entitlement enforcement, a PII classifier or a verified provider revocation propagation guarantee. Those capabilities remain separately qualified requirements.
