# Publisher UI: focused OSS library and workflow recommendation

Research date: 2026-10-13. Scope: the approved small React/TypeScript/Vite publisher, especially plan Tasks 9, 10 and 12. This is a research recommendation, not an implemented or compatibility-tested dependency selection. No application files, dependencies, locks, or commits were changed for this research.

## Recommendation in one minute

Use **Radix Themes + React Hook Form + TanStack Query**, with **openapi-typescript** generating types from the gateway's checked-in OpenAPI and **openapi-fetch** underneath the existing planned `GatewayClient`. Use **React Router in client-side data mode** for the four pages and reliable navigation blocking. Keep ordinary CSS, native semantic tables, and a small renderer for the gateway's version diff. Do not add a dashboard framework, client-side policy engine, second manifest authority, rich-text renderer, or general code editor.

This is a modest dependency set with one job per library:

- Radix Themes: a consistent styled surface and accessible interactive primitives.
- React Hook Form: draft-input state, dirty tracking, repeatable fields, and errors.
- TanStack Query: authorized server reads, pending test polling, and mutation state.
- openapi-typescript / openapi-fetch: generated transport types and a thin typed HTTP client.
- React Router: URLs, deep links, Back/Forward, and unsaved-change protection.

The Python gateway remains the only authority for validation, permissions, test approval, canonical digests, publication, and lifecycle. In particular, **never optimistically mark a version published or a test approved**.

## Repository facts that constrain the choice

Read-only inspection covered the [design](../../design-records/2026-10-13-spaceport.md), [plan](../plans/2026-10-13-spaceport.md), `gateway/contracts/{manifest,canonical,errors}.py`, and `gateway/registry/{models,repository}.py`. At inspection, the publisher and Task 10 REST implementation were not yet present.

1. `StrictModel` forbids extra fields and freezes validated values. `Manifest` has Python cross-field validators for decimal precision, typed defaults/enums, date boundaries, allowed operators, redaction coverage, examples, fixtures, secret-like values, and digest consistency. Generated TypeScript or JSON Schema cannot by themselves replace these rules.
2. `canonical.py` produces RFC 8785 canonical bytes and SHA-256; `Manifest` fills `content_digest` on validation. The editor must omit or clear an old content digest when submitting changed content according to the finalized request DTO, then display the digest returned by the gateway. Do not implement a competing JavaScript digest algorithm.
3. `save_draft` uses `expected_revision`; planned publication requires `draft_id`, `tested_digest`, `expected_revision`, and `reason`. These existing concepts are sufficient for stale-edit protection; the UI should not invent another concurrency protocol.
4. `GatewayError.as_dict()` currently exposes only `code`, `request_id`, and `safe_message`. `validate_manifest()` deliberately collapses failures to a generic safe error. Authoritative server field-level issues are **not currently available**.
5. Publication/test responses, capability hints, and the concrete `VersionDiff` schema need to be fixed by Tasks 9–10 before the UI generates against them. Do not infer capabilities by decoding unverified token payloads, or fabricate an `/auth/me` endpoint.

### Small contract clarifications worth resolving before the UI stage

- Define draft DTO fields clearly: server-owned digest/revision versus editable manifest data, and the revision used by each save/publish call.
- If field-level server feedback is desired, have the gateway owner add a bounded issue list containing an approved field path, stable issue code, and safe message. Never return raw Pydantic `input`, `ctx`, exception strings, or rejected secret values. Otherwise implement honest global server errors plus local usability hints.
- Document the exact evidence tuple exposed for review: draft digest/revision, binding digest/revision, policy revision, fixture version, and run identifier/outcome. Missing/currently unverified evidence must disable publication.
- Return permission-filtered action availability only where the API already has suitable authorized response context, or settle a small explicit contract addition with the API owner. Such hints improve the UI; they are never authorization.
- Ensure diff responses identify their base version and target draft digest/revision. Without that association, a stale diff must not appear to approve a newer edit.

## Library comparison and decisions

Licenses below were checked in official upstream sources on the research date. Final lockfile versions and the complete transitive inventory still need the Task 12 compatibility pass and Task 14 notice audit. Do not copy current documentation's `@latest` commands into the reproducible install path.

| Concern | Candidate and verified license | Assessment for this publisher | Decision |
| --- | --- | --- | --- |
| Styled UI | [Radix Themes docs](https://www.radix-ui.com/themes/docs/overview/getting-started), [repository/license: MIT](https://github.com/radix-ui/themes/blob/main/LICENSE) | Pre-styled components and a local CSS import avoid building a design system from scratch. Suitable text fields, callouts, cards, tables, tabs, dialogs and badges. Use native selects where sufficient. | **Choose**; one provider, a few local wrapper components, ordinary CSS. |
| Headless primitives | [Radix Primitives accessibility](https://www.radix-ui.com/primitives/docs/overview/accessibility), [repository/license: MIT](https://github.com/radix-ui/primitives/blob/main/LICENSE) | Handles many focus, keyboard and ARIA behaviors. Still requires correct labels, composition and app testing. Themes is the lower-effort visual starting point. | Use through Themes; add an individual primitive only if needed. |
| Owned component source | [shadcn/ui docs](https://ui.shadcn.com/docs), [repository/license: MIT](https://github.com/shadcn-ui/ui/blob/main/LICENSE.md) | Excellent if maintaining a custom, source-owned component system is a goal. Its code-distribution model puts copied component maintenance in this repository. That is extra work for four screens. | Viable alternative, not alongside Themes. Avoid a registry/CLI dependency at runtime. |
| Broad component kit | [Mantine docs](https://mantine.dev/getting-started/), [repository/license: MIT](https://github.com/mantinedev/mantine/blob/master/LICENSE) | Useful comprehensive styled kit, especially if more complex controls become necessary. Would be a coherent replacement for Themes, not a second visual system. | Runner-up; do not mix it into the selected stack. |
| Form state | [React Hook Form official documentation source](https://github.com/react-hook-form/documentation/blob/master/src/content/docs/useform.mdx), [repository/license: MIT](https://github.com/react-hook-form/react-hook-form/blob/master/LICENSE) | Manages defaults, dirty state, submission and errors without prescribing the domain schema. Fits a deliberately organized manifest editor. Defaults/reset behavior needs care during refetch. | **Choose**, with local required/format hints and server-authoritative validation. |
| TypeScript schema | [Zod docs](https://zod.dev/), [repository/license: MIT](https://github.com/colinhacks/zod/blob/main/LICENSE) | Good TypeScript validation tool, but a handwritten complete Zod manifest would duplicate Python rules. It would not establish binding validity, policy approval, or canonical equivalence. | **Do not add for V1 manifest validation**. Revisit only for a separate, clearly UI-local schema. |
| Generated forms | [RJSF introduction](https://rjsf-team.github.io/react-jsonschema-form/docs/), [validation docs](https://rjsf-team.github.io/react-jsonschema-form/docs/usage/validation/), [repository/license: Apache-2.0](https://github.com/rjsf-team/react-jsonschema-form/blob/main/LICENSE.md) | Generates forms from JSON Schema and supports UI customization. Useful for genuinely arbitrary schemas. This fixed domain needs explanations, approved selectors, paired semantics and review gates; custom widgets would dominate. AJV schema validation is only part of Python validation. Strict CSP also needs its precompiled-validator approach rather than enabling unsafe evaluation. | **Defer**. It is not the canonical validator or the simplest editor here. |
| Server state | [TanStack Query mutation docs](https://tanstack.com/query/latest/docs/framework/react/guides/mutations), [defaults](https://github.com/TanStack/query/blob/main/docs/framework/react/guides/important-defaults.md), [repository/license: MIT](https://github.com/TanStack/query/blob/main/LICENSE) | Useful for catalog/detail reads, test polling and explicit mutation states. Out-of-box caching, refetches and read retries require intentional policy for this permission-sensitive app. | **Choose** with the safeguards below. No persisted query cache or offline mutation replay. |
| Contract types | [openapi-typescript docs](https://openapi-ts.dev/introduction), [repository/license: MIT](https://github.com/openapi-ts/openapi-typescript/blob/main/LICENSE) | Generates runtime-free TypeScript from OpenAPI 3.0/3.1. Types reduce handwritten transport drift; they do not validate runtime business semantics. | **Choose** as a locked development dependency using the local OpenAPI snapshot. |
| HTTP transport | [openapi-fetch docs](https://openapi-ts.dev/openapi-fetch/), [repository/license: MIT](https://github.com/openapi-ts/openapi-typescript/blob/main/LICENSE) | Thin typed wrapper over fetch, with generated request/response shapes. Centralizes bearer injection and safe error adaptation in `GatewayClient`. | **Choose** rather than adding Axios or manually repeating endpoint types. |
| Navigation | [React Router client data-mode docs](https://reactrouter.com/start/data/installation), [navigation blocking](https://reactrouter.com/api/hooks/useBlocker), [repository/license: MIT](https://github.com/remix-run/react-router/blob/main/LICENSE.md) | Client-only routes work with the Vite app. SPA navigation blocking helps avoid lost draft edits; hard reloads need a separate before-unload warning. | **Choose**; TanStack Query remains the server-state owner. No second backend, SSR, or full-stack framework required. |
| Data table | [TanStack Table repository](https://github.com/TanStack/table), [license: MIT](https://github.com/TanStack/table/blob/main/LICENSE) | Headless table machinery helps complex sorting/column state, but it does not supply the complete accessible visual table. A maximum-50-product catalog and bounded fixture comparison do not need it initially. | **Defer**. Use semantic HTML/Theme tables and gateway pagination. |
| Text diff | [jsdiff repository](https://github.com/kpdecker/jsdiff), [license: BSD-3-Clause](https://github.com/kpdecker/jsdiff/blob/master/LICENSE) | Can calculate display-only line/word differences. It cannot decide contract compatibility, canonical equality or publication readiness. | **Defer**. Start with structured gateway `VersionDiff`; add only if reviewers need inline text changes. |

### Build and self-hosting boundaries

Bundle JS, CSS and any icons locally; use system fonts or reviewed local fonts. No CDN imports, remote schema fetches, online editor services, public playgrounds, or external link-preview requests in the runtime. The suggested libraries are ordinary packages, not a required hosted control plane. That is an architecture recommendation, **not yet evidence of zero outbound traffic**: the denied-egress browser/stack test remains necessary. Verify package engine/peer constraints with the chosen locked React/TypeScript/Vite/Node combination, preserve licenses, and include transitive packages and development-test tools in the inventory.

## Editor structure: guide authors through the actual contract

Use a readable page with an in-page section navigation and a persistent save/status area, not a mandatory wizard. An author should be able to return to a section without losing values.

1. **Meaning:** name, description, owner/domain, grain, aggregation meaning, approved HTTPS documentation links, examples.
2. **Contract:** explicit input/output field rows; type, required, nullable, enum, default; decimal precision/scale; units/currency; allowed filters/sorting. Distinguish absent default from `{ value: null }`, and distinguish null from an empty string. Decimal values/tolerance remain strings; do not apply `Number()`, `parseFloat()` or `valueAsNumber` to them.
3. **Time and limits:** timezone, `[start,end)` date bounds, source-load freshness requirement, bounded limits. Explain source watermark versus response retrieval time.
4. **Binding and access:** approved binding/revision and attachable policy choices from the gateway. Show relevant permitted metadata; do not expose raw SQL/Cube member editing, secrets, policy overrides or selectable impersonated principals. No authorized choices is an actionable empty state, not a free-text fallback.
5. **Fixtures:** expected rows or expected error, permitted tolerances and watermark fields exactly as the contract supports. Permission-negative cases must use the server's fixed test identities/context. The UI must not imply that a publisher's role grants underlying data access.

Keep an optional **read-only manifest preview** for transparency; a plain text/JSON view is enough initially. If raw JSON editing is later approved, bound its size/depth, retain the same save/test/review gates, and do not support code execution, remote `$ref` retrieval or secret-bearing input.

RHF tracks local input and dirty state. Initialize from one fetched draft snapshot. A background refetch may show “A newer revision is available”; it must not silently call `reset()` over dirty work. After a successful save of the exact current edit generation, reset the form to the server-returned snapshot. If the user edits while save is in flight, either block editing explicitly or preserve newer edits as unsaved; do not reset them away. Field type changes must explicitly remove incompatible values such as decimal-only precision/scale rather than leaving hidden stale fields in the payload.

## Test, review and publish as an explicit state sequence

The UI should communicate: **Unsaved changes → Saved, not tested → Tests running → Tests failed / Tests passed for this revision → Ready for review → Publishing → Published**. These are presentation states derived from actual gateway responses, not a new persisted workflow.

- Any local edit immediately removes the ready-to-publish state. A late green response for an earlier draft stays historical and cannot re-enable Publish.
- Show test-run ID, the exact tested digest/revision, required evidence availability, and separate known-answer versus permission-negative outcomes. Present expected and actual typed values in a readable table; show error codes and missing evidence distinctly. A test failure is data about the run, not an HTTP failure and not an empty table.
- Poll an existing nonterminal test run with bounded backoff. Stop polling on terminal state, logout, unauthorized response, or route teardown; resume from its run ID when appropriate. Never create a new test run merely because a read failed or the page remounted.
- Review starts with business meaning, changed fields and test evidence, with technical digests expandable/copyable. Diff display names the old version and target draft revision. Group definition/schema/access/binding/limits/fixture changes; do not silently collapse removed fields or policy changes.
- Render the gateway diff as text. Preserve semantic array order and typed differences (`null`, empty string, number, decimal string). A visual diff is not the canonical digest or a compatibility verdict. Never apply an untrusted patch to live draft state as part of rendering.
- Publish requires current evidence, the review target, publisher capability, connectivity, and a transition reason. The gateway must recheck all prerequisites transactionally. Use an explicit confirmation summarizing product/version, tested revision and action.
- Deprecate and withdraw are distinct actions: deprecation warns existing consumers; withdrawal blocks new execution and result retrieval. Give each its actual consequence and require its reason. Rollback targets an existing compatible release, never an edited published manifest.

### Repeated clicks, conflicts and uncertain outcomes

Disable the action while pending and guard the handler against double dispatch. Make the request from an immutable snapshot of the reviewed target. Set mutation retries to false explicitly; do not persist/replay publish or lifecycle mutations when offline. Client serialization alone does not replace the gateway's revision checks.

For 409, preserve local work and show “This revision changed. Reload and review the current version before publishing.” Reload is an explicit choice; never overwrite the newer server draft or blindly retry with a newly fetched revision number. Two-browser-tab publication races must be an acceptance test.

If the response is lost after submission, the correct state is “Publication outcome not yet confirmed.” Read authorized product/version/draft state to reconcile the exact intended version and digest. A completed server publication may coexist with a failed HTTP response. Do not claim failure, repeat the mutation, or show success until reconciled. If the available API cannot distinguish the outcome, leave it visibly unresolved and require a fresh review.

Closing a pending dialog or navigating away does not cancel a committed or ongoing server action. Cancel only means “do not submit” before dispatch; after dispatch show the real pending state and reconcile on return.

## Permission-sensitive server state

Use query keys containing an **in-memory session generation**, operation, product/version/draft/run ID and relevant read parameters. Never put bearer tokens in keys, URLs, logs, browser storage, or diagnostics. Do not share a QueryClient across authenticated sessions.

On logout/401 or identity change: abort in-flight reads, clear query/mutation caches and private UI state, rotate the session generation, and remove the bearer. Reject late responses from an earlier generation. On hidden/forbidden/not-found results, remove the previously cached detail/evidence rather than leaving it displayed beneath an error.

Do not treat cached capability hints as permission or reuse one principal's catalog for another. Avoid static/infinite permission caching and unauthorized placeholder data. Invalidate related draft/test/diff/catalog/detail queries after confirmed mutations; the successful mutation response may update only the state it actually proves. Focus/reconnect refetches must never overwrite unsaved form inputs. Server reauthorization remains mandatory on every privileged operation.

The `GatewayClient` must turn openapi-fetch's HTTP error result into a rejected, sanitized application error for TanStack Query; returning `{ error }` as successful query data would break error states. Keep HTTP status alongside the stable gateway code internally. Disable automatic retries for 401, hidden/not-found, 409 and 422, and define bounded retries only for safe transient reads; respect supported 429 retry timing. Polling a run is a GET, while starting it is a mutation. Never let a generic retry policy blur that distinction.

## Every page needs the unglamorous states

| State | User experience | Security/correctness constraint |
| --- | --- | --- |
| First load | Stable layout, descriptive loading text, `aria-busy`; avoid flashing “no products.” | No previously authorized content from another session. |
| Empty catalog | “No products available for this account,” with search clear/reset when relevant. | Do not disclose hidden products, counts or names. Server filters before pagination. |
| Empty authorized binding/policy list | Explain that no attachable option is available; offer a safe return path. | No broad-policy/manual-ID bypass. |
| Missing action permission | Read-only content if authorized; short explanation beside relevant controls. | No permission-request workflow or recipient is invented. Server remains decisive. |
| Hidden or unknown product | The same neutral “Product unavailable” and return to catalog. | Do not distinguish nonexistence from forbidden access. |
| 401 | Sign-in/session-expired state; remove private content and pending authority. | Do not automatically re-submit a previous privileged action after sign-in. |
| 422 | Preserve edits, safe global message and request ID; linked field issues only if the gateway provides them. | Do not print rejected inputs or raw backend exception text. |
| 409 | Revision conflict with explicit reload/review choice. | Never auto-advance `expected_revision`. |
| 413 / 429 | Explain size/rate limit and next safe step; honor supported retry timing. | No automatic mutation loop. |
| 503 / network failure | Safe explanation and Retry for reads; mutation outcome reconciliation when needed. | Never expose upstream SQL/stack traces; do not convert outage into empty success. |
| Freshness unknown/stale or binding drift | Explicit warning/failure with verified watermark/version information. | Never use the current clock or fetch completion as proof of source freshness. |
| Rendering failure | Local error boundary with a safe recovery path and no submitted values in diagnostics. | Do not leak tokens or private manifest content in telemetry. |

## Accessibility and safe rendering

Radix's focus/keyboard/ARIA support reduces primitive-level work, but does not certify the composed app. Provide a skip link, main landmark, one clear page heading, explicit labels/help/errors, visible focus, sufficient contrast and non-color-only status text. Use real buttons and links. Focus the error summary or first relevant field after failed submission; link errors to their fields. Announce transitions politely rather than re-announcing every poll.

Tables need captions and header associations. Keep an accessible empty/result summary and avoid premature virtualization. Large fixture/diff views can be bounded and paginated without turning the whole page into horizontal scrolling. On narrow screens, stack metadata and use a labeled scroll region only for genuinely wide comparisons. Dialogs need a title, consequence text, sensible initial focus, Escape/Cancel behavior and focus return. Never make a tooltip the only explanation for a disabled action.

Render descriptions, fixture strings, and diffs as ordinary React text. Use no `dangerouslySetInnerHTML` or Markdown renderer for V1. Validate outbound links as allowed credential-free HTTPS URLs and use safe new-tab/referrer attributes; never fetch link previews or render remote metadata as executable instructions. Keep the synthetic-demo banner on every route, including error and review screens.

## Test mapping for Task 12 and acceptance

Use React Testing Library role/name queries and user interactions for component behavior; use the existing planned Playwright suite against the real gateway for workflow authority. Playwright's accessibility guide supports axe scans but explicitly says automated checks do not replace manual assessment. Sources: [Testing Library query guidance](https://testing-library.com/docs/queries/about/), [Playwright accessibility guidance](https://playwright.dev/docs/accessibility-testing). Licenses: [React Testing Library MIT](https://github.com/testing-library/react-testing-library/blob/main/LICENSE), [Playwright Apache-2.0](https://github.com/microsoft/playwright/blob/main/LICENSE), [axe-core MPL-2.0](https://github.com/dequelabs/axe-core/blob/develop/LICENSE). Audit the exact wrapper/transitive packages actually selected too.

| Test area | Component/contract coverage | Real-browser acceptance |
| --- | --- | --- |
| Contract generation | Regeneration produces no diff; generated types compile; no handwritten manifest authority. | Run UI against the actual pinned OpenAPI/backend, not mock-only success. |
| Form semantics | Null vs absent default, decimals/tolerance strings, false/0 preservation, field-type change cleanup, 422 retains edits. | Author saves a valid multi-section product; Python rejects adversarial input. |
| Evidence binding | Edit removes green readiness; stale save/test/diff response cannot restore it; failed required negative case blocks Publish. | Author tests, publisher reviews exact digest, consumer discovers authorized release. |
| Concurrency | Pending guard prevents repeated dispatch; 409 requires explicit reload; no blind retry. | Two tabs edit/publish; only the transactionally valid reviewed revision wins. |
| Uncertain writes | Lost response shows unresolved state; reconcile exact version/digest before changing status. | Drop publish response after server commit; UI finds actual outcome without duplicate publication. |
| Query/cache isolation | Logout/401 clears token/private data; previous-session response cannot repopulate; forbidden error removes cached detail. | Switch profiles and revoke access; restricted catalog/detail/evidence never reappear. |
| Read race and form preservation | Older search response cannot replace newer search; focus refetch cannot reset dirty fields. | Navigate rapidly, Back/Forward, reload, Close/Cancel and return during pending tests. |
| Empty/error behavior | First load differs from empty; cover 401/404/409/413/422/429/503 and safe request ID. | Backend outage then recovery; no false success or stale authority. |
| Metadata safety | Hostile HTML/Markdown and `javascript:` URLs never execute; no raw exception render. | Browser verifies inert content, safe links, no surprise external request. |
| Lifecycle meaning | Deprecate, withdraw and rollback have distinct text/targets/reason requirements. | Verify published immutability and the server's actual lifecycle consequence. |
| Accessibility | Labels, role/name queries, error links, focus return, statuses not color-only. | Keyboard-only full flow; narrow viewport, zoom, dialogs and errors; axe scans plus manual screen-reader check. |
| Offline/self-hosting | No CDN URLs or persistence/replay adapter in production bundle. | Complete approved synthetic flow with external egress denied; capture browser/network evidence. |

Required existing commands remain `npm --prefix publisher test -- --run`, `npm --prefix publisher run build`, and `npm --prefix publisher run test:e2e`, plus a real `tsc --noEmit` type-check script and the repository's backend/security gates. This research did **not** install or execute those tools and claims no UI test pass.

## Adoption order

1. Settle the bounded Task 9–10 DTO questions, publish the OpenAPI snapshot, and generate client types.
2. Lock the selected libraries after engine/peer compatibility checks. Start with one catalog/detail/read-only review slice, including errors and the demo banner.
3. Add the structured editor and explicit saved/tested/reviewed states, then concurrency and interrupted-flow tests before enabling publish/lifecycle buttons.
4. Complete real-gateway role flows, keyboard/mobile QA, offline network evidence, and the dependency/license inventory.

This keeps the existing architecture and acceptance gates intact while reducing bespoke UI state, accessibility primitives, and transport typing work.
