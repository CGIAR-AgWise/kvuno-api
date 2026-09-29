import { NavLink, Outlet, useNavigate } from 'react-router-dom'
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
            data-bs-toggle="collapse"
            data-bs-target="#nav-main"
            aria-controls="nav-main"
            aria-expanded="false"
            aria-label="Toggle navigation"
          >
            <span className="navbar-toggler-icon" />
          </button>
          <div className="collapse navbar-collapse" id="nav-main">
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
            <button
              className="btn btn-outline-light btn-sm"
              onClick={async () => {
                await logout()
                navigate('/login', { replace: true })
              }}
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
