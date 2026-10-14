import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import { App } from '../App';
import { draft, diff, run } from './review-fixtures';
import { json, product } from './fixtures';
beforeEach(() => window.history.replaceState({}, '', '/drafts/draft-1/review'));
afterEach(() => vi.unstubAllGlobals());
async function openReview(fetcher: (request: Request) => Promise<Response>) {
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-test');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
  await screen.findByRole('heading', { name: /Review publication/ });
}
async function standard(request: Request) {
  const path = new URL(request.url).pathname;
  return json(path.endsWith('/diff') ? diff : path.includes('/tests/') ? run : draft);
}
async function confirmPublish() {
  await userEvent.click(screen.getByRole('button', { name: 'Publish' }));
  const dialog = screen.getByRole('dialog');
  await userEvent.type(
    within(dialog).getByLabelText('Transition reason'),
    'Verified definitions and fixtures',
  );
  return within(dialog).getByRole('button', { name: 'Confirm publication' });
}
it('requires matching exact assessed evidence and server publisher capability', async () => {
  await openReview(standard);
  expect(screen.getByRole('button', { name: 'Publish' })).toBeEnabled();
  expect(screen.getByText('Revenue · Version 1.0.0 · Revision 1')).toBeVisible();
});
it.each(['stale', 'wrong-run', 'wrong-draft', 'author-only', 'missing-negative'])(
  'blocks publication for %s',
  async (variant) => {
    await openReview(async (request) => {
      const path = new URL(request.url).pathname;
      if (path.endsWith('/diff'))
        return json({ ...diff, ...(variant === 'stale' ? { stale_evidence: ['policy'] } : {}) });
      if (path.includes('/tests/'))
        return json({
          ...run,
          ...(variant === 'wrong-run' ? { id: 'another-run' } : {}),
          ...(variant === 'wrong-draft' ? { draft_digest: 'c'.repeat(64) } : {}),
          ...(variant === 'missing-negative' ? { checks: [run.checks[0]] } : {}),
        });
      return json({ ...draft, ...(variant === 'author-only' ? { can_publish: false } : {}) });
    });
    expect(screen.getByRole('button', { name: 'Publish' })).toBeDisabled();
  },
);
it('keeps publish pending until confirmed and guards repeated submission', async () => {
  let finish: (value: Response) => void = () => {};
  let writes = 0;
  await openReview(async (request) => {
    if (request.method === 'POST') {
      writes++;
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    }
    return standard(request);
  });
  await userEvent.dblClick(await confirmPublish());
  expect(screen.getByRole('button', { name: 'Submitting…' })).toBeDisabled();
  expect(writes).toBe(1);
  expect(screen.queryByText(/Published version/)).toBeNull();
  finish(
    json({
      product_id: 'revenue',
      version: '1.0.0',
      version_id: 'version-1',
      digest: draft.digest,
    }),
  );
  expect(await screen.findByText('Published version 1.0.0')).toBeVisible();
});
it('requires explicit current-head reload after conflict and does not retry publication', async () => {
  let writes = 0;
  await openReview(async (request) => {
    const path = new URL(request.url).pathname;
    if (request.method === 'POST') {
      writes++;
      return json({ code: 'CONFLICT', request_id: 'conflict-1' }, 409);
    }
    if (path === '/v1/drafts/draft-1' && writes)
      return json({ ...draft, is_current_revision: false, current_draft_id: 'draft-2' });
    if (path.includes('draft-2'))
      return json(
        path.endsWith('/diff')
          ? {
              ...diff,
              draft_id: 'draft-2',
              revision: 2,
              latest_test_run_id: null,
              stale_evidence: ['missing'],
            }
          : { ...draft, id: 'draft-2', revision: 2, current_draft_id: 'draft-2' },
      );
    return standard(request);
  });
  await userEvent.click(await confirmPublish());
  expect(await screen.findByRole('alert')).toHaveTextContent('This revision changed');
  expect(screen.getByRole('button', { name: 'Publish' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: 'Reload current revision' }));
  await waitFor(() => expect(window.location.pathname).toBe('/drafts/draft-2/review'));
  expect(await screen.findByText(/Revision 2/)).toBeVisible();
  expect(writes).toBe(1);
});
it('treats a lost publish response as uncertain and reconciles only the exact pinned digest', async () => {
  let writes = 0;
  await openReview(async (request) => {
    if (request.method === 'POST') {
      writes++;
      throw new TypeError('network body includes PRIVATE');
    }
    if (new URL(request.url).pathname.startsWith('/v1/products/')) return json(product);
    return standard(request);
  });
  await userEvent.click(await confirmPublish());
  expect(await screen.findByText('Publication outcome not yet confirmed.')).toBeVisible();
  expect(screen.getByRole('button', { name: 'Publish' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: 'Check publication outcome' }));
  expect(await screen.findByText('Published version 1.0.0')).toBeVisible();
  expect(writes).toBe(1);
  expect(screen.queryByText(/PRIVATE/)).toBeNull();
});
it('removes protected evidence after a publication permission denial', async () => {
  await openReview(async (request) =>
    request.method === 'POST'
      ? json({ code: 'NOT_FOUND_OR_FORBIDDEN', request_id: 'denied-publish' }, 404)
      : standard(request),
  );
  await userEvent.click(await confirmPublish());
  await screen.findByRole('alert');
  expect(screen.queryByText('Authorized evidence pins')).toBeNull();
  expect(screen.queryByText('"9007199254741023.31"')).toBeNull();
});

it('returns keyboard focus to the publish trigger after Escape', async () => {
  await openReview(standard);
  const trigger = screen.getByRole('button', { name: 'Publish' });
  await userEvent.click(trigger);
  expect(screen.getByRole('dialog')).toBeVisible();
  await userEvent.keyboard('{Escape}');
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(trigger).toHaveFocus();
});
