import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, it, expect, vi } from 'vitest';
import { App } from '../App';
import { manifest, json, product } from './fixtures';
import { draft, diff, run } from './review-fixtures';
afterEach(() => vi.unstubAllGlobals());
async function open(path: string, fetcher: (r: Request) => Promise<Response>) {
  window.history.replaceState({}, '', path);
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-test');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
}
const denied = () => json({ code: 'NOT_FOUND_OR_FORBIDDEN', request_id: 'revoked' }, 404);
async function editorStandard(r: Request) {
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
}
function expectNoDefinition() {
  expect(screen.queryByLabelText('Name')).toBeNull();
  expect(screen.queryByText('Read-only manifest preview')).toBeNull();
  expect(screen.queryByText(/9007199254740993/)).toBeNull();
}
it('clears the editor definition and preview when tests are denied', async () => {
  await open('/drafts/draft-1/edit', async (r) =>
    r.method === 'POST' ? denied() : editorStandard(r),
  );
  await screen.findByRole('heading', { name: 'Edit definition' });
  await userEvent.click(screen.getByRole('button', { name: 'Run tests' }));
  await screen.findByRole('alert');
  expectNoDefinition();
});
it.each(['locator', 'head'])(
  'clears definition when the %s reload read is denied',
  async (stage) => {
    let reads = 0;
    await open('/drafts/draft-1/edit', async (r) => {
      const path = new URL(r.url).pathname;
      if (path === '/v1/drafts/draft-1') {
        if (++reads > 1 && stage === 'locator') return denied();
        return json({
          ...draft,
          definition: manifest,
          is_current_revision: false,
          current_draft_id: 'draft-2',
        });
      }
      if (path === '/v1/drafts/draft-2') return denied();
      return editorStandard(r);
    });
    await screen.findByRole('heading', { name: 'Edit definition' });
    await userEvent.click(screen.getByRole('button', { name: 'Reload current revision' }));
    await screen.findByRole('alert');
    expectNoDefinition();
  },
);
it('clears review evidence when the head reload is denied', async () => {
  let reads = 0;
  await open('/drafts/draft-1/review', async (r) => {
    const p = new URL(r.url).pathname;
    if (p.endsWith('/diff')) return json(diff);
    if (p.includes('/tests/')) return json(run);
    return ++reads === 1
      ? json({ ...draft, is_current_revision: false, current_draft_id: 'draft-2' })
      : denied();
  });
  await screen.findByRole('heading', { name: 'Review publication' });
  await userEvent.click(screen.getByRole('button', { name: 'Reload current revision' }));
  await screen.findByRole('alert');
  expect(screen.queryByText('Authorized evidence pins')).toBeNull();
  expect(screen.queryByText(/9007199254741023/)).toBeNull();
});
it('clears metadata, result values and pagination when a subsequent execution is denied', async () => {
  const result = {
    execution_id: 'execution-1',
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
    timezone: 'America/New_York',
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
    next_cursor: 'next',
  };
  await open('/products/revenue?version=1.0.0&execution=execution-1', async (r) =>
    r.method === 'POST'
      ? denied()
      : json(new URL(r.url).pathname.includes('/executions/') ? result : product),
  );
  await screen.findByText('"9007199254740993.01"');
  expect(screen.getByRole('button', { name: 'Next result page' })).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await screen.findByRole('alert');
  expect(screen.queryByRole('heading', { name: 'Daily revenue' })).toBeNull();
  expect(screen.queryByText('"9007199254740993.01"')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Next result page' })).toBeNull();
  expect(screen.queryByText('Authorized provenance')).toBeNull();
});
it('does not restore a denied editor from a late passing in-flight test response', async () => {
  let finish: (r: Response) => void = () => {};
  let revoked = false;
  await open('/drafts/draft-1/edit', async (r) => {
    if (r.method === 'POST')
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    if (revoked) return denied();
    return editorStandard(r);
  });
  await screen.findByRole('heading', { name: 'Edit definition' });
  await userEvent.click(screen.getByRole('button', { name: 'Run tests' }));
  revoked = true;
  // The browser focus signal triggers the real query's current-authorized read.
  const { focusManager } = await import('@tanstack/react-query');
  act(() => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
  await screen.findByRole('alert');
  await act(async () => {
    finish(json(run));
    await new Promise((resolve) => setTimeout(resolve, 30));
  });
  expectNoDefinition();
  expect(screen.queryByText(/Tests passed/)).toBeNull();
  await userEvent.click(screen.getByRole('link', { name: 'Catalog' }));
  // Revisiting must make a fresh authorized read, never show the old cached definition.
  await act(async () => {
    window.history.pushState({}, '', '/drafts/draft-1/edit');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await waitFor(() => expect(screen.getByRole('alert')).toBeVisible());
  expectNoDefinition();
  focusManager.setFocused(undefined);
});
it('retains local edits after a non-denial validation error', async () => {
  await open('/drafts/draft-1/edit', async (r) =>
    r.method === 'PUT' ? json({ code: 'INVALID_INPUT' }, 422) : editorStandard(r),
  );
  await screen.findByRole('heading', { name: 'Edit definition' });
  await userEvent.type(screen.getByLabelText('Name'), ' local');
  await userEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('Name')).toHaveValue(`${manifest.name} local`);
  expect(screen.getByText('Read-only manifest preview')).toBeInTheDocument();
});
it.each(['tests', 'reconciliation'])(
  'clears review evidence after denied %s',
  async (operation) => {
    await open('/drafts/draft-1/review', async (r) => {
      const path = new URL(r.url).pathname;
      if (r.method === 'POST') {
        if (operation === 'reconciliation') throw new TypeError('Lost response');
        return denied();
      }
      if (path.startsWith('/v1/products/')) return denied();
      return json(path.endsWith('/diff') ? diff : path.includes('/tests/') ? run : draft);
    });
    await screen.findByRole('heading', { name: 'Review publication' });
    if (operation === 'tests')
      await userEvent.click(screen.getByRole('button', { name: 'Run tests' }));
    else {
      await userEvent.click(screen.getByRole('button', { name: 'Publish' }));
      await userEvent.type(screen.getByLabelText('Transition reason'), 'Checked evidence');
      await userEvent.click(screen.getByRole('button', { name: 'Confirm publication' }));
      await screen.findByText('Publication outcome not yet confirmed.');
      await userEvent.click(screen.getByRole('button', { name: 'Check publication outcome' }));
    }
    await screen.findByRole('heading', { name: 'View unavailable' });
    expect(screen.queryByText('Authorized evidence pins')).toBeNull();
    expect(screen.queryByText(/9007199254741023/)).toBeNull();
  },
);
it('discards a denied catalog projection including its private search state', async () => {
  await open('/catalog?q=private-search', async (r) =>
    new URL(r.url).searchParams.get('q') === 'revoked'
      ? denied()
      : json({ items: [product], next_cursor: 'next' }),
  );
  await screen.findByRole('link', { name: 'Daily revenue' });
  await userEvent.clear(screen.getByLabelText('Search available products'));
  await userEvent.type(screen.getByLabelText('Search available products'), 'revoked');
  await userEvent.click(screen.getByRole('button', { name: 'Search' }));
  await screen.findByRole('alert');
  expect(screen.queryByLabelText('Search available products')).toBeNull();
  expect(screen.queryByRole('link', { name: 'Daily revenue' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Next products' })).toBeNull();
});
it('reopens a denied view only through a new authorized read and ignores the old response', async () => {
  let finish: (r: Response) => void = () => {};
  let revoked = false;
  let reads = 0;
  await open('/drafts/draft-1/edit', async (r) => {
    if (r.method === 'POST')
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    if (new URL(r.url).pathname === '/v1/drafts/draft-1') reads++;
    if (revoked) return denied();
    return editorStandard(r);
  });
  await screen.findByRole('heading', { name: 'Edit definition' });
  await userEvent.click(screen.getByRole('button', { name: 'Run tests' }));
  revoked = true;
  const { focusManager } = await import('@tanstack/react-query');
  act(() => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
  await screen.findByRole('heading', { name: 'View unavailable' });
  const deniedReads = reads;
  revoked = false;
  await userEvent.click(screen.getByRole('button', { name: 'Reload authorized view' }));
  await screen.findByRole('heading', { name: 'Edit definition' });
  expect(reads).toBeGreaterThan(deniedReads);
  await act(async () => {
    finish(json(run));
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
  expect(screen.queryByText(/Tests passed/)).toBeNull();
  focusManager.setFocused(undefined);
});
