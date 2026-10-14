import { createContext, useContext, useRef, useState, useEffect, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { GatewayClient } from './api';
type Session = { generation: number; client: GatewayClient; queries: QueryClient };
type Auth = {
  session: Session | null;
  start: (bearer: string) => void;
  end: () => void;
  expired: boolean;
};
const AuthContext = createContext<Auth | null>(null);
let generation = 0;
export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const active = useRef<Session | null>(null);
  const [expired, setExpired] = useState(false);
  function clear() {
    active.current?.client.dispose();
    active.current?.queries.clear();
    active.current = null;
    setSession(null);
  }
  function start(bearer: string) {
    clear();
    setExpired(false);
    const id = ++generation;
    const queries = new QueryClient({
      defaultOptions: {
        queries: {
          retry: false,
          staleTime: 0,
          gcTime: 0,
          refetchOnWindowFocus: true,
          networkMode: 'always',
        },
        mutations: { retry: false, networkMode: 'always' },
      },
    });
    const client = new GatewayClient(bearer, () => {
      if (active.current?.generation === id) {
        clear();
        setExpired(true);
      }
    });
    const next = { generation: id, client, queries };
    active.current = next;
    setSession(next);
  }
  useEffect(
    () => () => {
      active.current?.client.dispose();
      active.current?.queries.clear();
    },
    [],
  );
  return (
    <AuthContext.Provider
      value={{
        session,
        start,
        end: () => {
          clear();
          setExpired(false);
        },
        expired,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}
export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error('Session context missing');
  return value;
}
export function useGateway() {
  const { session } = useAuth();
  if (!session) throw new Error('Session required');
  return session;
}
export function AuthGate({ children }: { children: ReactNode }) {
  const { session, start, expired } = useAuth();
  const [bearer, setBearer] = useState('');
  if (session)
    return (
      <QueryClientProvider key={session.generation} client={session.queries}>
        {children}
      </QueryClientProvider>
    );
  return (
    <section className="sign-in panel">
      <p className="eyebrow">Private, in-memory session</p>
      <h1>Open your catalog</h1>
      <p>
        Use the bearer supplied by your local demo operator. It stays in memory and is removed when
        this session ends or the page reloads.
      </p>
      {expired && <p role="status">Your session expired. Sign in again.</p>}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (bearer.trim()) {
            start(bearer.trim());
            setBearer('');
          }
        }}
      >
        <label htmlFor="session-bearer">Session bearer</label>
        <input
          id="session-bearer"
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={bearer}
          onChange={(event) => setBearer(event.target.value)}
          required
        />
        <button type="submit">Start session</button>
      </form>
    </section>
  );
}
