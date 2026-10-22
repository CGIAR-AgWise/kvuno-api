import { useEffect, useState } from 'react'
import { fetchRuns } from '../api'
import type { Run } from '../api'

/**
 * Recent ingestion runs, newest first.
 *
 * Progress is recorded per file, so without this a five-file upload showed
 * five unrelated jobs and there was no way to ask how the batch as a whole was
 * doing. Each card answers that: how many files, how many done or failed, and
 * the overall row progress.
 *
 * Polls rather than streaming: runs settle on a timescale of seconds to
 * minutes, and the jobs table beside it is already the live view.
 */
const RUN_STATUS: Record<Run['status'], { label: string; variant: string }> = {
  pending: { label: 'Queued', variant: 'text-bg-secondary' },
  running: { label: 'Running', variant: 'text-bg-primary' },
  completed: { label: 'Completed', variant: 'text-bg-success' },
  error: { label: 'Failed', variant: 'text-bg-danger' },
  partial: { label: 'Partial', variant: 'text-bg-warning' },
  stale: { label: 'Stalled', variant: 'text-bg-warning' },
}

function when(seconds: number): string {
  if (!seconds) return '—'
  return new Date(seconds * 1000).toLocaleString()
}

export default function RunPanel({ pollMs = 5000 }: { pollMs?: number }) {
  const [runs, setRuns] = useState<Run[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    let timer: number | undefined

    const load = async () => {
      try {
        setRuns(await fetchRuns(10, controller.signal))
        setError(null)
      } catch (err) {
        if ((err as Error).name !== 'AbortError') setError((err as Error).message)
      } finally {
        if (!controller.signal.aborted) {
          timer = window.setTimeout(load, pollMs)
        }
      }
    }

    void load()
    return () => {
      controller.abort()
      if (timer) window.clearTimeout(timer)
    }
  }, [pollMs])

  if (error) {
    return <div className="alert alert-warning small mb-0">Run history unavailable: {error}</div>
  }
  if (!runs.length) {
    return <p className="text-muted small mb-0">No ingestion runs recorded yet.</p>
  }

  return (
    <div className="d-flex flex-column gap-2">
      {runs.map((run) => {
        const meta = RUN_STATUS[run.status] ?? RUN_STATUS.pending
        const pct =
          run.rows.total > 0 ? Math.round((run.rows.current / run.rows.total) * 100) : 0
        return (
          <div key={run.id} className="border rounded p-2 bg-white">
            <div className="d-flex justify-content-between align-items-start gap-2">
              <div className="min-w-0">
                <div className="fw-medium small text-truncate">{run.label}</div>
                <div className="text-muted" style={{ fontSize: '.75rem' }}>
                  {when(run.created_at)}
                  {run.initiated_by ? ` · ${run.initiated_by}` : ''}
                </div>
              </div>
              <span className={`badge ${meta.variant} text-nowrap`}>{meta.label}</span>
            </div>

            <div className="d-flex gap-2 align-items-center mt-2">
              <div className="progress flex-grow-1" style={{ height: 5 }}>
                <div
                  className={`progress-bar ${
                    run.status === 'running' ? 'progress-bar-striped progress-bar-animated' : ''
                  }`}
                  role="progressbar"
                  aria-label={`Progress of ${run.label}`}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={pct}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <span className="text-muted small text-nowrap kvuno-status">
                {run.rows.current.toLocaleString()} / {run.rows.total.toLocaleString()}
              </span>
            </div>

            <div className="d-flex gap-2 flex-wrap mt-1" style={{ fontSize: '.75rem' }}>
              <span className="text-muted">{run.counts.total} file(s)</span>
              {run.counts.completed > 0 && (
                <span className="text-success">{run.counts.completed} done</span>
              )}
              {run.counts.processing > 0 && (
                <span className="text-primary">{run.counts.processing} running</span>
              )}
              {run.counts.error > 0 && (
                <span className="text-danger">{run.counts.error} failed</span>
              )}
              {run.counts.stale > 0 && (
                <span className="text-warning">{run.counts.stale} stalled</span>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
