# Third-party notices

This repository incorporates and depends on third-party software. Its licenses and notices remain applicable. No license for original repository code has been selected; no public distribution authorization is implied.

The inspected locked Python/npm dependency inventory is [docs/operations/dependency-inventory.json](docs/operations/dependency-inventory.json). Available upstream LICENSE/COPYING/NOTICE files are copied verbatim in [docs/operations/notices](docs/operations/notices). Do not replace those notices with this summary. The inventory records uninstalled/conditional artifacts whose notice contents remain unverified.

Runtime/build images: official Python, PostgreSQL, Nginx, Node, astral/uv and Cube images are pinned by digest in the Compose/Dockerfiles. Their OS, bundled libraries and browser notices must additionally be inventoried from the exact prepared artifacts. Preserve upstream image/package attribution and source notices. See [licensing status](docs/operations/licensing.md) and [production gates](docs/operations/production-gates.md).

Regenerate the package inventory after dependency changes with `uv run --locked --offline python -m scripts.license_inventory` in an environment containing the exact locked Python, publisher and TypeScript-client packages. Set `GATEWAY_NPM_CACHES` to the local npm caches containing the lock-integrity-verified tarballs; mismatched installed versions or bytes fail, and unavailable artifact bytes are identified without copying unverified notices. An absent notice or metadata field does not establish that software is unlicensed or that attribution is unnecessary.
