import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { Link } from 'react-router-dom'
import { UPLOADS_CHANGED, deleteJobFile, fetchJobProgress, startProcessing } from '../api'
import type { Job } from '../api'
import { STATUSES, useJobStream } from '../hooks/useJobStream'
import type { StatusFilter } from '../hooks/useJobStream'
import { useAuth } from '../context/AuthContext'
import { useToast } from '../context/ToastContext'
import RunPanel from '../components/RunPanel'
import StatusBadge, { TONE_BAR } from '../components/StatusBadge'
import type { Tone } from '../components/StatusBadge'

interface StatusMeta {
  tone: Tone
  bar: string
  label: string
}

const STATUS: Record<string, StatusMeta> = {
  processing: { tone: 'info', bar: TONE_BAR.info, label: 'Processing' },
  // A data-folder RDS being converted to Parquet before it is imported. Its
  // read cannot be measured (no total), so the bar is indeterminate until
  // the write reports rows. Listed under the Processing filter.
  converting: { tone: 'info', bar: TONE_BAR.info, label: 'Converting' },
  completed: { tone: 'success', bar: TONE_BAR.success, label: 'Completed' },
  error: { tone: 'danger', bar: TONE_BAR.danger, label: 'Failed' },
  // Worker stopped reporting (crash, OOM kill, restart). Not running.
  stale: { tone: 'warning', bar: TONE_BAR.warning, label: 'Stalled' },
  unknown: { tone: 'neutral', bar: TONE_BAR.neutral, label: 'Pending' },
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
  const { jobs, connected, remove } = useJobStream()
  const { isAdmin } = useAuth()
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
    for (const job of jobs) {
      const key = job.status === 'converting' ? 'processing' : job.status
      acc[key] = (acc[key] ?? 0) + 1
    }
    return acc
  }, [jobs])

  const visible = useMemo(() => {
    const term = search.toLowerCase().trim()
    return jobs.filter((job) => {
      const status = job.status === 'converting' ? 'processing' : job.status
      if (filter !== 'all' && status !== filter) return false
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

  async function deleteFile(file: string): Promise<boolean> {
    try {
      await deleteJobFile(file)
    } catch (err) {
      toast.push('Could not delete ' + file + ': ' + (err as Error).message, 'danger')
      return false
    }
    remove(file)
    // The upload may have been one awaiting mapping; refresh the sidebar count.
    window.dispatchEvent(new Event(UPLOADS_CHANGED))
    toast.push('Deleted ' + file + '. Imported rows were kept.', 'success')
    return true
  }

  return (
    <div className="pb-3">
      <div className="d-flex flex-wrap gap-2 align-items-center mb-3">
        <div className="kv-segment" role="group" aria-label="Filter by status">
          {STATUSES.map((status) => (
            <button
              key={status}
              type="button"
              className={filter === status ? 'active' : ''}
              aria-pressed={filter === status}
              onClick={() => setFilter(status)}
            >
              {status === 'all' ? 'All' : statusMeta(status).label}
              {status !== 'all' && counts[status] ? <span className="kv-count">{counts[status]}</span> : null}
            </button>
          ))}
        </div>
        <input
          type="search"
          aria-label="Search files"
          className="form-control form-control-sm ms-md-auto"
          style={{ maxWidth: '18rem' }}
          placeholder="Search files…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <span className={'kv-live' + (connected ? ' is-live' : '')} role="status">
          {connected ? 'Live' : 'Reconnecting…'}
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
        {jobs.length === 0 ? (
          <div className="kv-empty">
            <p>
              No imports yet. <Link to="/upload">Upload a file</Link> to start one.
            </p>
          </div>
        ) : visible.length === 0 ? (
          <div className="kv-empty">
            <p>No jobs match this status or search.</p>
          </div>
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
                  const active = job.status === 'processing' || job.status === 'converting'
                  // Converting with no total yet: still reading the RDS.
                  const indeterminate = job.status === 'converting' && !total
                  const pct = indeterminate
                    ? 100
                    : total > 0
                      ? Math.round(((job.current ?? 0) / total) * 100)
                      : 0
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
                        <StatusBadge tone={meta.tone} label={meta.label} />
                      </td>
                      <td className="d-none d-md-table-cell text-nowrap small text-muted kvuno-status">
                        {(job.current ?? 0).toLocaleString()} / {total.toLocaleString()}
                      </td>
                      <td className="d-none d-sm-table-cell">
                        <div className="progress" style={{ height: 6 }}>
                          <div
                            className={
                              'progress-bar' +
                              (active ? ' progress-bar-striped progress-bar-animated' : '')
                            }
                            role="progressbar"
                            aria-label="Progress"
                            aria-valuemin={0}
                            aria-valuemax={100}
                            aria-valuenow={indeterminate ? undefined : job.status === 'completed' ? 100 : pct}
                            style={{
                              width: (job.status === 'completed' ? 100 : pct) + '%',
                              backgroundColor: meta.bar,
                              opacity: indeterminate ? 0.45 : undefined,
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
                              (job.status === 'stale' ? 'secondary' : 'danger') +
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
        <JobDetail
          job={liveDetail}
          onClose={() => setDetail(null)}
          onRetry={retry}
          onDelete={isAdmin ? deleteFile : undefined}
        />
      )}
    </div>
  )
}

function JobDetail({
  job,
  onClose,
  onRetry,
  onDelete,
}: {
  job: Job
  onClose: () => void
  onRetry: (file: string) => Promise<void>
  /** Administrators only; resolves whether the file was deleted. */
  onDelete?: (file: string) => Promise<boolean>
}) {
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)
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
                  <StatusBadge tone={meta.tone} label={meta.label} />
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
              {onDelete && !confirming && shown.status !== 'processing' && shown.status !== 'converting' && (
                <button
                  type="button"
                  className="btn btn-sm btn-outline-danger me-auto"
                  disabled={busy}
                  onClick={() => setConfirming(true)}
                >
                  <i className="bi bi-trash me-1" aria-hidden="true" />
                  Delete file
                </button>
              )}
              {onDelete && confirming && (
                <div className="me-auto d-flex flex-wrap align-items-center gap-2 small">
                  <span>Delete the file, its upload and its job history? Imported rows are kept.</span>
                  <button
                    type="button"
                    className="btn btn-sm btn-danger"
                    disabled={busy}
                    onClick={async () => {
                      setBusy(true)
                      // onDelete reports its own failure as a toast and resolves false.
                      if (await onDelete(file)) onClose()
                      else {
                        setBusy(false)
                        setConfirming(false)
                      }
                    }}
                  >
                    Delete
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm btn-outline-secondary"
                    disabled={busy}
                    onClick={() => setConfirming(false)}
                  >
                    Cancel
                  </button>
                </div>
              )}
              {(shown.status === 'error' || shown.status === 'stale') && (
                <button
                  type="button"
                  className={
                    'btn btn-sm btn-outline-' + (shown.status === 'stale' ? 'secondary' : 'danger')
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
