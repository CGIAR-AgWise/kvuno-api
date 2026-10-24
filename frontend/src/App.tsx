import { Suspense, lazy, useEffect } from 'react'
import type { ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AuthProvider, useAuth } from './context/AuthContext'
import { ToastProvider } from './context/ToastContext'
import Layout from './components/Layout'
import Loading from './components/Loading'
import Login from './pages/Login'
import Register from './pages/Register'

/**
 * Route-level code splitting. Explore alone pulls in Leaflet, markercluster
 * and leaflet.heat — roughly 200 KB of the bundle — and none of the other
 * pages need any of it, so the first load stays small.
 *
 * Each page's code is then prefetched once the browser is idle (see
 * usePrefetchPages), so moving between pages does not wait on a download.
 */
const PAGES = {
  jobs: () => import('./pages/Jobs'),
  upload: () => import('./pages/Upload'),
  quality: () => import('./pages/Quality'),
  tokens: () => import('./pages/Tokens'),
  admin: () => import('./pages/AdminUsers'),
  explore: () => import('./pages/Explore'),
}

const Jobs = lazy(PAGES.jobs)
const Explore = lazy(PAGES.explore)
const Upload = lazy(PAGES.upload)
const Quality = lazy(PAGES.quality)
const Tokens = lazy(PAGES.tokens)
const AdminUsers = lazy(PAGES.admin)

/**
 * Download every page's chunk after the first render, when the browser is
 * idle. The imports are the same promises lazy() uses, so a later navigation
 * renders at once. Signed-in only: the login page has no use for them.
 */
function usePrefetchPages(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return
    const load = () => {
      for (const page of Object.values(PAGES)) void page().catch(() => undefined)
    }
    // Safari has no requestIdleCallback; fall back to a short delay.
    if (typeof window.requestIdleCallback === 'function') {
      const id = window.requestIdleCallback(load, { timeout: 3000 })
      return () => window.cancelIdleCallback(id)
    }
    const id = window.setTimeout(load, 1500)
    return () => window.clearTimeout(id)
  }, [enabled])
}

/**
 * Gate for authenticated routes. Waits for the localStorage read before
 * deciding, otherwise a signed-in user sees the login page flash on reload.
 * `next` preserves the originally requested destination.
 */
function RequireAuth({ children }: { children: ReactNode }) {
  const { token, ready } = useAuth()
  const location = useLocation()
  usePrefetchPages(ready && Boolean(token))

  if (!ready) {
    return <Loading />
  }
  if (!token) {
    const next = encodeURIComponent(`${location.pathname}${location.search}`)
    return <Navigate to={`/login?next=${next}`} replace />
  }
  return <>{children}</>
}

/**
 * Administrators only (the server's ADMIN_EMAILS). Waits for `me` so an admin
 * reloading the page is not bounced before their account has loaded; the API
 * enforces this too — the guard only keeps others off a page that would 403.
 */
function RequireAdmin({ children }: { children: ReactNode }) {
  const { me, isAdmin } = useAuth()
  if (!me) return <Loading />
  if (!isAdmin) return <Navigate to="/jobs" replace />
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
              <Route
                path="/admin/users"
                element={
                  <RequireAdmin>
                    <AdminUsers />
                  </RequireAdmin>
                }
              />
            </Route>
            <Route path="*" element={<Navigate to="/jobs" replace />} />
          </Routes>
        </Suspense>
      </AuthProvider>
    </ToastProvider>
  )
}
