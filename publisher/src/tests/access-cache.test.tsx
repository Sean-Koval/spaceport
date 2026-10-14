import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useQuery, useQueries, type QueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { expect, it } from 'vitest';
import { AuthProvider, AuthGate, useGateway } from '../auth';
import { ProtectedView, useAccess } from '../access';
import { GatewayError } from '../api';
it('discards cached private projections and cancels late reads upon a concrete denial', async () => {
  let queries: QueryClient | undefined;
  let finish: (value: string) => void = () => {};
  function Probe() {
    const session = useGateway();
    queries = session.queries;
    const access = useAccess();
    useQueries({
      queries: ['draft', 'review', 'product', 'execution', 'choices'].map((scope) => ({
        queryKey: [session.generation, scope, 'private'],
        queryFn: async () => ({ value: 'private' }),
        initialData: { value: 'private' },
      })),
    });
    useQuery({
      queryKey: [session.generation, 'execution', 'pending'],
      queryFn: () =>
        new Promise<string>((resolve) => {
          finish = resolve;
        }),
    });
    return (
      <>
        <p>Private content</p>
        <button onClick={() => access.reject(new GatewayError(404, 'NOT_FOUND_OR_FORBIDDEN'))}>
          Deny
        </button>
      </>
    );
  }
  render(
    <MemoryRouter>
      <AuthProvider>
        <AuthGate>
          <ProtectedView>
            <Probe />
          </ProtectedView>
        </AuthGate>
      </AuthProvider>
    </MemoryRouter>,
  );
  await userEvent.type(screen.getByLabelText('Session bearer'), 'synthetic-test');
  await userEvent.click(screen.getByRole('button', { name: 'Start session' }));
  expect(queries!.getQueryCache().getAll().length).toBeGreaterThan(1);
  await userEvent.click(screen.getByRole('button', { name: 'Deny' }));
  expect(screen.queryByText('Private content')).toBeNull();
  expect(queries!.getQueryCache().getAll()).toEqual([]);
  await act(async () => {
    finish('late-private');
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
  expect(queries!.getQueryCache().getAll()).toEqual([]);
  expect(screen.queryByText('Private content')).toBeNull();
});
