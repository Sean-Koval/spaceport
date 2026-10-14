import { expect, it, vi } from 'vitest';
import { GatewayClient } from '../api';

const item = {
  id: 'draft-1',
  product_id: 'revenue',
  version: '1.0.0',
  name: 'Revenue draft',
  revision: 3,
  editable_full_definition: true,
  can_test: false,
  can_publish: true,
};
const valid = { items: [item], next_cursor: 'opaque-next' };

it('reads the exact same-origin drafts endpoint with encoded search, opaque cursor, limit, and bearer', async () => {
  const requests: Request[] = [];
  const controller = new AbortController();
  const client = new GatewayClient(
    'private-bearer',
    () => {},
    async (input) => {
      requests.push(new Request(input));
      return Response.json(valid);
    },
  );
  await expect(
    client.drafts('sales & revenue', 'opaque+/= cursor', controller.signal),
  ).resolves.toEqual(valid);
  expect(requests).toHaveLength(1);
  const request = requests[0];
  const url = new URL(request.url);
  expect(url.origin).toBe(window.location.origin);
  expect(url.pathname).toBe('/v1/drafts');
  expect([...url.searchParams.keys()].sort()).toEqual(['cursor', 'limit', 'q']);
  expect(url.searchParams.get('q')).toBe('sales & revenue');
  expect(url.searchParams.get('cursor')).toBe('opaque+/= cursor');
  expect(url.searchParams.get('limit')).toBe('50');
  expect(request.method).toBe('GET');
  expect(request.headers.get('authorization')).toBe('Bearer private-bearer');
  expect(request.headers.get('accept')).toBe('application/json');
  expect(request.cache).toBe('no-store');
  expect(request.credentials).toBe('omit');
  expect(url.href).not.toContain('private-bearer');
  controller.abort();
  expect(request.signal.aborted).toBe(true);
});

it('uses an empty search and omits the cursor for the initial drafts page', async () => {
  let request: Request | undefined;
  const client = new GatewayClient(
    'synthetic-test',
    () => {},
    async (input) => {
      request = new Request(input);
      return Response.json({ items: [], next_cursor: null });
    },
  );
  await expect(client.drafts()).resolves.toEqual({ items: [], next_cursor: null });
  expect(new URL(request!.url).searchParams.get('q')).toBe('');
  expect(new URL(request!.url).searchParams.has('cursor')).toBe(false);
  expect(new URL(request!.url).searchParams.get('limit')).toBe('50');
});

it.each([
  [
    'extra private definition',
    { ...valid, items: [{ ...item, definition: { secret: 'private' } }] },
  ],
  ['unapproved test status', { ...valid, items: [{ ...item, test_status: 'passed' }] }],
  ['extra page count', { ...valid, total_count: 900 }],
  ['missing capability', { ...valid, items: [{ ...item, can_publish: undefined }] }],
  ['non-boolean capability', { ...valid, items: [{ ...item, editable_full_definition: 'true' }] }],
  ['invalid revision', { ...valid, items: [{ ...item, revision: 0 }] }],
  ['non-integer revision', { ...valid, items: [{ ...item, revision: 1.5 }] }],
  ['path-like draft identifier', { ...valid, items: [{ ...item, id: '../private' }] }],
  ['empty draft identifier', { ...valid, items: [{ ...item, id: '' }] }],
  ['oversized draft identifier', { ...valid, items: [{ ...item, id: 'd'.repeat(129) }] }],
  [
    'path-like product identifier',
    { ...valid, items: [{ ...item, product_id: 'orders/private' }] },
  ],
  ['oversized product identifier', { ...valid, items: [{ ...item, product_id: 'p'.repeat(129) }] }],
  ['invalid semantic version', { ...valid, items: [{ ...item, version: 'latest' }] }],
  [
    'oversized semantic version',
    { ...valid, items: [{ ...item, version: `1.0.0-${'x'.repeat(59)}` }] },
  ],
  ['empty summary name', { ...valid, items: [{ ...item, name: '' }] }],
  ['oversized summary name', { ...valid, items: [{ ...item, name: 'n'.repeat(257) }] }],
  ['duplicate draft identifier', { ...valid, items: [item, { ...item, product_id: 'orders' }] }],
  [
    'duplicate product head',
    { ...valid, items: [item, { ...item, id: 'draft-2', version: '2.0.0' }] },
  ],
  [
    'oversized page',
    {
      ...valid,
      items: Array.from({ length: 51 }, (_, index) => ({
        ...item,
        id: `draft-${index}`,
        product_id: `product-${index}`,
      })),
    },
  ],
  ['empty cursor', { ...valid, next_cursor: '' }],
  ['oversized cursor', { ...valid, next_cursor: 'x'.repeat(4097) }],
  ['non-string cursor', { ...valid, next_cursor: 123 }],
])('rejects a draft page with %s before returning any projection data', async (_reason, body) => {
  const client = new GatewayClient(
    'synthetic-test',
    () => {},
    async () => Response.json(body),
  );
  await expect(client.drafts()).rejects.toMatchObject({
    status: 503,
    code: 'UPSTREAM_UNAVAILABLE',
  });
});

it('accepts valid summary bounds, Unicode names, and an opaque cursor at its maximum length', async () => {
  const body = {
    items: [
      {
        ...item,
        id: 'd'.repeat(128),
        product_id: 'p'.repeat(128),
        version: `1.0.0-${'x'.repeat(58)}`,
        name: '🚀'.repeat(256),
        revision: 1,
      },
    ],
    next_cursor: 'x'.repeat(4096),
  };
  const client = new GatewayClient(
    'synthetic-test',
    () => {},
    async () => Response.json(body),
  );
  await expect(client.drafts()).resolves.toEqual(body);
});

it('expires the session on a denied drafts read and rejects an older late result', async () => {
  let reads = 0;
  let finish: (response: Response) => void = () => {};
  const expired = vi.fn();
  const client = new GatewayClient('synthetic-test', expired, async () =>
    ++reads === 1
      ? new Promise<Response>((resolve) => {
          finish = resolve;
        })
      : Response.json({ code: 'UNAUTHENTICATED' }, { status: 401 }),
  );
  const pending = client.drafts().then(
    () => 'accepted',
    () => 'rejected',
  );
  await expect(client.drafts()).rejects.toMatchObject({ status: 401 });
  finish(Response.json(valid));
  expect(await pending).toBe('rejected');
  expect(expired).toHaveBeenCalledOnce();
  expect(reads).toBe(2);
});
