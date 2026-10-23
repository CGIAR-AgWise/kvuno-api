import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

const LINKS = [
  { to: '/jobs', label: 'Jobs', icon: 'bi-list-check' },
  { to: '/explore', label: 'Explore', icon: 'bi-map' },
  { to: '/upload', label: 'Upload', icon: 'bi-cloud-arrow-up' },
  { to: '/quality', label: 'Quality', icon: 'bi-shield-check' },
  { to: '/tokens', label: 'Tokens', icon: 'bi-key' },
]

export default function Layout() {
  const { logout } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
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
            </ul>
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
