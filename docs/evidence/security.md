# Security acceptance matrix

Status: **BLOCKED pending current-source runtime evidence and independent review**. This names behavior and executable cases, not a complete matrix PASS. See [acceptance provenance](acceptance.md).

## Six authority profiles, nine surfaces

**A** = current explicit data scope; **D** = uniform denial; **F** = filtered/empty discovery; **own** = execution-owner-bound. Examples/provenance are fields of describe, not extra endpoints. Cache covers saved-result replay plus Cube's separate tenant/entitlement boundary.

| Profile | Search | Description | Examples | Provenance | Query | Result | Cursor | Cache/replay | Audit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Anonymous | D/401 | D/401 | D/401 | D/401 | D/401 | D/401 | D/401 | D/401 | D/401 |
| Consumer north/south | A/F | A | A | A | A | A/own | A/own | Fresh scope; no cross-principal reuse | D without audit_view |
| Author only | F | D | D | D | D | D | D | D | D without audit_view |
| Publisher only | F | D | D | D | D | D | D | D | D without audit_view |
| Operator only | F | D | D | D | D | D | D | D | D without audit_view |
| Service key + consumer grant | A/F as stored identity | A | A | A | A as service | A/own | A/own | Fresh service scope | D without audit_view |

Service is an identity kind, not a data-grant role. Operator `operate` never implies consumer, policy_admin or audit_view. Synthetic production-mode service-key tests use reserved issuer URLs without contacting them; no live OIDC/delegation or production account is exercised.

### Which surface proves each row

- Anonymous, north/south consumers, author and publisher: `tests/security/test_adversarial_matrix.py::test_actual_external_profile_metadata_query_result_and_audit_matrix`, actual composed REST/MCP and four existing demo credentials. Forged user/role/session headers accompany requests. Search/description parity and safe hidden/missing errors are checked; denied descriptions expose no examples/provenance.
- Positive consumer query/result/metadata: both profiles in `tests/e2e/test_acceptance.py::test_actual_catalog_examples_provenance_and_result_match_across_transports`; actual independent clients and same-connection isolation in `tests/e2e/test_two_clients.py`.
- Operator and service: `test_adversarial_matrix.py::test_actual_postgres_operator_and_service_authority_over_same_socket`, real migrated PostgreSQL, real hash-store authentication and REST/MCP sockets. Immutable TestRun/publication/owner results are deliberately seeded. Proves foreign first-page/cursor denial, service-client audit attribution, credential switching and immediate grant/principal revocation. Does not prove Cube completion, live fixture publication, six external demo accounts or OIDC delegation.
- Service admission returns an accepted execution with no worker in this fixture. The stored service ID must own it despite forged headers. Demo tokens are rejected in service production identity mode.
- Authorized audit view/redaction: `tests/integration/test_rest_services.py::test_real_choices_audit_and_mutation_use_independent_scopes`, `::test_real_audit_release_requires_successful_commit`; `tests/integration/test_audit.py::test_append_audit_is_redacted_in_callers_transaction`. Actual PostgreSQL services, not composed audit UI.
- Positive cursor/transport paging: `tests/integration/test_mcp_services.py::test_real_mcp_rest_catalog_result_pagination_revocation_and_identity`; `tests/integration/test_results.py::test_complete_result_paginates_200_rows_and_preserves_total`. Seeded backend results, not live Cube output.
- Author/publisher separation and current field projection: `tests/security/test_publisher_roles.py`; actual isolated fixture execution in `tests/integration/test_publishing.py`. Publish authority never substitutes for data access.

## Failure and boundary requirements

| Requirement | Test source | Evidence boundary |
| --- | --- | --- |
| Forged identity/source URL/raw SQL/member/>366-day range | New `test_actual_rest_mcp_reject_authority_members_and_date_overflow`; `tests/security/test_transport_inputs.py`, `tests/contract/test_mcp.py` | New composed cases; existing transport doubles are contract-only. |
| Grant/principal/policy revocation and withdrawal before work/completion/result/cursor | `tests/security/test_execution_reauthorization.py`, `test_policy_revocation.py`, `test_result_isolation.py`; `tests/integration/test_publishing.py::test_deprecation_warns_and_withdrawal_blocks_new_and_stored_output` | Real PostgreSQL; individual adapter doubles/seeded data explicitly identified. |
| No retained identity on same connection | `tests/e2e/test_two_clients.py::test_actual_same_connection_owner_isolation_and_no_session_authority`; new operator/service socket cases | Composed consumer case; separate real-DB socket cases. |
| Cached pages/cursors reauthorize | `test_result_isolation.py::test_every_replay_or_cursor_rechecks_live_authorization`, `::test_tampered_expired_and_foreign_cursors_are_uniformly_unavailable`, `::test_foreign_cursor_does_not_unlock_an_owned_result` | Real PostgreSQL/seeded results. |
| Cube tenant/same-principal-entitlement cache isolation | `tests/integration/test_cube_compatibility.py::test_actual_cache_isolation_both_tenant_orders_and_entitlements`; `test_cube_adapter_runtime.py::test_actual_adapter_same_principal_entitlements_do_not_mix` | Actual pinned Cube required. |
| 64-KiB request, duplicate JSON/headers, depth, query bounds | `tests/security/test_transport_inputs.py`, `tests/contract/test_mcp.py` | Parser/ASGI and socket + service-double contracts. |
| 1,000 rows / 1,048,576 complete normalized bytes | `tests/unit/test_normalization.py::test_1001_rows_fails_instead_of_partial_success`, `::test_complete_envelope_size_not_only_rows_is_counted`, `::test_exact_maximum_envelope_bytes_succeeds`, `::test_1000_unique_grain_rows_succeed` | Unit transformation proof, no oversized composed-dataset claim. |
| 366/367 days and DST calendar bounds | `tests/contract/test_cube_adapter.py::test_prepare_enforces_calendar_range`, `::test_dst_range_uses_local_midnights`; `test_cube_compatibility.py::test_half_open_dst_boundary_and_empty_result`; new actual overflow case | Contract plus real Cube response and composed denial. |
| Two principal / ten deployment source attempts | `tests/integration/test_execution_queue.py::test_three_worker_processes_enforce_two_principal_ten_deployment` and reservation/fencing tests | Real PostgreSQL/worker processes, separately bounded source fixtures. |
| Draft CAS, edit, publish race, stale policy/binding/evidence | `tests/integration/test_publishing.py::test_save_draft_is_authorized_and_compare_and_swap`, `::test_edit_after_green_evidence_requires_retest`, `::test_two_publishers_race_exact_tested_digest`, `::test_current_publication_evidence_is_invalidated_by_authority_changes` | Real PostgreSQL; Cube-specific publication case covers real upstream. |
| Drift/outage/safe errors | `tests/contract/test_cube_adapter.py::test_changed_binding_digest_fails_before_network`, `::test_meta_member_drift_fails_safely`, `::test_upstream_errors_never_echo_diagnostics`; `tests/integration/test_operations.py::test_actual_readiness_faults` | Adapter doubles plus separate composed dependency faults. |
| Gateway timeout is not cancellation | `tests/integration/test_execution_queue.py::test_gateway_timeout_never_releases_uncertain_source_capacity`; `tests/contract/test_cube_adapter.py::test_statement_timeout_evidence_does_not_prove_queued_upstream_lifetime` | Real PostgreSQL capacity semantics plus contract boundary. |
| Independently observed source stop | `tests/integration/test_cube_compatibility.py::test_source_timeout_through_cube_observes_source_stop`, `::test_source_roles_are_read_only_separate_and_statement_timeout_is_server_enforced` | Actual source statement/activity evidence required separately. |
| Fail-closed audit and redaction | `tests/security/test_policy_revocation.py::test_required_audit_failure_prevents_upstream_and_result_release`; `test_result_isolation.py::test_audit_insert_failure_blocks_existing_first_page_and_cursor`; `tests/integration/test_audit.py` | Real PostgreSQL privileges/rollback; owner tamperability explicitly tested. |
| Absent/stale/partial/forged receipts | `tests/unit/test_acceptance_evidence.py` | Verifier controls only, never runtime proof. |

Unqualified security filenames above are under `tests/security/`; Cube runtime filenames are under `tests/integration/`. The new test names without paths are in `tests/security/test_adversarial_matrix.py`.

## Reporting rules

Required identities appear exactly once in their expected receipts with zero skips/failures/errors. Recompute the backend collection after adding tests; the three-group prerequisite is not the entire backend suite. Require source/image binding and actual testcase child outcomes, not JUnit summary attributes alone.

Hashes are integrity checks, not proof against a writer who can replace evidence and receipts together. Admission requires trusted exact-source execution and independent review. Application-role append-only audit does not prevent database-owner changes. Export no raw tokens, parameters, SQL, credentials or unfiltered diagnostics.
