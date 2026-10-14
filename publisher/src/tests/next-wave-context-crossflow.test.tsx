/** Integrated component checks with transport doubles, not browser/backend proof. */
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { focusManager } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../App';
import { json, product } from './fixtures';

afterEach(() => {
  vi.unstubAllGlobals();
  focusManager.setFocused(undefined);
});

async function open(fetcher: (request: Request) => Promise<Response>) {
  window.history.replaceState({}, '', '/products/revenue?version=1.0.0');
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-crossflow-session');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
  await screen.findByRole('heading', { name: 'Daily revenue' });
}

async function navigate(destination: string) {
  await act(async () => {
    window.history.pushState({}, '', destination);
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
}

it('discards a prior ambiguous execution when navigating through the draft inbox', async () => {
  const requests: any[] = [];
  await open(async (request) => {
    if (request.method === 'POST') {
      requests.push(await request.json());
      throw new TypeError('Synthetic lost response');
    }
    return json(
      new URL(request.url).pathname === '/v1/drafts' ? { items: [], next_cursor: null } : product,
    );
  });
  await userEvent.click(screen.getByRole('button', { name: 'Use example 1' }));
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await screen.findByRole('button', { name: 'Retry same request' });
  expect(requests).toHaveLength(1);
  await userEvent.click(screen.getByRole('link', { name: 'Drafts' }));
  await screen.findByRole('heading', { name: 'Saved drafts' });
  expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
  await navigate('/products/revenue?version=1.0.0');
  await screen.findByRole('heading', { name: 'Daily revenue' });
  expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
  expect(requests).toHaveLength(1);
  await userEvent.click(screen.getByRole('button', { name: 'Use example 1' }));
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await screen.findByRole('button', { name: 'Retry same request' });
  expect(requests).toHaveLength(2);
  expect(requests[0].idempotency_key).toEqual(expect.any(String));
  expect(requests[1].idempotency_key).not.toBe(requests[0].idempotency_key);
});

it('does not navigate out of the inbox when an earlier execution response arrives', async () => {
  let finish!: (response: Response) => void;
  await open(async (request) => {
    if (request.method === 'POST')
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    return json(
      new URL(request.url).pathname === '/v1/drafts' ? { items: [], next_cursor: null } : product,
    );
  });
  await userEvent.click(screen.getByRole('button', { name: 'Use example 1' }));
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await waitFor(() => expect(finish).toBeDefined());
  await userEvent.click(screen.getByRole('link', { name: 'Drafts' }));
  await screen.findByText('No saved drafts available for this account.');
  await act(async () => {
    finish(
      json({
        execution_id: 'late-consumer-execution',
        state: 'accepted',
        accepted_at: '2026-10-13T10:00:00Z',
      }),
    );
  });
  expect(window.location.pathname + window.location.search).toBe('/drafts');
  expect(screen.queryByText('Execution late-consumer-execution')).toBeNull();
  expect(screen.getByRole('heading', { name: 'Saved drafts' })).toBeVisible();
});

it('drops protected controls and an ambiguous retry after same-digest visibility tightening', async () => {
  let restricted = false;
  let reads = 0;
  await open(async (request) => {
    if (request.method === 'POST') throw new TypeError('Synthetic lost response');
    reads++;
    return json(
      restricted
        ? {
            ...product,
            partial: true,
            inputs: {},
            outputs: { day: product.outputs.day },
            filters: { day: product.filters.day },
            sort: { fields: ['day'], max_fields: 1 },
            examples: [],
            description: null,
            owner: null,
            domain: null,
            grain: null,
            aggregation: null,
            date_boundaries: null,
            units: {},
          }
        : product,
    );
  });
  await userEvent.click(screen.getByRole('button', { name: 'Use example 1' }));
  await userEvent.click(screen.getByRole('button', { name: 'Add filter' }));
  await userEvent.selectOptions(screen.getByLabelText('Filter 1 field'), 'segment');
  await userEvent.type(screen.getByLabelText('Filter 1 value (string)'), 'private-filter-marker');
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await screen.findByRole('button', { name: 'Retry same request' });
  const previousReads = reads;
  restricted = true;
  await act(async () => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
  await waitFor(() => expect(reads).toBeGreaterThan(previousReads));
  await screen.findByText(
    'This is an authorized partial view. Only returned fields and metadata are shown.',
  );
  expect(screen.queryByDisplayValue('private-filter-marker')).toBeNull();
  expect(screen.queryByLabelText('Filter 1 field')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Run product' })).toBeEnabled();
});

it('ignores an in-flight execution receipt after the authorized projection changes', async () => {
  let restricted = false;
  let finish!: (response: Response) => void;
  const executionReads: string[] = [];
  await open(async (request) => {
    if (request.method === 'POST')
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    if (new URL(request.url).pathname.includes('/executions/')) {
      executionReads.push(request.url);
      return json({
        execution_id: 'previous-projection-execution',
        state: 'accepted',
        accepted_at: '2026-10-13T10:00:00Z',
      });
    }
    return json(
      restricted ? { ...product, partial: true, description: null, examples: [] } : product,
    );
  });
  await userEvent.click(screen.getByRole('button', { name: 'Use example 1' }));
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await waitFor(() => expect(finish).toBeDefined());
  restricted = true;
  await act(async () => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
  await screen.findByText(
    'This is an authorized partial view. Only returned fields and metadata are shown.',
  );
  await act(async () => {
    finish(
      json({
        execution_id: 'previous-projection-execution',
        state: 'accepted',
        accepted_at: '2026-10-13T10:00:00Z',
      }),
    );
  });
  expect(window.location.search).toBe('?version=1.0.0');
  expect(executionReads).toEqual([]);
  expect(screen.queryByText('Execution previous-projection-execution')).toBeNull();
  expect(screen.getByRole('button', { name: 'Run product' })).toBeEnabled();
});
