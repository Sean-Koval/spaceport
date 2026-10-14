# GCP/GKE pilot reference, deliberately inactive

This directory packages the organization runtime foundation for a future **GKE
reference target**. It is code and review material, not a deployment command, a
turnkey environment, cloud acceptance evidence, or permission to provision
anything. No cluster, namespace, IAM binding, service account, OAuth grant,
credential, database, certificate, live query, paid resource, or public endpoint
is created by this work.

All `*.reference.yaml` files use JSON-form YAML so they remain inspectable with
Python's standard JSON parser. They intentionally contain invalid `REPLACE_*`
organization names and image-digest placeholders. Deployments have zero replicas;
both owner Jobs are suspended. Do not bulk-apply this directory. Substituting a
namespace or image is not approval to start a workload.

## What is included

- Three independent Deployments: API, execution worker, and retention process.
- Two separate, suspended owner Jobs: migrate first; provision a reviewed identity
  mapping separately. Neither runs as an application startup hook. Jobs have no
  automatic retries and a bounded deadline; partial-state recovery needs review.
- A private `ClusterIP` API Service. No `Ingress`, public `LoadBalancer`, Gateway,
  external IP, frontend Deployment, or public DNS automation is included.
- GKE Secret Manager CSI references for four distinct workload identities: API,
  worker, retention, and bootstrap. These reference operator-owned Kubernetes
  service accounts; no service account or IAM objects are supplied.
- A fail-closed NetworkPolicy baseline. It intentionally permits no traffic until
  operators approve precise ingress and egress rules for their actual topology.
- A conditional NGINX TLS configuration, including the **exact** discovery route
  `/.well-known/oauth-protected-resource/mcp`. It is not an edge deployment.
- Fictional configuration and user-only identity examples. They are never loaded
  implicitly, contain no credentials, and do not assert an identity-provider
  integration. Configure `GATEWAY_PILOT_CONFIG_FILE` explicitly.

## Mandatory operator prerequisites

Before any separately approved deployment, record the following decisions and
verify the exact, reviewed image and final manifests in the target environment.
These prerequisites are not actions performed or authorized by this reference.

1. **GKE and policy.** Select the project, namespace, location, cluster mode,
   supported GKE version, node isolation, workload identity, managed Secret
   Manager add-on, network-policy enforcement, artifact registry, quotas and cost
   owner. For Standard, use eligible Workload Identity-enabled nodes; the
   documented metadata-server node selector is not valid on Autopilot. No node
   selector is guessed here. Confirm security admission permits UID/GID 65532,
   non-root init containers, memory `emptyDir`, and read-only root filesystems.
2. **Identity provider.** Review the exact HTTPS issuer, audience (the canonical
   public origin plus `/mcp`), explicit JWKS URL, allowed client IDs and asymmetric
   algorithms. The existing verifier requires RFC 9068 access tokens, including
   its token-type and client checks. A generic OIDC login is not sufficient.
   Confirm trusted issuer/JWKS DNS, reachability and the pinned image's default
   HTTPS trust. There is no arbitrary JWKS discovery or forwarded-header identity.
3. **Registry.** Supply the database hostname matching its server certificate,
   port, database and actual CA bundle. Connections use `verify-full`; there is
   no insecure-mode override. The database administrator must separately create
   the fixed roles `gateway_owner`, `gateway_app`, `gateway_audit_retention`, and
   `gateway_source_reader` with the existing migration privilege contract.
   The owner is not a superuser, role creator, database creator or RLS bypass
   role. Existing migrations validate separation; the Job does not create roles
   or credentials. Review database ownership, public schema access, backup,
   restoration, audit retention, deletion policy and private connectivity.
   Serving app and audit-retention connections explicitly set four-second
   statement and two-second lock timeouts, independent of role defaults. Owner
   migration/provisioning has a separate operator-reviewed execution budget.
4. **Synthetic Cube/source topology.** Provide two independent HTTPS services,
   one for the reviewed consumer bundle and one for the fixture-test bundle.
   They must preserve the checked-in `cube/cube.js`, `security.js`, their exact
   model directories/digests and endpoint/context bindings. Consumer digest is
   `228090dcbeaf8416a1e8a14d4e17c2873f9963ec3346d728650308ee5684d8ba`; fixture digest
   is `8c3e9a8b030fb495bbdbbec19061df43c17d508ffadaf390a025d19120e43f17`.
   Each endpoint needs its distinct matching gateway signing secret. The pinned
   Cube driver uses source host `demo-db`, database `gateway_demo`, `demo_reader`
   for consumer and `fixture_reader` for tests, with their independent mounted
   passwords and the reviewed `demo_source` / `test_source` schemas, fixtures and
   source-load watermark. Operator preparation must preserve that name resolution,
   least privilege and isolation, or obtain a separately reviewed new bundle.
   The pinned driver does not configure a custom source TLS transport. An
   organization requiring that connection to use TLS needs a separately reviewed
   source/TLS change and new bundle evidence. **This directory does not supply
   runnable Cube TLS proxies, Cube Deployments, synthetic PostgreSQL, seed Jobs,
   source credentials, or source networking.** Do not point these endpoints at
   live BigQuery, Domo or another model to fill that gap.
5. **HTTPS trust.** Cube and JWKS use their existing HTTP clients with environment
   proxy/trust overrides disabled. Their certificates must validate against the
   application image's default trust store. `registry-ca.pem` configures only
   PostgreSQL, not Cube or JWKS. Private/self-signed Cube or issuer certificates
   need a separately reviewed trust-store/client change. Endpoint separation is
   also an operator trust decision; distinct URL strings do not prove separate
   upstreams, datasets, keys or authorization boundaries.
6. **Secret delivery and privileges.** Pre-existing KSA workload identities need
   only access to their named secrets, never project-wide owner access. Replace
   each Secret Manager version placeholder with a reviewed numeric version, not
   `latest`. No secret synchronization to Kubernetes Secrets or secret-valued
   environment variable is configured. API and worker may read the five app
   secrets; retention only its two registry secrets; bootstrap only the owner
   password. Input bytes must match the existing runtime format (43 URL-safe
   ASCII secret characters, optionally newline). Supplying/pinning values is an
   operator action; this repository does not generate them.
7. **Immutable configuration.** Create an operator-reviewed, versioned immutable
   ConfigMap with precisely `pilot.json` and `registry-ca.pem` keys. Replace the
   config reference consistently in every workload. The CA must be a real valid
   CA PEM, not a placeholder. Provisioning additionally references a distinct,
   reviewed ConfigMap with `identities.json`. Real subject identifiers require
   appropriate read permissions and review. The example has three separate
   fictional users, one tenant grant each, and separate author/publisher roles.
8. **Traffic and TLS edge.** Approve the canonical public hostname, certificate,
   renewal/delivery, frontend artifact and any public exposure independently.
   Update that hostname together in config, probe Host headers and edge config.
   Allow only the approved edge to API port 8000, needed DNS, registry, JWKS and
   distinct Cube destinations. Kubernetes NetworkPolicy alone is not a portable
   DNS-name egress allowlist; do not replace precise policy with unrestricted
   internet/443 access. Account for actual GKE policy and metadata-server behavior.
   The optional edge terminates TLS on 8443 and forwards HTTP to the private API
   Service. It does **not** provide hop-by-hop mTLS; if that is required, an
   approved mesh/proxy design is still missing. No such traffic is opened here.

The managed add-on authenticates with the Pod workload identity. Disabling
`automountServiceAccountToken` prevents a generic API token file in containers;
it does not revoke the Pod's workload identity or make metadata APIs inaccessible.
Keep per-role IAM scope and approved network boundaries even though only init
mounts the CSI files. Kubernetes supports CSI token delivery through the kubelet's
TokenRequest mechanism rather than a general-purpose token mount. Verify the
chosen managed-driver/GKE behavior and UID readability with an approved synthetic
mount test before activation. Do not silently enable broad token automounting,
create credentials, or grant IAM to make a failing mount succeed.

## Projection snapshot contract

The strict runtime intentionally refuses symlinked config and secret files.
ConfigMap and CSI projections can use Kubernetes' AtomicWriter symlink layout,
so mounting them directly at runtime paths is not this reference's contract.

The init command `python -m gateway.pilot_mounts <profile>` reads only fixed
allowlisted files under `/mnt/pilot/{config,secrets,identities,auth}`. It accepts
regular files or the exact `name -> ..data/name` and `..data -> ..generation`
projection shape. No arbitrary symlink chain is resolved. Real opens use
`O_NOFOLLOW`, pin directory descriptors, and reject non-regular files, hard links,
group/world-writable payloads, oversized input, observed mutation and generation
replacement. Each projection group is read from one stable generation. Different
mount groups are not one atomic transaction: use versioned immutable config and
pinned secret versions for a coherent release.

Init runs as UID/GID 65532, without root or extra capabilities. It creates bounded
private regular files (0600) and directories (0700) under a memory-backed volume's
`runtime` directory. Runtime containers mount only `runtime/config`,
`runtime/secrets`, and any explicitly requested additional directory read-only.
The original CSI/ConfigMap inputs are not mounted in runtime containers. Runtime
validation still checks the actual JSON, secrets, CA and identity contents; copying
bytes never makes them trusted or valid.

Limits: config 64 KiB, registry CA 256 KiB (deliberately below the loader's 1 MiB
limit), each secret 128 bytes, identity map 256 KiB, optional credential hash file
1 MiB. All must be nonempty. Source mounts and their ancestry are operator-trusted
and read-only; this helper does not defend against a privileged host administrator
replacing mounts. Memory files are not guaranteed secure erasure on Pod teardown.
Failed ordinary writes are cleaned up; an interrupted init can leave a partial
snapshot. Retrying against an existing snapshot refuses to overwrite it. Replace
that Pod to obtain a new `emptyDir`, rather than weakening the checks.

**Rotation requires replacement Pods.** CSI refresh, ConfigMap changes and changing
Secret Manager `latest` do not change the runtime snapshot. Deploy new versioned
references, complete the API/worker/retention rollout, verify use of the new
versions without printing secret bytes, then retire old material according to its
rotation plan. A rollback can restore old keys; review that explicitly. Optional
file-based service credential revocation needs completed rollout of every reader;
current registry principal/grant revocation remains the immediate authorization
control. Never claim this snapshot provides hot rotation.

### Optional service credentials

Default examples deliberately omit `/run/auth` and set `credential_hash_file` to
`null`. There are no demo bearer credentials in this profile. To use separately
approved service principals, review all of these changes together:

- Add only the existing hash-only `credential-hashes.json` to a separate trusted
  projection mounted at `/mnt/pilot/auth` in API, worker and provision init.
- Add `--include-service-hashes` to those three init commands and mount snapshot
  subPath `runtime/auth` at `/run/auth`, read-only, in those runtime containers.
- Set `credential_hash_file` to `/run/auth/credential-hashes.json`; add exact service
  mappings with explicit service client IDs and approved tenant/role grants.
- Permit the provision identity to read that hash file if the delivery mechanism
  requires it. Do not give it application/Cube keys. Retention and migrate never
  receive hashes. Hash updates and revocations require the rollout described above.

No command here generates plaintext credentials, credential hashes or identity
grants from JWT group claims. Browser usage remains manual bearer entry; there is
no authorization-code/PKCE browser login, token refresh or vendor SSO claim.

## Serving and acceptance gates

API startup, liveness and Kubernetes readiness probes all use `/healthz` with the
explicit canonical Host header. This is **process-only routing availability**, not
database/source/data readiness. It lets authorized users reach authoring before a
first product has been published. Using `/readyz` as the initial Service readiness
probe would remove all API endpoints and block first publication.

The reference Pod termination grace is 140 seconds, leaving a cushion beyond the
125-second API drain plus bounded owned-resource cleanup. A grace setting is not
measured shutdown evidence; validate cancellation, database timeout and release
behavior on the actual topology without force-releasing source reservations.

`/readyz` remains a separate domain/release diagnostic. It can correctly fail
until migrations, provisioning, genuine fixture tests and a separately authorized
first publish have occurred, and until current grants, registry state and Cube metadata checks are healthy. Do not relabel a process-ready Pod
as data-ready. No automatic publish, manufactured passing evidence or readiness
bypass is included. Worker/retention process existence is not proof of progress;
external operation, queue, audit and failure monitoring remain operator work.

Before activating even a private pilot, require reviewed organization values,
approved upstream prerequisites, local structural tests, target Kubernetes schema
validation, approved admission/CSI mount tests, synthetic OIDC negative-path
checks, actual TLS verification, least-privilege database checks, real fixture
execution, source-watermark/freshness checks, retention checks, revocation/rotation tests and exact proxy discovery
routing. Record each result and the exact image/config/secret-version identities.
Public ingress and production acceptance require separate decisions.

Local tests are limited to Python filesystem behavior and inspectable reference
contracts:

```sh
uv run --locked pytest tests/unit/test_pilot_mounts.py tests/unit/test_pilot_gke.py
```

Passing them does not prove GKE schema validity, provider authentication, live
permissions, TLS infrastructure, resource sizing, network rules, frontend behavior,
performance, or a successful organization deployment.

## Primary references

Reviewed for this reference on 2026-10-14; verify applicability to the selected
cluster and driver version before any approved activation.

- [GKE managed Secret Manager add-on](https://docs.cloud.google.com/secret-manager/docs/secret-manager-managed-csi-component): managed CSI driver name, `provider: gke`, named resources/versions, workload identity and Standard/Autopilot distinction.
- [Kubernetes projected volumes](https://kubernetes.io/docs/concepts/storage/projected-volumes/): projected sources and update behavior.
- [Kubernetes AtomicWriter implementation](https://github.com/kubernetes/kubernetes/blob/master/pkg/volume/util/atomic_writer.go): constrained projection layout underlying the snapshot reader.
- [Secrets Store CSI AtomicWriter implementation](https://github.com/kubernetes-sigs/secrets-store-csi-driver/blob/main/pkg/util/fileutil/atomic_writer.go): corresponding CSI layout; this is not a claim that every driver/version uses it.
- [Kubernetes CSIDriver API](https://kubernetes.io/docs/reference/kubernetes-api/storage/csi-driver-v1/): CSI TokenRequest delivery is distinct from mounting a generic API token into the application.
