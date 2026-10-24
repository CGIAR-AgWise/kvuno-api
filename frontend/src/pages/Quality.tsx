import { useEffect, useMemo, useState } from 'react'
import { fetchConflicts, fetchDuplicates, fetchStats } from '../api'
import type { Conflict, DuplicateGroup, QualityStats } from '../api'
import Pagination from '../components/Pagination'

type Tab = 'conflicts' | 'duplicates'

const NO_FILTERS = new URLSearchParams()

export default function Quality() {
  const [tab, setTab] = useState<Tab>('conflicts')
  const [stats, setStats] = useState<QualityStats | null>(null)
  const [rows, setRows] = useState<Conflict[]>([])
  const [meta, setMeta] = useState({ total: 0, pages: 1, current_page: 1 })
  const [page, setPage] = useState(1)
  const [filters, setFilters] = useState({ country: '', source: '', search: '' })
  const [duplicates, setDuplicates] = useState<DuplicateGroup[]>([])
  const [dupMeta, setDupMeta] = useState({ total: 0, pages: 1, current_page: 1 })
  const [dupPage, setDupPage] = useState(1)
  const [dupsLoading, setDupsLoading] = useState(false)
  const [dupError, setDupError] = useState('')
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
   * Duplicates: one page of groups, grouped by the server. Loaded only while
   * the tab is open, and only the page shown — this used to fetch every page
   * of conflicts to group them in the browser.
   */
  useEffect(() => {
    if (tab !== 'duplicates') return
    const controller = new AbortController()
    setDupsLoading(true)
    setDupError('')
    void fetchDuplicates(NO_FILTERS, dupPage, controller.signal)
      .then(({ rows: data, meta: m }) => {
        setDuplicates(data)
        setDupMeta({ total: m.total, pages: m.pages, current_page: m.current_page })
      })
      .catch((err: Error) => {
        if (err.name !== 'AbortError') setDupError(err.message)
      })
      .finally(() => {
        if (!controller.signal.aborted) setDupsLoading(false)
      })
    return () => controller.abort()
  }, [tab, dupPage])


  const maxSource = stats?.conflicts_by_source?.reduce((m, s) => Math.max(m, s.count), 1) ?? 1

  return (
    <div className="pb-3">
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
                  className="progress-bar"
                  role="progressbar"
                  aria-label={`Conflicts from ${s.source}`}
                  aria-valuemin={0}
                  aria-valuemax={maxSource}
                  aria-valuenow={s.count}
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
            onClick={() => setTab('conflicts')}
          >
            Conflicts
          </button>
        </li>
        <li className="nav-item">
          <button
            className={'nav-link' + (tab === 'duplicates' ? ' active' : '')}
            onClick={() => setTab('duplicates')}
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

          <div className="kvuno-table-fit">
            <table className="table table-hover align-middle mb-0">
              <thead className="table-light">
                <tr>
                  <th className="d-none d-md-table-cell">#</th>
                  <th>Country</th>
                  <th>Province</th>
                  <th>Variety</th>
                  <th>Season</th>
                  <th>Date</th>
                  <th>Source</th>
                  <th className="d-none d-md-table-cell">Logged</th>
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
                      <td className="d-none d-md-table-cell">{r.id}</td>
                      <td>{r.country}</td>
                      <td>{r.province}</td>
                      <td>{r.variety}</td>
                      <td>{r.season_type}</td>
                      <td className="text-nowrap kvuno-status">{r.opt_date}</td>
                      <td>{r.source}</td>
                      <td className="d-none d-md-table-cell text-muted">{r.created_at?.slice(0, 10) ?? ''}</td>
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
          {dupError && <div className="alert alert-danger">{dupError}</div>}
          {dupsLoading && duplicates.length === 0 ? (
            <p className="text-muted text-center py-4 mb-0">Loading duplicates…</p>
          ) : duplicates.length === 0 ? (
            <div className="kv-empty">
              <p>No duplicates found.</p>
            </div>
          ) : (
            <>
              <div className="kvuno-table-fit" aria-busy={dupsLoading}>
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
                      <tr
                        key={[g.country, g.province, g.variety, g.season_type, g.opt_date]
                          .map((v) => v ?? '')
                          .join('|')}
                      >
                        <td>{g.country}</td>
                        <td>{g.province}</td>
                        <td>{g.variety}</td>
                        <td>{g.season_type}</td>
                        <td className="text-nowrap kvuno-status">{g.opt_date}</td>
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
              <div className="d-flex align-items-center mt-3">
                <Pagination page={dupMeta.current_page} pages={dupMeta.pages} onChange={setDupPage} />
                <span className="text-muted small">
                  {dupMeta.total.toLocaleString()} group{dupMeta.total !== 1 ? 's' : ''}
                </span>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
