/**
 * One look for every status in the app (jobs, runs, uploads, accounts).
 *
 * Each tone has its own hue AND its own icon, so statuses stay distinct
 * without relying on colour, and none of them borrows the brand red: the old
 * badges used Bootstrap's "primary", which became AKILIMO red, so Processing
 * looked exactly like Failed.
 */
export type Tone = 'info' | 'success' | 'danger' | 'warning' | 'neutral'

const ICON: Record<Tone, string> = {
  info: 'bi-arrow-repeat',
  success: 'bi-check-circle-fill',
  danger: 'bi-x-circle-fill',
  warning: 'bi-exclamation-triangle-fill',
  neutral: 'bi-clock',
}

/** Progress-bar colour for each tone, matching the badge text. */
export const TONE_BAR: Record<Tone, string> = {
  info: '#2563eb',
  success: '#16a34a',
  danger: '#dc2626',
  warning: '#d97706',
  neutral: '#9ca3af',
}

export default function StatusBadge({ tone, label, icon }: { tone: Tone; label: string; icon?: string }) {
  return (
    <span className={'kv-status kv-status-' + tone}>
      <i className={'bi ' + (icon ?? ICON[tone])} aria-hidden="true" />
      {label}
    </span>
  )
}
