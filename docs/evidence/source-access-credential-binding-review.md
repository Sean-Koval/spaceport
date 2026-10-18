# Source credential binding design delta

Date: 15 October 2026. Additive design delta to the frozen source-access specification and transaction plan. The coordinator approved this direction and independent review accepted it for implementation review subject to the conditions below. This is not implementation qualification or organization activation approval.

## Exact protected mapping

Each `SourceApprovalPin` requires `credential_binding_digest`. Compute SHA-256 of `b"spaceport-source-credential-binding-v1\0"` followed by RFC 8785 canonical JSON of the applicable closed projection:

- BigQuery: `provider`, `mode` (`gke-workload-identity`), `expected_principal`.
- Domo: `provider`, `mode` (`oauth-client-credentials`), `client_id_sha256`, `expected_customer`, `expected_user_id`, `expected_role`, `client_id_version`, `client_secret_version`.

Domo source descriptor authentication adds optional immutable `client_id_version` and `client_secret_version` references. Both must be present for a serving approval, owner import, startup or serving. The optional absent values must be omitted from serialization so existing retained descriptor bytes/digests, old held work and observation paths remain compatible. Offline templates may omit them; no placeholder or invented provider version is generated.

A version reference matches exactly:

`^projects/(?:[a-z][a-z0-9-]{4,28}[a-z0-9]|[1-9][0-9]{0,19})/secrets/[A-Za-z0-9_-]{1,255}/versions/[1-9][0-9]{0,18}$`

Maximum length: 512 ASCII characters. Zero, leading-zero numeric project/version identifiers, aliases, `latest`, whitespace, URL encodings, queries and fragments are invalid. The two references must be explicit; credential values are never read or hashed to construct this projection.

The complete existing source descriptor separately pins the mounted credential paths and their version mapping. The protected `source_profiles` content/digest lets SQL independently derive the credential projection and compare it with approval pins. The existing physical identity projection remains distinct and excludes mounted path aliases and the new version-reference fields. Python and SQL must agree on the canonical credential digest. Missing Domo versions deny rather than producing a null-version serving digest.

## Trust and sequencing

Every configured package is statically checked against its exact approval document before provider credentials, tokens, clients or source I/O. Registry credentials are necessarily read to check protected active approval state; this is not provider access. Registry readiness uses current database time and checks all configured packages, without source queries.

A configuration reference or digest is not proof of the actual bytes mounted into a container, credential provenance, current provider identity, source permissions or non-personal source content. Operators must ensure immutable mounts resolve the named versions and must separately qualify live runtime identity and least-privilege permissions before organization activation. Domo expected identity is an approved expectation, not a substitute for qualification.

BigQuery ADC/workload identity does not have a fabricated credential secret version. Its configured expected principal is pinned, and actual runtime identity must still be independently verified during authorized live qualification. Internal Cube signing-key rotation is separate from upstream source identity. No credential values enter approval content, diagnostics, logs, receipts or source identity digests.

No production configuration, cloud resource, IAM grant, live credential read, query, dispatch, deployment or merge is performed by this design change.
