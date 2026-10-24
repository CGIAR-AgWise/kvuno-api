import { useState } from 'react'
import { useNavigate, useSearchParams, Link } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { ApiError } from '../api'
import AuthShell from '../components/AuthShell'

export default function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  // A 403 is a known account that may not sign in yet (awaiting approval,
  // rejected, disabled) — shown as a notice, not as a failed attempt.
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await login(username.trim(), password)
      navigate(params.get('next') ?? '/jobs', { replace: true })
    } catch (err) {
      const message = err instanceof ApiError ? err.message : 'Login failed'
      const sentence = message.charAt(0).toUpperCase() + message.slice(1) + '.'
      if (err instanceof ApiError && err.status === 403) setNotice(sentence)
      else setError(sentence)
      setBusy(false)
    }
  }

  return (
    <AuthShell>
      <h1 className="kv-auth-title">Sign in</h1>
      <p className="text-muted mb-4">Use the username you registered with.</p>
      {notice && (
        <div className="alert alert-warning d-flex gap-2" role="status">
          <i className="bi bi-hourglass-split" aria-hidden="true" />
          <div>{notice}</div>
        </div>
      )}
      {error && <div className="alert alert-danger">{error}</div>}
      <form onSubmit={submit}>
        <div className="mb-3">
          <label htmlFor="username" className="form-label">
            Username
          </label>
          <input
            id="username"
            className="form-control"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            required
          />
        </div>
        <div className="mb-3">
          <label htmlFor="password" className="form-label">
            Password
          </label>
          <input
            id="password"
            type="password"
            className="form-control"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </div>
        <button className="btn btn-primary w-100 py-2 mt-2" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      <p className="text-muted small mt-4">
        No account? <Link to="/register">Request one</Link>
      </p>
    </AuthShell>
  )
}
