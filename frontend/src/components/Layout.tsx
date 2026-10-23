import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { fetchAdminUsers } from '../api'
import { useAuth } from '../context/AuthContext'

/**
 * How many accounts await approval, for the admin's navbar badge. Refreshed on
 * navigation and when the Accounts page changes something, so the badge is
 * current without polling.
 */
function usePendingCount(enabled: boolean, pathname: string): number {
  const [count, setCount] = useState(0)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    const bump = () => setNonce((n) => n + 1)
    window.addEventListener('kvuno:accounts-changed', bump)
    return () => window.removeEventListener('kvuno:accounts-changed', bump)
  }, [])

  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    fetchAdminUsers({ status: 'pending' }, controller.signal)
      .then((res) => setCount(res.counts.pending ?? 0))
      .catch(() => undefined)
    return () => controller.abort()
  }, [enabled, pathname, nonce])

  return enabled ? count : 0
}

const LINKS = [
  { to: '/jobs', label: 'Jobs', icon: 'bi-list-check' },
  { to: '/explore', label: 'Explore', icon: 'bi-map' },
  { to: '/upload', label: 'Upload', icon: 'bi-cloud-arrow-up' },
  { to: '/quality', label: 'Quality', icon: 'bi-shield-check' },
  { to: '/tokens', label: 'Tokens', icon: 'bi-key' },
]

export default function Layout() {
  const { logout, isAdmin, me } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const pending = usePendingCount(isAdmin, location.pathname)
  // Bootstrap's JS is not loaded (CSS only), so the navbar collapse is driven
  // by React state: `show` is the class Bootstrap's own plugin would toggle.
  const [navOpen, setNavOpen] = useState(false)

  // Close the mobile menu after navigating, as a native app menu would.
  useEffect(() => {
    setNavOpen(false)
  }, [location.pathname])

  async function signOut() {
    try {
      await logout()
    } catch {
      // logout() already cleared local state; a failed server revoke must not
      // strand the user on a signed-in page.
    } finally {
      navigate('/login', { replace: true })
    }
  }

  return (
    <div className="min-vh-100 d-flex flex-column">
      <nav className="navbar navbar-expand-sm navbar-dark bg-dark mb-3">
        <div className="container-fluid px-3 px-xl-5">
          <NavLink className="navbar-brand fw-semibold" to="/jobs">
            kvuno
          </NavLink>
          <button
            className="navbar-toggler"
            type="button"
            aria-controls="nav-main"
            aria-expanded={navOpen}
            aria-label="Toggle navigation"
            onClick={() => setNavOpen((open) => !open)}
          >
            <span className="navbar-toggler-icon" />
          </button>
          <div className={`collapse navbar-collapse${navOpen ? ' show' : ''}`} id="nav-main">
            <ul className="navbar-nav me-auto">
              {LINKS.map((link) => (
                <li className="nav-item" key={link.to}>
                  <NavLink
                    className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
                    to={link.to}
                  >
                    <i className={`bi ${link.icon} me-1`} />
                    {link.label}
                  </NavLink>
                </li>
              ))}
              {isAdmin && (
                <li className="nav-item">
                  <NavLink
                    className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
                    to="/admin/users"
                  >
                    <i className="bi bi-people me-1" />
                    Accounts
                    {pending > 0 && (
                      <span className="badge rounded-pill text-bg-warning ms-1">
                        {pending}
                        <span className="visually-hidden"> awaiting approval</span>
                      </span>
                    )}
                  </NavLink>
                </li>
              )}
            </ul>
            {me && (
              <span className="navbar-text small text-white-50 me-sm-3 py-2 py-sm-0" title={me.email}>
                <i className="bi bi-person-circle me-1" aria-hidden="true" />
                {me.username}
              </span>
            )}
            {/* A plain link, not a router <Link>: Swagger UI is served by the
                API (flask-openapi3, doc_prefix /api-docs), which nginx and the
                Vite dev server both proxy — the SPA has no route for it. */}
            <a
              className="nav-link text-white-50 me-sm-3 py-2 py-sm-0"
              href="/api-docs/swagger"
              target="_blank"
              rel="noreferrer"
            >
              <i className="bi bi-braces me-1" />
              API docs
            </a>
            <button
              type="button"
              className="btn btn-outline-light btn-sm"
              onClick={() => void signOut()}
            >
              <i className="bi bi-box-arrow-right me-1" />
              Sign out
            </button>
          </div>
        </div>
      </nav>

      <main className="container-fluid px-3 px-xl-5 flex-grow-1">
        <Outlet />
      </main>

      <footer className="text-center text-muted small py-4">
        kvuno &middot;{' '}
        <a
          href="https://agwise.cgiar.org"
          className="text-reset text-decoration-none"
          target="_blank"
          rel="noreferrer"
        >
          AgWise
        </a>
      </footer>
    </div>
  )
}
