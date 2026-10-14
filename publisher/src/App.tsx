import { Component, Suspense, lazy, useEffect, useState, type ReactNode } from 'react';
import {
  createBrowserRouter,
  RouterProvider,
  Link,
  Outlet,
  Navigate,
  useLocation,
} from 'react-router-dom';
import { Theme } from '@radix-ui/themes';
import { AuthProvider, AuthGate, useAuth } from './auth';
import { ProtectedView } from './access';
import { Icon } from './Icon';
const Catalog = lazy(() => import('./pages/Catalog'));
const Product = lazy(() => import('./pages/Product'));
const Editor = lazy(() => import('./pages/Editor'));
const Review = lazy(() => import('./pages/Review'));
class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <div role="alert">
        <h1>This view could not be displayed</h1>
        <p>Reload the page to start a fresh session.</p>
        <a href="/catalog">Return to catalog</a>
      </div>
    ) : (
      this.props.children
    );
  }
}
function Layout() {
  const { session, end } = useAuth();
  const location = useLocation();
  useEffect(() => {
    document.getElementById('main')?.focus();
  }, [location.pathname]);
  return (
    <>
      <a className="skip" href="#main">
        Skip to content
      </a>
      <div className="demo-banner">
        Synthetic demo · illustrative data only · no production readiness claim
      </div>
      <header>
        <Link className="brand" to="/catalog">
          <Icon name="port" className="brand-mark" />
          Spaceport
        </Link>
        <nav aria-label="Primary">
          <Link to="/catalog">Catalog</Link>
          {session && (
            <>
              <Link to="/drafts/new">Create draft</Link>
              <button className="text-button" onClick={end}>
                <Icon name="exit" />
                End session
              </button>
            </>
          )}
        </nav>
      </header>
      <main id="main" tabIndex={-1}>
        <ErrorBoundary key={session?.generation ?? 0}>
          <AuthGate>
            <Suspense fallback={<p role="status">Loading page…</p>}>
              <ProtectedView>
                <Outlet />
              </ProtectedView>
            </Suspense>
          </AuthGate>
        </ErrorBoundary>
      </main>
      <footer>
        Definitions are versioned. Access and publication are enforced by the gateway.
      </footer>
    </>
  );
}
export function App() {
  const [router] = useState(() =>
    createBrowserRouter([
      {
        element: <Layout />,
        children: [
          { path: '/', element: <Navigate to="/catalog" replace /> },
          { path: '/catalog', element: <Catalog /> },
          { path: '/products/:productId', element: <Product /> },
          { path: '/drafts/new', element: <Editor /> },
          { path: '/drafts/:draftId/edit', element: <Editor /> },
          { path: '/drafts/:draftId/review', element: <Review /> },
          {
            path: '*',
            element: (
              <>
                <h1>Page unavailable</h1>
                <Link to="/catalog">Return to catalog</Link>
              </>
            ),
          },
        ],
      },
    ]),
  );
  useEffect(() => () => router.dispose(), [router]);
  return (
    <Theme accentColor="orange" grayColor="gray" radius="none">
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </Theme>
  );
}
