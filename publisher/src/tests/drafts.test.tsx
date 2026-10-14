import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { focusManager, QueryClient, type QueryCache } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../App';
import { json } from './fixtures';

const editable = {
  id: 'editable-draft',
  product_id: 'revenue',
  version: '1.0.0',
  name: 'Private revenue draft',
  revision: 3,
  editable_full_definition: true,
  can_test: false,
  can_publish: false,
};
const readonly = {
  ...editable,
  id: 'review-only-draft',
  product_id: 'orders',
  name: 'Authorized orders draft',
  editable_full_definition: false,
  can_test: true,
  can_publish: true,
};
const page = (items = [editable], next_cursor: string | null = null) => ({ items, next_cursor });
const denied = () =>
  json(
    {
      code: 'NOT_FOUND_OR_FORBIDDEN',
      safe_message: 'PRIVATE SQL PASSWORD',
      request_id: 'draft-access-revoked',
    },
    404,
  );

afterEach(() => {
  focusManager.setFocused(undefined);
  vi.unstubAllGlobals();
});

async function signIn(bearer = 'synthetic-test') {
  await userEvent.type(screen.getByLabelText('Session bearer'), bearer);
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
}
async function open(fetcher: (request: Request) => Promise<Response>, path = '/drafts') {
  window.history.replaceState({}, '', path);
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  await signIn();
}
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
function expectEmptyCaches(caches: Set<QueryCache>) {
  expect(caches.size).toBeGreaterThan(0);
  for (const cache of caches) expect(cache.getAll()).toEqual([]);
}
function expectNoPrivateDrafts() {
  expect(screen.queryByText(editable.name)).toBeNull();
  expect(screen.queryByText(readonly.name)).toBeNull();
  expect(document.querySelector('main')?.innerHTML).not.toContain('/drafts/editable-draft');
  expect(document.querySelector('main')?.innerHTML).not.toContain('/drafts/review-only-draft');
  expect(screen.queryByRole('link', { name: 'Resume editing' })).toBeNull();
  expect(screen.queryByRole('link', { name: 'Review draft' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Next drafts' })).toBeNull();
}
async function finishRead(finish: (response: Response) => void, response: Response) {
  await act(async () => {
    finish(response);
    await new Promise((resolve) => setTimeout(resolve, 25));
  });
}

it('requires a session and lists only authorized draft links without publication or test claims', async () => {
  const requests: Request[] = [];
  window.history.replaceState({}, '', '/drafts');
  vi.stubGlobal('fetch', async (request: Request) => {
    requests.push(request);
    return json(page([editable, readonly]));
  });
  render(<App />);
  expect(screen.getByLabelText('Session bearer')).toBeVisible();
  expect(requests).toHaveLength(0);
  expect(screen.queryByRole('heading', { name: 'Saved drafts' })).toBeNull();
  await signIn();
  await screen.findByRole('link', { name: editable.name });
  expect(screen.getByRole('heading', { name: 'Saved drafts' })).toBeVisible();
  expect(screen.getByRole('link', { name: 'Drafts' })).toHaveAttribute('href', '/drafts');
  expect(screen.getByRole('link', { name: editable.name })).toHaveAttribute(
    'href',
    '/drafts/editable-draft/review',
  );
  expect(screen.getByRole('link', { name: readonly.name })).toHaveAttribute(
    'href',
    '/drafts/review-only-draft/review',
  );
  expect(screen.getByRole('link', { name: 'Resume editing' })).toHaveAttribute(
    'href',
    '/drafts/editable-draft/edit',
  );
  expect(screen.getAllByRole('link', { name: 'Review draft' })).toHaveLength(2);
  expect(screen.queryByRole('button', { name: /publish|run tests/i })).toBeNull();
  expect(screen.queryByText(/tests passed|ready to publish|latest test|test status/i)).toBeNull();
  expect(requests).toHaveLength(1);
  expect(requests[0].method).toBe('GET');
  expect(new URL(requests[0].url).pathname).toBe('/v1/drafts');
});

it('distinguishes a pending authorized read from an empty saved-draft list', async () => {
  let finish: (response: Response) => void = () => {};
  await open(
    () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  expect(await screen.findByText('Loading saved drafts…')).toBeVisible();
  expect(screen.queryByText('No saved drafts available for this account.')).toBeNull();
  await finishRead(finish, json(page([])));
  expect(screen.getByText('No saved drafts available for this account.')).toBeVisible();
  expect(screen.queryByText('Loading saved drafts…')).toBeNull();
  expectNoPrivateDrafts();
});

it('preserves the search while paging, hides old results during transitions, and resets the cursor on search', async () => {
  const urls: URL[] = [];
  let finish: (response: Response) => void = () => {};
  await open(async (request) => {
    const url = new URL(request.url);
    urls.push(url);
    if (url.searchParams.get('cursor'))
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    return json(page([editable], 'opaque-next'));
  }, '/drafts?q=private%20revenue');
  await screen.findByRole('link', { name: editable.name });
  expect(screen.getByLabelText('Search saved drafts')).toHaveValue('private revenue');
  expect(urls[0].searchParams.get('q')).toBe('private revenue');
  expect(urls[0].searchParams.get('limit')).toBe('50');
  await userEvent.click(screen.getByRole('button', { name: 'Next drafts' }));
  expect(await screen.findByText('Loading saved drafts…')).toBeVisible();
  expectNoPrivateDrafts();
  expect(urls.at(-1)!.searchParams.get('q')).toBe('private revenue');
  expect(urls.at(-1)!.searchParams.get('cursor')).toBe('opaque-next');
  await finishRead(finish, json(page([readonly])));
  expect(screen.getByRole('link', { name: readonly.name })).toBeVisible();
  expect(screen.queryByRole('link', { name: editable.name })).toBeNull();
  await userEvent.click(screen.getByRole('button', { name: 'First drafts' }));
  await screen.findByRole('link', { name: editable.name });
  expect(urls.at(-1)!.searchParams.get('q')).toBe('private revenue');
  expect(urls.at(-1)!.searchParams.has('cursor')).toBe(false);
  await userEvent.click(screen.getByRole('button', { name: 'Next drafts' }));
  await finishRead(finish, json(page([readonly])));
  await userEvent.clear(screen.getByLabelText('Search saved drafts'));
  await userEvent.type(screen.getByLabelText('Search saved drafts'), 'orders & sales');
  await userEvent.click(screen.getByRole('button', { name: 'Search' }));
  await screen.findByRole('link', { name: editable.name });
  expect(urls.at(-1)!.searchParams.get('q')).toBe('orders & sales');
  expect(urls.at(-1)!.searchParams.has('cursor')).toBe(false);
  expect(new URLSearchParams(window.location.search).get('q')).toBe('orders & sales');
  expect(new URLSearchParams(window.location.search).has('cursor')).toBe(false);
  expect(urls.every((url) => url.pathname === '/v1/drafts')).toBe(true);
});

it('hides every previously loaded card and paging action throughout a refresh and a transient failure', async () => {
  let reads = 0;
  let finish: (response: Response) => void = () => {};
  await open(async () =>
    ++reads === 1
      ? json(page([editable, readonly], 'private-cursor'))
      : new Promise<Response>((resolve) => {
          finish = resolve;
        }),
  );
  await screen.findByRole('link', { name: editable.name });
  await userEvent.click(screen.getByRole('button', { name: 'Refresh drafts' }));
  expect(await screen.findByText('Loading saved drafts…')).toBeVisible();
  expectNoPrivateDrafts();
  await finishRead(
    finish,
    json({ code: 'UPSTREAM_UNAVAILABLE', safe_message: 'PRIVATE SQL PASSWORD' }, 503),
  );
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'The gateway or source is temporarily unavailable.',
  );
  expectNoPrivateDrafts();
  expect(screen.queryByText(/PRIVATE SQL PASSWORD/)).toBeNull();
  expect(reads).toBe(2);
});

it('clears private search, draft links, and all session query caches when focus revalidation is denied', async () => {
  const caches = observeCaches();
  let reads = 0;
  let finish: (response: Response) => void = () => {};
  await open(
    async () =>
      ++reads === 1
        ? json(page([editable, readonly], 'private-cursor'))
        : new Promise<Response>((resolve) => {
            finish = resolve;
          }),
    '/drafts?q=private-revenue',
  );
  await screen.findByRole('link', { name: editable.name });
  act(() => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
  expect(await screen.findByText('Loading saved drafts…')).toBeVisible();
  expectNoPrivateDrafts();
  await finishRead(finish, denied());
  await screen.findByRole('heading', { name: 'View unavailable' });
  expect(screen.getByRole('alert')).toHaveTextContent('This item is unavailable to this account.');
  expect(screen.getByRole('alert')).toHaveTextContent('draft-access-revoked');
  expect(screen.queryByLabelText('Search saved drafts')).toBeNull();
  expect(screen.queryByText(/PRIVATE SQL PASSWORD/)).toBeNull();
  expectNoPrivateDrafts();
  expectEmptyCaches(caches);
});

it('expires the current account and clears draft queries when its refresh returns 401', async () => {
  const caches = observeCaches();
  let reads = 0;
  await open(async () =>
    ++reads === 1
      ? json(page([editable], 'private-cursor'))
      : json({ code: 'UNAUTHENTICATED', request_id: 'expired-draft-session' }, 401),
  );
  await screen.findByRole('link', { name: editable.name });
  await userEvent.click(screen.getByRole('button', { name: 'Refresh drafts' }));
  expect(await screen.findByLabelText('Session bearer')).toHaveValue('');
  expect(screen.getByText('Your session expired. Sign in again.')).toBeVisible();
  expect(screen.queryByRole('heading', { name: 'Saved drafts' })).toBeNull();
  expect(screen.queryByLabelText('Search saved drafts')).toBeNull();
  expectNoPrivateDrafts();
  expectEmptyCaches(caches);
  expect(reads).toBe(2);
});

it('does not restore an old in-flight result after a search is denied in the same draft-list scope', async () => {
  const caches = observeCaches();
  let reads = 0;
  let finishOld: (response: Response) => void = () => {};
  await open(async (request) => {
    if (new URL(request.url).searchParams.get('q') === 'revoked') return denied();
    if (++reads === 1) return json(page([editable], 'old-cursor'));
    return new Promise<Response>((resolve) => {
      finishOld = resolve;
    });
  });
  await screen.findByRole('link', { name: editable.name });
  await userEvent.click(screen.getByRole('button', { name: 'Refresh drafts' }));
  await screen.findByText('Loading saved drafts…');
  await userEvent.type(screen.getByLabelText('Search saved drafts'), 'revoked');
  await userEvent.click(screen.getByRole('button', { name: 'Search' }));
  await screen.findByRole('heading', { name: 'View unavailable' });
  await finishRead(finishOld, json(page([editable, readonly], 'late-private-cursor')));
  expectNoPrivateDrafts();
  expect(screen.queryByLabelText('Search saved drafts')).toBeNull();
  expectEmptyCaches(caches);
});

it.each(['success', 'unauthorized'])(
  'clears a replaced account and ignores its late %s response without affecting the new account',
  async (outcome) => {
    const caches = observeCaches();
    const newer = { ...readonly, id: 'new-account-draft', name: 'New account draft' };
    let oldReads = 0;
    let oldRequest: Request | undefined;
    let finishOld: (response: Response) => void = () => {};
    await open(async (request) => {
      if (request.headers.get('authorization') === 'Bearer next-account')
        return json(page([newer]));
      if (++oldReads === 1) return json(page([editable], 'old-private-cursor'));
      oldRequest = request;
      return new Promise<Response>((resolve) => {
        finishOld = resolve;
      });
    });
    await screen.findByRole('link', { name: editable.name });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh drafts' }));
    await screen.findByText('Loading saved drafts…');
    await userEvent.click(screen.getByRole('button', { name: 'End session' }));
    expectNoPrivateDrafts();
    expectEmptyCaches(caches);
    expect(screen.getByLabelText('Session bearer')).toHaveValue('');
    expect(oldRequest!.signal.aborted).toBe(true);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    await signIn('next-account');
    await screen.findByRole('link', { name: newer.name });
    await finishRead(
      finishOld,
      outcome === 'success'
        ? json(page([editable], 'late-private-cursor'))
        : json({ code: 'UNAUTHENTICATED' }, 401),
    );
    expect(screen.getByRole('link', { name: newer.name })).toBeVisible();
    expect(screen.queryByRole('link', { name: editable.name })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Next drafts' })).toBeNull();
    expect(screen.queryByLabelText('Session bearer')).toBeNull();
    for (const cache of caches) {
      expect(JSON.stringify(cache.getAll().map((query) => query.state.data))).not.toContain(
        editable.name,
      );
    }
  },
);

it('fails closed when a refresh returns an extra summary field instead of the approved projection', async () => {
  let reads = 0;
  await open(async () =>
    json(
      ++reads === 1
        ? page([editable], 'old-cursor')
        : {
            items: [{ ...editable, definition: { private_sql: 'PRIVATE SQL PASSWORD' } }],
            next_cursor: null,
          },
    ),
  );
  await screen.findByRole('link', { name: editable.name });
  await userEvent.click(screen.getByRole('button', { name: 'Refresh drafts' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'The gateway or source is temporarily unavailable.',
  );
  expectNoPrivateDrafts();
  expect(screen.queryByText(/PRIVATE SQL PASSWORD/)).toBeNull();
  expect(reads).toBe(2);
});
