/** The seed row, pulsing: shown while a page's code or data is on its way. */
export default function Loading() {
  return (
    <div className="kv-loading" role="status">
      <span className="kv-seeds" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      <span className="visually-hidden">Loading…</span>
    </div>
  )
}
