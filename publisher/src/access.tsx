import {
  createContext,
  useContext,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useGateway } from './auth';
import { GatewayError } from './api';
import { ErrorNotice } from './ui';
export function isAccessDenied(error: unknown) {
  return error instanceof GatewayError && [401, 403, 404].includes(error.status);
}
type Access = { reject: (error: unknown) => boolean; current: () => boolean };
const AccessContext = createContext<Access | null>(null);
function AccessScope({ children, reload }: { children: ReactNode; reload: () => void }) {
  const { queries } = useGateway();
  const [denied, setDenied] = useState<unknown>(null);
  const active = useRef(true);
  const deniedOnce = useRef(false);
  useLayoutEffect(() => {
    active.current = !deniedOnce.current;
    return () => {
      active.current = false;
    };
  }, []);
  const access: Access = {
    current: () => active.current,
    reject: (error) => {
      if (!isAccessDenied(error)) return false;
      if (active.current) {
        deniedOnce.current = true;
        active.current = false;
        // Permission changes can affect projections, evidence and stored results together.
        // Discard all session-local cached reads conservatively, including in-flight reads.
        void queries.cancelQueries();
        queries.removeQueries();
        setDenied(error);
      }
      return true;
    },
  };
  return denied ? (
    <>
      <h1>View unavailable</h1>
      <ErrorNotice error={denied} />
      <button onClick={reload}>Reload authorized view</button>
      <Link to="/catalog">Return to catalog</Link>
    </>
  ) : (
    <AccessContext.Provider value={access}>{children}</AccessContext.Provider>
  );
}
export function ProtectedView({ children }: { children: ReactNode }) {
  const location = useLocation();
  const { generation } = useGateway();
  const [epoch, setEpoch] = useState(0);
  // Executions may update their own search parameter; changing product/version ends the scope.
  const version = new URLSearchParams(location.search).get('version');
  return (
    <AccessScope
      key={`${generation}:${location.pathname}:${version}:${epoch}`}
      reload={() => setEpoch((value) => value + 1)}
    >
      {children}
    </AccessScope>
  );
}
export function useAccess(error?: unknown) {
  const access = useContext(AccessContext);
  if (!access) throw new Error('Protected view required');
  useLayoutEffect(() => {
    if (error) access.reject(error);
  }, [error, access]);
  return access;
}
export function useInvocation() {
  const location = useLocation();
  const { generation } = useGateway();
  const access = useAccess();
  const route = useRef('');
  const identity = `${generation}:${location.key}:${location.pathname}:${location.search}:${location.hash}`;
  route.current = identity;
  return () => access.current() && route.current === identity;
}
