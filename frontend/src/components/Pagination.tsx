interface Props {
  page: number
  pages: number
  onChange: (page: number) => void
}

/**
 * Numbered pager. Deliberately paged, not infinite scroll — the explore table
 * exists to be read and compared, and a stable page number is shareable via
 * the URL. Rationale is recorded in docs/DESIGN.md.
 */
export default function Pagination({ page, pages, onChange }: Props) {
  if (pages <= 1) return null

  const start = Math.max(1, page - 2)
  const end = Math.min(pages, page + 2)
  const btn = 'btn btn-outline-secondary btn-sm'
  const items: React.ReactNode[] = []

  items.push(
    <button key="prev" className={btn} disabled={page <= 1} onClick={() => onChange(page - 1)}>
      ‹
    </button>,
  )
  if (start > 1) {
    items.push(
      <button key="first" className={btn} onClick={() => onChange(1)}>
        1
      </button>,
    )
    if (start > 2) {
      items.push(
        <button key="dots1" className={btn} disabled>
          …
        </button>,
      )
    }
  }
  for (let i = start; i <= end; i += 1) {
    items.push(
      <button
        key={i}
        className={`btn btn-sm ${i === page ? 'btn-primary' : 'btn-outline-secondary'}`}
        onClick={() => onChange(i)}
      >
        {i}
      </button>,
    )
  }
  if (end < pages) {
    if (end < pages - 1) {
      items.push(
        <button key="dots2" className={btn} disabled>
          …
        </button>,
      )
    }
    items.push(
      <button key="last" className={btn} onClick={() => onChange(pages)}>
        {pages}
      </button>,
    )
  }
  items.push(
    <button key="next" className={btn} disabled={page >= pages} onClick={() => onChange(page + 1)}>
      ›
    </button>,
  )

  return (
    <div className="btn-group btn-group-sm me-2" role="group" aria-label="Pagination">
      {items}
    </div>
  )
}
