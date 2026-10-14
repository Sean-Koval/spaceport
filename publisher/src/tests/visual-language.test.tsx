import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from '../App';
import { product, json } from './fixtures';

beforeEach(() => window.history.replaceState({}, '', '/catalog'));
afterEach(() => vi.unstubAllGlobals());

it('uses decorative SVG icons without changing accessible action names', async () => {
  vi.stubGlobal('fetch', async () => json({ items: [product], next_cursor: null }));
  const { container } = render(<App />);
  const brand = screen.getByRole('link', { name: 'Spaceport' });
  expect(brand.querySelector('svg')).not.toBeNull();
  expect(brand).not.toHaveTextContent('▦');
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-test');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
  const productLink = await screen.findByRole('link', { name: 'Daily revenue' });
  expect(productLink.querySelector('svg')).not.toBeNull();
  expect(screen.getByRole('button', { name: 'Search' }).querySelector('svg')).not.toBeNull();
  expect(screen.getByRole('button', { name: 'End session' })).toBeVisible();
  expect(screen.getByText('published')).toBeVisible();
  for (const icon of container.querySelectorAll('svg')) {
    expect(icon).toHaveAttribute('aria-hidden', 'true');
    expect(icon).toHaveAttribute('focusable', 'false');
  }
  expect(container.textContent).not.toMatch(
    /[\p{Extended_Pictographic}\uFE0F\u{1F1E6}-\u{1F1FF}▦✓◇◐↗←→↓]/u,
  );
});
