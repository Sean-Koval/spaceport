# Locked toolchain provenance

Official registry metadata was read on 2026-10-13 from https://registry.npmjs.org using `npm view <package> version engines peerDependencies license --json`. The raw initial compatibility pass is `registry-metadata.txt`; the exact selected dependency inventory is `dependency-inventory.json` and the immutable install resolution/integrity authority is `../package-lock.json`.

Selected runtime: Node 24.19.0 / npm 11.9.0, matching the observed executor. React/React DOM 19.3.0, Vite 8.3.4, plugin-react 6.1.2 and Vitest 5.0.4 satisfy their declared engine/peer ranges with that runtime. jsdom 30.1.2 requires Node ^24.15.0 (or its other documented branches), also satisfied.

The initial newest TypeScript metadata was 7.0.2. It was deliberately **not selected**: openapi-typescript 7.13.0 declares TypeScript ^5.x, so the latest official 5.x result, **5.9.3**, was pinned instead. Node type declarations use official **24.19.2** rather than latest 26.x. All other selected versions correspond to the matching entries in the raw metadata. Prettier **3.9.10** was separately checked from the official registry: Node >=14, MIT; it is a local formatting development tool only.

`npm ci --ignore-scripts --no-audit` is the reproducible install path. `.npmrc` enables exact saves, strict engines and disables automatic audit transmission. No install scripts, unpinned remote generators, runtime CDNs, hosted form services or Helix source/logo copies are used.

A requested `npm audit --json` attempt was denied by the execution reviewer because sending this private dependency graph/metadata to the public registry was not explicitly authorized. It was not retried or replaced by another online scanner. **Vulnerability scanning is unverified.** The local inventory is provenance/license metadata, not a security or licensing approval.

Primary technical references checked during implementation:
- https://vite.dev/guide/ (runtime/build requirements)
- https://reactrouter.com/api/hooks/useBlocker (data-router navigation protection)
- https://tanstack.com/query/latest/docs/framework/react/guides/query-cancellation (abort-aware reads)
