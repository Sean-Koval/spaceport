import { it, expect, vi } from 'vitest';
import { GatewayClient } from '../api';
it('uses same-origin exact route and bearer without putting it in URL', async () => {
  const requests: Request[] = [];
  const client = new GatewayClient(
    'synthetic-test',
    () => {},
    async (input) => {
      requests.push(new Request(input));
      return Response.json({ items: [], next_cursor: null });
    },
  );
  await client.catalog('daily & revenue');
  expect(requests).toHaveLength(1);
  expect(new URL(requests[0].url).pathname).toBe('/v1/products');
  expect(new URL(requests[0].url).searchParams.get('q')).toBe('daily & revenue');
  expect(requests[0].headers.get('authorization')).toBe('Bearer synthetic-test');
  expect(requests[0].url).not.toContain('synthetic-test');
});
it('sanitizes error body and never retries a failed operation', async () => {
  let calls = 0;
  const client = new GatewayClient(
    'synthetic-test',
    () => {},
    async () => {
      calls++;
      return Response.json(
        { code: 'UPSTREAM_UNAVAILABLE', safe_message: 'PRIVATE SQL PASSWORD', request_id: 'req-1' },
        { status: 503 },
      );
    },
  );
  await expect(client.catalog()).rejects.toMatchObject({
    status: 503,
    code: 'UPSTREAM_UNAVAILABLE',
    requestId: 'req-1',
  });
  await expect(client.catalog()).rejects.not.toThrow('PRIVATE');
  expect(calls).toBe(2);
});
it('clears the session on 401 and rejects late responses from the old identity', async () => {
  let finish: (value: Response) => void = () => {};
  let calls = 0;
  const expired = vi.fn();
  const client = new GatewayClient('synthetic-test', expired, async () =>
    ++calls === 1
      ? new Promise<Response>((resolve) => {
          finish = resolve;
        })
      : Response.json({ code: 'UNAUTHENTICATED', request_id: 'req-2' }, { status: 401 }),
  );
  const old = client.catalog();
  const oldResult = old.then(
    () => ({ ok: true }),
    () => ({ ok: false }),
  );
  await expect(client.catalog()).rejects.toMatchObject({ status: 401 });
  finish(Response.json({ items: [], next_cursor: null }));
  expect(await oldResult).toEqual({ ok: false });
  expect(expired).toHaveBeenCalledOnce();
});
