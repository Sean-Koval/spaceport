import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import { App } from '../App';
import { product, json } from './fixtures';
const execution = {
  execution_id: 'execution-1',
  state: 'succeeded',
  accepted_at: '2026-10-13T10:00:00Z',
  started_at: '2026-10-13T10:00:01Z',
  completed_at: '2026-10-13T10:00:02Z',
  product_id: 'revenue',
  version: '1.0.0',
  product_digest: product.digest,
  typed_schema: product.outputs,
  rows: [{ day: '2024-03-11', segment: null, revenue: '9007199254741023.31' }],
  units: product.units,
  grain: 'daily',
  effective_range: { start: '2024-03-09', end: '2024-03-12', interval: '[start,end)' },
  timezone: 'America/New_York',
  provenance: product.provenance,
  row_count: 1,
  truncated: false,
  source_watermark: '2026-10-13T09:00:00Z',
  freshness: {
    status: 'fresh',
    assessed_at: '2026-10-13T10:00:02Z',
    age_seconds: 3602,
    max_age_seconds: 86400,
  },
  cache_status: 'unknown',
  warnings: [],
  next_cursor: null,
};
beforeEach(() =>
  window.history.replaceState({}, '', '/products/revenue?version=1.0.0&execution=execution-1'),
);
afterEach(() => vi.unstubAllGlobals());
async function open(fetcher: (r: Request) => Promise<Response>) {
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-test');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
  await screen.findByRole('heading', { name: 'Daily revenue' });
}
it('separates completion, source load and freshness, showing exact typed rows and pinned version', async () => {
  await open(async (r) =>
    json(new URL(r.url).pathname.includes('/executions/') ? execution : product),
  );
  expect(await screen.findByText('"9007199254741023.31"')).toBeVisible();
  expect(screen.getByText('Source loaded')).toBeVisible();
  expect(screen.getByText('Completed')).toBeVisible();
  expect(screen.getByText('Freshness checked')).toBeVisible();
  expect(screen.getByText('Result version 1.0.0')).toBeVisible();
  expect(screen.getByText('Cache: unknown')).toBeVisible();
  expect(screen.getByText(/At most 1000 rows/)).toBeVisible();
});
it('never turns unknown freshness into a fresh badge or inferred watermark', async () => {
  await open(async (r) =>
    json(
      new URL(r.url).pathname.includes('/executions/')
        ? {
            ...execution,
            source_watermark: null,
            freshness: { ...execution.freshness, status: 'unknown', age_seconds: null },
          }
        : product,
    ),
  );
  expect(await screen.findByText('Freshness: unknown')).toBeVisible();
  expect(screen.getByText('Source-load evidence unavailable')).toBeVisible();
  expect(screen.queryByText('Freshness: fresh')).toBeNull();
});
it('clears rows and pagination when execution access is revoked', async () => {
  let revoked = false;
  await open(async (r) =>
    new URL(r.url).pathname.includes('/executions/')
      ? revoked
        ? json({ code: 'NOT_FOUND_OR_FORBIDDEN', request_id: 'denied-result' }, 404)
        : json(execution)
      : json(product),
  );
  await screen.findByText('"9007199254741023.31"');
  revoked = true;
  await userEvent.click(screen.getByRole('button', { name: 'Refresh result' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('unavailable to this account');
  expect(screen.queryByText('"9007199254741023.31"')).toBeNull();
});
it('executes only the pinned named product and approved input keys', async () => {
  window.history.replaceState({}, '', '/products/revenue?version=1.0.0');
  let body: any;
  await open(async (r) => {
    if (r.method === 'POST') {
      body = await r.json();
      return json({
        execution_id: 'execution-1',
        state: 'accepted',
        accepted_at: execution.accepted_at,
      });
    }
    return json(new URL(r.url).pathname.includes('/executions/') ? execution : product);
  });
  await userEvent.click(screen.getByRole('button', { name: 'Use example 1' }));
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await screen.findByText('"9007199254741023.31"');
  expect(body).toEqual({
    product_id: 'revenue',
    version: '1.0.0',
    inputs: { start: '2024-03-09', end: '2024-03-12' },
    filters: [],
    sort: [],
  });
});
