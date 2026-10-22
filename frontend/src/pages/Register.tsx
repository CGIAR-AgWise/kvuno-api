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
      await register(form.username.trim(), form.email.trim(), form.password)
      // The API does not auto-login on register, so sign in straight away.
      await adopt(form.username.trim(), form.password)
      navigate('/jobs', { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Registration failed')
      setBusy(false)
    }
  }

  return (
    <div className="container py-5" style={{ maxWidth: '26rem' }}>
      <h1 className="h4 mb-4 text-center">Create an account</h1>
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
