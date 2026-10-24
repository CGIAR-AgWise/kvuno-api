import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { fetchAdminUsers } from '../api'
import { useAuth } from '../context/AuthContext'

/**
 * How many accounts await approval, for the admin's sidebar badge. Refreshed on
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

interface Page {
  to: string
  label: string
  icon: string
  title: string
  description: string
  /** Column width: narrow for forms, medium for lists, wide for tables and the map. */
  width: 'narrow' | 'medium' | 'wide'
  adminOnly?: boolean
}

/**
 * The sidebar, grouped by what people do. "Data" follows a file through its
 * life — upload it, watch it import, explore it, review its conflicts — so the
 * order is the workflow. Each page's header (title + one line) comes from here
 * too, so every page opens the same way.
 */
const GROUPS: { label: string; pages: Page[] }[] = [
  {
    label: 'Data',
    pages: [
      {
        to: '/upload',
        label: 'Upload',
        icon: 'bi-cloud-arrow-up',
        title: 'Upload files',
        description: 'Send RDS or Parquet files, then match their columns to planting data.',
        width: 'narrow',
      },
      {
        to: '/jobs',
        label: 'Jobs',
        icon: 'bi-list-check',
        title: 'Import jobs',
        description: 'Follow each file from the queue to a finished import, and retry the ones that failed.',
        width: 'wide',
      },
      {
        to: '/explore',
        label: 'Explore',
        icon: 'bi-map',
        title: 'Explore recommendations',
        description: 'Filter planting recommendations and see where they apply on the map.',
        width: 'wide',
      },
      {
        to: '/quality',
        label: 'Quality',
        icon: 'bi-shield-check',
        title: 'Data quality',
        description: 'Review rows that conflicted with data already stored.',
        width: 'wide',
      },
    ],
  },
  {
    label: 'Access',
    pages: [
      {
        to: '/tokens',
        label: 'API tokens',
        icon: 'bi-key',
        title: 'API tokens',
        description: 'Create and revoke tokens for scripts that call the API.',
        width: 'narrow',
      },
      {
        to: '/admin/users',
        label: 'Accounts',
        icon: 'bi-people',
        title: 'Accounts',
        description: 'New registrations wait here until approved. Approving sends the user their activation email.',
        width: 'medium',
        adminOnly: true,
      },
    ],
  },
]

const COLLAPSED_KEY = 'kvuno:sidebar-collapsed'

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === '1'
  } catch {
    return false
  }
}

/** The brand mark: three seeds in a furrow. */
function SeedRow({ className = '' }: { className?: string }) {
  return (
    <span className={'kv-seeds ' + className} aria-hidden="true">
      <i />
      <i />
      <i />
    </span>
  )
}

export default function Layout() {
  const { logout, isAdmin, me } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const pending = usePendingCount(isAdmin, location.pathname)
  // Phones and tablets: the sidebar is a drawer. Desktop: it can be narrowed
  // to icons, remembered per browser.
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(readCollapsed)
  const sidebarRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    setDrawerOpen(false)
  }, [location.pathname])

  useEffect(() => {
    if (!drawerOpen) return
    // Into the drawer, on the current page's link, so the keyboard starts there.
    const sidebar = sidebarRef.current
    const target = sidebar?.querySelector<HTMLElement>('.kv-nav-link.active') ?? sidebar?.querySelector<HTMLElement>('a')
    target?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setDrawerOpen(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [drawerOpen])

  function toggleCollapsed() {
    setCollapsed((value) => {
      try {
        localStorage.setItem(COLLAPSED_KEY, value ? '0' : '1')
      } catch {
        // Storage unavailable (private mode): the choice lasts this visit only.
      }
      return !value
    })
  }

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

  const groups = GROUPS.map((g) => ({ ...g, pages: g.pages.filter((p) => !p.adminOnly || isAdmin) }))
  const current = groups
    .flatMap((g) => g.pages.map((p) => ({ ...p, group: g.label })))
    .find((p) => location.pathname.startsWith(p.to))

  return (
    <div className={'kv-shell' + (collapsed ? ' is-collapsed' : '')}>
      <a className="kv-skip" href="#kv-main">
        Skip to content
      </a>

      <aside ref={sidebarRef} id="kv-sidebar" className={'kv-sidebar' + (drawerOpen ? ' is-open' : '')} aria-label="Main">
        <NavLink to="/jobs" className="kv-brand">
          <SeedRow />
          <span className="kv-brand-name kv-label">kvuno</span>
        </NavLink>

        <nav className="kv-nav">
          {groups.map((group) => (
            <div key={group.label} className="kv-nav-group">
              <div className="kv-nav-heading kv-label">{group.label}</div>
              {group.pages.map((page) => (
                <NavLink
                  key={page.to}
                  to={page.to}
                  className={({ isActive }) => 'kv-nav-link' + (isActive ? ' active' : '')}
                  title={collapsed ? page.label : undefined}
                >
                  <i className={'bi ' + page.icon} aria-hidden="true" />
                  <span className="kv-label">{page.label}</span>
                  {page.to === '/admin/users' && pending > 0 && (
                    <span className="kv-badge">
                      {pending}
                      <span className="visually-hidden"> awaiting approval</span>
                    </span>
                  )}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        <div className="kv-sidebar-foot">
          {/* A plain link, not a router <Link>: Swagger UI is served by the
              API (flask-openapi3, doc_prefix /api-docs), which nginx and the
              Vite dev server both proxy — the SPA has no route for it. */}
          <a
            className="kv-nav-link"
            href="/api-docs/swagger"
            target="_blank"
            rel="noreferrer"
            title={collapsed ? 'API docs' : undefined}
          >
            <i className="bi bi-braces" aria-hidden="true" />
            <span className="kv-label">API docs</span>
            <i className="bi bi-box-arrow-up-right kv-label kv-ext" aria-hidden="true" />
          </a>

          {me && (
            <div className="kv-user" title={me.email}>
              <span className="kv-avatar" aria-hidden="true">
                {me.username.charAt(0).toUpperCase()}
              </span>
              <span className="kv-label kv-user-text">
                <span className="kv-user-name">{me.username}</span>
                <span className="kv-user-role">{isAdmin ? 'Administrator' : 'Member'}</span>
              </span>
            </div>
          )}

          <button
            type="button"
            className="kv-nav-link"
            onClick={() => void signOut()}
            title={collapsed ? 'Sign out' : undefined}
          >
            <i className="bi bi-box-arrow-left" aria-hidden="true" />
            <span className="kv-label">Sign out</span>
          </button>

          <button
            type="button"
            className="kv-collapse"
            onClick={toggleCollapsed}
            aria-pressed={collapsed}
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            <i className={'bi ' + (collapsed ? 'bi-chevron-double-right' : 'bi-chevron-double-left')} />
          </button>
        </div>
      </aside>

      {drawerOpen && <div className="kv-scrim" onClick={() => setDrawerOpen(false)} aria-hidden="true" />}

      <div className="kv-main">
        <header className="kv-topbar">
          <button
            type="button"
            className="kv-menu"
            aria-controls="kv-sidebar"
            aria-expanded={drawerOpen}
            aria-label="Open navigation"
            onClick={() => setDrawerOpen(true)}
          >
            <i className="bi bi-list" />
          </button>
          <span className="kv-topbar-brand">
            <SeedRow className="kv-seeds-sm" />
            kvuno
          </span>
        </header>

        <main id="kv-main" className="kv-content" tabIndex={-1}>
          {/* One centred column per page, header included, so the title lines
              up with the content beneath it at every width. */}
          <div className={'kv-container kv-container-' + (current?.width ?? 'wide')}>
            {current && (
              <header className="kv-page-head">
                <div className="kv-eyebrow">{current.group}</div>
                <h1 className="kv-title">{current.title}</h1>
                <p className="kv-lede">{current.description}</p>
              </header>
            )}
            <Outlet />
          </div>
        </main>

        <footer className="kv-footer">
          <div className={'kv-container kv-container-' + (current?.width ?? 'wide')}>
            kvuno &middot;{' '}
            <a href="https://agwise.cgiar.org" target="_blank" rel="noreferrer">
              AgWise
            </a>
          </div>
        </footer>
      </div>
    </div>
  )
}
