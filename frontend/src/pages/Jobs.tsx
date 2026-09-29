import { useMemo, useState } from 'react'
import { startProcessing } from '../api'
import type { Job } from '../api'
import { STATUSES, useJobStream } from '../hooks/useJobStream'
import type { StatusFilter } from '../hooks/useJobStream'
import { useToast } from '../context/ToastContext'

interface StatusMeta {
  color: string
  bar: string
  label: string
}

const STATUS: Record<string, StatusMeta> = {
  processing: { color: 'primary', bar: '#0d6efd', label: 'Processing' },
  completed: { color: 'success', bar: '#198754', label: 'Completed' },
  error: { color: 'danger', bar: '#dc3545', label: 'Failed' },
  // Worker stopped reporting (crash, OOM kill, restart). Not running.
  stale: { color: 'warning', bar: '#fd7e14', label: 'Stalled' },
  unknown: { color: 'secondary', bar: '#6c757d', label: 'Pending' },
}

const statusMeta = (status?: string): StatusMeta => (status && STATUS[status]) || STATUS.unknown

/**
 * Rows/sec and a finish estimate from the job's original start time. Only
 * meaningful while running — the clock restarts when a job resumes.
 */
function rateInfo(job: Job): string {
  if (job.status !== 'processing') return ''
  const total = job.total ?? 0
  const current = job.current ?? 0
  const started = job.started_at ?? 0
  if (!started || !total || !current) return ''
  const elapsed = Date.now() / 1000 - started
  if (elapsed < 5) return ''
  const perSec = current / elapsed
  if (!(perSec > 0)) return ''
  const remainSec = Math.round((total - current) / perSec)
  if (remainSec < 0) return ''
  const fmt = (s: number) =>
    s < 60
      ? Math.round(s) + 's'
      : s < 3600
        ? Math.round(s / 60) + 'm'
        : (s / 3600).toFixed(1) + 'h'
  return perSec.toLocaleString(undefined, { maximumFractionDigits: 0 }) + ' rows/s, ~' + fmt(remainSec) + ' left'
}

export default function Jobs() {
  const { jobs, connected } = useJobStream()
  const toast = useToast()
  const [filter, setFilter] = useState<StatusFilter>('all')
  const [search, setSearch] = useState('')
  const [detail, setDetail] = useState<Job | null>(null)
  const [retrying, setRetrying] = useState<string | null>(null)

  const counts = useMemo(() => {
    const acc: Record<string, number> = {
      completed: 0,
      processing: 0,
      error: 0,
      stale: 0,
      unknown: 0,
    }
    for (const job of jobs) acc[job.status] = (acc[job.status] ?? 0) + 1
    return acc
  }, [jobs])

  const visible = useMemo(() => {
    const term = search.toLowerCase().trim()
    return jobs.filter((job) => {
      if (filter !== 'all' && job.status !== filter) return false
      if (term) {
        const name = (job.original_name || job.file || '').toLowerCase()
        if (!name.includes(term)) return false
      }
      return true
    })
  }, [jobs, filter, search])

  async function retry(file: string) {
    setRetrying(file)
    try {
      await startProcessing(file, {})
      toast.push('Re-queued ' + file, 'success')
    } catch (err) {
      toast.push('Retry failed: ' + (err as Error).message, 'danger')
    } finally {
      setRetrying(null)
    }
  }

  return (
    <div className="py-3">
      <div className="d-flex flex-wrap gap-2 align-items-center mb-3">
        <div className="btn-group btn-group-sm" role="group" aria-label="Filter by status">
          {STATUSES.map((status) => (
            <button
              key={status}
              className={'btn ' + (filter === status ? 'btn-primary' : 'btn-outline-secondary')}
              onClick={() => setFilter(status)}
            >
              {status === 'all' ? 'All' : statusMeta(status).label}
              {status !== 'all' && counts[status] ? ' (' + counts[status] + ')' : ''}
            </button>
          ))}
        </div>
        <input
          type="search"
          className="form-control form-control-sm ms-auto"
          style={{ maxWidth: '18rem' }}
          placeholder="Search files…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <span className={'badge ' + (connected ? 'text-bg-success' : 'text-bg-secondary')}>
          {connected ? 'live' : 'reconnecting…'}
        </span>
      </div>

      <div className="kvuno-card p-3">
        {visible.length === 0 ? (
          <p className="text-muted text-center py-5 mb-0">No matching jobs.</p>
        ) : (
          <div className="table-responsive">
            <table className="table table-hover align-middle mb-0">
              <thead className="table-light">
                <tr>
                  <th>File</th>
                  <th>Status</th>
                  <th>Rows</th>
                  <th style={{ minWidth: 140 }}>Progress</th>
                  <th>Message</th>
                  <th>Updated</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {visible.map((job) => {
                  const total = job.total ?? 1
                  const pct = total > 0 ? Math.round(((job.current ?? 0) / total) * 100) : 0
                  const meta = statusMeta(job.status)
                  const rate = rateInfo(job)
                  const retryable = job.status === 'error' || job.status === 'stale'
                  return (
                    <tr key={job.file} onClick={() => setDetail(job)} style={{ cursor: 'pointer' }}>
                      <td>
                        <span className="kvuno-truncate fw-medium small" title={job.file}>
                          {job.original_name || job.file}
                        </span>
                      </td>
                      <td>
                        <span className={'badge rounded-pill text-bg-' + meta.color}>{meta.label}</span>
                      </td>
                      <td className="text-nowrap small text-muted kvuno-status">
                        {(job.current ?? 0).toLocaleString()} / {total.toLocaleString()}
                      </td>
                      <td>
                        <div className="progress" style={{ height: 6 }}>
                          <div
                            className={
                              'progress-bar' +
                              (job.status === 'processing'
                                ? ' progress-bar-striped progress-bar-animated'
                                : '')
                            }
                            role="progressbar"
                            style={{
                              width: (job.status === 'completed' ? 100 : pct) + '%',
                              backgroundColor: meta.bar,
                            }}
                          />
                        </div>
                      </td>
                      <td className="small text-muted">
                        {job.message}
                        {rate && (
                          <div className="text-muted" style={{ fontSize: '.75rem' }}>
                            {rate}
                          </div>
                        )}
                      </td>
                      <td className="small text-muted text-nowrap">
                        {job.mtime ? new Date(job.mtime * 1000).toLocaleString() : '—'}
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        {retryable && (
                          <button
                            className={
                              'btn btn-outline-' +
                              (job.status === 'stale' ? 'warning' : 'danger') +
                              ' btn-sm'
                            }
                            disabled={retrying === job.file}
                            onClick={() => void retry(job.file)}
                          >
                            Retry
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {detail && <JobDetail job={detail} onClose={() => setDetail(null)} onRetry={retry} />}
    </div>
  )
}

function JobDetail({
  job,
  onClose,
  onRetry,
}: {
  job: Job
  onClose: () => void
  onRetry: (file: string) => Promise<void>
}) {
  const meta = statusMeta(job.status)
  const total = job.total ?? 0
  const current = job.current ?? 0
  const [busy, setBusy] = useState(false)

  return (
    <>
      <div className="modal d-block" tabIndex={-1} role="dialog" onClick={onClose}>
        <div className="modal-dialog modal-dialog-centered" onClick={(e) => e.stopPropagation()}>
          <div className="modal-content">
            <div className="modal-header">
              <h5 className="modal-title kvuno-truncate">{job.original_name || job.file}</h5>
              <button type="button" className="btn-close" onClick={onClose} aria-label="Close" />
            </div>
            <div className="modal-body">
              <dl className="row mb-0">
                <dt className="col-sm-4">Status</dt>
                <dd className="col-sm-8">
                  <span className={'badge rounded-pill text-bg-' + meta.color}>{meta.label}</span>
                </dd>
                <dt className="col-sm-4">Rows processed</dt>
                <dd className="col-sm-8">
                  {current.toLocaleString()} / {total.toLocaleString()}
                </dd>
                <dt className="col-sm-4">Progress</dt>
                <dd className="col-sm-8">
                  <div className="progress" style={{ height: 6, maxWidth: 200 }}>
                    <div
                      className="progress-bar"
                      style={{ width: (total > 0 ? Math.round((current / total) * 100) : 0) + '%' }}
                    />
                  </div>
                </dd>
                <dt className="col-sm-4">Message</dt>
                <dd className="col-sm-8">{job.message || '—'}</dd>
              </dl>
            </div>
            <div className="modal-footer">
              {(job.status === 'error' || job.status === 'stale') && (
                <button
                  className="btn btn-outline-danger btn-sm"
                  disabled={busy}
                  onClick={async () => {
                    setBusy(true)
                    await onRetry(job.file)
                    onClose()
                  }}
                >
                  Retry
                </button>
              )}
              <button className="btn btn-secondary btn-sm" onClick={onClose}>
                Close
              </button>
            </div>
          </div>
        </div>
      </div>
      <div className="modal-backdrop show" />
    </>
  )
}
