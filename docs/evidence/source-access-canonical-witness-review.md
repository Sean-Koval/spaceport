# Source access canonical byte witnesses

Date: 15 October 2026. Additive implementation detail approved by the coordinator for the frozen source-access transaction plan. This is a bounded storage and equality seam, not a new authorization source or qualification of execution/provider paths.

## Representation and independent bindings

Reviewed manifests and normalized queries may contain RFC 8785 floating-point numbers. PostgreSQL JSONB serialization is not an ECMAScript number serializer. Migration 0007 therefore does not use its approval-schema canonical serializer to regenerate manifest or query bytes.

- `product_versions.manifest_canonical` is nullable TEXT for historical rows. New application writes use `canonical_bytes(manifest.model_dump(mode='python', exclude={'content_digest'})).decode()`. SQL compares parsed JSON with `product_versions.manifest - 'content_digest'` and SHA-256 of the exact UTF-8 text with the immutable version content digest. Reviewed acceptance separately requires that digest to equal the independently installed approval's exact manifest digest.
- `executions.query_canonical` is nullable TEXT for historical rows. New reviewed acceptance requires `canonical_bytes(spec.canonical_query).decode()`. SQL compares parsed JSON with the execution's normalized query and SHA-256 of the exact text with its immutable canonical hash. Query inputs and filters must also pass the independently reviewed domains and complete manifest validation. A query hash never substitutes for an approved manifest/package hash.
- The protected approval digest remains its existing domain-separated `spaceport-source-approval-v1` digest. Package, credential-binding and projection digests retain their distinct existing formulas. This seam does not change any of those domains.

Both witnesses are bounded to 65,536 UTF-8 bytes. Duplicate object keys are rejected recursively before JSONB conversion, with bounded depth and collections. Missing text, mismatched JSON, mismatched hash and malformed text deny. SQL only proves duplicate-free JSON equality and exact protected byte-hash equality; it deliberately does not claim to independently implement RFC 8785 number formatting. Application RFC 8785 bytes and the independently installed manifest hash are the canonical representation authority. Equivalent JSON with different raw bytes cannot match that protected manifest digest.

Historical NULL witnesses and generation-zero executions are not backfilled or upgraded into reviewed authority. The acceptance trigger alone derives generation one for new reviewed bindings, and a deferred constraint requires the exact immutable companion before commit.

## Protected query semantics

Direct app-role execution insertion is also subject to the normalized query's closed shape, exact product/version, required and defaulted inputs, manifest scalar type/enum checks, reviewed value domains, strictly positive date interval, manifest and current-policy date limit, filter operators/cardinality/unique values, and sort fields/directions/uniqueness/cardinality. A matching hash alone grants nothing.

## Finalization and durable denial contract

`SourceAccessService.finalize` consumes one to 200 private requests only after the caller has acquired the complete capacity/product/draft/policy/version/execution/source prefix and flushed its earlier work. It independently reconstructs each protected request, locks unique approval-state rows in sorted order, checks all rows against one fresh database-clock instant after the final lock wait, writes allow audits in the same transaction, and repeats the final check after the audit flush. Returned receipts are tied to that UoW and transaction.

A trusted authority denial rolls back the caller transaction before committing exactly one safe target/value-free deny event in a separate transaction. Only successful committed denial returns the supplied correlation ID. Invalid private objects have no trusted actor and return a fresh generic error without manufacturing a denial receipt. An unavailable denial audit returns a fresh unavailable error; callers must not interpret the supplied correlation ID as a committed receipt in that case.

`load_execution_access` reads immutable accepted pins only. It grants no action, checks no replacement approval, and does not require a result audience merely to read an execute-only owner's deadline. Every use still requires the appropriate retained-owner resolution and final gate.

## Verification scope

The checkpoint tests include RFC 8785 numeric byte/hash examples, duplicate-key and hash failures, genuine app-role valid acceptance controls, direct app-role invalid query acceptance, immutable companion/client pins, missing companion, replacement non-rebinding, expiry after lock wait, deferred acceptance expiry, result versus execution duration, final audit expiry rollback, exactly one correlated denial, and failed denial audit behavior. Exact test counts and frozen revision are reported with the checkpoint, after a fresh run.

Fixture invocation/attempt companions, publication receipts and provider/lifecycle gates are subsequent checkpoints. This storage/finalizer checkpoint alone is not execution readiness or organization activation approval.

Fresh checkpoint verification on 15 October 2026: 384 focused unit tests passed; 76 genuine PostgreSQL registry/accepted-owner/finalization/shared-clock tests passed, with second-upgrade identity preservation for all 76 databases and normal cluster cleanup verified. Before the last strict-policy-shape correction, the broader run also passed 114 tests including legacy queue cases; its one setup error required an explicitly prepared private synthetic Cube stack that was not available. That blocked provider composition is not claimed as passing. Strict current-grant and policy-shape regressions were observed failing before their SQL correction, then passed in the final 76-test run.
