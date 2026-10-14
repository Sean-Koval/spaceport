# Fixed MCP compatibility profile

Status: **local real-HTTP SDK compatibility verified; actual database/Cube demonstration not yet run**. The source profile is intentionally bounded to the four product tools. Studio integration and live OIDC delegation are **NOT TESTED**. This document is not a production-readiness or offline-stack acceptance claim.

## Exact versions and provenance

- Python SDK `mcp==2.3.0` and its exact `mcp-types==2.3.0` dependency. [Official release](https://pypi.org/project/mcp/2.3.0/) and [tagged SDK source](https://github.com/modelcontextprotocol/python-sdk/tree/v2.3.0).
- Independent TypeScript SDK `@modelcontextprotocol/client@2.3.1`, the split v2 client package. [Official tagged source](https://github.com/modelcontextprotocol/typescript-sdk/tree/v2.3.1).
- Protocol revision `2026-07-28`, fixed explicitly in both harnesses. Python uses `Client(..., mode='2026-07-28')` and an explicit `session.send_discover(PROTOCOL)` HTTP request; the SDK's pinned constructor and cached `discover()` alone do not perform discovery. TypeScript uses `versionNegotiation: { mode: { pin: '2026-07-28' } }` and verifies `transport.protocolVersion` after connection.
- Observed local runtimes: Python 3.12.14, Node 24.19.0, npm 11.9.0, uv 0.12.23.
- Resolved ASGI/client graph: FastAPI 0.143.0, Starlette 1.8.0, Uvicorn 0.54.0, Pydantic 2.14.1, AnyIO 4.15.1, HTTPX2/httpcore2 2.13.1, SSE-Starlette 3.5.0. Gateway Cube/JWKS traffic retains HTTPX 0.28.1. Exact distributions and hashes are in `uv.lock`; npm package integrities are in `clients/package-lock.json`.
- No SDK CLI/exporter extras, LLM, Studio, hosted control plane or paid account is used. npm installation used `--ignore-scripts --no-audit --no-fund`; this task did not transmit an npm audit.

## Transport, composition and authority

`create_mcp_server(services, settings=MCPSettings(...))` returns the official SDK `MCPServer`. `mount_mcp(app, server)` mounts its JSON Streamable HTTP child at the parent root after REST routes. The actual endpoint is `/mcp`, without a slash redirect or `/mcp/mcp` duplication. The caller must enter `server.session_manager.run()` in its existing lifespan and close the shared resources exactly once. Task14 owns that runtime composition.

The factory requires the same `Services` instance as REST and its configured shared `TransportBoundary` with `/mcp` in `authenticated_prefixes`. Every MCP request authenticates through the existing authenticator. Only the verified `Principal` in the actual Starlette request scope is used by tools. A bearer change on one TCP connection is a fresh principal. Session headers, cookies, request IDs, client names/capabilities, tool arguments, `_meta`, forwarded identity and URL parameters do not grant identity. Direct in-memory/stdio calls have no HTTP authority and fail closed.

Demo authentication uses provisioned local bearer credentials and an ordinary `Bearer` challenge. There is no demo OAuth issuer or protected-resource metadata. Production settings require the real configured HTTPS issuer/resource and the existing validated authentication profile. `AuthenticatorTokenVerifier` delegates validation to the shared authenticator; it preserves the verified subject and OAuth client ID separately, reports the real issuer, and does not invent a token resource binding. `validate_token_resource=False` is deliberate because the authenticator verifies the configured audience. Protected-resource metadata is available at `/.well-known/oauth-protected-resource/mcp`, and the challenge names its canonical URL. Metadata is anonymous; tools are authenticated. Fixture signature/JWKS checks are not live IdP delegation acceptance.

Explicit exact Host and Origin allowlists remain enabled; absent Origin supports non-browser SDK clients. Disallowed/null/duplicate Origin or Host is rejected. This demo profile supports direct SDK HTTP, **not cross-origin browser MCP preflight**. For exact `/mcp` OPTIONS only, the mount's outer wrapper removes the preflight classification header before the existing shared guard: no bearer produces 401; an authenticated OPTIONS produces 405. Original Host/Origin checks remain intact. REST and OAuth metadata routes are unaffected. OPTIONS grants no tool or session authority.

The HTTP profile is sessionless and uses `stateless_http=True`, `json_response=True`, disabled subscriptions and no cache hints. Only modern `2026-07-28` requests are accepted. The official SDK protocol classifier runs before transport selection, so a legacy HTTP header cannot route a modern body through the legacy leg. Unsupported revision is the SDK's HTTP 400 / JSON-RPC `-32022`; header/body mismatch uses its own protocol error. There is no silent version fallback. Legacy MCP is not accepted or tested as a compatibility profile.

## Fixed tools and schemas

[`mcp-tools.json`](mcp-tools.json) is the exact observed HTTP `tools/list` snapshot, including all input/output schemas. It is asserted in socket tests.

1. `search_products(query='', cursor=None, limit=50)`: query length at most 256, strict integer limit 1–50, bounded opaque catalog continuation; calls the shared catalog.
2. `describe_product(product_id, version=None)`: shared Identifier/Version rules, authorized `ProductDescription` only.
3. `query_product(request: QueryRequest)`: exactly one required strict request object, the shared recursive query schema, `submit_and_wait(..., seconds=5)`.
4. `get_result(execution_id, cursor=None)`: shared result ownership/current authorization/lifecycle service, no caller page-size or source selector.

Root and nested argument extras are forbidden. Raw argument objects are validated before SDK parameter conversion. A public SDK `Tool`/`FuncMetadata` seam installs the same strict models used for the advertised schemas and bypasses the SDK's JSON-string pre-parser; string values are not unexpectedly reparsed/coerced. Server middleware is a provisional SDK API, pinned and exercised here. No private tool-manager monkey patch is used.

Tools do not expose bindings, policy rules, internal execution authority, arbitrary SQL, Cube members, resources, prompts, subscriptions, sampling, tasks, publishing or admin operations. Tool discovery has a fixed four-item inventory and accepts no continuation cursor. Catalog and result pagination remain separate shared service operations.

## Response shape and limits

Every successful tool returns the same unwrapped business object as REST in `structuredContent`. There is no `{result: ...}` business wrapper. The text content is the 41-byte fixed summary `Gateway response is in structuredContent.`; rows/schema/provenance are not repeated as text. The `get_result` output schema explicitly describes `ExecutionView | ResultEnvelope`, and both installed clients consume it successfully.

Expected application failures have `isError=true` and a small `{code, request_id, safe_message}` structured object. The text representation repeats only that small error, with fixed gateway-safe messages and a server-generated correlation ID. Unexpected failures and output validation failures become a fixed availability error without exception text or Pydantic input echoes. Stored failed/timed-out/expired execution views remain ordinary successful tool responses and contain no rows. HTTP authentication remains 401; protocol errors remain protocol errors.

The shared synchronous query wait returns an execution ID on a successful currently authorized response. If its deadline interrupts an in-flight current-authorization/audit read, it fails closed with `UPSTREAM_UNAVAILABLE`; it does not return the preceding view. Admission may already have committed. Clients can explicitly retry the same immutable query and idempotency key to recover that execution, then poll with current authentication and authorization. This does not imply automatic resubmission, source cancellation, or release of held source capacity.

- Request ceiling: 65,536 actual bytes for the full JSON-RPC message, including whitespace. Declared lengths and streamed/chunked bodies are both checked. Compressed request bodies, duplicate JSON members, invalid UTF-8, nonfinite numbers including `1e309`, excessive nesting, batches, ambiguous headers and unknown envelope fields are refused before SDK parsing.
- Business ceiling is unchanged: 1,048,576 UTF-8 bytes for the complete canonical unpaginated normalized envelope, at most 1,000 rows and at most 200 per service result page. Over-limit business execution fails explicitly.
- Separate transport ceiling: **8,388,608 bytes** for the full HTTP JSON response. The mount buffers only up to that bound and fails before emitting a partial 200 response. Both harnesses have bounded JSON response readers, a bounded 8 MiB SSE-event fallback, request timeouts and a 140-second overall process operation deadline. SSE is not a supported acceptance profile.
- Measured worst-case-style legal test: 200 rows containing quotes, backslashes, newlines, NUL, accented characters and emoji; canonical envelope **1,048,498 bytes**, actual JSON-RPC response **1,739,972 bytes**, text summary **41 bytes**. Both independent harnesses read and compare the complete result. A separate 451-row case verifies 200/200/51 continuation and full row-count preservation. These use explicit business doubles for transport measurement, not source acceptance.

Neither harness caches tool answers. TypeScript retains SDK tool-schema metadata for output validation with zero default cache TTL and explicitly refreshes its list; each tool invocation performs HTTP. Python cache is disabled. No credentials are passed in command-line arguments or emitted in compatibility metadata. Decimal values remain strings; no JavaScript Number arithmetic is used for revenue.

## Repeatable verification

Local socket contracts, including Python and TypeScript subprocesses:

```sh
uv sync --locked
npm --prefix clients ci --ignore-scripts --no-audit --no-fund
uv run --locked pytest tests/contract/test_mcp.py -q -W error
```

Each independent client expects `DPG_MCP_URL` and `DPG_TOKEN_FILE`, an operator-selected local bearer file. Optional `DPG_QUERY_START` / `DPG_QUERY_END` select a valid half-open interval. They independently search, pin the described current version, submit once with an intentional idempotency key, poll terminal state, fetch every page and emit machine-readable normalized answers. The only omitted comparison fields are execution ID, accepted/started/completed timestamps, next cursor, and freshness assessment time/derived age. Product/version/digest, complete rows/types/units/grain/range/timezone, provenance, source watermark, cache status, freshness status/threshold, row count, truncation and warnings stay in the comparison.

Actual PostgreSQL/app-role parity and pagination (requires the existing explicitly authorized disposable fixture):

```sh
uv run --locked pytest tests/integration/test_mcp_services.py -q
```

Actual full-stack independent acceptance, after the Task14 private synthetic stack is seeded and its revenue product is published:

```sh
GATEWAY_DEMO_TEST_AUTHORIZATION=private-synthetic-gateway-demo \
DPG_MCP_URL=http://127.0.0.1:8080/mcp \
DPG_DEMO_NORTH_TOKEN_FILE=/operator/selected/demo-north.token \
DPG_DEMO_SOUTH_TOKEN_FILE=/operator/selected/demo-south.token \
uv run --locked pytest tests/e2e/test_two_clients.py -q
```

Missing authorization/runtime/credential setup fails these mandatory suites explicitly. It is never an empty skipped success. Same-profile answers must match across languages; north and south must have distinct scopes and exact totals `9007199254741023.31` and `100.01`. Both profiles/languages also query an empty range, and a same-connection test rejects another principal's execution plus session-only authority. The PostgreSQL suite separately tests real catalog/REST parity, 451-row result pagination through both transports, current grant revocation and inactive-principal authentication. Synthetic policies/source results are not inferred from the socket-double suite.

Actual full gateway/worker/PostgreSQL/Cube execution, blocked-egress operation, restart persistence, production deployment, Studio, live OAuth login/registration/delegation, external source/cache certification and service-client rotation remain separate open gates.
