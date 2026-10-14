import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { it, expect, vi, afterEach } from 'vitest';
import { App } from '../App';
import { draft, diff, run } from './review-fixtures';
import { json, product } from './fixtures';
afterEach(() => vi.unstubAllGlobals());
it('requires explicit consequences and reason for a server-authorized lifecycle action', async () => {
  window.history.replaceState({}, '', '/drafts/draft-1/review');
  let writes: unknown[] = [];
  vi.stubGlobal('fetch', async (request: Request) => {
    const path = new URL(request.url).pathname;
    if (path.endsWith('/lifecycle')) {
      writes.push(await request.json());
      return json({ changed: true });
    }
    if (path.startsWith('/v1/products/')) return json(product);
    return json(path.endsWith('/diff') ? diff : path.includes('/tests/') ? run : draft);
  });
  render(<App />);
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-test');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
  await userEvent.click(await screen.findByText('Lifecycle actions for this product'));
  await userEvent.click(screen.getByRole('button', { name: 'Open existing published version' }));
  await userEvent.selectOptions(await screen.findByLabelText('Lifecycle action'), 'withdraw');
  await userEvent.click(screen.getByRole('button', { name: 'Review lifecycle change' }));
  const dialog = screen.getByRole('dialog');
  expect(dialog).toHaveTextContent('Blocks new execution and retrieval of stored results');
  expect(within(dialog).getByRole('button', { name: 'Confirm lifecycle change' })).toBeDisabled();
  await userEvent.type(
    within(dialog).getByLabelText('Lifecycle reason'),
    'Synthetic version retired',
  );
  await userEvent.click(within(dialog).getByRole('button', { name: 'Confirm lifecycle change' }));
  expect(await screen.findByText('Gateway confirmed: withdraw 1.0.0.')).toBeVisible();
  expect(writes).toEqual([
    { action: 'withdraw', version: '1.0.0', reason: 'Synthetic version retired' },
  ]);
});
