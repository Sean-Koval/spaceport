# Production gates: blocked until explicitly satisfied

A successful synthetic demo automatically satisfies none of these.

- Ownership: name service, data/product, source/model, security and operations owners; agree incident response and support responsibilities.
- Original-code license/public distribution: owner decision; complete dependency/image/browser notices and review upstream obligations before distribution.
- Identity: actual issuer/resource audience, user-bound delegation, JWKS rotation/revocation/outage tests, external secret manager and key rotation. Studio integration remains NOT TESTED.
- Authorization: real tenant/grant mapping, policy change controls, per-field/freeform metadata rules, schema migration/privilege review, independent audit storage if tamper resistance against owners is required.
- Ingress: operator-approved TLS termination, certificate lifecycle, narrow host/origin rules, authentication and body limits on REST/MCP, reverse-proxy timeouts, no source/developer ports.
- Egress: enforced platform firewall/DNS policy, required upstream allowlists, identity/source endpoint controls, observation against actual deployment topology. Synthetic internal networking is not a production policy.
- Data protection: approved result/audit retention; encrypted registry/results/backups and managed keys; restore-key availability; backup integrity/restore drills; deletion policy. Default synthetic retention is24 hours results and30 days redacted audits.
- Source: retained reviewed Cube model bundles and migrations; actual timezone/decimal/cardinality/isolation behavior; source cost/time/concurrency ceilings; query kill/cancellation proof or safe independent reservation reconciliation. A gateway timeout never proves source cancellation.
- Reliability: durable worker failover, retry/idempotency/reclaim/reservation quarantine, load/capacity metrics, dependency readiness/fault injection, bounded cleanup/resource inventories, monitoring that does not leak credentials or rows.
- BigQuery: separately authorized live execution with real time/cost controls and credentials. Mock contracts are not live acceptance; Domo phase1b is not implemented.
- Release: independently reviewed exact source commit, locked and scanned dependency/image/browser provenance, genuine end-to-end UI and both MCP clients, offline/restart/restore/egress evidence, negative security tests, owner approval for any paid resources or deployment.
