import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

/**
 * Sign-in and registration: the product bar, one bordered box with the form,
 * whatever goes under it (`after`), and who runs the service.
 */
export default function AuthShell({
  title,
  children,
  after,
}: {
  title: string
  children: ReactNode
  after?: ReactNode
}) {
  return (
    <div className="kv-auth">
      <header className="kv-auth-bar">
        <Link to="/login" className="kv-brand">
          KVuno
        </Link>
        {/* Served by the API, proxied by nginx — not an SPA route. */}
        <a href="/api-docs/swagger" target="_blank" rel="noreferrer">
          API documentation
        </a>
      </header>

      <main className="kv-auth-main">
        <div className="w-100">
          <section className="kv-auth-box mx-auto" aria-labelledby="kv-auth-title">
            <h1 id="kv-auth-title" className="kv-auth-title">
              {title}
            </h1>
            {children}
          </section>
          {after && <div className="kv-auth-after">{after}</div>}
        </div>
      </main>

      <footer className="kv-auth-foot">
        KVuno serves AgWise planting-date recommendations · AgWise, CGIAR
      </footer>
    </div>
  )
}
