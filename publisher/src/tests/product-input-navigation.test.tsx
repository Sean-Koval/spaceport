import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, it, expect, vi } from 'vitest';
import { App } from '../App';
import { json, product } from './fixtures';
afterEach(() => vi.unstubAllGlobals());
async function open(fetcher: (r: Request) => Promise<Response>) {
  window.history.replaceState({}, '', '/products/revenue?version=1.0.0');
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-test');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
  await screen.findByRole('heading', { name: 'Daily revenue' });
}
it.each([
  '/catalog',
  '/products/another?version=1.0.0',
  '/products/revenue?version=2.0.0',
  '/products/revenue?version=1.0.0&execution=new-result',
  '/products/another?version=2.0.0&execution=new-result',
])('leaves destination %s unchanged after old execution completes', async (destination) => {
  let finish: (r: Response) => void = () => {};
  let posts = 0;
  await open(async (r) => {
    if (r.method === 'POST') {
      posts++;
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    }
    const url = new URL(r.url);
    if (url.pathname === '/v1/products') return json({ items: [], next_cursor: null });
    if (url.pathname.includes('/executions/'))
      return json({
        execution_id: 'new-result',
        state: 'accepted',
        accepted_at: '2026-10-13T10:00:00Z',
      });
    return json({
      ...product,
      product_id: url.pathname.split('/').at(-1),
      version: url.searchParams.get('version') ?? '1.0.0',
    });
  });
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await act(async () => {
    window.history.pushState({}, '', destination);
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await screen.findByRole('heading', {
    name: destination === '/catalog' ? 'Catalog' : 'Daily revenue',
  });
  await act(async () => {
    finish(
      json({
        execution_id: 'late-execution',
        state: 'accepted',
        accepted_at: '2026-10-13T10:00:00Z',
      }),
    );
    await new Promise((resolve) => setTimeout(resolve, 30));
  });
  expect(window.location.pathname + window.location.search).toBe(destination);
  expect(screen.queryByText('Execution late-execution')).toBeNull();
  if (destination.includes('execution='))
    expect(await screen.findByText('Execution new-result')).toBeVisible();
  expect(posts).toBe(1);
});
it('does not apply an old execution to a replacement session', async () => {
  let finish: (r: Response) => void = () => {};
  await open(async (r) =>
    r.method === 'POST'
      ? new Promise<Response>((resolve) => {
          finish = resolve;
        })
      : json(product),
  );
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await userEvent.click(screen.getByRole('button', { name: 'End session' }));
  await userEvent.type(screen.getByLabelText('Session bearer'), 'replacement-synthetic');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
  await screen.findByRole('heading', { name: 'Daily revenue' });
  await act(async () => {
    finish(
      json({
        execution_id: 'old-session-result',
        state: 'accepted',
        accepted_at: '2026-10-13T10:00:00Z',
      }),
    );
    await new Promise((resolve) => setTimeout(resolve, 30));
  });
  expect(window.location.search).toBe('?version=1.0.0');
  expect(screen.queryByText('Execution old-session-result')).toBeNull();
});
it.each([
  { type: 'string', values: ['A', 'B'], selected: 'A' },
  { type: 'integer', values: [0, 2], selected: 0 },
  { type: 'boolean', values: [false, true], selected: false },
  { type: 'string', values: [null, 'A'], selected: null },
])(
  'submits the displayed typed enum member for $type / $selected',
  async ({ type, values, selected }) => {
    let body: any;
    await open(async (r) => {
      if (r.method === 'POST') {
        body = await r.json();
        return json({ code: 'INVALID_INPUT' }, 422);
      }
      return json({
        ...product,
        inputs: { segment: { type, nullable: values.includes(null as never), enum: values } },
      });
    });
    await userEvent.selectOptions(screen.getByLabelText('segment value mode'), 'value');
    const selection = screen.queryByLabelText(`segment (${type})`);
    if (selected === null) {
      // Null is either an explicit enum selection or the explicit null mode, never an empty string.
      expect(
        selection
          ? (selection as HTMLSelectElement).selectedOptions[0].textContent
          : (screen.getByLabelText('segment value mode') as HTMLSelectElement).selectedOptions[0]
              .textContent,
      ).toBe('null');
    } else
      expect((selection as HTMLSelectElement).selectedOptions[0].textContent).toBe(
        JSON.stringify(selected),
      );
    await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
    await waitFor(() => expect(body).toBeDefined());
    expect(body.inputs.segment).toEqual(selected);
  },
);
it('can select a non-null enum member after an explicitly selected null', async () => {
  let body: any;
  await open(async (r) => {
    if (r.method === 'POST') {
      body = await r.json();
      return json({ code: 'INVALID_INPUT' }, 422);
    }
    return json({
      ...product,
      inputs: { segment: { type: 'string', nullable: true, enum: [null, 'A'] } },
    });
  });
  await userEvent.selectOptions(screen.getByLabelText('segment value mode'), 'value');
  const select = screen.getByLabelText('segment (string)');
  expect(select).toHaveValue('0');
  await userEvent.selectOptions(select, '1');
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await waitFor(() => expect(body).toBeDefined());
  expect(body.inputs.segment).toBe('A');
});
it('shows an unselected invalid enum explicitly and prevents submitting it', async () => {
  await open(async () =>
    json({
      ...product,
      examples: [{ description: 'Invalid example', inputs: { segment: 'unexpected' } }],
    }),
  );
  await userEvent.click(screen.getByRole('button', { name: 'Use example 1' }));
  expect(screen.getByLabelText('segment (string)')).toHaveValue('-1');
  expect(screen.getByRole('button', { name: 'Run product' })).toBeDisabled();
});
it('preserves an explicit nullable null even when non-null enum members are restricted', async () => {
  let body: any;
  await open(async (r) => {
    if (r.method === 'POST') {
      body = await r.json();
      return json({ code: 'INVALID_INPUT' }, 422);
    }
    return json({
      ...product,
      inputs: { segment: { type: 'string', nullable: true, enum: ['A', 'B'] } },
    });
  });
  await userEvent.selectOptions(screen.getByLabelText('segment value mode'), 'null');
  expect(screen.getByRole('button', { name: 'Run product' })).toBeEnabled();
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await waitFor(() => expect(body).toBeDefined());
  expect(body.inputs.segment).toBeNull();
});
