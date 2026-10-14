import { act, render, screen, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, type QueryCache } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../App';
import { json, manifest, product } from './fixtures';
import { draft, run } from './review-fixtures';
afterEach(() => vi.unstubAllGlobals());
function observeCaches() {
  const caches = new Set<QueryCache>();
  const get = QueryClient.prototype.getQueryCache;
  vi.spyOn(QueryClient.prototype, 'getQueryCache').mockImplementation(function (this: QueryClient) {
    const cache = get.call(this);
    caches.add(cache);
    return cache;
  });
  return caches;
}
function expectEmpty(caches: Set<QueryCache>) {
  expect(caches.size).toBeGreaterThan(0);
  for (const cache of caches) expect(cache.getAll()).toEqual([]);
}
async function open(path: string, fetcher: (r: Request) => Promise<Response>) {
  window.history.replaceState({}, '', path);
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-test');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
}
async function navigate(path: string) {
  await act(async () => {
    window.history.pushState({}, '', path);
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
}
async function finishDenied(finish: (r: Response) => void) {
  await act(async () => {
    finish(json({ code: 'NOT_FOUND_OR_FORBIDDEN', request_id: 'revoked' }, 404));
    await new Promise((resolve) => setTimeout(resolve, 25));
  });
}
function result(id: string) {
  return {
    execution_id: id,
    state: 'succeeded',
    accepted_at: '2026-10-13T10:00:00Z',
    started_at: null,
    completed_at: '2026-10-13T10:00:02Z',
    product_id: 'revenue',
    version: '1.0.0',
    product_digest: product.digest,
    typed_schema: product.outputs,
    rows: [{ day: '2024-03-11', segment: null, revenue: '9007199254740993.01' }],
    units: product.units,
    grain: 'daily',
    effective_range: null,
    timezone: product.timezone,
    provenance: product.provenance,
    row_count: 2,
    truncated: false,
    source_watermark: null,
    freshness: {
      status: 'unknown',
      assessed_at: '2026-10-13T10:00:02Z',
      age_seconds: null,
      max_age_seconds: 86400,
    },
    cache_status: 'unknown',
    warnings: [],
    next_cursor: 'next-page',
  };
}
it('clears protected product data and caches when a pending denial follows same-version execution-query navigation', async () => {
  const caches = observeCaches();
  let finish: (r: Response) => void = () => {};
  let posts = 0;
  await open('/products/revenue?version=1.0.0&execution=old-result', async (r) => {
    if (r.method === 'POST') {
      posts++;
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    }
    const path = new URL(r.url).pathname;
    return json(path.includes('/executions/') ? result(path.split('/').at(-1)!) : product);
  });
  await screen.findByText('"9007199254740993.01"');
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await navigate('/products/revenue?version=1.0.0&execution=new-result');
  await screen.findByText('Execution new-result');
  expect(screen.getByRole('button', { name: 'Next result page' })).toBeVisible();
  await finishDenied(finish);
  expect(screen.queryByRole('heading', { name: 'Daily revenue' })).toBeNull();
  expect(screen.queryByText('Authorized provenance')).toBeNull();
  expect(screen.queryByText('"9007199254740993.01"')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Next result page' })).toBeNull();
  expectEmpty(caches);
  expect(posts).toBe(1);
});
it('clears protected editor fields, preview, evidence and caches after hash navigation and a denied pending test', async () => {
  const caches = observeCaches();
  let finish: (r: Response) => void = () => {};
  let posts = 0;
  await open('/drafts/draft-1/edit', async (r) => {
    if (r.method === 'POST') {
      if (++posts === 1) return json(run);
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    }
    const path = new URL(r.url).pathname;
    if (path === '/v1/bindings')
      return json({
        items: [
          {
            binding_id: manifest.binding_id,
            revision: 1,
            digest: 'b'.repeat(64),
            adapter_revision: manifest.adapter_revision,
            model_bundle_digest: manifest.model_bundle_digest,
          },
        ],
        next_cursor: null,
      });
    if (path === '/v1/policies')
      return json({ items: [{ policy_id: manifest.policy_id, revision: 1 }], next_cursor: null });
    return json({ ...draft, definition: manifest });
  });
  await screen.findByRole('heading', { name: 'Edit definition' });
  await userEvent.click(screen.getByRole('button', { name: 'Run tests' }));
  await screen.findByText(/Tests passed for saved revision/);
  await userEvent.click(screen.getByRole('button', { name: 'Run tests' }));
  await navigate('/drafts/draft-1/edit#fixtures');
  expect(screen.getByLabelText('Name')).toBeVisible();
  await finishDenied(finish);
  expect(screen.queryByLabelText('Name')).toBeNull();
  expect(screen.queryByText('Read-only manifest preview')).toBeNull();
  expect(screen.queryByText(/9007199254740993/)).toBeNull();
  expect(screen.queryByText(/Tests passed for saved revision/)).toBeNull();
  expectEmpty(caches);
  expect(posts).toBe(2); // One completed setup test, one denied pending test, no replay.
});
it('does not clear a replacement product or session when the genuinely unmounted scope finishes denied', async () => {
  // Both boundary replacements are checked independently, using the real session and route guards.
  for (const replacement of ['product', 'session']) {
    let finish: (r: Response) => void = () => {};
    let posts = 0;
    await open('/products/revenue?version=1.0.0', async (r) => {
      if (r.method === 'POST') {
        posts++;
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
      }
      const id = new URL(r.url).pathname.split('/').at(-1)!;
      return json({
        ...product,
        product_id: id,
        name: id === 'other' ? 'Other product' : product.name,
      });
    });
    await screen.findByRole('heading', { name: 'Daily revenue' });
    await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
    if (replacement === 'product') await navigate('/products/other?version=1.0.0');
    else {
      await userEvent.click(screen.getByRole('button', { name: 'End session' }));
      await userEvent.type(screen.getByLabelText('Session bearer'), 'new-synthetic-session');
      await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
    }
    const name = replacement === 'product' ? 'Other product' : 'Daily revenue';
    await screen.findByRole('heading', { name });
    const destination = window.location.pathname + window.location.search;
    await finishDenied(finish);
    expect(screen.getByRole('heading', { name })).toBeVisible();
    expect(screen.queryByRole('heading', { name: 'View unavailable' })).toBeNull();
    expect(window.location.pathname + window.location.search).toBe(destination);
    expect(posts).toBe(1);
    cleanup();
  }
});
