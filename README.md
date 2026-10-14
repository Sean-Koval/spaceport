# Spaceport

Spaceport is a private, self-hosted synthetic demonstration of reviewed data products: typed definitions, independently authorized publication, tenant-scoped execution, durable results, a publisher UI, and a fixed MCP contract. No model provider, vendor account or hosted control plane is needed for the synthetic design. Spaceport is the working name; public naming clearance remains unresolved.

## Current verification status

The application packaging is a source candidate. Final-source complete backend, composed UI/MCP/operations demo, and fresh no-acquisition repeat are **NOT RUN**. Docker is unavailable in the implementation executor. Ordinary `make verify-demo` runs 13-slot business verification unprivileged and fails closed when any required component, test, or evidence is missing; it omits the integrated `acceptance_security` slot. The stronger packet-observer approval/implementation is currently absent, so its mandatory slot remains explicitly **BLOCKED** without preventing startup or business tests. Separate Cube compatibility runs and mocked adapter tests are not full demo acceptance. Production and public distribution remain blocked by their explicit gates.

## Prepare and run locally

Use a supported Linux Docker host with Docker Compose, the exact Python 3.12.14 / uv 0.12.23 and Node 24.19.0 / npm 11.9.0 toolchains. Read [the operations runbook](docs/operations/runbook.md) first. Do not use production credentials or an existing production database.

Preparation may access package/image registries. All dependencies, image layers and Chromium must be obtained before offline verification:

```sh
uv sync --locked --dev
npm --prefix publisher ci --ignore-scripts --no-audit --no-fund
cd publisher && npm exec -- playwright install chromium && cd ..
make demo-prepare STATE_DIR=/tmp/my-new-private-gateway
```

`STATE_DIR` must be an absolute, previously nonexistent directory outside the repository with an existing private parent. The command generates only synthetic credentials, builds locked images and records exact local image IDs and source hashes. Never commit or publish its contents.

```sh
make demo-up STATE_DIR=/tmp/my-new-private-gateway
```

Open http://127.0.0.1:8080. The token file is `STATE_DIR/identities/demo-tokens.json`; load only the desired account's synthetic token into the browser session. Tokens stay in memory. Author, publisher, north consumer and south consumer are separate identities. First startup is live but deliberately unready until the author saves/tests a definition and the publisher approves the exact tested revision. The UI includes a synthetic revenue template. There is no pre-fabricated green test or publication.

For ordinary 13-slot business verification, use a newly prepared stack before manually creating a draft, plus the exact-source/image backend compatibility and actual-bootstrap receipt described in the runbook. The acquiring harness recorder hook now exists in source; genuine final-source runtime execution is still **NOT RUN**. This sequence alone is not final integrated acceptance:

```sh
make verify-demo STATE_DIR=/tmp/my-new-private-gateway PREREQUISITES_DIR=/absolute/retained-prerequisites
make evidence STATE_DIR=/tmp/my-new-private-gateway
make demo-down STATE_DIR=/tmp/my-new-private-gateway
```

`demo-up` and `verify-demo` never pull images or install dependencies. `demo-down` preserves named volumes and state. Run `demo-up` again to resume. Source edits require a fresh prepared state so the image/source identity cannot silently drift.

## Final acceptance route

With the pinned toolchains, locked Python dependencies and a clean checkout on a supported Linux Docker host, run the two reviewed wrapper modes separately:

```sh
umask 077
bash scripts/verify_gateway_integration.sh --backend /ABSOLUTE/PRIVATE_BACKEND_PARENT/run
bash scripts/verify_gateway_integration.sh --demo /ANOTHER_PRIVATE_DEMO_PARENT/run
```

Each parent must already exist outside the repository, be owner-controlled and mode 0700. Each `run` child must be previously nonexistent. Use distinct new private run roots; do not pass a previously prepared `STATE_DIR` or an already-created temporary directory. Keep npm/browser caches and setup metadata outside run state. Clear ambient `PYTEST_ADDOPTS` and `PYTEST_PLUGINS` before invoking either mode.

The wrapper runs the fixed locked/offline Python entrypoint and owns bounded preparation, receipts and exact-resource cleanup. Both modes acquire locked MCP client dependencies before mandatory offline unit/contract execution. Backend mode executes the complete current `postgres or cube` selection against genuine disposable backends. Demo mode prepares its publisher/browser/images, then requires all 14 integration slots, including `acceptance_security`, in both independent clean cycles. The second cycle reuses prepared dependencies and image IDs without acquisition or restoring the first cycle's published data. Demo prerequisite receipts do not replace the separate complete-backend gate.

These are required commands, not recorded successful runs. Final backend/demo/repeat execution remains **NOT RUN**. Packet observation remains **BLOCKED**, so the current demo wrapper must remain nonzero/incomplete even if its business suites succeed. Full acceptance also requires verified cleanup, actual rendered-browser inspection and final independent review. See [the acceptance evidence index](docs/evidence/acceptance.md) and [limitations](docs/evidence/limitations.md).

## Recovery and boundaries

[Backup and fresh-stack restore](docs/operations/runbook.md#backup-and-restore) preserve the registry and actual source-load watermark. A restore refuses an existing state directory and uses a distinct project; it never overwrites the working demo.

- [Production gates](docs/operations/production-gates.md)
- [Identity and credential handling](docs/operations/identity.md)
- [Compatibility baseline](docs/compatibility/versions.md)
- [Dependency/license inventory](docs/operations/licensing.md)
- [Original approved specification](docs/superpowers/specs/2026-10-13-data-product-gateway.md)

Original-code licensing has not been selected by the owner. Upstream dependency licenses/notices remain applicable; this repository does not imply permission for public distribution.
