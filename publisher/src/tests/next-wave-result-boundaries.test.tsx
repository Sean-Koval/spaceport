/** Component regressions only: response fixtures are not backend/browser evidence. */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from '../App';
import { json, product } from './fixtures';

const accepted = {
  execution_id: 'selected-execution',
  state: 'accepted',
  accepted_at: '2026-10-13T10:00:00Z',
};
const result = {
  ...accepted,
  state: 'succeeded',
  started_at: '2026-10-13T10:00:01Z',
  completed_at: '2026-10-13T10:00:02Z',
  product_id: product.product_id,
  version: product.version,
  product_digest: product.digest,
  typed_schema: product.outputs,
  rows: [{ day: '2024-03-11', segment: 'protected-result-marker', revenue: '10.10' }],
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

beforeEach(() => {
  window.history.replaceState(
    {},
    '',
    '/products/revenue?version=1.0.0&execution=selected-execution',
  );
});
afterEach(() => vi.unstubAllGlobals());

async function open(fetcher: (request: Request) => Promise<Response>) {
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-boundary-session');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
}

const mismatchedResults = [
  { name: 'another product', change: { product_id: 'unrelated-product' } },
  { name: 'another version of the selected product', change: { version: '2.0.0' } },
  { name: 'another digest of the selected version', change: { product_digest: 'c'.repeat(64) } },
  { name: 'another execution', change: { execution_id: 'unrequested-execution' } },
];

it.each(mismatchedResults)('never renders a succeeded result for $name', async ({ change }) => {
  await open(async (request) =>
    json(
      new URL(request.url).pathname.includes('/executions/') ? { ...result, ...change } : product,
    ),
  );
  expect(await screen.findByRole('alert')).toBeVisible();
  expect(screen.queryByText('"protected-result-marker"')).toBeNull();
  expect(screen.queryByText('Freshness: fresh')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Next result page' })).toBeNull();
});

it.each(mismatchedResults)(
  'removes previously displayed output when a refresh returns $name',
  async ({ change }) => {
    let changed = false;
    await open(async (request) =>
      json(
        new URL(request.url).pathname.includes('/executions/')
          ? changed
            ? { ...result, ...change }
            : result
          : product,
      ),
    );
    await screen.findByText('"protected-result-marker"');
    changed = true;
    await userEvent.click(screen.getByRole('button', { name: 'Refresh result' }));
    expect(await screen.findByRole('alert')).toBeVisible();
    expect(screen.queryByText('"protected-result-marker"')).toBeNull();
    expect(screen.queryByText('Freshness: fresh')).toBeNull();
  },
);

it.each(['accepted', 'running', 'failed', 'timed_out', 'expired'])(
  'rejects a different execution ID even in the %s state',
  async (state) => {
    await open(async (request) =>
      json(
        new URL(request.url).pathname.includes('/executions/')
          ? { ...accepted, execution_id: 'unrequested-execution', state }
          : product,
      ),
    );
    expect(await screen.findByRole('alert')).toBeVisible();
    expect(screen.queryByText('Execution unrequested-execution')).toBeNull();
    expect(screen.queryByRole('heading', { name: `Execution result · ${state}` })).toBeNull();
  },
);

it('checks the pinned result identity again on the next page', async () => {
  await open(async (request) => {
    const url = new URL(request.url);
    return json(
      url.pathname.includes('/executions/')
        ? url.searchParams.has('cursor')
          ? { ...result, version: '2.0.0' }
          : { ...result, next_cursor: 'opaque-page-cursor' }
        : product,
    );
  });
  await screen.findByText('"protected-result-marker"');
  await userEvent.click(screen.getByRole('button', { name: 'Next result page' }));
  expect(await screen.findByRole('alert')).toBeVisible();
  expect(screen.queryByText('"protected-result-marker"')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Next result page' })).toBeNull();
});
