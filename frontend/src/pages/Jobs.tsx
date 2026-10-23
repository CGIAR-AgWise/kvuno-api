import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { fetchJobProgress, startProcessing } from '../api'
import type { Job } from '../api'
import { STATUSES, useJobStream } from '../hooks/useJobStream'
import type { StatusFilter } from '../hooks/useJobStream'
import { useToast } from '../context/ToastContext'
import RunPanel from '../components/RunPanel'

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
  // The file whose detail is open, plus the row as it was when clicked. The
  // modal shows the live row from the SSE list, so it keeps updating.
  const [detail, setDetail] = useState<Job | null>(null)
  const liveDetail = detail ? (jobs.find((j) => j.file === detail.file) ?? detail) : null
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
      // No mapping: the server re-uses the one saved with the upload.
      await startProcessing(file)
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
          aria-label="Search files"
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

      <div className="row g-3">
        <div className="col-lg-4 order-lg-2">
          <div className="kvuno-card p-3 h-100">
            <h2 className="h6 mb-2">Runs</h2>
            <RunPanel />
          </div>
        </div>
        <div className="col-lg-8 order-lg-1">
      <div className="kvuno-card p-3">
        {visible.length === 0 ? (
          <p className="text-muted text-center py-5 mb-0">No matching jobs.</p>
        ) : (
          <div className="kvuno-table-fit">
            <table className="table table-hover align-middle mb-0">
              <thead className="table-light">
                <tr>
                  <th>File</th>
                  <th>Status</th>
                  <th className="d-none d-md-table-cell">Rows</th>
                  <th className="d-none d-sm-table-cell" style={{ minWidth: '5rem' }}>Progress</th>
                  <th>Message</th>
                  <th className="d-none d-md-table-cell">Updated</th>
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
                    <tr
                      key={job.file}
                      onClick={() => setDetail(job)}
                      onKeyDown={(e: KeyboardEvent<HTMLTableRowElement>) => {
                        // Only the row itself: Enter on the Retry button inside
                        // must not also open the detail.
                        if (e.target !== e.currentTarget) return
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          setDetail(job)
                        }
                      }}
                      tabIndex={0}
                      aria-label={'Details for ' + (job.original_name || job.file)}
                      style={{ cursor: 'pointer' }}
                    >
                      <td>
                        <span className="kvuno-truncate fw-medium small" title={job.file}>
                          {job.original_name || job.file}
                        </span>
                      </td>
                      <td>
                        <span className={'badge rounded-pill text-bg-' + meta.color}>{meta.label}</span>
                      </td>
                      <td className="d-none d-md-table-cell text-nowrap small text-muted kvuno-status">
                        {(job.current ?? 0).toLocaleString()} / {total.toLocaleString()}
                      </td>
                      <td className="d-none d-sm-table-cell">
                        <div className="progress" style={{ height: 6 }}>
                          <div
                            className={
                              'progress-bar' +
                              (job.status === 'processing'
                                ? ' progress-bar-striped progress-bar-animated'
                                : '')
                            }
                            role="progressbar"
                            aria-label="Progress"
                            aria-valuemin={0}
                            aria-valuemax={100}
                            aria-valuenow={job.status === 'completed' ? 100 : pct}
                            style={{
                              width: (job.status === 'completed' ? 100 : pct) + '%',
                              backgroundColor: meta.bar,
                            }}
                          />
                        </div>
                      </td>
                      <td className="small text-muted">
                        {job.error || job.message}
                        {job.error && job.message && (
                          <div className="text-muted" style={{ fontSize: '.75rem' }}>
                            {job.message}
                          </div>
                        )}
                        {(job.attempts ?? 0) > 1 && (
                          <div className="text-muted" style={{ fontSize: '.75rem' }}>
                            attempt {job.attempts}
                          </div>
                        )}
                        {rate && (
                          <div className="text-muted" style={{ fontSize: '.75rem' }}>
                            {rate}
                          </div>
                        )}
                      </td>
                      <td className="d-none d-md-table-cell small text-muted">
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
        </div>
      </div>

      {liveDetail && (
        <JobDetail job={liveDetail} onClose={() => setDetail(null)} onRetry={retry} />
      )}
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
  const [busy, setBusy] = useState(false)
  const [fetched, setFetched] = useState<Partial<Job> | null>(null)
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const closeRef = useRef(onClose)
  useEffect(() => {
    closeRef.current = onClose
  }, [onClose])
  const file = job.file

  // Refresh once from GET /ui/progress/<file> (same shape as a /ui/jobs/data
  // item). The row from the SSE list stays the primary source; the fetched
  // copy only wins when it is newer, e.g. after the row left the list.
  useEffect(() => {
    const controller = new AbortController()
    fetchJobProgress(file, controller.signal)
      .then((res) => {
        if (res.status !== 'unknown') setFetched(res)
      })
      .catch(() => undefined)
    return () => controller.abort()
  }, [file])

  // Focus the dialog on open, close on Escape, give focus back on close.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    dialogRef.current?.focus()
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape') closeRef.current()
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      previous?.focus?.()
    }
  }, [])

  const shown: Job =
    fetched && (fetched.mtime ?? 0) > (job.mtime ?? 0) ? { ...job, ...fetched, file } : job
  const meta = statusMeta(shown.status)
  const total = shown.total ?? 0
  const current = shown.current ?? 0
  const pct = shown.status === 'completed' ? 100 : total > 0 ? Math.round((current / total) * 100) : 0
  const titleId = 'job-detail-title'

  return (
    <>
      <div className="modal d-block" tabIndex={-1} onClick={onClose}>
        <div
          ref={dialogRef}
          className="modal-dialog modal-dialog-centered"
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          tabIndex={-1}
          style={{ outline: 'none' }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="modal-content">
            <div className="modal-header">
              <h5 className="modal-title kvuno-truncate" id={titleId}>
                {shown.original_name || shown.file}
              </h5>
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
                      role="progressbar"
                      aria-label="Progress"
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={pct}
                      style={{ width: pct + '%' }}
                    />
                  </div>
                </dd>
                <dt className="col-sm-4">{shown.error ? 'Error' : 'Message'}</dt>
                <dd className="col-sm-8" style={{ whiteSpace: 'pre-wrap' }}>
                  {shown.error || shown.message || '—'}
                </dd>
                <dt className="col-sm-4">Attempts</dt>
                <dd className="col-sm-8">{shown.attempts ?? 1}</dd>
                {shown.worker && (
                  <>
                    <dt className="col-sm-4">Worker</dt>
                    <dd className="col-sm-8">{shown.worker}</dd>
                  </>
                )}
              </dl>
            </div>
            <div className="modal-footer">
              {(shown.status === 'error' || shown.status === 'stale') && (
                <button
                  type="button"
                  className={
                    'btn btn-sm btn-outline-' + (shown.status === 'stale' ? 'warning' : 'danger')
                  }
                  disabled={busy}
                  onClick={async () => {
                    setBusy(true)
                    // retry() reports its own failure as a toast and never throws.
                    await onRetry(file)
                    onClose()
                  }}
                >
                  Retry
                </button>
              )}
              <button type="button" className="btn btn-secondary btn-sm" onClick={onClose}>
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
