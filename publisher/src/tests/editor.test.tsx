import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import { App } from '../App';
import { manifest, json } from './fixtures';
import { draft, run, diff } from './review-fixtures';
const editable = { ...draft, definition: manifest };
const binding = {
  binding_id: manifest.binding_id,
  revision: 1,
  digest: 'b'.repeat(64),
  adapter_revision: manifest.adapter_revision,
  model_bundle_digest: manifest.model_bundle_digest,
};
async function standard(request: Request) {
  const path = new URL(request.url).pathname;
  if (path === '/v1/bindings') return json({ items: [binding], next_cursor: null });
  if (path === '/v1/policies')
    return json({ items: [{ policy_id: manifest.policy_id, revision: 1 }], next_cursor: null });
  return json(path.endsWith('/diff') ? diff : editable);
}
beforeEach(() => window.history.replaceState({}, '', '/drafts/draft-1/edit'));
afterEach(() => vi.unstubAllGlobals());
async function openEditor(fetcher: (r: Request) => Promise<Response> = standard) {
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-test');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
  await screen.findByRole('heading', { name: 'Edit definition' });
}
it('saves the exact edited revision and switches to the new immutable ID', async () => {
  let body: any;
  let reads: string[] = [];
  await openEditor(async (request) => {
    const path = new URL(request.url).pathname;
    reads.push(path);
    if (request.method === 'PUT') {
      body = await request.json();
      return json({ id: 'draft-2', product_id: 'revenue', revision: 2, digest: 'c'.repeat(64) });
    }
    if (path === '/v1/drafts/draft-2')
      return json({
        ...editable,
        id: 'draft-2',
        revision: 2,
        digest: 'c'.repeat(64),
        current_draft_id: 'draft-2',
        definition: { ...manifest, name: 'Updated revenue' },
      });
    return standard(request);
  });
  const name = screen.getByLabelText('Name');
  await userEvent.clear(name);
  await userEvent.type(name, 'Updated revenue');
  expect(screen.getByRole('button', { name: 'Review publication' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await waitFor(() => expect(window.location.pathname).toBe('/drafts/draft-2/edit'));
  expect(body.expected_revision).toBe(1);
  expect(body.manifest.content_digest).toBeNull();
  expect(body.manifest.fixtures[0].expected_rows[2].revenue).toBe('9007199254740993.01');
  expect(reads).toContain('/v1/drafts/draft-2');
});
it('does not offer full replacement for a projected definition', async () => {
  await openEditor(async (request) =>
    new URL(request.url).pathname === '/v1/drafts/draft-1'
      ? json({
          ...editable,
          editable_full_definition: false,
          can_test: false,
          definition: { name: 'Revenue' },
        })
      : standard(request),
  );
  expect(screen.queryByRole('button', { name: 'Save draft' })).toBeNull();
  expect(screen.getByText(/read-only authorized projection/i)).toBeVisible();
});
it('preserves dirty inputs on conflict and requires confirmation before head reload', async () => {
  let writes = 0;
  await openEditor(async (request) => {
    const path = new URL(request.url).pathname;
    if (request.method === 'PUT') {
      writes++;
      return json({ code: 'CONFLICT', request_id: 'conflict-edit' }, 409);
    }
    if (path === '/v1/drafts/draft-1' && writes)
      return json({ ...editable, is_current_revision: false, current_draft_id: 'draft-2' });
    return standard(request);
  });
  await userEvent.clear(screen.getByLabelText('Name'));
  await userEvent.type(screen.getByLabelText('Name'), 'Local work');
  await userEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('This revision changed');
  expect(screen.getByLabelText('Name')).toHaveValue('Local work');
  await userEvent.click(screen.getByRole('button', { name: 'Reload current revision' }));
  expect(screen.getByRole('dialog')).toHaveTextContent('Discard unsaved changes');
  await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
  expect(screen.getByLabelText('Name')).toHaveValue('Local work');
  expect(writes).toBe(1);
});
it('does not let a late passing test response approve unsaved edits', async () => {
  let finish: (value: Response) => void = () => {};
  await openEditor(async (request) =>
    request.method === 'POST'
      ? new Promise<Response>((resolve) => {
          finish = resolve;
        })
      : standard(request),
  );
  await userEvent.click(screen.getByRole('button', { name: 'Run tests' }));
  await userEvent.type(screen.getByLabelText('Name'), ' changed');
  finish(json(run));
  await screen.findByText(/Tests passed for saved revision/);
  expect(screen.getByRole('button', { name: 'Review publication' })).toBeDisabled();
  expect(screen.getByText(/Unsaved changes/)).toBeVisible();
});
it('removes the protected definition when a save is denied', async () => {
  await openEditor(async (request) =>
    request.method === 'PUT'
      ? json({ code: 'NOT_FOUND_OR_FORBIDDEN', request_id: 'denied-edit' }, 404)
      : standard(request),
  );
  await userEvent.type(screen.getByLabelText('Name'), ' edited');
  await userEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await screen.findByRole('alert');
  expect(screen.queryByLabelText('Name')).toBeNull();
  expect(screen.queryByText('Read-only manifest preview')).toBeNull();
});

it('does not navigate back to a saved draft after the user leaves a pending save', async () => {
  let finish: (response: Response) => void = () => {};
  await openEditor(async (request) => {
    const path = new URL(request.url).pathname;
    if (request.method === 'PUT')
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    if (path === '/v1/products') return json({ items: [], next_cursor: null });
    if (path === '/v1/drafts/draft-2')
      return json({ ...editable, id: 'draft-2', revision: 2, current_draft_id: 'draft-2' });
    return standard(request);
  });
  await userEvent.type(screen.getByLabelText('Name'), ' changed');
  await userEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await userEvent.click(screen.getByRole('link', { name: 'Catalog' }));
  await userEvent.click(screen.getByRole('button', { name: 'Discard and leave' }));
  await screen.findByRole('heading', { name: 'Catalog' });
  finish(json({ id: 'draft-2', product_id: 'revenue', revision: 2, digest: 'c'.repeat(64) }));
  await waitFor(() =>
    expect(screen.queryByRole('heading', { name: 'Edit definition' })).toBeNull(),
  );
  await new Promise((resolve) => setTimeout(resolve, 50));
  expect(window.location.pathname).toBe('/catalog');
});
