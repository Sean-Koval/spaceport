# GCS artifacts: offline contract only

`gateway/artifacts/gcs.py` is an executable, internal exact-object reader exercised
against synthetic injected transports. It has no HTTP/SDK implementation, credential
loader, runtime registration, REST route, MCP tool, public schema, or live-source
activation switch. It adds no dependency.

A passing fixture is **not** evidence of working GCS access, deployed policy,
source-load lineage, provider cancellation, storage/egress charges, or readiness.
Do not enable live access on the strength of these tests.

## Retained operator authority

Load `GcsArtifactDescriptor` values from reviewed operator configuration into
`StaticArtifactCatalog`. The catalog snapshots canonical bytes, rejects duplicate
artifact/revision keys, has at most 1,024 entries, and returns independently
validated copies. Its mapping and ordinary attribute assignment are immutable.
Descriptors are immutable strict models; unknown fields and type coercion fail.

Each descriptor pins:

- Artifact ID and positive revision
- Associated product ID, semantic version and authoritative content digest
- Owner, classification, policy ID and tenant ID
- Exact bucket and object name, generation and metageneration
- Expected byte length, lowercase SHA-256 and canonical media type
- Optional source-load checkpoint with a reviewed evidence digest, and optional
  maximum source age

The descriptor digest binds every field. A read supplies artifact ID, revision and
that exact digest; changed or absent authority fails before calling the gate or
transport. Bucket names use a conservative lowercase 3–63-character subset.
Object names are bounded ASCII exact paths; empty, dot and parent components,
trailing slashes, percent-encoding, URL query syntax and globs are rejected.
Generation/metageneration are canonical positive decimal strings of at most
20 digits, not numbers. There is no latest-object resolution, prefix, URL, custom
header or signed-capability field.

## Mandatory fresh authorization

Construct `GcsArtifactReader` with both an explicit transport and an async
`authorize(descriptor, principal_id)` callback. Neither has a default. The callback
receives a private descriptor snapshot. `principal_id` must come only from verified
server identity context, never a client-supplied claim. The callback must verify
that exact identity context, independently resolve current policy and audit the
authorization decision. There is no public artifact authorization adapter. It returns a strict `ArtifactAuthorization` that
binds the descriptor digest, principal, tenant, policy ID/revision, scope digest,
byte cap, timeout, and optional freshness requirement.

The reader checks this grant before any source metadata/content operation. It
calls the gate again after closing the complete content stream and before releasing
bytes. The second grant must equal the first: changes to principal, tenant, policy,
resource, scope or limits discard the buffered result. Repeated reads always
reauthorize; there is no authorization or result cache. Exceptions from either the
gate or transport are redacted, including upstream `GatewayError` text.

The fake gate is only a contract test. Product-policy inheritance versus a separate
artifact-read action, authenticated caller derivation, durable audit behavior and
actual policy wiring remain a separate reviewed integration decision. Supplying a
permissive callback does not make an integration authorized.

## Transport contract and read sequence

`GcsObjectTransport` offers only two async context-manager operations:

1. `open_metadata(GcsReadRequest)`
2. `open_content(GcsReadRequest)`

Both receive exactly the retained bucket/object and generation, together with
`if_generation_match` and `if_metageneration_match`. A transport must apply **all**
preconditions at the source. Each operation is invoked at most once. An eventual
implementation must not retry internally, follow redirects, issue ranges, silently
fall back to a current generation, decompress, transcode, or create credentials.
There is no list, write, delete, extract, temporary-file or URL-sharing operation.

Each response provides `GcsResponseHead` and async `read(maximum_bytes) -> bytes`.
Reads must return raw undecoded bytes, at most the requested amount, with an empty
byte string only at EOF. Context exit must close the response. The request carries
the remaining timeout and logical maximum response size. The reader requests at
most 64 KiB per chunk and probes at most **one byte beyond the logical limit** to
identify overflow. A future transport must preserve that probe, rather than report
an artificial EOF at the cap. A transport returning a larger chunk is rejected.

The synthetic metadata envelope is strict JSON with exactly `bucket`,
`object_name`, `generation`, `metageneration`, `size` and `media_type`; duplicate keys,
nonfinite numbers, malformed JSON and extra fields fail. The reader bounds the
raw metadata stream to 64 KiB and validates the envelope against the descriptor.
This envelope is an internal contract, not a claim that the raw GCS JSON API or an
SDK already implements this interface. Any future provider mapping needs separate
review and evidence that its raw response is bounded before projection/parsing.

The content stream is bounded by the pinned expected size, the current policy cap,
and the global 1 MiB maximum. Zero-length artifacts are supported. Both responses
must be HTTP 200 with the exact media type and length. Redirect/partial responses,
HTTP 412, a content range, any compression other than identity, and reported
transcoding are rejected. Content response headers must independently report the
pinned generation and metageneration. Metadata versions, content versions,
expected size and computed SHA-256 must all match. Unknown or unavailable version
headers are not permission to skip a check.

Only a complete `ArtifactBytes` result is released, after verification and final
authorization. It contains the bounded bytes, artifact identity, descriptor digest,
media type, SHA-256 and reviewed source checkpoint. It contains no URL or source
location. Content is excluded from the model’s `repr` and `str` to reduce accidental
blob disclosure in logs; explicit serialization still contains the content and must
not be logged. No bytes are yielded to callers while a stream is in progress.

## Time and freshness

One monotonic deadline starts before catalog resolution and the first gate call.
It covers authorization, metadata/content reads, response closure, validation and
release authorization. The maximum is 120 seconds; caller and current policy limits
can only shorten it. A policy timeout is measured from the original start, so time
spent obtaining that policy consumes its budget. Expiry/cancellation releases no
result. Explicit elapsed-time checks also refuse late results from a callback that
suppresses cancellation.

The injected callback and transport are trusted cooperative async implementations:
Python cannot forcibly terminate synchronous blocking code or guarantee provider
cancellation. The local timeout is not proof that a remote operation stopped or
that charges ended. That requires separate source-specific evidence.

Freshness is evaluated before source I/O and again before release, using the
stricter descriptor/current-policy maximum age. Required missing or future-dated
source-load evidence fails closed, and stale evidence is rejected. Fetch time,
object update time, generation time and cache state never become a source-load
watermark. When no freshness is required and no checkpoint exists, the returned
checkpoint stays null.

## Reproducible offline evidence

Synthetic input: `fixtures/adapter/gcs.json`.

Run:

```sh
.venv/bin/python -m pytest tests/contract/test_gcs_artifacts.py -q
```

Tests deny outbound socket connection entry points. Coverage includes strict and
immutable configuration, missing mandatory dependencies, unknown/digest-mismatched
references, exact generation/metageneration on both operations, source drift,
redirect/range/compression rejection, malformed/oversized metadata, incorrect
content type/size/hash, max-plus-one overflow, empty/1 MiB boundaries, complete
buffer release, fresh pre/post authorization, mid-read policy changes, required
freshness, combined deadline accounting, cancellation and redacted failures.

Unproven and deliberately excluded: real GCS calls, credentials/IAM, storage class,
location, egress/retrieval/request cost, provider-specific response mapping, deployed
audit/authorization, signed URL access, and production API/MCP integration. Actual
source access and cost need separate approval and source-specific qualification.
