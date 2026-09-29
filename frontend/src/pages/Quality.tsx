import { useEffect, useMemo, useState } from 'react'
import { fetchAllConflicts, fetchConflicts, fetchStats } from '../api'
import type { Conflict, QualityStats } from '../api'
import Pagination from '../components/Pagination'
import { useToast } from '../context/ToastContext'

type Tab = 'conflicts' | 'duplicates'

interface DuplicateGroup {
  key: string
  country: string | null
  province: string | null
  variety: string | null
  season_type: string | null
  opt_date: string | null
  count: number
  sources: Record<string, number>
}

export default function Quality() {
  const toast = useToast()
  const [tab, setTab] = useState<Tab>('conflicts')
  const [stats, setStats] = useState<QualityStats | null>(null)
  const [rows, setRows] = useState<Conflict[]>([])
  const [meta, setMeta] = useState({ total: 0, pages: 1, current_page: 1 })
  const [page, setPage] = useState(1)
  const [filters, setFilters] = useState({ country: '', source: '', search: '' })
  const [duplicates, setDuplicates] = useState<DuplicateGroup[]>([])
  const [dupsTruncated, setDupsTruncated] = useState(false)
  const [dupsLoading, setDupsLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    void fetchStats(controller.signal)
      .then(setStats)
      .catch(() => undefined)
    return () => controller.abort()
  }, [])

  const params = useMemo(() => {
    const p = new URLSearchParams()
    for (const [key, value] of Object.entries(filters)) if (value) p.set(key, value)
    return p
  }, [filters])

  useEffect(() => {
    const controller = new AbortController()
    setError('')
    void fetchConflicts(params, page, controller.signal)
      .then(({ rows: data, meta: m }) => {
        setRows(data)
        setMeta({ total: m.total, pages: m.pages, current_page: m.current_page })
      })
      .catch((err: Error) => {
        if (err.name !== 'AbortError') setError(err.message)
      })
    return () => controller.abort()
  }, [params, page])

  /**
   * The duplicates view groups by country/province/variety/season/date, so it
   * needs every conflict, not one page. The old UI asked for per_page=10000,
   * which the server now clamps to 500 — that silently truncated the counts.
   * This walks the pages instead.
   */
  async function loadDuplicates() {
    setDupsLoading(true)
    try {
      const all = await fetchAllConflicts()
      const grouped = new Map<string, DuplicateGroup>()
      for (const row of all.rows) {
        const key =
          [row.country, row.province, row.variety, row.season_type, row.opt_date]
            .map((v) => v ?? '')
            .join('|')
        let group = grouped.get(key)
        if (!group) {
          group = {
            key,
            country: row.country,
            province: row.province,
            variety: row.variety,
            season_type: row.season_type,
            opt_date: row.opt_date,
            count: 0,
            sources: {},
          }
          grouped.set(key, group)
        }
        group.count += 1
        const source = row.source || 'unknown'
        group.sources[source] = (group.sources[source] ?? 0) + 1
      }
      setDuplicates([...grouped.values()])
      setDupsTruncated(all.truncated)
    } catch (err) {
      toast.push('Failed to load duplicates: ' + (err as Error).message, 'danger')
    } finally {
      setDupsLoading(false)
    }
  }

  function switchTab(next: Tab) {
    setTab(next)
    if (next === 'duplicates' && duplicates.length === 0) void loadDuplicates()
  }

  const maxSource = stats?.conflicts_by_source?.reduce((m, s) => Math.max(m, s.count), 1) ?? 1

  return (
    <div className="py-3">
      <div className="row g-3 mb-3">
        {(
          [
            ['Records', stats?.total_records],
            ['Conflicts', stats?.total_conflicts],
            ['Files', stats?.total_files],
          ] as const
        ).map(([label, value]) => (
          <div className="col-6 col-md-3" key={label}>
            <div className="kvuno-card p-3">
              <div className="text-muted small">{label}</div>
              <div className="fs-4 fw-semibold kvuno-status">
                {(value ?? 0).toLocaleString()}
              </div>
            </div>
          </div>
        ))}
        <div className="col-6 col-md-3">
          <div className="kvuno-card p-3">
            <div className="text-muted small">Coordinate coverage</div>
            <div className="fs-4 fw-semibold kvuno-status">{stats?.coverage_pct ?? 0}%</div>
          </div>
        </div>
      </div>

      <div className="kvuno-card p-3 mb-3">
        <h2 className="h6 text-muted">Conflicts by source</h2>
        {stats?.conflicts_by_source?.length ? (
          stats.conflicts_by_source.map((s) => (
            <div className="d-flex align-items-center gap-2 mb-1" key={s.source}>
              <span className="text-muted small text-end kvuno-truncate" style={{ width: '9rem' }}>
                {s.source}
              </span>
              <div className="progress flex-grow-1" style={{ height: 8 }}>
                <div
                  className="progress-bar bg-warning"
                  role="progressbar"
                  style={{ width: Math.round((s.count / maxSource) * 100) + '%' }}
                />
              </div>
              <span className="small fw-semibold kvuno-status">{s.count}</span>
            </div>
          ))
        ) : (
          <p className="text-muted small mb-0">No conflict source data.</p>
        )}
      </div>

      <ul className="nav nav-tabs mb-3">
        <li className="nav-item">
          <button
            className={'nav-link' + (tab === 'conflicts' ? ' active' : '')}
            onClick={() => switchTab('conflicts')}
          >
            Conflicts
          </button>
        </li>
        <li className="nav-item">
          <button
            className={'nav-link' + (tab === 'duplicates' ? ' active' : '')}
            onClick={() => switchTab('duplicates')}
          >
            Duplicates
          </button>
        </li>
      </ul>

      {tab === 'conflicts' ? (
        <div className="kvuno-card p-3">
          <div className="row g-2 mb-3">
            {(
              [
                ['country', 'Country'],
                ['source', 'Source'],
                ['search', 'Search…'],
              ] as const
            ).map(([key, label]) => (
              <div className="col-md-4" key={key}>
                <input
                  className="form-control form-control-sm"
                  placeholder={label}
                  aria-label={label}
                  value={filters[key]}
                  onChange={(e) => {
                    setFilters((f) => ({ ...f, [key]: e.target.value }))
                    setPage(1)
                  }}
                />
              </div>
            ))}
          </div>

          {error && <div className="alert alert-danger">{error}</div>}

          <div className="table-responsive">
            <table className="table table-hover align-middle mb-0">
              <thead className="table-light">
                <tr>
                  <th>#</th>
                  <th>Country</th>
                  <th>Province</th>
                  <th>Variety</th>
                  <th>Season</th>
                  <th>Date</th>
                  <th>Source</th>
                  <th>Logged</th>
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="text-center text-muted small py-4">
                      No conflicts found.
                    </td>
                  </tr>
                ) : (
                  rows.map((r) => (
                    <tr key={r.id}>
                      <td>{r.id}</td>
                      <td>{r.country}</td>
                      <td>{r.province}</td>
                      <td>{r.variety}</td>
                      <td>{r.season_type}</td>
                      <td>{r.opt_date}</td>
                      <td>{r.source}</td>
                      <td className="text-muted">{r.created_at?.slice(0, 10) ?? ''}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          <div className="d-flex align-items-center mt-3">
            <Pagination page={meta.current_page} pages={meta.pages} onChange={setPage} />
            <span className="text-muted small">
              {meta.total.toLocaleString()} conflict{meta.total !== 1 ? 's' : ''}
            </span>
          </div>
        </div>
      ) : (
        <div className="kvuno-card p-3">
          {dupsLoading ? (
            <p className="text-muted text-center py-4 mb-0">Loading duplicates…</p>
          ) : duplicates.length === 0 ? (
            <p className="text-muted text-center py-4 mb-0">No duplicates found.</p>
          ) : (
            <>
              {dupsTruncated && (
                <div className="alert alert-warning small">
                  Showing a partial set — these counts are a lower bound.
                </div>
              )}
              <div className="table-responsive">
                <table className="table table-hover align-middle mb-0">
                  <thead className="table-light">
                    <tr>
                      <th>Country</th>
                      <th>Province</th>
                      <th>Variety</th>
                      <th>Season</th>
                      <th>Date</th>
                      <th>Count</th>
                      <th>Sources</th>
                    </tr>
                  </thead>
                  <tbody>
                    {duplicates.map((g) => (
                      <tr key={g.key}>
                        <td>{g.country}</td>
                        <td>{g.province}</td>
                        <td>{g.variety}</td>
                        <td>{g.season_type}</td>
                        <td>{g.opt_date}</td>
                        <td>
                          <span className="badge text-bg-warning">{g.count}x</span>
                        </td>
                        <td className="small text-muted">
                          {Object.entries(g.sources)
                            .map(([source, count]) => source + ' (' + count + ')')
                            .join(', ')}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
