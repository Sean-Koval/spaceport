# Authorized saved-draft discovery

`GET /v1/drafts` is a read-only REST inbox. The four MCP tools are unchanged.

## Request and response

- `q`: optional text, default empty, at most 256 characters. Casefolded substring search uses only the authorized product ID, name and version.
- `limit`: integer 1–50, default 50.
- `cursor`: optional opaque continuation, at most 4,096 characters. Unknown or repeated parameters are rejected.
- `DraftPage`: `items` and nullable `next_cursor`. No total, hidden count or test state is returned.
- Each `DraftSummary` has exactly `id`, `product_id`, `version`, `name`, `revision`, `editable_full_definition`, `can_test` and `can_publish`.

Only the latest saved head for each product is eligible. A head referenced by any `ProductVersion.draft_id` is excluded, including versions later deprecated or withdrawn. A passed test is never treated as publication. Ordering is product ID under PostgreSQL `C` collation, then draft ID; there is only one current head per product. An edited head retains the product's position. Continuations are not historical snapshots: a new product before the last position requires starting again.

## Authorization and bounds

Current exact author-or-publisher review policy determines visibility, independently of creator identity and consumer discovery access. A coarse current-policy/tenant/capability filter precedes the 1,000-candidate work bound so unrelated scopes cannot infer the global population from a limit error. Every candidate is separately review-authorized before search matching and page selection. Denial aborts its own transaction; it is never continued to inspect another candidate.

Summaries contain no manifest, fixture rows, credentials, internal policy/grant records or authority objects. Capability fields are hints only. Full-definition editing requires author capability and all output fields visible. Testing retains its author gate. Publisher hints require the same independent base and target scopes as the publish operation. The inbox only links to edit/review; it performs no test or publication action.

The response rechecks selected heads, actual publication relationships, review authorization and capability hints before release. Product heads are locked before policy heads, and changes to the principal/grant/policy fingerprint fail closed. A base publication change also fails closed. Mandatory authorization audit records must commit before the page is released.

Encrypted five-minute cursors have the distinct `drafts` purpose and bind the authenticated principal, current identity/grants/policy fingerprint, normalized search and limit. Expired, altered, cross-purpose, cross-account or changed-scope continuations are rejected with the same unavailable response. Consumers and unrelated tenants receive an empty page with no metadata, count or continuation.

## Client handling and evidence

The inbox uses session-local query state, removes protected content while refreshing or on errors, and clears cached reads on access denial or account/session changes. Late old-session responses cannot repopulate the inbox. Edit, test and publish retain their existing digest/revision checks.

Unit/transport and component tests exercise projection shape, argument bounds, cursor bindings, refresh/denial and session transitions. PostgreSQL tests in `tests/integration/test_draft_discovery.py` must run against the real disposable backend; mock tests and collection do not satisfy that gate. Actual browser/composed-stack acceptance remains separate.
