import type { ReactNode } from 'react'

/**
 * Sign-in and registration: the brand panel (desktop only) beside the form.
 * Same soil-and-seeds treatment as the sidebar, so signing in feels like the
 * app it leads to.
 */
export default function AuthShell({ children }: { children: ReactNode }) {
  return (
    <div className="kv-auth">
      <aside className="kv-auth-panel">
        <div className="kv-brand">
          <span className="kv-seeds" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <span className="kv-brand-name">kvuno</span>
        </div>
        <div>
          <p className="kv-auth-statement">
            Planting dates, <em>by place and season.</em>
          </p>
          <p>
            Load AgWise recommendation files, follow each import, and check the results on the map.
          </p>
        </div>
        <div className="kv-auth-foot">Part of AgWise</div>
      </aside>

      <main className="kv-auth-form">
        <div className="kv-auth-mobile-brand">
          <span className="kv-seeds" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          kvuno
        </div>
        {children}
      </main>
    </div>
  )
}
