import { useEffect, useState } from 'react'
import { createToken, fetchTokens, revokeToken } from '../api'
import type { TokenInfo } from '../api'
import { useToast } from '../context/ToastContext'

function fmtDate(iso: string | null): string {
  if (!iso) return 'Never'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    timeZoneName: 'short',
  })
}

export default function Tokens() {
  const toast = useToast()
  const [tokens, setTokens] = useState<TokenInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [issued, setIssued] = useState<string | null>(null)
  const [expiry, setExpiry] = useState('30')
  const [busy, setBusy] = useState(false)
  const [revoking, setRevoking] = useState<number | null>(null)

  async function load() {
    setLoading(true)
    try {
      // The endpoint is paginated; fetchTokens walks the pages.
      setTokens(await fetchTokens())
    } catch (err) {
      toast.push('Failed to load tokens: ' + (err as Error).message, 'danger')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function create() {
    setBusy(true)
    try {
      setIssued(await createToken(expiry === '0' ? null : Number(expiry)))
      toast.push('Token created', 'success')
      await load()
    } catch (err) {
      toast.push((err as Error).message, 'danger')
    } finally {
      setBusy(false)
    }
  }

  async function revoke(id: number) {
    setRevoking(id)
    try {
      await revokeToken(id)
      toast.push('Token revoked', 'success')
      await load()
    } catch (err) {
      toast.push('Failed to revoke token', 'danger')
    } finally {
      setRevoking(null)
    }
  }

  return (
    <div className="py-3" style={{ maxWidth: '60rem' }}>
      <div className="d-flex flex-wrap gap-2 align-items-end mb-3">
        <div>
          <label htmlFor="expiry-select" className="form-label small text-muted mb-1">
            Expires in
          </label>
          <select
            id="expiry-select"
            className="form-select form-select-sm"
            value={expiry}
            onChange={(e) => setExpiry(e.target.value)}
          >
            <option value="7">7 days</option>
            <option value="30">30 days</option>
            <option value="90">90 days</option>
            <option value="0">Never</option>
          </select>
        </div>
        <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => void create()}>
          <i className="bi bi-plus-lg me-1" />
          New token
        </button>
      </div>

      {issued && (
        <div className="alert alert-success">
          <div className="fw-semibold mb-1">Copy this token now — it is not shown again.</div>
          <div className="d-flex gap-2">
            <input
              className="form-control form-control-sm font-monospace"
              readOnly
              value={issued}
              onFocus={(e) => e.currentTarget.select()}
              aria-label="New token"
            />
            <button
              className="btn btn-outline-success btn-sm text-nowrap"
              onClick={() => {
                void navigator.clipboard
                  .writeText(issued)
                  .then(() => toast.push('Copied to clipboard', 'success'))
                  .catch(() => toast.push('Copy failed — select and copy manually', 'warning'))
              }}
            >
              Copy
            </button>
          </div>
        </div>
      )}

      <div className="kvuno-card p-3">
        {loading ? (
          <p className="text-muted text-center py-4 mb-0">Loading…</p>
        ) : tokens.length === 0 ? (
          <p className="text-muted text-center py-4 mb-0">No active tokens found.</p>
        ) : (
          <div className="table-responsive">
            <table className="table table-hover align-middle mb-0">
              <thead className="table-light">
                <tr>
                  <th>#</th>
                  <th>Created</th>
                  <th>Expires</th>
                  <th>Last used</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {tokens.map((tk) => (
                  <tr key={tk.id}>
                    <td>{tk.id}</td>
                    <td>{fmtDate(tk.created_at)}</td>
                    <td>{fmtDate(tk.expires_at)}</td>
                    <td>{fmtDate(tk.last_used_at)}</td>
                    <td className="text-end">
                      <button
                        className="btn btn-outline-danger btn-sm"
                        disabled={revoking === tk.id}
                        onClick={() => void revoke(tk.id)}
                      >
                        Revoke
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
