import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { it, expect, vi, afterEach } from 'vitest';
import { App } from '../App';
import { draft, diff, run } from './review-fixtures';
import { json, product } from './fixtures';
afterEach(() => vi.unstubAllGlobals());
async function open(read: () => Response) {
  const writes: unknown[] = [];
  const reads: string[] = [];
  window.history.replaceState({}, '', '/drafts/draft-1/review');
  vi.stubGlobal('fetch', async (request: Request) => {
    const url = new URL(request.url);
    if (request.method === 'POST') {
      writes.push(await request.json());
      return json({ changed: true });
    }
    if (url.pathname.startsWith('/v1/products/')) {
      reads.push(url.pathname + url.search);
      return read();
    }
    return json(
      url.pathname.endsWith('/diff') ? diff : url.pathname.includes('/tests/') ? run : draft,
    );
  });
  render(<App />);
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-test');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
  await userEvent.click(await screen.findByText('Lifecycle actions for this product'));
  return { writes, reads };
}
it('does not allow lifecycle actions before explicitly opening the published version', async () => {
  const { writes, reads } = await open(() => json(product));
  expect(screen.queryByLabelText('Lifecycle action')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Review lifecycle change' })).toBeNull();
  expect(reads).toEqual([]);
  expect(writes).toEqual([]);
});
it.each(['unpublished', 'forbidden', 'wrong-version', 'wrong-product'])(
  'cannot dispatch for %s target',
  async (variant) => {
    const { writes } = await open(() =>
      variant === 'unpublished' || variant === 'forbidden'
        ? json({ code: 'NOT_FOUND_OR_FORBIDDEN' }, 404)
        : json({
            ...product,
            ...(variant === 'wrong-version' ? { version: '9.9.9' } : { product_id: 'another' }),
          }),
    );
    await userEvent.click(screen.getByRole('button', { name: 'Open existing published version' }));
    await screen.findByRole('alert');
    expect(screen.queryByRole('button', { name: 'Review lifecycle change' })).toBeNull();
    expect(screen.queryByLabelText('Existing target version')).toBeNull();
    expect(writes).toEqual([]);
  },
);
it('locks confirmation and the request to the explicitly read version', async () => {
  const { writes, reads } = await open(() => json(product));
  await userEvent.click(screen.getByRole('button', { name: 'Open existing published version' }));
  const target = await screen.findByLabelText('Existing target version');
  expect(target).toHaveValue('1.0.0');
  expect(target).toHaveAttribute('readonly');
  await userEvent.type(target, '9.9.9');
  expect(target).toHaveValue('1.0.0');
  await userEvent.selectOptions(screen.getByLabelText('Lifecycle action'), 'rollback');
  await userEvent.click(screen.getByRole('button', { name: 'Review lifecycle change' }));
  const dialog = screen.getByRole('dialog');
  expect(dialog).toHaveTextContent('rollback revenue 1.0.0?');
  expect(dialog).toHaveTextContent(
    'Moves the current pointer to an existing compatible published version.',
  );
  await userEvent.type(
    within(dialog).getByLabelText('Lifecycle reason'),
    'Reviewed existing target',
  );
  await userEvent.click(within(dialog).getByRole('button', { name: 'Confirm lifecycle change' }));
  await screen.findByText('Gateway confirmed: rollback 1.0.0.');
  expect(reads).toEqual(['/v1/products/revenue?version=1.0.0']);
  expect(writes).toEqual([
    { action: 'rollback', version: '1.0.0', reason: 'Reviewed existing target' },
  ]);
});
