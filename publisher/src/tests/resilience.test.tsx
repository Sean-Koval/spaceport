import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { focusManager } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../App';
import { draft, diff, run } from './review-fixtures';
import { json, manifest, product } from './fixtures';

afterEach(() => {
  focusManager.setFocused(undefined);
  vi.unstubAllGlobals();
});

async function open(path: string, fetcher: (request: Request) => Promise<Response>) {
  window.history.replaceState({}, '', path);
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-test');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
}

async function editorRead(request: Request) {
  const path = new URL(request.url).pathname;
  if (path === '/v1/bindings')
    return json({
      items: [
        {
          binding_id: manifest.binding_id,
          revision: manifest.binding_revision,
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

async function editName(name: string) {
  await userEvent.clear(screen.getByLabelText('Name'));
  await userEvent.type(screen.getByLabelText('Name'), name);
}

async function reviewRead(request: Request) {
  const path = new URL(request.url).pathname;
  if (path.startsWith('/v1/products/')) return json(product);
  return json(path.endsWith('/diff') ? diff : path.includes('/tests/') ? run : draft);
}

async function navigate(path: string) {
  await act(async () => {
    window.history.pushState({}, '', path);
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
}

async function confirmPublish() {
  await userEvent.click(screen.getByRole('button', { name: 'Publish' }));
  const dialog = screen.getByRole('dialog');
  await userEvent.type(
    within(dialog).getByLabelText('Transition reason'),
    'Reviewed exact evidence',
  );
  await userEvent.click(within(dialog).getByRole('button', { name: 'Confirm publication' }));
}

it.each(['503', 'network'])(
  'preserves dirty editor values through a background %s failure and blocks writes until a fresh read',
  async (failure) => {
    let readState: 'ready' | 'failed' | 'retry' = 'ready';
    let finish: (response: Response) => void = () => {};
    let writes = 0;
    await open('/drafts/draft-1/edit', async (request) => {
      if (request.method !== 'GET') writes++;
      if (new URL(request.url).pathname === '/v1/drafts/draft-1') {
        if (readState === 'failed') {
          if (failure === 'network') throw new TypeError('Lost response');
          return json({ code: 'UPSTREAM_UNAVAILABLE' }, 503);
        }
        if (readState === 'retry')
          return new Promise<Response>((resolve) => {
            finish = resolve;
          });
      }
      return editorRead(request);
    });
    await screen.findByRole('heading', { name: 'Edit definition' });
    await editName('Unsaved important work');
    const original = screen.getByLabelText('Name');
    readState = 'failed';
    await act(async () => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await screen.findByRole('alert');
    expect(screen.getByLabelText('Name')).toBe(original);
    expect(original).toHaveValue('Unsaved important work');
    expect(screen.getByText(/Unsaved changes/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Run tests' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Save draft' }));
    readState = 'retry';
    await userEvent.click(screen.getByRole('button', { name: 'Retry read' }));
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled();
    await act(async () => finish(json({ ...draft, definition: manifest })));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save draft' })).toBeEnabled());
    expect(screen.getByLabelText('Name')).toBe(original);
    expect(original).toHaveValue('Unsaved important work');
    expect(screen.getByText(/Unsaved changes/)).toBeVisible();
    expect(writes).toBe(0);
  },
);

it('shows an unavailable screen when the initial draft read fails without an authorized snapshot', async () => {
  await open('/drafts/draft-1/edit', async () => json({ code: 'UPSTREAM_UNAVAILABLE' }, 503));
  await screen.findByRole('heading', { name: 'Definition unavailable' });
  expect(screen.queryByLabelText('Name')).toBeNull();
  expect(screen.getByRole('button', { name: 'Retry read' })).toBeEnabled();
});

it('blocks testing a clean saved revision while its latest draft read is unavailable', async () => {
  let failed = false;
  let writes = 0;
  await open('/drafts/draft-1/edit', async (request) => {
    if (request.method !== 'GET') writes++;
    if (failed && new URL(request.url).pathname === '/v1/drafts/draft-1')
      return json({ code: 'UPSTREAM_UNAVAILABLE' }, 503);
    return editorRead(request);
  });
  await screen.findByRole('heading', { name: 'Edit definition' });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Run tests' })).toBeEnabled());
  failed = true;
  await act(async () => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
  await screen.findByRole('alert');
  expect(screen.getByLabelText('Name')).toHaveValue(manifest.name);
  expect(screen.getByRole('button', { name: 'Run tests' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: 'Run tests' }));
  await userEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  expect(writes).toBe(0);
  failed = false;
  await userEvent.click(screen.getByRole('button', { name: 'Retry read' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Run tests' })).toBeEnabled());
  expect(writes).toBe(0);
});

it.each([403, 404])(
  'removes dirty fields and preview after a background %s denial',
  async (status) => {
    let denied = false;
    await open('/drafts/draft-1/edit', async (request) =>
      denied && new URL(request.url).pathname === '/v1/drafts/draft-1'
        ? json({ code: 'NOT_FOUND_OR_FORBIDDEN' }, status)
        : editorRead(request),
    );
    await screen.findByRole('heading', { name: 'Edit definition' });
    await editName('Unsaved protected work');
    denied = true;
    await act(async () => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await screen.findByRole('heading', { name: 'View unavailable' });
    expect(screen.queryByLabelText('Name')).toBeNull();
    expect(screen.queryByText('Read-only manifest preview')).toBeNull();
    expect(screen.queryByText(/Unsaved protected work/)).toBeNull();
  },
);

async function confirmReload() {
  await editName('Local work');
  await userEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await screen.findByRole('alert');
  await userEvent.click(screen.getByRole('button', { name: 'Reload current revision' }));
  await userEvent.click(
    within(screen.getByRole('dialog')).getByRole('button', { name: 'Discard and reload' }),
  );
}

async function assertReloadLocked() {
  const name = screen.getByLabelText('Name');
  expect(name).toBeDisabled();
  expect(screen.getByLabelText('Approved binding revision')).toBeDisabled();
  expect(screen.getByLabelText('Approved access policy')).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Run tests' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Review publication' })).toBeDisabled();
  const reload = screen.getByRole('button', { name: 'Reloading current revision…' });
  expect(reload).toBeDisabled();
  await userEvent.type(name, ' newly entered');
  await userEvent.dblClick(reload);
  await userEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  expect(name).toHaveValue('Local work');
}

it('locks edits and competing actions through both confirmed current-head reload reads', async () => {
  const newest = {
    ...draft,
    id: 'draft-2',
    revision: 2,
    current_draft_id: 'draft-2',
    definition: { ...manifest, name: 'Remote newest' },
  };
  const finish: Array<(response: Response) => void> = [];
  const reloadReads: string[] = [];
  let writes = 0;
  await open('/drafts/draft-1/edit', async (request) => {
    const path = new URL(request.url).pathname;
    if (request.method === 'PUT') {
      writes++;
      return json({ code: 'CONFLICT' }, 409);
    }
    if (path === '/v1/drafts/draft-2' && window.location.pathname === '/drafts/draft-2/edit')
      return json(newest);
    if (writes && path.startsWith('/v1/drafts/')) {
      reloadReads.push(path);
      return new Promise<Response>((resolve) => finish.push(resolve));
    }
    return editorRead(request);
  });
  await screen.findByRole('heading', { name: 'Edit definition' });
  await confirmReload();
  await assertReloadLocked();
  expect(reloadReads).toEqual(['/v1/drafts/draft-1']);
  await act(async () =>
    finish[0](json({ ...draft, definition: manifest, current_draft_id: 'draft-2' })),
  );
  await waitFor(() => expect(reloadReads).toHaveLength(2));
  await assertReloadLocked();
  await act(async () => finish[1](json(newest)));
  await waitFor(() => expect(window.location.pathname).toBe('/drafts/draft-2/edit'));
  expect(screen.getByLabelText('Name')).toHaveValue('Remote newest');
  expect(screen.getByLabelText('Name')).toBeEnabled();
  expect(reloadReads).toEqual(['/v1/drafts/draft-1', '/v1/drafts/draft-2']);
  expect(writes).toBe(1);
});

it.each(['locator', 'head'])(
  'restores the unchanged dirty editor when the pending %s reload read fails',
  async (stage) => {
    let finish: (response: Response) => void = () => {};
    let writes = 0;
    const reads: string[] = [];
    await open('/drafts/draft-1/edit', async (request) => {
      const path = new URL(request.url).pathname;
      if (request.method === 'PUT') {
        writes++;
        return json({ code: 'CONFLICT' }, 409);
      }
      if (writes && path.startsWith('/v1/drafts/')) {
        reads.push(path);
        if (stage === 'head' && path === '/v1/drafts/draft-1')
          return json({ ...draft, definition: manifest, current_draft_id: 'draft-2' });
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
      }
      return editorRead(request);
    });
    await screen.findByRole('heading', { name: 'Edit definition' });
    await confirmReload();
    await assertReloadLocked();
    await act(async () => finish(json({ code: 'UPSTREAM_UNAVAILABLE' }, 503)));
    await screen.findByRole('alert');
    await waitFor(() => expect(screen.getByLabelText('Name')).toBeEnabled());
    expect(screen.getByLabelText('Name')).toHaveValue('Local work');
    expect(screen.getByText(/Unsaved changes/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Reload current revision' })).toBeEnabled();
    expect(window.location.pathname).toBe('/drafts/draft-1/edit');
    expect(reads).toEqual(
      stage === 'head' ? ['/v1/drafts/draft-1', '/v1/drafts/draft-2'] : ['/v1/drafts/draft-1'],
    );
    expect(writes).toBe(1);
  },
);

it.each([
  ['query', 'success'],
  ['query', 'conflict'],
  ['query', 'network'],
  ['hash', 'success'],
  ['hash', 'conflict'],
  ['hash', 'network'],
])(
  'settles a pending publication after same-draft %s navigation with a %s response',
  async (navigation, outcome) => {
    let finish: (response: Response) => void = () => {};
    let fail: (error: unknown) => void = () => {};
    const writes: Array<{ path: string; body: unknown }> = [];
    const productReads: string[] = [];
    await open('/drafts/draft-1/review?run=run-1', async (request) => {
      const url = new URL(request.url);
      if (request.method === 'POST') {
        writes.push({ path: url.pathname, body: await request.json() });
        return new Promise<Response>((resolve, reject) => {
          finish = resolve;
          fail = reject;
        });
      }
      if (url.pathname.startsWith('/v1/products/')) productReads.push(url.pathname + url.search);
      return reviewRead(request);
    });
    await screen.findByRole('heading', { name: 'Review publication' });
    await confirmPublish();
    const destination =
      navigation === 'query'
        ? '/drafts/draft-1/review'
        : '/drafts/draft-1/review?run=run-1#evidence';
    await navigate(destination);
    await screen.findByRole('heading', { name: 'Review publication', hidden: true });
    await act(async () => {
      if (outcome === 'network') fail(new TypeError('Lost response'));
      else
        finish(
          outcome === 'conflict'
            ? json({ code: 'CONFLICT' }, 409)
            : json({
                product_id: 'revenue',
                version: '1.0.0',
                version_id: 'version-1',
                digest: draft.digest,
              }),
        );
    });
    if (outcome === 'success') {
      expect(await screen.findByText('Published version 1.0.0')).toBeVisible();
      expect(screen.queryByRole('button', { name: 'Check publication outcome' })).toBeNull();
    } else {
      const recovery =
        outcome === 'conflict' ? 'Reload current revision' : 'Check publication outcome';
      expect(await screen.findByRole('button', { name: recovery })).toBeEnabled();
      expect(screen.getByRole('button', { name: 'Refresh review' })).toBeEnabled();
      expect(screen.getByRole('button', { name: 'Publish' })).toBeDisabled();
      if (outcome === 'network') {
        await userEvent.click(screen.getByRole('button', { name: recovery }));
        await screen.findByText('Published version 1.0.0');
        expect(productReads).toEqual(['/v1/products/revenue?version=1.0.0']);
      }
    }
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(window.location.pathname + window.location.search + window.location.hash).toBe(
      destination,
    );
    expect(writes).toEqual([
      {
        path: '/v1/drafts/draft-1/publish',
        body: {
          tested_digest: draft.digest,
          expected_revision: draft.revision,
          reason: 'Reviewed exact evidence',
        },
      },
    ]);
  },
);

it.each([
  ['draft', 'success'],
  ['draft', 'conflict'],
  ['draft', 'network'],
  ['draft', 'denied'],
  ['session', 'success'],
  ['session', 'conflict'],
  ['session', 'network'],
  ['session', 'denied'],
])(
  'protects a replacement %s from a late %s publication response',
  async (replacement, outcome) => {
    let finish: (response: Response) => void = () => {};
    let fail: (error: unknown) => void = () => {};
    let writes = 0;
    await open('/drafts/draft-1/review', async (request) => {
      const path = new URL(request.url).pathname;
      if (request.method === 'POST') {
        writes++;
        return new Promise<Response>((resolve, reject) => {
          finish = resolve;
          fail = reject;
        });
      }
      if (path.includes('draft-2'))
        return json(
          path.endsWith('/diff')
            ? {
                ...diff,
                draft_id: 'draft-2',
                latest_test_run_id: null,
                stale_evidence: ['missing'],
              }
            : {
                ...draft,
                id: 'draft-2',
                current_draft_id: 'draft-2',
                definition: { ...draft.definition, name: 'Replacement review' },
              },
        );
      return reviewRead(request);
    });
    await screen.findByRole('heading', { name: 'Review publication' });
    await confirmPublish();
    if (replacement === 'draft') {
      await navigate('/drafts/draft-2/review');
      await screen.findByText('Replacement review · Version 1.0.0 · Revision 1');
    } else {
      // Exercise session replacement even though the pending dialog makes shell controls inert.
      fireEvent.click(screen.getByRole('button', { name: 'End session', hidden: true }));
      await userEvent.type(
        screen.getByLabelText('Session bearer'),
        'replacement-synthetic-session',
      );
      await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
      await screen.findByRole('heading', { name: 'Review publication' });
    }
    const destination = window.location.pathname + window.location.search;
    await act(async () => {
      if (outcome === 'network') fail(new TypeError('Lost response'));
      else
        finish(
          outcome === 'conflict'
            ? json({ code: 'CONFLICT' }, 409)
            : outcome === 'denied'
              ? json({ code: 'NOT_FOUND_OR_FORBIDDEN' }, 404)
              : json({
                  product_id: 'revenue',
                  version: '1.0.0',
                  version_id: 'version-1',
                  digest: draft.digest,
                }),
        );
    });
    expect(screen.queryByText('Published version 1.0.0')).toBeNull();
    expect(screen.queryByText('Publication outcome not yet confirmed.')).toBeNull();
    expect(screen.queryByRole('heading', { name: 'View unavailable' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Reload current revision' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'Review publication' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Refresh review' })).toBeEnabled();
    expect(window.location.pathname + window.location.search).toBe(destination);
    expect(writes).toBe(1);
  },
);

it.each(['query', 'hash'])(
  'clears protected review evidence after a same-draft %s navigation and a denied pending publication',
  async (navigation) => {
    let finish: (response: Response) => void = () => {};
    let writes = 0;
    await open('/drafts/draft-1/review?run=run-1', async (request) => {
      if (request.method === 'POST') {
        writes++;
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
      }
      return reviewRead(request);
    });
    await screen.findByRole('heading', { name: 'Review publication' });
    await confirmPublish();
    await navigate(
      navigation === 'query'
        ? '/drafts/draft-1/review'
        : '/drafts/draft-1/review?run=run-1#evidence',
    );
    await act(async () => finish(json({ code: 'NOT_FOUND_OR_FORBIDDEN' }, 404)));
    await screen.findByRole('heading', { name: 'View unavailable' });
    expect(screen.queryByText('Authorized evidence pins')).toBeNull();
    expect(screen.queryByText(/9007199254741023/)).toBeNull();
    expect(screen.queryByText('Published version 1.0.0')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Check publication outcome' })).toBeNull();
    expect(writes).toBe(1);
  },
);

async function loseLifecycle(action: 'deprecate' | 'withdraw' | 'rollback') {
  await userEvent.click(await screen.findByText('Lifecycle actions for this product'));
  await userEvent.click(screen.getByRole('button', { name: 'Open existing published version' }));
  await screen.findByLabelText('Existing target version');
  await userEvent.selectOptions(screen.getByLabelText('Lifecycle action'), action);
  await userEvent.click(screen.getByRole('button', { name: 'Review lifecycle change' }));
  const dialog = screen.getByRole('dialog');
  await userEvent.type(within(dialog).getByLabelText('Lifecycle reason'), 'Reviewed exact target');
  await userEvent.click(within(dialog).getByRole('button', { name: 'Confirm lifecycle change' }));
  await screen.findByText(/Lifecycle outcome not yet confirmed/);
}

it('offers an exact lifecycle recovery read that hides stale metadata and verifies an observed deprecation', async () => {
  const reads: string[] = [];
  const writes: unknown[] = [];
  let finish: (response: Response) => void = () => {};
  await open('/drafts/draft-1/review', async (request) => {
    const url = new URL(request.url);
    if (request.method === 'POST') {
      writes.push(await request.json());
      throw new TypeError('Lost response');
    }
    if (url.pathname.startsWith('/v1/products/')) {
      reads.push(url.pathname + url.search);
      if (reads.length > 1)
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
    }
    return reviewRead(request);
  });
  await loseLifecycle('deprecate');
  expect(screen.getByText(/Reviewed target: Daily revenue · published/)).toBeVisible();
  const read = screen.getByRole('button', { name: 'Open existing published version' });
  expect(read).toBeEnabled();
  await userEvent.dblClick(read);
  expect(screen.queryByText(/Reviewed target:/)).toBeNull();
  expect(screen.queryByLabelText('Existing target version')).toBeNull();
  expect(screen.getByRole('button', { name: 'Opening existing version…' })).toBeDisabled();
  await act(async () =>
    finish(json({ ...product, status: 'deprecated', name: 'Updated authorized name' })),
  );
  expect(await screen.findByText('Authorized version state: deprecated 1.0.0.')).toBeVisible();
  expect(screen.getByText(/Reviewed target: Updated authorized name · deprecated/)).toBeVisible();
  expect(screen.queryByText(/Lifecycle outcome not yet confirmed/)).toBeNull();
  expect(screen.getByRole('button', { name: 'Review lifecycle change' })).toBeEnabled();
  expect(reads).toEqual([
    '/v1/products/revenue?version=1.0.0',
    '/v1/products/revenue?version=1.0.0',
  ]);
  expect(writes).toEqual([
    { action: 'deprecate', version: '1.0.0', reason: 'Reviewed exact target' },
  ]);
});

it.each([
  ['deprecate', 'published'],
  ['rollback', 'published'],
  ['rollback', 'deprecated'],
  ['withdraw', 'published'],
  ['withdraw', 'deprecated'],
] as const)(
  'keeps an uncertain %s unresolved when an exact target read reports %s',
  async (action, status) => {
    let writes = 0;
    const reads: string[] = [];
    await open('/drafts/draft-1/review', async (request) => {
      const url = new URL(request.url);
      if (request.method === 'POST') {
        writes++;
        throw new TypeError('Lost response');
      }
      if (url.pathname.startsWith('/v1/products/')) {
        reads.push(url.pathname + url.search);
        return json(
          reads.length === 1 ? product : { ...product, status, name: 'Fresh authorized target' },
        );
      }
      return reviewRead(request);
    });
    await loseLifecycle(action);
    await userEvent.click(screen.getByRole('button', { name: 'Open existing published version' }));
    await screen.findByText(/Reviewed target: Fresh authorized target/);
    expect(screen.getByText(/Lifecycle outcome not yet confirmed/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Review lifecycle change' })).toBeDisabled();
    expect(screen.getByLabelText('Lifecycle action')).toBeDisabled();
    expect(screen.queryByText(/Gateway confirmed:/)).toBeNull();
    expect(screen.queryByText(/Authorized version state:/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Open existing published version' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Review lifecycle change' }));
    expect(writes).toBe(1);
    expect(reads).toEqual([
      '/v1/products/revenue?version=1.0.0',
      '/v1/products/revenue?version=1.0.0',
    ]);
  },
);

it.each([403, 404])(
  'hides lifecycle target metadata after a denied %s recovery read without claiming withdrawal',
  async (status) => {
    let reads = 0;
    let writes = 0;
    await open('/drafts/draft-1/review', async (request) => {
      if (request.method === 'POST') {
        writes++;
        throw new TypeError('Lost response');
      }
      if (new URL(request.url).pathname.startsWith('/v1/products/') && ++reads > 1)
        return json({ code: 'NOT_FOUND_OR_FORBIDDEN' }, status);
      return reviewRead(request);
    });
    await loseLifecycle('withdraw');
    await userEvent.click(screen.getByRole('button', { name: 'Open existing published version' }));
    await screen.findByRole('alert');
    expect(screen.queryByText(/Reviewed target:/)).toBeNull();
    expect(screen.queryByLabelText('Existing target version')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Review lifecycle change' })).toBeNull();
    expect(screen.getByText(/Lifecycle outcome not yet confirmed/)).toBeVisible();
    expect(screen.queryByText(/Gateway confirmed:/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Open existing published version' })).toBeEnabled();
    expect(screen.getByRole('heading', { name: 'Review publication' })).toBeVisible();
    expect(reads).toBe(2);
    expect(writes).toBe(1);
  },
);

it('does not treat a different immutable digest as confirmation of the uncertain lifecycle target', async () => {
  let reads = 0;
  let writes = 0;
  await open('/drafts/draft-1/review', async (request) => {
    if (request.method === 'POST') {
      writes++;
      throw new TypeError('Lost response');
    }
    if (new URL(request.url).pathname.startsWith('/v1/products/') && ++reads > 1)
      return json({ ...product, digest: 'c'.repeat(64), status: 'deprecated' });
    return reviewRead(request);
  });
  await loseLifecycle('deprecate');
  await userEvent.click(screen.getByRole('button', { name: 'Open existing published version' }));
  await screen.findByRole('alert');
  expect(screen.queryByText(/Reviewed target:/)).toBeNull();
  expect(screen.getByText(/Lifecycle outcome not yet confirmed/)).toBeVisible();
  expect(screen.queryByText(/Authorized version state:/)).toBeNull();
  expect(screen.queryByText(/Gateway confirmed:/)).toBeNull();
  expect(writes).toBe(1);
});

it('keeps a failed lifecycle recovery read safely retryable without replaying the change', async () => {
  let reads = 0;
  let writes = 0;
  await open('/drafts/draft-1/review', async (request) => {
    if (request.method === 'POST') {
      writes++;
      throw new TypeError('Lost mutation response');
    }
    if (new URL(request.url).pathname.startsWith('/v1/products/')) {
      reads++;
      if (reads === 2) throw new TypeError('Lost recovery read');
      if (reads === 3) return json({ ...product, status: 'deprecated' });
    }
    return reviewRead(request);
  });
  await loseLifecycle('deprecate');
  await userEvent.click(screen.getByRole('button', { name: 'Open existing published version' }));
  await screen.findByRole('alert');
  expect(screen.queryByText(/Reviewed target:/)).toBeNull();
  expect(screen.getByText(/Lifecycle outcome not yet confirmed/)).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Review lifecycle change' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Open existing published version' })).toBeEnabled();
  await userEvent.click(screen.getByRole('button', { name: 'Open existing published version' }));
  await screen.findByText('Authorized version state: deprecated 1.0.0.');
  expect(reads).toBe(3);
  expect(writes).toBe(1);
});
