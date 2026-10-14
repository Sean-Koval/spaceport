/** Independent official TypeScript SDK harness. No Python subprocess or JS Number decimals. */
import { readFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { Client, StreamableHTTPClientTransport } from '@modelcontextprotocol/client';

const PROTOCOL = '2026-07-28';
const MAX_WIRE_BYTES = 8 * 1024 * 1024;
const overall = AbortSignal.timeout(140000);

async function boundedFetch(input: string | URL | Request, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  headers.set('Accept-Encoding', 'identity');
  const signal = AbortSignal.any([overall, AbortSignal.timeout(10000), ...(init?.signal ? [init.signal] : [])]);
  const response = await fetch(input, { ...init, headers, signal, redirect: 'error' });
  if ((response.headers.get('content-encoding') ?? 'identity') !== 'identity') {
    await response.body?.cancel(); throw new Error('Compressed response is unsupported');
  }
  if (!response.body) return response;
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = []; let bytes = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value.byteLength > MAX_WIRE_BYTES - bytes) throw new Error('Response exceeds transport limit');
      bytes += value.byteLength; chunks.push(value);
    }
  } catch (error) { await reader.cancel(); throw error; }
  const body = new Uint8Array(bytes); let offset = 0;
  for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
  return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
}

function normalize(input: any): any {
  const value = structuredClone(input);
  for (const key of ['execution_id', 'accepted_at', 'started_at', 'completed_at', 'next_cursor']) delete value[key];
  if (value.freshness) { delete value.freshness.assessed_at; delete value.freshness.age_seconds; }
  return value;
}
function sorted(value: any): any {
  if (Array.isArray(value)) return value.map(sorted);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k, sorted(value[k])]));
  return value;
}
const equal = (a: any, b: any) => JSON.stringify(sorted(a)) === JSON.stringify(sorted(b));

async function run() {
  const url = new URL(process.env.DPG_MCP_URL!);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.pathname !== '/mcp' || url.search || url.hash) throw new Error('Explicit /mcp URL required');
  const bytes = await readFile(process.env.DPG_TOKEN_FILE!);
  if (!bytes.length || bytes.length > 16384 || bytes.some(value => value > 127)) throw new Error('Invalid credential file');
  const token = bytes.toString('ascii').trim();
  if (!token || /\s/.test(token)) throw new Error('Invalid credential file');
  const transport = new StreamableHTTPClientTransport(url, {
    requestInit: { headers: { Authorization: `Bearer ${token}` }, redirect: 'error' },
    fetch: boundedFetch, reconnectionOptions: { maxRetries: 0 },
  });
  const client = new Client({ name: 'gateway-typescript-acceptance', version: '1.0.0' }, {
    versionNegotiation: { mode: { pin: PROTOCOL } }, inputRequired: { autoFulfill: false }, defaultCacheTtlMs: 0,
  });
  try {
    await client.connect(transport);
    if (transport.protocolVersion !== PROTOCOL) throw new Error('Protocol pin mismatch');
    const listing = await client.listTools(undefined, { cache: 'refresh' });
    if (!equal(listing.tools.map(tool => tool.name).sort(), ['describe_product', 'get_result', 'query_product', 'search_products'])) throw new Error('Unexpected tool contract');
    async function call(name: string, args: Record<string, any>): Promise<any> {
      const result = await client.callTool({ name, arguments: args }, { timeout: 10000, signal: overall });
      if (result.isError || !result.structuredContent || typeof result.structuredContent !== 'object') throw new Error('Gateway tool failed');
      return result.structuredContent;
    }
    let cursor: string | null = null; const seenCatalog = new Set<string>(); let selected: any;
    for (let pageNumber = 0; pageNumber < 20; pageNumber++) {
      const page = await call('search_products', { query: 'revenue', limit: 50, cursor });
      if (!Array.isArray(page.items) || page.items.length > 50) throw new Error('Invalid catalog page');
      selected = page.items.find((item: any) => item.product_id === 'revenue');
      if (selected) break;
      cursor = page.next_cursor;
      if (!cursor) break;
      if (seenCatalog.has(cursor)) throw new Error('Repeated catalog cursor');
      seenCatalog.add(cursor);
    }
    if (!selected) throw new Error('Authorized product not found');
    const description = await call('describe_product', { product_id: 'revenue', version: selected.version });
    const execution = await call('query_product', { request: {
      product_id: 'revenue', version: description.version,
      inputs: { start: process.env.DPG_QUERY_START ?? '2024-03-09', end: process.env.DPG_QUERY_END ?? '2024-03-12' },
      sort: [{ field: 'day', direction: 'asc' }, { field: 'segment', direction: 'asc' }],
      idempotency_key: randomUUID(),
    } });
    async function getResult(cursor?: string): Promise<any> {
      const args: Record<string, any> = { execution_id: execution.execution_id };
      if (cursor !== undefined) args.cursor = cursor;
      const result = await call('get_result', args);
      if (result.execution_id !== execution.execution_id) throw new Error('Result execution identity mismatch');
      if (result.state === 'succeeded' && (result.product_id !== description.product_id
        || result.product_digest !== description.digest || result.version !== description.version)) throw new Error('Result product identity mismatch');
      return result;
    }
    const deadline = performance.now() + 125000; let result: any;
    for (;;) {
      result = await getResult();
      if (result.state === 'succeeded') break;
      if (!['accepted', 'running'].includes(result.state)) throw new Error('Execution terminated without results');
      if (performance.now() >= deadline) throw new Error('Execution deadline exceeded');
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    const first = result; const rows: any[] = []; const seen = new Set<string>();
    for (;;) {
      if (!Array.isArray(result.rows) || result.rows.length > 200 || rows.length + result.rows.length > 1000 || result.truncated !== false) throw new Error('Invalid or partial result page');
      if (result.rows.some((row: any) => typeof row.revenue !== 'string')) throw new Error('Lossy decimal');
      rows.push(...result.rows); cursor = result.next_cursor;
      if (!cursor) break;
      if (seen.has(cursor) || seen.size >= 1000) throw new Error('Repeated result cursor');
      seen.add(cursor);
      result = await getResult(cursor);
      const left = normalize(first); const right = normalize(result); delete left.rows; delete right.rows;
      if (!equal(left, right)) throw new Error('Result page metadata changed');
    }
    if (rows.length !== first.row_count) throw new Error('Result count mismatch');
    const answer = normalize(first); answer.rows = rows;
    return { compatibility: { sdk: '@modelcontextprotocol/client@2.3.1', protocol: transport.protocolVersion,
      transport: 'streamable-http-json', max_wire_bytes: MAX_WIRE_BYTES }, answer };
  } finally { await client.close(); }
}
try {
  const result = JSON.stringify(await run());
  if (Buffer.byteLength(result) > MAX_WIRE_BYTES) throw new Error('Output exceeds limit');
  process.stdout.write(result + '\n');
} catch {
  process.stderr.write('{"error":"CLIENT_ACCEPTANCE_FAILED"}\n');
  process.exitCode = 1;
}
