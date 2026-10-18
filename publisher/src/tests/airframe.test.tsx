import { readFileSync } from 'node:fs';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from '../App';
import { product, json } from './fixtures';

beforeEach(() => window.history.replaceState({}, '', '/catalog'));
afterEach(() => vi.unstubAllGlobals());

it('uses the shared Airframe navigation, mark, icon sprite and catalog card', async () => {
  vi.stubGlobal('fetch', async () => json({ items: [product], next_cursor: null }));
  const { container } = render(<App />);
  expect(screen.getByRole('link', { name: 'Catalog' })).toHaveAttribute('aria-current', 'page');
  expect(screen.getByRole('link', { name: 'Spaceport' }).querySelector('.af-mark')).not.toBeNull();
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-test');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
  const productLink = await screen.findByRole('link', { name: 'Daily revenue' });
  expect(productLink.closest('li')).toHaveClass('af-card');
  expect(screen.getByRole('button', { name: 'Search' })).toHaveClass('af-btn');
  expect(container.querySelector('use[href$="#af-egress"]')).toHaveAttribute(
    'href',
    '/airframe/airframe-icons.svg#af-egress',
  );
  expect(screen.getByText(/Synthetic demo/)).toBeVisible();
});

it('uses token-only product styles and same-origin font declarations', () => {
  const styles = readFileSync('src/styles.css', 'utf8');
  expect(styles).not.toMatch(/#[0-9a-f]{3,8}\b/i);
  expect(styles).toContain('var(--af-canvas)');
  expect(styles).toContain('forced-colors: active');
  const fonts = readFileSync('src/airframe-fonts.css', 'utf8');
  expect(fonts).not.toMatch(/https?:/);
  expect(fonts).toContain('/airframe/fonts/');
});

it('keeps text, controls and focus above their Airframe contrast floors in both themes', () => {
  const css = readFileSync('src/generated/airframe/airframe.css', 'utf8');
  const readColors = (text: string) =>
    Object.fromEntries(
      [...text.matchAll(/--af-([\w-]+):\s*(#[\da-fA-F]{6});/g)].map((m) => [m[1], m[2]]),
    );
  const dark = readColors(css.split('@media')[0]);
  const light = { ...dark, ...readColors(css) };
  const luminance = (hex: string) => {
    const rgb = [1, 3, 5].map((offset) => parseInt(hex.slice(offset, offset + 2), 16) / 255);
    const linear = rgb.map((value) =>
      value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4,
    );
    return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
  };
  const ratio = (a: string, b: string) => {
    const values = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (values[0] + 0.05) / (values[1] + 0.05);
  };
  for (const theme of [dark, light]) {
    for (const ground of ['canvas', 'surface', 'raised']) {
      for (const text of ['fg', 'fg-muted', 'fg-subtle']) {
        expect(ratio(theme[text], theme[ground])).toBeGreaterThanOrEqual(4.5);
      }
      expect(ratio(theme.focus, theme[ground])).toBeGreaterThanOrEqual(3);
      expect(ratio(theme['line-strong'], theme[ground])).toBeGreaterThanOrEqual(3);
    }
    expect(ratio(theme['on-brand'], theme.brand)).toBeGreaterThanOrEqual(4.5);
    expect(ratio(theme['on-brand'], theme['brand-hover'])).toBeGreaterThanOrEqual(4.5);
  }
});
