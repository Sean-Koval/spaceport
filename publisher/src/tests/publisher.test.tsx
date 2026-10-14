import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import { App } from '../App';
import { product, json } from './fixtures';
beforeEach(() => {
  window.history.replaceState({}, '', '/catalog');
});
afterEach(() => vi.unstubAllGlobals());
async function signIn() {
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-test');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
}
it('keeps the synthetic banner on sign-in and catalog, showing only authorized data', async () => {
  vi.stubGlobal('fetch', async () => json({ items: [product], next_cursor: null }));
  render(<App />);
  expect(screen.getByText(/Synthetic demo/)).toBeVisible();
  await signIn();
  expect(await screen.findByRole('link', { name: 'Daily revenue' })).toBeVisible();
  expect(screen.queryByText('restricted-revenue')).toBeNull();
  expect(screen.getByRole('link', { name: 'Skip to content' })).toHaveAttribute('href', '#main');
});
it('distinguishes loading and empty catalog', async () => {
  let finish: (value: Response) => void = () => {};
  vi.stubGlobal(
    'fetch',
    () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  render(<App />);
  await signIn();
  expect(screen.getByText('Loading products…')).toBeVisible();
  expect(screen.queryByText(/No products available/)).toBeNull();
  finish(json({ items: [], next_cursor: null }));
  expect(await screen.findByText('No products available for this account.')).toBeVisible();
});
it('clears private data on logout without persisting the bearer', async () => {
  vi.stubGlobal('fetch', async () => json({ items: [product], next_cursor: null }));
  render(<App />);
  await signIn();
  await screen.findByRole('link', { name: 'Daily revenue' });
  await userEvent.click(screen.getByRole('button', { name: 'End session' }));
  expect(screen.queryByRole('link', { name: 'Daily revenue' })).toBeNull();
  expect(screen.getByLabelText('Session bearer')).toHaveValue('');
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);
});
it('uses neutral errors and hides raw server messages', async () => {
  vi.stubGlobal('fetch', async () =>
    json(
      {
        code: 'NOT_FOUND_OR_FORBIDDEN',
        safe_message: 'restricted-revenue SECRET SQL',
        request_id: 'request-1',
      },
      404,
    ),
  );
  render(<App />);
  await signIn();
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'This item is unavailable to this account.',
  );
  expect(screen.getByRole('alert')).toHaveTextContent('request-1');
  expect(screen.queryByText(/SECRET SQL/)).toBeNull();
});
it('expires on a 401 and removes private page content', async () => {
  vi.stubGlobal('fetch', async () =>
    json({ code: 'UNAUTHENTICATED', request_id: 'request-2' }, 401),
  );
  render(<App />);
  await signIn();
  await waitFor(() => expect(screen.getByLabelText('Session bearer')).toBeVisible());
  expect(screen.queryByRole('heading', { name: 'Catalog' })).toBeNull();
});
