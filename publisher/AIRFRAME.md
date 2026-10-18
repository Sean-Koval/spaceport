# Spaceport and Airframe

Spaceport uses the shared Agent Factory design system. This is the bounded UI part of the owner's 18 October 2026 restart request, alongside the [readiness and resumption plan](https://github.com/Sean-Koval/spaceport/pull/24). It does not lift live-source, deployment, publication or security-acceptance gates.

## Source and scope

The integration is pinned to [Agent Factory `theme-v6`](https://github.com/Sean-Koval/agent-factory/tree/16c947fcbe1ba5763fe14dae414eeb1f20a427bf), commit `16c947fcbe1ba5763fe14dae414eeb1f20a427bf`. This was the latest released theme checked on 18 October 2026; Agent Factory `main` was `37aae5c175abc56f214a79fc20dd7c9ff51fef45`. Do not silently follow `main`.

- Exact generated tokens and component CSS replace the separate paper/rust palette.
- Navigation, catalog cards, primary/secondary buttons, product glyph and icons consume Airframe classes and assets. Other existing layout classes are token-only adapters, not a second theme.
- Night and Day follow the operating system. A direct `data-theme="dark"` or `data-theme="light"` on the document can also select the upstream theme for testing.
- Normal, successful and published states stay neutral. Yellow identifies an action or focus; orange identifies a caution. The synthetic-data notice remains visible on every route.
- Input labels, action names, route focus, confirmation behavior, session handling, access checks and mutation semantics remain intact. This change does not add browser SSO or prove any source connection.
- Primary actions, text-entry fields and primary-navigation tabs have a 44px minimum target. Compact controls still need rendered target-size review. Token contrast is checked separately from the still-required rendered-browser and assistive-technology evaluation.

## Provenance and local-only delivery

`vendor/airframe/source/` retains twelve upstream files byte-for-byte. `vendor/airframe/manifest.json` records their Git blob IDs and SHA-256 hashes. The upstream Apache-2.0 license and the three font OFL files are retained without changing the repository's license.

The upstream component CSS imports Google Fonts. Spaceport must not request remote fonts at runtime, so `scripts/airframe.mjs` produces a documented local derivative: it removes exactly that one known import and fails if the import changes or another external CSS resource appears. No token, selector or component declaration is rewritten. `src/airframe-fonts.css` loads the three upstream static font files from the same origin. Static fonts cover the supplied regular/body, medium/mono and expanded-bold/display faces; this is not the upstream Google Fonts variable-font distribution. These [upstream Latin subsets](https://github.com/Sean-Koval/agent-factory/blob/16c947fcbe1ba5763fe14dae414eeb1f20a427bf/airframe/fonts/README.md) cover basic Latin and selected punctuation; unsupported glyphs use the declared system fallbacks.

The same script copies the icon sprite, favicon, fonts and licenses to `public/airframe/`, and generates the Spaceport SVG symbol directly from upstream `marks.json`. The local icon wrapper maps existing application names to upstream icons. The right-pointing navigation arrow replaces the former hand-drawn diagonal arrow.

```sh
npm --prefix publisher run generate:airframe
npm --prefix publisher run check:airframe
```

The check verifies both upstream hashes and every generated output. The build runs it before TypeScript and Vite. Updating the theme requires a reviewed immutable upstream revision, refreshed files and hashes, regeneration, and the full verification sequence below. Do not edit the upstream files or generated outputs directly.

## README and documentation coordination

- [PR 22](https://github.com/Sean-Koval/spaceport/pull/22) owns the README banner and brand-asset sync. This integration reuses its exact `theme-v6` source for the application favicon rather than changing its README work.
- [PR 23](https://github.com/Sean-Koval/spaceport/pull/23) owns the front-page rewrite and documentation moves. Its proposed freeze guard must be reconciled with the new restart scope before adoption.
- The old standalone design preview is historical. It is not the source of truth for this app.
- On 18 October, the PR 22 SVG loaded in authenticated cloud Chrome at its natural 960 × 320 size. Its image box retained a fixed 320px height when constrained to 723px wide; remove the fixed HTML height in that PR before considering mobile presentation finished. The user's previously reported missing-SVG behavior was not reproduced in that browser.
- The README's requested annotated application screenshot still needs a running, composed synthetic demo. A fixture-rendered catalog or a build alone cannot supply that evidence.

## Verification and remaining gates

Run the locked Publisher checks:

```sh
npm --prefix publisher run check:airframe
npm --prefix publisher test -- --run
npm --prefix publisher run typecheck
npm --prefix publisher run check:generated
npm --prefix publisher run format:check
npm --prefix publisher run build
```

Then run the existing real-gateway browser suite described in [the Publisher guide](README.md#actual-browser-acceptance). It is separate from component tests. Inspect desktop and 390px/320px layouts, Night and Day, 200% zoom, reduced motion, forced colors, keyboard/focus return, repeated actions and Back/Forward. Check loading, empty, denial, error, stale-evidence and confirmation states. Capture network evidence with no external font/icon requests and no bearer values in screenshots, traces or logs.

Local checks can establish code and asset consistency; they do not certify visual layout, browser accessibility, composed acceptance or work readiness. The current task environment has no Docker executable. Chromium process startup was blocked by local socket permissions, the escalation path failed at an environment mount, the supported cloud browser could not reach the local preview, and its security policy disallows local-file navigation. No local app browser pass or application screenshot is claimed.
