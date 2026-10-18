# Spaceport theme preview

A self-contained, offline-capable HTML design study of the proposed Spaceport visual direction. It is separate from the publisher application and changes no runtime IDs, APIs, routes, or production behavior.

## View

Hosted preview: https://spaceport-design-preview.breezycat.chatgpt.site/ (owner-private; sign in with the owner’s ChatGPT account). The hosted page contains the same HTML as this directory.

Alternatively, download `index.html` and open it in a modern browser. No build, dependency installation, account, or network connection is required. GitHub's file view shows source rather than rendering HTML.

For local HTTP viewing, run from this directory:

```sh
python -m http.server 8767 --bind 127.0.0.1
```

Then open `http://127.0.0.1:8767`. This address only works on the machine running that command.

## Included

- Catalog with six synthetic products, name/meaning search, and domain filtering
- Product-specific detail, contract JSON, and three fixed synthetic result rows
- Illustrative publication review with an explicit, non-persisting simulation
- Dark and light themes, responsive layouts, skip link, visible focus, accessible labels, and route focus
- No external assets, fonts, API calls, credentials, real customer data, or publication capability

Date inputs demonstrate validation. They do not filter the fixed synthetic result rows; the interface discloses this. Copy uses the browser Clipboard API where permitted and otherwise displays the exact reference for manual copying. Theme and simulated publication state are intentionally not persisted.

## Design

Graphite and off-white surfaces, warm orange actions, restrained blue contract metadata, thin borders, squared controls, monospace technical labels, a small original geometric port mark, and local SVG icons with visible text labels. System fonts make the file portable; it does not claim to embed Inter or a custom condensed font. This is a working-application interpretation of the approved E2B-inspired direction, not a clone of E2B assets.

## Verification

After installing the publisher's existing development dependencies, run from the repository root:

```sh
node docs/archive/spaceport-preview/test-preview.cjs
```

18 DOM checks cover search, empty state, domain filters, selected-product data, synthetic runs, date validation, themes, review simulation, navigation/focus, skip-link route preservation, accessible names, unique IDs, SVG accessibility, direct task headings, and the absence of external assets or network calls. JavaScript syntax was also checked.

These tests do not validate rendered layout, actual clipboard permissions, assistive technology behavior, or contrast of every visual state. CSS includes narrow-screen layouts and minimum control heights, but browser screenshots and mobile visual verification are pending. The deployed owner-private site was opened using the official cloud browser and correctly showed its ChatGPT sign-in gate; this browser has no authenticated owner session. The browser could not reach the executor’s local HTTP server, and data URL navigation is prohibited. No access or browser restriction was bypassed, and no sharing settings were relaxed.
