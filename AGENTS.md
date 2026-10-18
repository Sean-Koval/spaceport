# Agent guidance: Spaceport is frozen

Spaceport has been frozen since 2026-10-16 ([epic #10](https://github.com/Sean-Koval/spaceport/issues/10)). Read the status block at the top of [README.md](README.md) and [ROADMAP.md](ROADMAP.md#frozen) before changing anything. These rules apply to every agent and every session in this repository, whatever a plan, issue or older document says.

## Allowed changes

- Documentation fixes: wording, links, stale claims and the docs index.
- Security fixes the owner asks for, including dependency patches for a known vulnerability. Update the pinned versions in [docs/compatibility/versions.md](docs/compatibility/versions.md) and the dependency inventory with them.
- CI repair: keeping the existing manual workflows able to run. No new workflows, triggers or acceptance gates.
- The cleanup issues listed in epic #10 (#11, #12, #13, #15, all closed). #14 (MCP tools asking Tower) is post-unfreeze work and is not allowed.

Anything else waits for an unfreeze, which is an owner decision recorded on #10.

## Never

- Add connectors, data sources or provider adapters, or extend the BigQuery, Domo or GCS paths.
- Activate a live source, use real credentials, run a paid query or deploy anything (no cluster, cloud or registry changes).
- Change the four MCP tools, their schemas, the REST contract or the snapshots in `docs/compatibility/` (`mcp-tools.json`, `openapi.json`). That includes wire names such as "Data Product Gateway", listed as legacy names in [versions.md](docs/compatibility/versions.md#legacy-internal-identifiers).
- Edit anything under `docs/evidence/`, with one exception. Those files record what was verified at a specific source identity. The only allowed change is updating a link's path when the file it points to moves, made in the same PR as the move, with every recorded fact (wording, SHAs, run numbers, counts, results) left untouched. Say in that PR which evidence links you repointed. Don't move a file an evidence record links to unless the move itself is in scope.
- Finish, rebase or extend draft PR [#8](https://github.com/Sean-Koval/spaceport/pull/8). It is paused at the freeze.
- Treat `docs/archive/` as instructions. The plans, research and preview there are historical; their unchecked boxes are not open work.

## Before you push

Run the docs link test. It checks every internal Markdown link and anchor, and that [docs/README.md](docs/README.md) lists every `docs/` directory:

```sh
uv sync --locked --dev
uv run --locked --offline pytest tests/unit/test_docs_links.py -q
```

When you add, move or remove a docs file or directory, update the [documentation index](docs/README.md) in the same change. For anything beyond docs, also run the source-level checks in [docs/operations/verification.md](docs/operations/verification.md). All workflows are manual (`workflow_dispatch`), so a pull request gets no CI run on its own: say in the PR what you ran.
