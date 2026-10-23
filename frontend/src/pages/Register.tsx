import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError, register } from '../api'
import { useAuth } from '../context/AuthContext'

export default function Register() {
  const { login: adopt } = useAuth()
  const navigate = useNavigate()
  const [form, setForm] = useState({ username: '', email: '', password: '', confirm: '' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  // Set once a request is waiting for an administrator. Signing in straight
  // away (as before approval existed) would just fail with "awaiting approval".
  const [pending, setPending] = useState<string | null>(null)

  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }))

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (form.password !== form.confirm) {
      setError('Passwords do not match')
      return
    }
    setBusy(true)
    setError('')
    try {
      const result = await register(form.username.trim(), form.email.trim(), form.password)
      if (result.status === 'pending') {
        setPending(form.email.trim())
        return
      }
      // Approved at once (an administrator's own address): sign straight in.
      await adopt(form.username.trim(), form.password)
      navigate('/jobs', { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Registration failed')
      setBusy(false)
    }
  }

  if (pending) {
    return (
      <div className="container py-5" style={{ maxWidth: '26rem' }}>
        <div className="kvuno-card p-4 text-center">
          <i className="bi bi-hourglass-split fs-1 text-primary" aria-hidden="true" />
          <h1 className="h5 mt-2">Request received</h1>
          <p className="text-muted mb-3">
            An administrator needs to approve your account before you can sign in. You&rsquo;ll get
            an email at <span className="fw-semibold text-body">{pending}</span> once it&rsquo;s
            activated.
          </p>
          <Link className="btn btn-outline-primary" to="/login">
            Back to sign in
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="container py-5" style={{ maxWidth: '26rem' }}>
      <h1 className="h4 mb-2 text-center">Create an account</h1>
      <p className="text-center text-muted small mb-4">
        New accounts are activated by an administrator.
      </p>
      {error && <div className="alert alert-danger">{error}</div>}
      <form onSubmit={submit} className="kvuno-card p-4">
        {(
          [
            ['username', 'Username', 'username'],
            ['email', 'Email', 'email'],
            ['password', 'Password', 'new-password'],
            ['confirm', 'Confirm password', 'new-password'],
          ] as const
        ).map(([key, label, autoComplete]) => (
          <div className="mb-3" key={key}>
            <label htmlFor={key} className="form-label">
              {label}
            </label>
            <input
              id={key}
              type={key === 'email' ? 'email' : key.startsWith('pass') || key === 'confirm' ? 'password' : 'text'}
              className="form-control"
              value={form[key]}
              onChange={set(key)}
              autoComplete={autoComplete}
              required
            />
          </div>
        ))}
        <button className="btn btn-primary w-100" disabled={busy}>
          {busy ? 'Creating…' : 'Register'}
        </button>
      </form>
      <p className="text-center text-muted small mt-3">
        Already registered? <Link to="/login">Sign in</Link>
      </p>
    </div>
  )
}
