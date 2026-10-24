import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError, register } from '../api'
import AuthShell from '../components/AuthShell'
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
      <AuthShell title="Request received">
        <p>
          An administrator needs to approve your account before you can sign in. You&rsquo;ll get
          an email at <strong>{pending}</strong> once it&rsquo;s activated.
        </p>
        <Link className="btn btn-outline-primary" to="/login">
          Back to sign in
        </Link>
      </AuthShell>
    )
  }

  return (
    <AuthShell
      title="Request access"
      after={
        <>
          Already have an account? <Link to="/login">Sign in</Link>
        </>
      }
    >
      <p className="text-muted small">An administrator approves new accounts before they can sign in.</p>
      {error && <div className="alert alert-danger">{error}</div>}
      <form onSubmit={submit}>
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
          {busy ? 'Sending request…' : 'Request access'}
        </button>
      </form>
    </AuthShell>
  )
}
