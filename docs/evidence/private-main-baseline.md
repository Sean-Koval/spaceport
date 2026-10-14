# Private-main baseline verification

This is a record of one completed backend gate, not composed-demo or production acceptance. It applies only to the source identities below. Subsequent documentation or implementation changes do not inherit an exact-source receipt.

## Source and trusted run

- Reviewed source commit: `d8a1517e16b228a1bdadb9e73914f76c1198f961`.
- Merged private-main commit: `e03a5a10fa78da78d18a964bc631a79633344b60` ([PR 1](https://github.com/Sean-Koval/spaceport/pull/1)).
- Both commits have tree `b589ae8a0560dea59b45a112fd2409984128a0d9`.
- [GitHub Actions run 38615111132](https://github.com/Sean-Koval/spaceport/actions/runs/38615111132), [job 116028094355](https://github.com/Sean-Koval/spaceport/actions/runs/38615111132/job/116028094355), attempt 1.
- Executing workflow: `.github/workflows/verify-gateway.yml` on main at `9f9fdb5aa07ce02a8e35860d66cdb88915415529`, explicitly checking out the reviewed source above in `backend` mode.
- Integration interval: 2026-10-14 18:26:32.626844 UTC to 18:40:55.699682 UTC.

## Results

The allowlisted CI summary records overall **PASS**:

| Stage | Outcome |
| --- | --- |
| Locked unit/contract execution and admission | PASS; 1,847 tests admitted |
| Complete genuine PostgreSQL/Cube backend selection | PASS; 294 tests admitted |
| Exact registered-resource cleanup | PASS |
| Always-run cleanup verification | Exit 0; 18:40:55–18:40:56 UTC |

The wrapper's complete-selection and evidence admission checks passed; there were no admitted skipped/error cases. Some database tests intentionally use seeded immutable evidence or adapter doubles and retain that narrower classification. The backend pass does not reclassify them as composed publication or live-source execution.

Actual tool versions recorded: Python 3.12.14, uv 0.12.23, Node v24.19.0, npm 11.9.0, Docker 28.0.4, Compose 2.38.2. Runner image: `20261004.327.1`.

Sanitized report integrity references from the run summary:

- Integration SHA-256: `26b019c45c17a5fef59cd16b4c4e047f6472a0e70af7b102ae2301d15ceb023c`.
- Cleanup SHA-256: `8f0c02bbeb78afb6d27b9a8f6e777021e96d2f3780e917170101011b3cad775b`.

These hashes identify the reports within the trusted run; they are not independent authentication. No raw runtime state or secrets were uploaded as an artifact.

## Gates still open

- Composed author → fixture test → separate publisher → consumer browser journey.
- Complete REST/MCP client reconciliation, clean no-acquisition repeat and the full 14-slot demo cycles.
- Reviewed packet-observer capability and bounded observed no-egress evidence.
- Live BigQuery, Domo implementation/reconciliation, Studio and live delegated OIDC interoperability.
- Private production approval, deployment, original-code licensing and public distribution.

See the [acceptance index](acceptance.md), [limitations](limitations.md) and [roadmap](../../ROADMAP.md). This record makes no frontend/browser claim beyond the separate source checks retained for the baseline.
