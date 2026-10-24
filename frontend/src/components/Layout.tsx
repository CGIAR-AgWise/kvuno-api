import { Suspense, useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { fetchAdminUsers } from '../api'
import { useAuth } from '../context/AuthContext'
import Loading from './Loading'

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
  /** Only where the page needs explaining; most do not. */
  note?: string
  /** Column width: narrow for forms, medium for lists, wide for tables and the map. */
  width: 'narrow' | 'medium' | 'wide'
  adminOnly?: boolean
}

/**
 * The sidebar. "Data" is in workflow order: upload a file, watch it import,
 * explore it, review its conflicts. Page titles come from here too.
 */
const GROUPS: { label: string; pages: Page[] }[] = [
  {
    label: 'Data',
    pages: [
      {
        to: '/upload',
        label: 'Upload',
        icon: 'bi-cloud-arrow-up',
        title: 'Upload',
        width: 'narrow',
      },
      {
        to: '/jobs',
        label: 'Jobs',
        icon: 'bi-list-check',
        title: 'Jobs',
        width: 'wide',
      },
      {
        to: '/explore',
        label: 'Explore',
        icon: 'bi-map',
        title: 'Explore',
        width: 'wide',
      },
      {
        to: '/quality',
        label: 'Quality',
        icon: 'bi-shield-check',
        title: 'Quality',
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
        width: 'narrow',
      },
      {
        to: '/admin/users',
        label: 'Accounts',
        icon: 'bi-people',
        title: 'Accounts',
        note: 'New registrations wait here until approved. Approving sends the user an activation email.',
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
  const current = groups.flatMap((g) => g.pages).find((p) => location.pathname.startsWith(p.to))

  return (
    <div className={'kv-shell' + (collapsed ? ' is-collapsed' : '')}>
      <a className="kv-skip" href="#kv-main">
        Skip to content
      </a>

      <aside ref={sidebarRef} id="kv-sidebar" className={'kv-sidebar' + (drawerOpen ? ' is-open' : '')} aria-label="Main">
        {/* The collapse toggle sits with the wordmark: it acts on the
            sidebar itself, and alone at the bottom it looked stranded. */}
        <div className="kv-brand-row">
          <NavLink to="/jobs" className="kv-brand" title="KVuno">
            <span className="kv-mark" aria-hidden="true" />
            <span className="kv-label">KVuno</span>
          </NavLink>
          <button
            type="button"
            className="kv-collapse"
            onClick={toggleCollapsed}
            aria-pressed={collapsed}
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            <i className={'bi ' + (collapsed ? 'bi-chevron-bar-right' : 'bi-chevron-bar-left')} aria-hidden="true" />
          </button>
        </div>

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
            <div className="kv-user kv-label" title={me.email}>
              <span className="kv-user-name">{me.username}</span>
              <span className="kv-user-role">{isAdmin ? 'Administrator' : 'Signed in'}</span>
            </div>
          )}

          {/* A real button, not another faint nav row: signing out should be
              easy to find. Icon-only (with its label for screen readers) when
              the sidebar is collapsed. */}
          <button
            type="button"
            className="kv-signout"
            onClick={() => void signOut()}
            title={collapsed ? 'Sign out' : undefined}
          >
            <i className="bi bi-box-arrow-right" aria-hidden="true" />
            <span className="kv-label">Sign out</span>
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
            <span className="kv-mark" aria-hidden="true" />
            KVuno
          </span>
        </header>

        <main id="kv-main" className="kv-content" tabIndex={-1}>
          {/* One centred column per page, header included, so the title lines
              up with the content beneath it at every width. */}
          <div className={'kv-container kv-container-' + (current?.width ?? 'wide')}>
            {current && (
              <header className="kv-page-head">
                <h1 className="kv-title">{current.title}</h1>
                {current.note && <p className="kv-note">{current.note}</p>}
              </header>
            )}
            {/* Inside the shell: while a page's code loads, the sidebar and
                header stay and only this area waits. */}
            <Suspense fallback={<Loading />}>
              <Outlet />
            </Suspense>
          </div>
        </main>

        <footer className="kv-footer">
          <div className={'kv-container kv-container-' + (current?.width ?? 'wide')}>
            KVuno, part of{' '}
            <a href="https://agwise.cgiar.org" target="_blank" rel="noreferrer">
              AgWise
            </a>
          </div>
        </footer>
      </div>
    </div>
  )
}
