# Organization pilot: GCP / GKE

Updated 15 October 2026. GCP/GKE is the selected infrastructure target. This document is a source-based deployment-readiness assessment and implementation sequence, not deployment approval, a capacity claim or a production release.

## The answer today

Spaceport has a substantive application foundation: governed product contracts, separate author/publisher workflows, authorized discovery, typed execution, durable PostgreSQL queue/results, current-policy reauthorization and shared REST/MCP services. The merged workflow implementation passed 1,950 unit/contract tests, 329 genuine PostgreSQL/Cube cases and 254 frontend tests on its recorded source. Those counts do not certify later changes. See the [roadmap evidence](../../ROADMAP.md#1-complete-two-everyday-workflows).

It is not yet qualified to serve organization data. The demo composition remains synthetic-only. The separately merged [reviewed-source pilot](source-pilot.md) implements bounded BigQuery/Cube and finite Domo REST serving, operator author/test/publish commands and GKE templates. Its recorded synthetic-provider backend acceptance does not establish live identity, source permissions, PII safety or organization readiness. The additional [source-access approval boundary](source-access-approvals.md) requires its own exact-source acceptance and owner qualification before activation. Actual browser/composed acceptance and operational qualification remain required.

The implemented vertical foundation is [the OIDC synthetic pilot runtime](../superpowers/specs/2026-10-14-organization-runtime-foundation.md). It wires existing identity/policy services into explicit HTTPS/TLS/mounted-secret configuration and controlled provisioning. Its [source-level evidence](../evidence/organization-runtime-foundation.md) does not establish a deployed pilot. It deliberately retains synthetic revenue data. Proving organization login against synthetic data is useful before permitting a warehouse query. Browser authorization-code/PKCE login and compatibility with the selected identity provider/Studio connection remain separate work.

## P0 sequence

1. **Complete the identity/runtime foundation.** Separate demo and pilot entry points; trusted issuer/JWKS/audience/client configuration; certificate-verified registry; canonical `/mcp` resource/discovery; explicit user and service mappings; reviewed GKE references. Never create grants or publish during application startup.
2. **Prove the complete synthetic journey.** On the exact release candidate, run real author → fixtures → separate publisher → consumer, both independent MCP clients and REST parity. Include revoked access, restart/restore, interrupted requests and real browser/keyboard/narrow-layout checks. Stronger packet-observed offline acceptance remains a separate, explicitly approved prerequisite; it does not prevent ordinary code review or private draft publication.
3. **Qualify the actual identity provider and Studio path.** Verify the issued access-token profile, resource audience, client IDs, subject mapping, refresh/expiry, key rotation and outage/revocation behavior. Test the actual host/network route from Studio. An OAuth label or Google-signed ID token is not evidence of compatibility.
4. **Qualify one bounded source product.** Select the metric/owner/known answer and approved project/dataset/location. Retain reviewed models and bindings; verify least privilege, types/timezone, row-scope isolation, row/watermark coherence, billing ceilings and source lifetime/cancellation or safe reservation reconciliation. Obtain scoped live-query/spending authorization separately. For Domo, qualify the direct REST adapter against exact approved datasets and authoritative business definitions. Shared integration identity does not inherit each employee's Domo permissions; delegated access remains unsupported.
5. **Qualify operations and the release.** Approve the full stored-data inventory/retention, encrypted backups and restore drill; measure workload/queue/resource behavior; establish worker/capacity/storage alerts, incident ownership, rollback and access-revocation procedures. Review and approve the exact deployment candidate before creating or changing infrastructure.

## Infrastructure to prepare

The target is GKE. Use the organization's approved cluster/project/region and networking standards rather than introducing an unrelated control plane. The [GKE reference directory](../../infra/gke/README.md) contains the bounded packaging for this foundation; it is not a turnkey cluster or live-source installation.

| Resource | Required boundary |
| --- | --- |
| GKE namespace and separate workload identities | API, worker, retention, owner-only bootstrap and consumer/fixture execution remain separate. Fixed initial capacity until measured; no automatic Cube replica scaling with its current in-memory queue/cache. |
| Private Cloud SQL PostgreSQL registry | Compatible PostgreSQL version, approved private route, certificate/hostname validation, four distinct non-elevated roles, connection/storage alerts and backups. Test the existing migrations without weakening their privilege checks. |
| Private consumer and fixture Cube services | Separate reviewed bundle identities, signing keys, source permissions and test data. No developer, SQL or direct warehouse interfaces exposed. The new pilot endpoint contract requires TLS; synthetic Compose HTTP endpoints cannot be copied unchanged. |
| HTTPS ingress and stable DNS | Exact UI/REST/MCP origin, certificate lifecycle, body/time/rate limits, protected-resource discovery routing and no direct database/Cube ports. Hosted Studio requires a verified reachable route; internal-only ingress is not automatically reachable from it. |
| Artifact Registry | Reviewed immutable application and supporting images, exact source/image provenance, separate build and deploy privileges. Image references must be resolved and scanned before rollout. |
| Secret Manager and file delivery | Per-workload readers, minimum mounted material, no owner credential in API/worker, tested non-root permissions and explicit rotation/revocation procedure. |
| Network policy and egress controls | Default-deny boundaries with approved DNS, IdP, registry and source destinations. NetworkPolicy by itself is not an FQDN-aware egress control or proof of observed no-egress behavior. |
| Logging, Monitoring and protected backup storage | Safe counts/durations/error codes, named alert responders, storage/backup alarms, retained recovery evidence and approved data retention. Never collect bearer headers, SQL, result rows or raw identity payloads. |

GKE supplies Kubernetes-level networking/customization control. Workload Identity Federation grants workload-specific Google API access without service-account key files; enabling it does not itself grant IAM access. It does not replace end-user OAuth or Studio delegation. [GKE and Cloud Run comparison](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/gke-and-cloud-run), [GKE workload identity](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/workload-identity).

Cloud SQL default built-in user creation can grant `cloudsqlsuperuser`; the migrations reject elevated attributes and explicit memberships. Use an approved minimal-role creation procedure and verify the actual role catalog. Private networking, TLS/server identity and backup/PITR are separate checks. [Cloud SQL user creation](https://docs.cloud.google.com/sql/docs/postgres/create-manage-users), [private IP options](https://docs.cloud.google.com/sql/docs/postgres/private-ip), [backups](https://docs.cloud.google.com/sql/docs/postgres/backup-recovery/backups), [point-in-time recovery](https://docs.cloud.google.com/sql/docs/postgres/backup-recovery/pitr).

GKE's Secret Manager CSI add-on supports file mounts and rotation, but its supported features do not establish compatibility with this application's strict file readers. The selected reference uses a bounded init snapshot into regular files in memory-backed storage, then read-only runtime mounts. Config/key/CA changes require a controlled workload rollout. Do not claim hot rotation; optional hash-file service keys remain off by default, and file-key revocation requires the documented rollout until a separately tested refresh path exists. Database principal/grant revocation remains freshly checked. [Managed Secret Manager CSI component](https://docs.cloud.google.com/secret-manager/docs/secret-manager-managed-csi-component).

The registry CA config applies only to PostgreSQL. Cube and JWKS HTTPS clients currently use the image's built-in trust roots with ambient proxy/trust configuration disabled. Their endpoints must present certificates trusted by that image. An internal private CA or mesh-specific TLS path requires a separately reviewed explicit trust configuration; do not disable certificate verification to make it connect.

## Credential and identity handoff

Provide identifiers, scopes and approved secret references through the operator process. No secret values belong in this document, source control or chat.

| Material | Consumer and rule |
| --- | --- |
| Issuer, JWKS URL, resource audience, allowed OAuth client IDs/algorithms | Public configuration for the existing strict RFC 9068 verifier; one canonical protected resource for REST/MCP. No request/token-selected key URLs. |
| User subject mapping and grants | Owner-approved `(issuer, subject)` → principal ID → one policy/tenant scope. JWT group/role claims do not grant access. The foundation's north/south groups are synthetic rehearsal scopes, not the organization's final entitlement model. |
| Studio delegated token | Must identify the verified user separately from the Studio client, and match the configured access-token profile/audience. Validate actual discovery/callback/refresh behavior. |
| Optional Studio service key | Existing hash-only mapping to an explicit fixed service principal. No user impersonation. Review every user's effective shared scope; key creation/storage and persistent access require separate approval. |
| Registry application secret | API, worker and retention; existing application grants, no owner/admin privileges. |
| Registry owner secret | Explicit one-shot migration/provisioning workload only. Never mounted into serving workloads. |
| Registry audit-retention secret | Retention workload only, alongside its required application-role connection. |
| Consumer and fixture Cube signing keys | Separate keys and separate endpoints; gateway/worker need the appropriate execution paths. Fixture source privileges remain isolated. |
| Audit HMAC and cursor keys | Gateway services only. HMAC parameter hashing is not a tamper-proof audit chain. Key rotation and backup recovery need explicit handling. |
| Registry CA certificate | Mounted trust material, with certificate/hostname validation enforced. CA rotation requires coordinated validation/rollout. |
| Future BigQuery identity | Prefer proven GKE workload identity for the actual self-hosted driver. Restrict job creation to the approved billing/query project and data reads to approved datasets/views. No write/export/pre-aggregation permission by default. This foundation does not activate it. |

Identity-provider selection is still required. In particular, Google Sign-In/IAP/infrastructure ID tokens must not be treated as RFC 9068 user access tokens. Cloud Run's separate `X-Serverless-Authorization` mechanism illustrates that infrastructure and application identities are distinct; it is not the selected GKE design or a verified Studio integration. [Google service-to-service authentication](https://docs.cloud.google.com/run/docs/authenticating/service-to-service).

## Performance: measured versus proposed

The source-level/backend pass counts establish correctness coverage, not capacity. Current limits include 1,000 rows, 1 MiB results, 200 rows/page, two running executions per principal, ten deployment-wide, five seconds of synchronous wait and a 120-second execution deadline. The shipped worker is serial, so ten is an admission ceiling rather than demonstrated throughput. Registry transactions currently use `NullPool`; Cube has per-scope state/pools and an in-memory queue/cache.

Proposed first qualification workload: one read-only aggregate product, 5–10 named users, two access profiles plus a narrower same-tenant profile, typical 7/30-day queries, bounded daily volume and a staffed pilot window. These are planning targets, not commitments or implemented rate limits:

- Catalog/describe/result page p95 under one second; submission response p95 under six seconds, including the existing five-second wait.
- Accepted-to-start p95 under two seconds at nominal load; typical completed answer p95 under ten seconds and p99 under thirty seconds.
- Zero unauthorized rows/metadata, silent truncation, deadline extension or unsafe source-slot release.
- Cold/warm runs, 1/5/10/30 client loads and failure/revocation/restart cases, then a staffed soak. Record source/image/model/workload identity, latency percentiles, queue age, error counts, CPU/RSS, database connections and source reservations.

Measure before tuning worker parallelism, pooling or caches. Unknown upstream attempts retain capacity intentionally; two uncertain attempts can strand one principal and ten can exhaust the deployment. Current readiness checks metadata, not worker progress or quarantined capacity. Source reconciliation and safe operational visibility are P0 before serving real data; force-releasing a reservation is not an acceptable workaround.

## UI/UX and browser acceptance

The catalog, draft inbox, editor/test/review flow, typed consumer filters/sorts and retry-safe execution exist. Preserve the sharp neutral/orange UI rather than starting a redesign.

Before organization users rely on it:

- Implement the selected IdP's browser sign-in/renewal/logout flow, with verified account context and a clear distinction between user and service identity. Current UI accepts a pasted bearer and has synthetic-only messaging.
- Keep rights policy-scoped. Every signed-in account currently sees authoring navigation; hiding a link cannot replace server checks, and a global role inferred from JWT claims would be wrong.
- Run real-browser desktop/narrow/zoom/keyboard checks across author → publisher → consumer, account switch and late responses, revocation, expired credentials, save/publication conflicts, lost execution responses, stale/unknown freshness and result paging.
- Improve field-local validation, focused summaries and unsaved-work logout behavior as bounded fixes. Do not delay forced revocation clearing to preserve edits.

Component tests and static screenshots cannot establish a working composed journey, SSO, accessibility conformance or live-source accuracy.

## Retention and privacy decision before real data

The existing sweep removes expired result rows and sufficiently old unreferenced redacted audits. Source group/session-referenced allow audits remain retained, including after approval revocation. Immutable owner/publication/source records and their referenced audits have no general erasure path in this slice. It does **not** remove canonical query inputs, principal/authorization snapshots, prepared queries or idempotency/execution history, which have immutable-table protections. Backups retain historical copies too. “24-hour results / 30-day unreferenced audits” is therefore not a complete retention policy.

Approve every stored category, pilot duration and volume/storage envelope before allowing sensitive inputs, or implement a separately reviewed archive/deletion design. Preserve evidence integrity; do not bypass immutable-table triggers with ad hoc cleanup. This is an organization privacy gate even if scalable lifecycle mechanics follow later.

## Remaining decisions

GCP/GKE is selected. Before any infrastructure action or live activation, still choose the approved project/cluster/region/network route, identity provider/client configuration, first product/owner/authoritative answer, allowed users and entitlement mapping, data retention/support owner, source budget and recovery objectives. No checklist entry or example file authorizes resource creation, credential grants, paid queries, merge or rollout.
