/** Shown while a page's code or data is on its way. */
export default function Loading() {
  return (
    <div className="kv-loading" role="status">
      <span className="spinner-border spinner-border-sm me-2" aria-hidden="true" />
      Loading…
    </div>
  )
}
