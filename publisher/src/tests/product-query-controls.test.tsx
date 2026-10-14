import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { focusManager } from '@tanstack/react-query';
import { App } from '../App';
import { json, product } from './fixtures';

const accepted = {
  execution_id: 'query-execution',
  state: 'accepted',
  accepted_at: '2026-10-14T10:00:00Z',
};
const queryProduct = {
  ...product,
  inputs: { start: { type: 'date' }, amount: { type: 'decimal', precision: 38, scale: 2 } },
  filters: { ...product.filters, revenue: { operators: ['gte', 'in'], max_values: 2 } },
};
afterEach(() => {
  focusManager.setFocused(undefined);
  vi.unstubAllGlobals();
});
async function open(
  fetcher: (request: Request) => Promise<Response>,
  path = '/products/revenue?version=1.0.0',
) {
  window.history.replaceState({}, '', path);
  vi.stubGlobal('fetch', fetcher);
  render(<App />);
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-test');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
  await screen.findByRole('heading', { name: 'Daily revenue' });
}
async function setInput(name: string, type: string, value: string) {
  await userEvent.selectOptions(screen.getByLabelText(`${name} value mode`), 'value');
  fireEvent.change(screen.getByLabelText(`${name} (${type})`), { target: { value } });
}
async function addFilter(field: string, operator: string, index = 1) {
  await userEvent.click(screen.getByRole('button', { name: 'Add filter' }));
  await userEvent.selectOptions(screen.getByLabelText(`Filter ${index} field`), field);
  await userEvent.selectOptions(screen.getByLabelText(`Filter ${index} operator`), operator);
}

it('submits permitted typed filters and ordered sorting with exact decimal and date values', async () => {
  let body: any;
  await open(async (request) => {
    if (request.method === 'POST') {
      body = await request.json();
      return json(accepted);
    }
    return json(request.url.includes('/executions/') ? accepted : queryProduct);
  });
  await setInput('amount', 'decimal', '9007199254740993.01');
  await setInput('start', 'date', '2024-03-10');
  await addFilter('revenue', 'gte');
  fireEvent.change(screen.getByLabelText('Filter 1 value (decimal)'), {
    target: { value: '9007199254740993.01' },
  });
  await addFilter('day', 'gte', 2);
  fireEvent.change(screen.getByLabelText('Filter 2 value (date)'), {
    target: { value: '2024-03-10' },
  });
  await userEvent.click(screen.getByRole('button', { name: 'Add sort' }));
  await userEvent.selectOptions(screen.getByLabelText('Sort 1 field'), 'segment');
  await userEvent.selectOptions(screen.getByLabelText('Sort 1 direction'), 'desc');
  await userEvent.click(screen.getByRole('button', { name: 'Add sort' }));
  expect(
    within(screen.getByLabelText('Sort 2 field')).queryByRole('option', { name: 'segment' }),
  ).toBeNull();
  expect(screen.getByRole('button', { name: 'Add sort' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await waitFor(() => expect(body).toBeDefined());
  expect(body).toEqual({
    product_id: 'revenue',
    version: '1.0.0',
    inputs: { amount: '9007199254740993.01', start: '2024-03-10' },
    filters: [
      { field: 'revenue', operator: 'gte', value: '9007199254740993.01' },
      { field: 'day', operator: 'gte', value: '2024-03-10' },
    ],
    sort: [
      { field: 'segment', direction: 'desc' },
      { field: 'day', direction: 'asc' },
    ],
    idempotency_key: expect.stringMatching(/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/),
  });
});

it('bounds list values and distinguishes nullable null from no-value is_null', async () => {
  const bodies: any[] = [];
  await open(async (request) => {
    if (request.method === 'POST') {
      bodies.push(await request.json());
      return json({ code: 'INVALID_INPUT' }, 422);
    }
    return json(queryProduct);
  });
  await addFilter('revenue', 'in');
  fireEvent.change(screen.getByLabelText('Filter 1 value 1 (decimal)'), {
    target: { value: '9007199254740993.01' },
  });
  await userEvent.click(screen.getByRole('button', { name: 'Add value to filter 1' }));
  fireEvent.change(screen.getByLabelText('Filter 1 value 2 (decimal)'), {
    target: { value: '-0.01' },
  });
  expect(screen.getByRole('button', { name: 'Add value to filter 1' })).toBeDisabled();
  await addFilter('segment', 'eq', 2);
  await userEvent.selectOptions(screen.getByLabelText('Filter 2 value mode'), 'null');
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await screen.findByRole('alert');
  expect(bodies[0].filters).toEqual([
    { field: 'revenue', operator: 'in', value: ['9007199254740993.01', '-0.01'] },
    { field: 'segment', operator: 'eq', value: null },
  ]);
  await userEvent.selectOptions(screen.getByLabelText('Filter 2 operator'), 'is_null');
  expect(screen.queryByLabelText('Filter 2 value (string)')).toBeNull();
  expect(screen.queryByLabelText('Filter 2 value mode')).toBeNull();
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await waitFor(() => expect(bodies).toHaveLength(2));
  expect(bodies[1].filters[1]).toEqual({ field: 'segment', operator: 'is_null' });
});

it('recovers a lost response only on explicit retry with the original immutable request and key', async () => {
  const bodies: any[] = [];
  await open(async (request) => {
    if (request.method === 'POST') {
      bodies.push(await request.json());
      if (bodies.length === 1) throw new TypeError('Lost response');
      return json(accepted);
    }
    return json(request.url.includes('/executions/') ? accepted : queryProduct);
  });
  await setInput('amount', 'decimal', '9007199254740993.01');
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await screen.findByRole('button', { name: 'Retry same request' });
  expect(bodies).toHaveLength(1);
  expect(screen.getByLabelText('amount (decimal)')).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: 'Edit as new request' }));
  fireEvent.change(screen.getByLabelText('amount (decimal)'), { target: { value: '1.00' } });
  await userEvent.click(screen.getByRole('button', { name: 'Retry same request' }));
  await screen.findByText('Execution query-execution');
  expect(bodies).toHaveLength(2);
  expect(bodies[1]).toEqual(bodies[0]);
  expect(bodies[0].idempotency_key).toEqual(expect.any(String));
  expect(screen.getByLabelText('amount (decimal)')).toHaveValue('1.00');
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await waitFor(() => expect(bodies).toHaveLength(3));
  expect(bodies[2].inputs.amount).toBe('1.00');
  expect(bodies[2].idempotency_key).not.toBe(bodies[0].idempotency_key);
});

it('coalesces repeated clicks and reuses the same key for an unchanged accepted intent', async () => {
  const bodies: any[] = [];
  let finish: (response: Response) => void = () => {};
  await open(async (request) => {
    if (request.method === 'POST') {
      bodies.push(await request.json());
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    }
    return json(request.url.includes('/executions/') ? accepted : queryProduct);
  });
  const run = screen.getByRole('button', { name: 'Run product' });
  fireEvent.click(run);
  fireEvent.click(run);
  await waitFor(() => expect(bodies).toHaveLength(1));
  expect(screen.getByRole('button', { name: 'Starting execution…' })).toBeDisabled();
  await act(async () => finish(json(accepted)));
  await screen.findByText('Execution query-execution');
  fireEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await waitFor(() => expect(bodies).toHaveLength(2));
  expect(bodies[1]).toEqual(bodies[0]);
  expect(bodies[0].idempotency_key).toEqual(expect.any(String));
  await act(async () => finish(json(accepted)));
});

it.each(['product', 'version', 'account'])(
  'clears uncertain requests on %s replacement',
  async (replacement) => {
    const bodies: any[] = [];
    await open(async (request) => {
      if (request.method === 'POST') {
        bodies.push(await request.json());
        throw new TypeError('Lost response');
      }
      const url = new URL(request.url);
      return json({
        ...queryProduct,
        product_id: url.pathname.split('/').at(-1),
        version: url.searchParams.get('version') ?? '1.0.0',
      });
    });
    await setInput('amount', 'decimal', '9007199254740993.01');
    await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
    await screen.findByRole('button', { name: 'Retry same request' });
    if (replacement === 'account') {
      await userEvent.click(screen.getByRole('button', { name: 'End session' }));
      await userEvent.type(screen.getByLabelText('Session bearer'), 'replacement-synthetic');
      await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
    } else {
      await act(async () => {
        window.history.pushState(
          {},
          '',
          replacement === 'version'
            ? '/products/revenue?version=2.0.0'
            : '/products/other?version=1.0.0',
        );
        window.dispatchEvent(new PopStateEvent('popstate'));
      });
    }
    await screen.findByRole('heading', { name: 'Daily revenue' });
    expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
    expect(screen.queryByLabelText('amount (decimal)')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
    await screen.findByRole('button', { name: 'Retry same request' });
    expect(bodies).toHaveLength(2);
    expect(bodies[1].idempotency_key).not.toBe(bodies[0].idempotency_key);
    expect(bodies[1].inputs).toEqual({});
  },
);

it('clears edits and unresolved intent if the authorized projection changes under the same digest', async () => {
  let narrowed = false;
  await open(async (request) => {
    if (request.method === 'POST') throw new TypeError('Lost response');
    return json(
      narrowed ? { ...queryProduct, inputs: {}, filters: {}, partial: true } : queryProduct,
    );
  });
  await setInput('amount', 'decimal', '9007199254740993.01');
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await screen.findByRole('button', { name: 'Retry same request' });
  narrowed = true;
  act(() => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
  await screen.findByText(/authorized partial view/);
  expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
  expect(screen.queryByText('9007199254740993.01')).toBeNull();
  expect(screen.queryByLabelText('amount (decimal)')).toBeNull();
});

it.each(['accepted', 'network', 'denied'])(
  'ignores a late %s response after a description digest replacement',
  async (outcome) => {
    let replaced = false;
    let finish: (response: Response) => void = () => {};
    let fail: (error: unknown) => void = () => {};
    const bodies: any[] = [];
    await open(async (request) => {
      if (request.method === 'POST') {
        bodies.push(await request.json());
        if (bodies.length === 1)
          return new Promise<Response>((resolve, reject) => {
            finish = resolve;
            fail = reject;
          });
        return json({ code: 'INVALID_INPUT' }, 422);
      }
      return json(
        replaced
          ? { ...queryProduct, digest: 'c'.repeat(64), name: 'Replacement contract' }
          : queryProduct,
      );
    });
    await setInput('amount', 'decimal', '9007199254740993.01');
    await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
    replaced = true;
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await screen.findByRole('heading', { name: 'Replacement contract' });
    await act(async () => {
      if (outcome === 'network') fail(new TypeError('Lost response'));
      else
        finish(
          json(
            outcome === 'denied' ? { code: 'NOT_FOUND_OR_FORBIDDEN' } : accepted,
            outcome === 'denied' ? 404 : 200,
          ),
        );
    });
    expect(screen.getByRole('heading', { name: 'Replacement contract' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
    expect(screen.queryByLabelText('amount (decimal)')).toBeNull();
    expect(window.location.search).toBe('?version=1.0.0');
    await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
    await screen.findByRole('alert');
    expect(bodies[1].idempotency_key).not.toBe(bodies[0].idempotency_key);
  },
);

it('pins latest before running and retains the same key after its result navigation', async () => {
  const bodies: any[] = [];
  await open(async (request) => {
    if (request.method === 'POST') {
      bodies.push(await request.json());
      return json(accepted);
    }
    return json(request.url.includes('/executions/') ? accepted : queryProduct);
  }, '/products/revenue');
  expect(window.location.search).toBe('?version=1.0.0');
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await screen.findByText('Execution query-execution');
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await waitFor(() => expect(bodies).toHaveLength(2));
  expect(bodies[1]).toEqual(bodies[0]);
});

it('starts an explicit fresh intent after acceptance without changing ordinary retry identity', async () => {
  const bodies: any[] = [];
  await open(async (request) => {
    if (request.method === 'POST') {
      bodies.push(await request.json());
      return json(accepted);
    }
    return json(request.url.includes('/executions/') ? accepted : queryProduct);
  });
  expect(screen.queryByRole('button', { name: 'Start new run' })).toBeNull();
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await screen.findByText('Execution query-execution');
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await waitFor(() => expect(bodies).toHaveLength(2));
  expect(bodies[1]).toEqual(bodies[0]);
  await userEvent.click(screen.getByRole('button', { name: 'Start new run' }));
  await waitFor(() => expect(bodies).toHaveLength(3));
  expect(bodies[2]).toEqual({ ...bodies[0], idempotency_key: expect.any(String) });
  expect(bodies[2].idempotency_key).not.toBe(bodies[0].idempotency_key);
});

it.each(['1e3', '01.00', '1.001', 'not-a-decimal'])(
  'blocks invalid decimal %s before any execution request',
  async (value) => {
    let writes = 0;
    await open(async (request) => {
      if (request.method === 'POST') writes++;
      return json(queryProduct);
    });
    await setInput('amount', 'decimal', value);
    expect(screen.getByText('Check the value for input amount.')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Run product' })).toBeDisabled();
    fireEvent.submit(screen.getByRole('button', { name: 'Run product' }).closest('form')!);
    expect(writes).toBe(0);
  },
);

it('does not retry an ambiguous 503 on focus or repeat clicks, and guards explicit retry clicks', async () => {
  const bodies: any[] = [];
  let finish: (response: Response) => void = () => {};
  await open(async (request) => {
    if (request.method === 'POST') {
      bodies.push(await request.json());
      if (bodies.length === 1) return json({ code: 'UPSTREAM_UNAVAILABLE' }, 503);
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    }
    return json(queryProduct);
  });
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await screen.findByRole('button', { name: 'Retry same request' });
  expect(screen.queryByRole('button', { name: 'Start new run' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Run product' }));
  act(() => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
  expect(bodies).toHaveLength(1);
  const retry = screen.getByRole('button', { name: 'Retry same request' });
  fireEvent.click(retry);
  fireEvent.click(retry);
  await waitFor(() => expect(bodies).toHaveLength(2));
  expect(bodies[1]).toEqual(bodies[0]);
  await act(async () => finish(json({ code: 'UPSTREAM_UNAVAILABLE' }, 503)));
  expect(screen.getByRole('button', { name: 'Retry same request' })).toBeEnabled();
});

it('clears the uncertain snapshot on denied retry and cannot restore it after authorized reload', async () => {
  const bodies: any[] = [];
  await open(async (request) => {
    if (request.method === 'POST') {
      bodies.push(await request.json());
      if (bodies.length === 2) return json({ code: 'NOT_FOUND_OR_FORBIDDEN' }, 404);
      throw new TypeError('Lost response');
    }
    return json(queryProduct);
  });
  await setInput('amount', 'decimal', '9007199254740993.01');
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await userEvent.click(await screen.findByRole('button', { name: 'Retry same request' }));
  await screen.findByRole('heading', { name: 'View unavailable' });
  expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
  expect(screen.queryByText('Original request for retry')).toBeNull();
  await userEvent.click(screen.getByRole('button', { name: 'Reload authorized view' }));
  await screen.findByRole('heading', { name: 'Daily revenue' });
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await screen.findByRole('button', { name: 'Retry same request' });
  expect(bodies[1]).toEqual(bodies[0]);
  expect(bodies[2].idempotency_key).not.toBe(bodies[0].idempotency_key);
  expect(bodies[2].inputs).toEqual({});
});

it('does not classify a key-generation failure before submission as an ambiguous execution', async () => {
  let writes = 0;
  await open(async (request) => {
    if (request.method === 'POST') writes++;
    return json(queryProduct);
  });
  vi.spyOn(crypto, 'randomUUID').mockImplementation(() => {
    throw new Error('Secure random unavailable');
  });
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await screen.findByRole('alert');
  expect(writes).toBe(0);
  expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Run product' })).toBeEnabled();
});

it('does not carry uncertainty from the old intent into a definitively rejected changed run', async () => {
  const bodies: any[] = [];
  await open(async (request) => {
    if (request.method === 'POST') {
      bodies.push(await request.json());
      if (bodies.length === 1) throw new TypeError('Lost response');
      return json({ code: 'INVALID_INPUT' }, 422);
    }
    return json(queryProduct);
  });
  await setInput('amount', 'decimal', '1.00');
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await userEvent.click(await screen.findByRole('button', { name: 'Edit as new request' }));
  fireEvent.change(screen.getByLabelText('amount (decimal)'), { target: { value: '2.00' } });
  await userEvent.click(screen.getByRole('button', { name: 'Run product' }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('could not validate'));
  expect(screen.queryByRole('button', { name: 'Retry same request' })).toBeNull();
  expect(screen.getByLabelText('amount (decimal)')).toBeEnabled();
  expect(bodies[1].idempotency_key).not.toBe(bodies[0].idempotency_key);
});
