import { useCallback, useEffect, useState } from 'react'
import { ApiError, fetchAdminUsers, reviewUser, setUserRole } from '../api'
import type { AccountStatus, AdminUser, AdminUserList, ReviewAction, Role } from '../api'
import Pagination from '../components/Pagination'
import { useAuth } from '../context/AuthContext'
import { useToast } from '../context/ToastContext'

/**
 * Account approval for administrators (the server's ADMIN_EMAILS).
 *
 * New registrations land here as `pending` and can do nothing until approved.
 * Approving sends the activation email (when the server has SMTP set up);
 * rejecting can include a reason; disabling ends every session at once.
 * Pending is the default tab because it is the only one that needs action.
 *
 * Roles: an approved account can be made an admin (and back). Owners — the
 * server's ADMIN_EMAILS — are admins permanently and cannot be changed here,
 * and nobody can change their own account, so access cannot be lost by mistake.
 * The server enforces all of this; the UI only hides what it would refuse.
 */

type Tab = AccountStatus | 'all'

const TABS: { key: Tab; label: string }[] = [
  { key: 'pending', label: 'Pending' },
  { key: 'approved', label: 'Approved' },
  { key: 'rejected', label: 'Rejected' },
  { key: 'disabled', label: 'Disabled' },
  { key: 'all', label: 'All' },
]

const STATUS_BADGE: Record<AccountStatus, string> = {
  pending: 'text-bg-warning',
  approved: 'text-bg-success',
  rejected: 'text-bg-danger',
  disabled: 'text-bg-secondary',
}

const EMPTY: Record<Tab, string> = {
  pending: 'No accounts are waiting for approval.',
  approved: 'No approved accounts match.',
  rejected: 'No rejected accounts match.',
  disabled: 'No disabled accounts match.',
  all: 'No accounts match.',
}

function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

/** What the row is asking to confirm, if anything. */
type Confirming = { id: number; action: 'reject' | 'disable' } | null

export default function AdminUsers() {
  const toast = useToast()
  const { me } = useAuth()
  const [tab, setTab] = useState<Tab>('pending')
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [data, setData] = useState<AdminUserList | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notify, setNotify] = useState(true)
  const [busy, setBusy] = useState<number | null>(null)
  const [confirming, setConfirming] = useState<Confirming>(null)
  const [reason, setReason] = useState('')

  // Debounce the search box so typing does not fire a request per key.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setQuery(search.trim())
      setPage(1)
    }, 300)
    return () => window.clearTimeout(timer)
  }, [search])

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true)
      setError('')
      try {
        setData(
          await fetchAdminUsers(
            { status: tab === 'all' ? undefined : tab, q: query || undefined, page },
            signal,
          ),
        )
      } catch (err) {
        if (signal?.aborted) return
        setError(err instanceof ApiError ? err.message : 'Could not load accounts')
      } finally {
        if (!signal?.aborted) setLoading(false)
      }
    },
    [tab, query, page],
  )

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  async function act(user: AdminUser, action: ReviewAction) {
    setBusy(user.id)
    try {
      const result = await reviewUser(user.id, action, {
        notify,
        reason: action === 'reject' && reason.trim() ? reason.trim() : undefined,
      })
      const done = { approve: 'approved', reject: 'rejected', disable: 'disabled' }[action]
      const mail =
        action === 'disable' || !notify
          ? ''
          : result.emailed
            ? ' Email sent.'
            : ' (Email is not configured on the server, so none was sent.)'
      toast.push(`${user.username} ${done}.${mail}`, action === 'approve' ? 'success' : 'warning')
      setConfirming(null)
      setReason('')
      await load()
      // Tell the navbar badge the pending count changed.
      window.dispatchEvent(new Event('kvuno:accounts-changed'))
    } catch (err) {
      toast.push(err instanceof ApiError ? err.message : 'Action failed', 'danger')
    } finally {
      setBusy(null)
    }
  }

  async function changeRole(user: AdminUser, role: Role) {
    setBusy(user.id)
    try {
      await setUserRole(user.id, role)
      toast.push(
        role === 'admin'
          ? `${user.username} is now an admin.`
          : `${user.username} is no longer an admin.`,
        'success',
      )
      await load()
    } catch (err) {
      toast.push(err instanceof ApiError ? err.message : 'Could not change the role', 'danger')
    } finally {
      setBusy(null)
    }
  }

  const counts = data?.counts
  const users = data?.users ?? []

  return (
    <div className="py-3" style={{ maxWidth: '72rem' }}>
      <div className="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-3">
        <div>
          <h1 className="h5 mb-0">Accounts</h1>
          <p className="text-muted small mb-0">
            New registrations wait here until approved. Approving sends the user their activation
            email.
          </p>
        </div>
        <div className="form-check form-switch mb-0">
          <input
            className="form-check-input"
            type="checkbox"
            role="switch"
            id="notify-users"
            checked={notify}
            onChange={(e) => setNotify(e.target.checked)}
          />
          <label className="form-check-label small" htmlFor="notify-users">
            Email the user about my decision
          </label>
        </div>
      </div>

      <div className="d-flex flex-wrap gap-2 align-items-center mb-3">
        <ul className="nav nav-pills small" role="tablist" aria-label="Account status">
          {TABS.map(({ key, label }) => {
            const count =
              key === 'all'
                ? counts && Object.values(counts).reduce((sum, n) => sum + n, 0)
                : counts?.[key]
            return (
              <li className="nav-item" key={key} role="presentation">
                <button
                  className={'nav-link py-1 px-2' + (tab === key ? ' active' : '')}
                  role="tab"
                  aria-selected={tab === key}
                  onClick={() => {
                    setTab(key)
                    setPage(1)
                    setConfirming(null)
                  }}
                >
                  {label}
                  {count !== undefined && (
                    <span
                      className={
                        'badge rounded-pill ms-1 ' +
                        (key === 'pending' && count > 0 && tab !== key
                          ? 'text-bg-warning'
                          : 'text-bg-light border')
                      }
                    >
                      {count}
                    </span>
                  )}
                </button>
              </li>
            )
          })}
        </ul>
        <input
          type="search"
          className="form-control form-control-sm ms-auto"
          style={{ maxWidth: '16rem' }}
          placeholder="Search username or email"
          aria-label="Search accounts"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {error && <div className="alert alert-danger">{error}</div>}

      <div className="kvuno-card p-3">
        {loading && !data ? (
          <p className="text-muted text-center py-4 mb-0">Loading…</p>
        ) : users.length === 0 ? (
          <p className="text-muted text-center py-4 mb-0">
            {query ? 'No accounts match your search.' : EMPTY[tab]}
          </p>
        ) : (
          <div className="kvuno-table-fit" aria-busy={loading}>
            <table className="table align-middle mb-0">
              <thead className="table-light">
                <tr>
                  <th>Account</th>
                  <th className="d-none d-md-table-cell">Registered</th>
                  <th>Status</th>
                  <th className="d-none d-lg-table-cell">Reviewed</th>
                  <th className="text-end">
                    <span className="visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => {
                  const rowBusy = busy === user.id
                  const asking = confirming?.id === user.id ? confirming.action : null
                  return [
                    <tr key={user.id}>
                      <td>
                        <div className="fw-semibold">
                          {user.username}
                          {user.is_owner ? (
                            <span
                              className="badge text-bg-dark ms-2 align-text-top"
                              title="Listed in ADMIN_EMAILS on the server"
                            >
                              Owner
                            </span>
                          ) : (
                            user.role === 'admin' && (
                              <span className="badge text-bg-primary ms-2 align-text-top">Admin</span>
                            )
                          )}
                          {user.id === me?.id && <span className="small text-muted ms-1">(you)</span>}
                        </div>
                        <div className="small text-muted">{user.email}</div>
                      </td>
                      <td className="d-none d-md-table-cell small text-muted">
                        {fmtDate(user.created_at)}
                      </td>
                      <td>
                        <span className={'badge text-capitalize ' + STATUS_BADGE[user.status]}>
                          {user.status}
                        </span>
                      </td>
                      <td className="d-none d-lg-table-cell small text-muted">
                        {fmtDate(user.reviewed_at)}
                      </td>
                      <td className="text-end">
                        {user.is_owner || user.id === me?.id ? (
                          <span className="small text-muted">
                            {user.is_owner ? 'Set by ADMIN_EMAILS' : 'Your account'}
                          </span>
                        ) : (
                          <div className="d-inline-flex flex-wrap justify-content-end gap-1">
                            {user.status !== 'approved' && (
                              <button
                                className="btn btn-success btn-sm"
                                disabled={rowBusy}
                                onClick={() => void act(user, 'approve')}
                              >
                                <i className="bi bi-check-lg me-1" aria-hidden="true" />
                                {user.status === 'pending' ? 'Approve' : 'Re-activate'}
                              </button>
                            )}
                            {user.status === 'pending' && (
                              <button
                                className="btn btn-outline-danger btn-sm"
                                disabled={rowBusy}
                                aria-expanded={asking === 'reject'}
                                onClick={() => {
                                  setReason('')
                                  setConfirming(asking === 'reject' ? null : { id: user.id, action: 'reject' })
                                }}
                              >
                                Reject
                              </button>
                            )}
                            {user.status === 'approved' && (
                              <button
                                className="btn btn-outline-primary btn-sm"
                                disabled={rowBusy}
                                onClick={() => void changeRole(user, user.role === 'admin' ? 'user' : 'admin')}
                              >
                                <i
                                  className={'bi me-1 ' + (user.role === 'admin' ? 'bi-person-dash' : 'bi-shield-lock')}
                                  aria-hidden="true"
                                />
                                {user.role === 'admin' ? 'Remove admin' : 'Make admin'}
                              </button>
                            )}
                            {user.status === 'approved' && (
                              <button
                                className="btn btn-outline-secondary btn-sm"
                                disabled={rowBusy}
                                aria-expanded={asking === 'disable'}
                                onClick={() =>
                                  setConfirming(asking === 'disable' ? null : { id: user.id, action: 'disable' })
                                }
                              >
                                Disable
                              </button>
                            )}
                          </div>
                        )}
                      </td>
                    </tr>,
                    asking && (
                      <tr key={user.id + '-confirm'} className="table-light">
                        <td colSpan={5}>
                          {asking === 'reject' ? (
                            <form
                              className="d-flex flex-wrap gap-2 align-items-end"
                              onSubmit={(e) => {
                                e.preventDefault()
                                void act(user, 'reject')
                              }}
                            >
                              <div className="flex-grow-1" style={{ minWidth: '14rem' }}>
                                <label className="form-label small mb-1" htmlFor={'reason-' + user.id}>
                                  Reason {notify ? '(included in the email)' : '(optional)'}
                                </label>
                                <input
                                  id={'reason-' + user.id}
                                  className="form-control form-control-sm"
                                  maxLength={500}
                                  value={reason}
                                  onChange={(e) => setReason(e.target.value)}
                                  placeholder="e.g. Not part of the project"
                                  autoFocus
                                />
                              </div>
                              <button className="btn btn-danger btn-sm" disabled={rowBusy}>
                                Reject {user.username}
                              </button>
                              <button
                                type="button"
                                className="btn btn-link btn-sm"
                                onClick={() => setConfirming(null)}
                              >
                                Cancel
                              </button>
                            </form>
                          ) : (
                            <div className="d-flex flex-wrap gap-2 align-items-center">
                              <span className="small">
                                Disable <strong>{user.username}</strong>? They are signed out
                                everywhere at once and cannot sign in until re-activated.
                              </span>
                              <button
                                className="btn btn-secondary btn-sm ms-auto"
                                disabled={rowBusy}
                                onClick={() => void act(user, 'disable')}
                              >
                                Disable
                              </button>
                              <button className="btn btn-link btn-sm" onClick={() => setConfirming(null)}>
                                Cancel
                              </button>
                            </div>
                          )}
                        </td>
                      </tr>
                    ),
                  ]
                })}
              </tbody>
            </table>
          </div>
        )}

        {data && data.pages > 1 && (
          <div className="d-flex align-items-center mt-3">
            <Pagination page={data.current_page} pages={data.pages} onChange={setPage} />
            <span className="text-muted small">{data.total.toLocaleString()} accounts</span>
          </div>
        )}
      </div>
    </div>
  )
}
