import { Suspense, lazy } from 'react'
import type { ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { useAuth } from './context/AuthContext'
import { ToastProvider } from './context/ToastContext'
import { AuthProvider } from './context/AuthContext'
import Layout from './components/Layout'
import Login from './pages/Login'
import Register from './pages/Register'

/**
 * Route-level code splitting. Explore alone pulls in Leaflet, markercluster
 * and leaflet.heat — roughly 200 KB of the bundle — and none of the other
 * pages need any of it, so those routes load on demand.
 */
const Jobs = lazy(() => import('./pages/Jobs'))
const Explore = lazy(() => import('./pages/Explore'))
const Upload = lazy(() => import('./pages/Upload'))
const Quality = lazy(() => import('./pages/Quality'))
const Tokens = lazy(() => import('./pages/Tokens'))

function Loading() {
  return <div className="container py-5 text-center text-muted">Loading…</div>
}

/**
 * Gate for authenticated routes. Waits for the localStorage read before
 * deciding, otherwise a signed-in user sees the login page flash on reload.
 * `next` preserves the originally requested destination.
 */
function RequireAuth({ children }: { children: ReactNode }) {
  const { token, ready } = useAuth()
  const location = useLocation()

  if (!ready) {
    return <Loading />
  }
  if (!token) {
    const next = encodeURIComponent(`${location.pathname}${location.search}`)
    return <Navigate to={`/login?next=${next}`} replace />
  }
  return <>{children}</>
}

export default function App() {
  return (
    <ToastProvider>
      <AuthProvider>
        <Suspense fallback={<Loading />}>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/register" element={<Register />} />
            <Route
              element={
                <RequireAuth>
                  <Layout />
                </RequireAuth>
              }
            >
              <Route path="/" element={<Navigate to="/jobs" replace />} />
              <Route path="/jobs" element={<Jobs />} />
              <Route path="/explore" element={<Explore />} />
              <Route path="/upload" element={<Upload />} />
              <Route path="/quality" element={<Quality />} />
              <Route path="/tokens" element={<Tokens />} />
            </Route>
            <Route path="*" element={<Navigate to="/jobs" replace />} />
          </Routes>
        </Suspense>
      </AuthProvider>
    </ToastProvider>
  )
}
