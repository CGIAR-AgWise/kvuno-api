import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { TusUpload } from '../lib/tus'
import {
  ApiError,
  TUS_ENDPOINT,
  UPLOADS_CHANGED,
  discardUpload,
  fetchDBColumns,
  fetchUpload,
  fetchUploads,
  getToken,
  processUpload,
} from '../api'
import type { UploadRecord } from '../api'
import { useAuth } from '../context/AuthContext'
import { useToast } from '../context/ToastContext'
import StatusBadge, { TONE_BAR } from '../components/StatusBadge'
import type { Tone } from '../components/StatusBadge'

/**
 * Upload page — tus resumable uploads, server-side inspection, column mapping.
 *
 * The flow, and why each step is where it is:
 *
 * 1. The browser sends the file over tus (lib/tus.ts). The server issues the
 *    id and stores bytes at an offset, so a dropped connection, a closed tab or
 *    an API restart resumes from the last byte instead of failing. The upload
 *    URL is kept in localStorage, so re-selecting the same file after a reload
 *    continues it.
 * 2. The server inspects the file in the background (RDS -> Parquet, preview,
 *    checksum). This page polls for it; nothing waits on one long request.
 * 3. The server seeds the mapping with its guesses, so what the user sees in
 *    the selects is exactly what will be sent.
 * 4. Unfinished uploads are listed from the server on load, so a reload never
 *    loses a file that is waiting to be mapped.
 * 5. Files waiting for a mapping are listed under "Awaiting mapping" — the
 *    user's own uploads and data-folder files the default mapping could not
 *    import (held by the scan, shared by everyone). A row opens its mapping
 *    when clicked; `?map=<id>` opens one directly. Only opened files are
 *    processed, so nothing is sent with a mapping nobody looked at.
 */

/** Tell the sidebar badge the number of files awaiting mapping may have changed. */
function uploadsChanged() {
  window.dispatchEvent(new Event(UPLOADS_CHANGED))
}

const CHUNK_SIZE = 5 * 1024 * 1024
const RETRY_DELAYS = [0, 1000, 3000, 5000, 10000, 20000, 30000]
const POLL_MS = 1500
const ALLOWED = ['rds', 'parquet']

interface ColumnOption {
  value: string
  label: string
}

const SKIP: ColumnOption = { value: '', label: '— skip —' }

interface Item {
  /** Local key; the server id once known. */
  key: string
  id: string | null
  name: string
  size: number
  sent: number
  /** 'sending' while bytes are in flight; otherwise the server's status. */
  status: 'sending' | 'paused' | UploadRecord['status']
  record: UploadRecord | null
  columnMap: Record<string, string>
  /** "Data date" (YYYY-MM-DD) sent with the mapping; '' = first import time. */
  dataDate: string
  error: string
}

/** The saved data date, else the file's last-modified date as a suggestion. */
function suggestedDate(record: UploadRecord): string {
  return record.data_date ?? record.file_modified_at?.slice(0, 10) ?? ''
}

function fromRecord(record: UploadRecord): Item {
  return {
    key: record.id,
    id: record.id,
    name: record.name,
    size: record.size,
    sent: record.received,
    // A 'receiving' row listed after a reload has no File to send from, so it
    // waits for the user to pick the same file again.
    status: record.status === 'receiving' ? 'paused' : record.status,
    record,
    columnMap: { ...record.column_map },
    dataDate: suggestedDate(record),
    error: record.error ?? '',
  }
}

function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + ' KB'
  return (bytes / 1024 / 1024).toFixed(1) + ' MB'
}

function statusBadge(item: Item): { label: string; tone: Tone; icon?: string } {
  switch (item.status) {
    case 'sending':
      return { label: 'Uploading', tone: 'info' }
    case 'paused':
      return { label: 'Incomplete', tone: 'warning' }
    case 'receiving':
    case 'received':
    case 'inspecting':
      return { label: 'Reading file…', tone: 'info', icon: 'bi-hourglass-split' }
    case 'ready':
      return { label: 'Ready to map', tone: 'success' }
    case 'failed':
      return { label: 'Failed', tone: 'danger' }
    case 'submitted':
      return { label: 'Submitted', tone: 'neutral', icon: 'bi-send-check' }
    case 'processing':
      // Listed by GET /ui/uploads since processing moved into the API; without
      // this case the badge was undefined and the page crashed on reload.
      return { label: 'Processing', tone: 'info' }
  }
}

function formatDuration(seconds: number): string {
  const s = Math.round(seconds)
  return s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's'
}

/**
 * Where the server's inspection has got to. An RDS is converted first: the
 * read cannot be measured (pyreadr reads the whole file in one call), so it
 * shows how long it has been reading; the Parquet write counts rows.
 */
function InspectionProgress({ progress }: { progress: UploadRecord['progress'] }) {
  if (progress?.stage === 'writing' && progress.total > 0) {
    const pct = Math.min(100, Math.round((progress.done / progress.total) * 100))
    return (
      <>
        <div
          className="progress mb-1"
          role="progressbar"
          aria-label="Conversion progress"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div className="progress-bar" style={{ width: pct + '%', backgroundColor: TONE_BAR.info }} />
        </div>
        <p className="small text-muted mb-0">
          Converting to Parquet: {progress.done.toLocaleString()} of {progress.total.toLocaleString()} rows
          written ({formatDuration(progress.elapsed)}).
        </p>
      </>
    )
  }
  const text =
    progress?.stage === 'queued'
      ? 'Waiting for the worker — it may be finishing another file first.'
      : progress?.stage === 'reading'
      ? 'Converting to Parquet: reading the R file (' +
        formatDuration(progress.elapsed) +
        '). Large RDS files take a while; there is no way to measure this step.'
      : progress?.stage === 'writing'
        ? 'Converting to Parquet: writing…'
        : progress?.stage === 'preview'
        ? 'Building the preview…'
        : progress?.stage === 'checksum'
          ? 'Checking whether this file was imported before…'
          : 'Reading the file…'
  return (
    <p className="small text-muted mb-0" role="status">
      <span className="spinner-border spinner-border-sm me-2" aria-hidden="true" />
      {text}
    </p>
  )
}

export default function Upload() {
  const toast = useToast()
  const navigate = useNavigate()
  const { isAdmin } = useAuth()
  const [searchParams] = useSearchParams()
  // Ready files whose mapping is open. A file uploaded on this visit opens by
  // itself once read; ones listed from the server wait for a click.
  const [open, setOpen] = useState<Set<string>>(() => {
    const id = searchParams.get('map')
    return new Set(id ? [id] : [])
  })
  const [dbColumns, setDbColumns] = useState<ColumnOption[]>([SKIP])
  const [maxSize, setMaxSize] = useState<number | null>(null)
  const [items, setItems] = useState<Item[]>([])
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [dragging, setDragging] = useState(false)

  const inputRef = useRef<HTMLInputElement | null>(null)
  const transfers = useRef(new Map<string, TusUpload>())

  const patch = useCallback((key: string, change: Partial<Item>) => {
    setItems((current) => current.map((item) => (item.key === key ? { ...item, ...change } : item)))
  }, [])

  function toggle(id: string) {
    setOpen((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // Columns, the server's size limit, and anything left from a previous visit.
  useEffect(() => {
    const controller = new AbortController()
    fetchDBColumns(controller.signal)
      .then(({ columns, max_file_size }) => {
        setDbColumns([SKIP, ...columns.map((c) => ({ value: c, label: c }))])
        setMaxSize(max_file_size)
      })
      .catch(() => undefined)
    fetchUploads(controller.signal)
      .then(({ uploads }) => {
        setItems((current) => {
          const known = new Set(current.map((i) => i.id))
          return [...current, ...uploads.filter((u) => !known.has(u.id)).map(fromRecord)]
        })
      })
      .catch(() => undefined)
    return () => controller.abort()
  }, [])

  // Pause (not cancel) in-flight transfers on leaving; they resume on re-select.
  useEffect(() => {
    const active = transfers.current
    return () => {
      for (const transfer of active.values()) transfer.abort()
      active.clear()
    }
  }, [])

  // Poll uploads the server is still reading until they settle.
  const waiting = items.filter(
    (i) => i.id && (i.status === 'received' || i.status === 'inspecting' || i.status === 'receiving'),
  )
  const waitingIds = waiting.map((i) => i.id).join(',')
  useEffect(() => {
    if (!waitingIds) return
    const controller = new AbortController()
    const timer = window.setInterval(() => {
      for (const id of waitingIds.split(',')) {
        fetchUpload(id, controller.signal)
          .then((record) => {
            if (record.status === 'ready') {
              // Uploaded on this visit (it was being polled): open its mapping.
              setOpen((current) => new Set(current).add(id))
              uploadsChanged()
            }
            setItems((current) =>
              current.map((item) =>
                item.id === id
                  ? {
                      ...item,
                      status: record.status,
                      record,
                      error: record.error ?? '',
                      // Adopt the server's suggestions once, when they arrive.
                      columnMap:
                        record.status === 'ready' && item.record?.status !== 'ready'
                          ? { ...record.column_map }
                          : item.columnMap,
                      dataDate: item.dataDate || suggestedDate(record),
                    }
                  : item,
              ),
            )
          })
          .catch((err: unknown) => {
            if (err instanceof ApiError && err.status === 404) {
              setItems((current) => current.filter((item) => item.id !== id))
            }
          })
      }
    }, POLL_MS)
    return () => {
      window.clearInterval(timer)
      controller.abort()
    }
  }, [waitingIds])

  function send(file: File) {
    const ext = file.name.split('.').pop()?.toLowerCase() ?? ''
    if (!ALLOWED.includes(ext)) {
      setError(file.name + ': only .rds and .parquet files are supported.')
      return
    }
    if (maxSize !== null && file.size > maxSize) {
      setError(file.name + ' is too large — the limit is ' + formatBytes(maxSize) + '.')
      return
    }

    const key = 'local-' + crypto.randomUUID()
    const token = getToken()
    const transfer = new TusUpload(file, {
      endpoint: TUS_ENDPOINT,
      chunkSize: CHUNK_SIZE,
      retryDelays: RETRY_DELAYS,
      // lastModified becomes the imported rows' created_at: when the data was
      // made, not when it was uploaded (RDS and Parquet carry no such time).
      metadata: {
        filename: file.name,
        filetype: file.type || 'application/octet-stream',
        lastModified: String(file.lastModified),
      },
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      onProgress: (sent) => patch(key, { sent }),
      onError: (err) => {
        transfers.current.delete(key)
        // A refused file (size, type, auth) cannot succeed by retrying; a
        // network failure can, by selecting the file again.
        const final = err.status !== null && err.status >= 400 && err.status < 500
        patch(key, { status: final ? 'failed' : 'paused', error: err.message })
      },
      onSuccess: (url) => {
        transfers.current.delete(key)
        const id = url.split('/').pop() ?? null
        // 'received' starts the poll; the server is now reading the file.
        patch(key, { id, status: 'received', sent: file.size, error: '' })
      },
    })
    transfers.current.set(key, transfer)

    setItems((current) => {
      // Re-selecting a file that was cut off replaces its "Incomplete" row.
      const rest = current.filter(
        (item) => !(item.status === 'paused' && item.name === file.name && item.size === file.size),
      )
      return [
        ...rest,
        {
          key,
          id: null,
          name: file.name,
          size: file.size,
          sent: 0,
          status: 'sending',
          record: null,
          columnMap: {},
          dataDate: '',
          error: '',
        },
      ]
    })

    // Continues a previous upload of the same file if the server still has it.
    transfer.start()
  }

  function addFiles(list: FileList | null) {
    setError('')
    if (!list) return
    for (const file of Array.from(list)) send(file)
    if (inputRef.current) inputRef.current.value = ''
  }

  function setColumn(key: string, column: string, target: string) {
    setItems((current) =>
      current.map((item) => {
        if (item.key !== key) return item
        const columnMap = { ...item.columnMap }
        // Removing the key is what makes "skip" actually skip.
        if (target) columnMap[column] = target
        else delete columnMap[column]
        return { ...item, columnMap }
      }),
    )
  }

  async function discard(item: Item) {
    const transfer = transfers.current.get(item.key)
    if (transfer) {
      transfers.current.delete(item.key)
      transfer.abort(true)
    }
    if (item.id) {
      try {
        await discardUpload(item.id)
      } catch (err) {
        if (!(err instanceof ApiError && err.status === 404)) {
          toast.push('Could not remove ' + item.name + ': ' + (err as Error).message, 'danger')
          return
        }
      }
    }
    setItems((current) => current.filter((i) => i.key !== item.key))
    uploadsChanged()
  }

  const ready = items.filter((i) => i.status === 'ready' && i.id)
  const active = items.filter((i) => !(i.status === 'ready' && i.id))
  const opened = ready.filter((i) => open.has(i.id as string))
  // A fully ingested duplicate is a no-op for the worker, so it is not queued.
  const queue = opened.filter((i) => i.record?.duplicate?.status !== 'processed')
  const skipped = opened.length - queue.length

  async function processAll() {
    if (queue.length === 0) return
    setSubmitting(true)
    setError('')
    let queued = 0
    const problems: string[] = []
    for (const item of queue) {
      try {
        const result = await processUpload(item.id as string, item.columnMap, item.dataDate)
        if (result.queued) {
          queued += 1
          setItems((current) => current.filter((i) => i.key !== item.key))
        } else {
          problems.push(item.name + ': ' + result.message)
        }
      } catch (err) {
        problems.push(item.name + ': ' + (err as Error).message)
      }
    }
    setSubmitting(false)
    uploadsChanged()
    if (problems.length) setError(problems.join('\n'))
    if (queued > 0) {
      toast.push(queued + ' file(s) sent for processing.', 'success')
      // A client-side route change keeps the toast visible.
      window.setTimeout(() => navigate('/jobs'), 1200)
    } else if (problems.length) {
      toast.push('Nothing was queued.', 'danger')
    }
  }

  return (
    <div className="pb-3">
      <div
        className={'kvuno-card p-5 text-center mb-3' + (dragging ? ' border-primary' : '')}
        style={{ borderStyle: 'dashed', cursor: 'pointer' }}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          addFiles(e.dataTransfer.files)
        }}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') inputRef.current?.click()
        }}
      >
        <i className="bi bi-cloud-arrow-up fs-1 text-muted" />
        <p className="mb-1 mt-2">Drop .rds or .parquet files here, or click to browse</p>
        <p className="text-muted small mb-0">
          {maxSize !== null ? 'Up to ' + formatBytes(maxSize) + ' each. ' : ''}
          Interrupted uploads resume where they stopped — just select the file again.
        </p>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".rds,.parquet"
          className="d-none"
          aria-label="Choose files"
          onChange={(e) => addFiles(e.target.files)}
        />
      </div>

      {error && (
        <div className="alert alert-danger" style={{ whiteSpace: 'pre-line' }}>
          {error}
        </div>
      )}

      {[...active, ...ready].map((item, index) => {
        const badge = statusBadge(item)
        const percent = item.size ? Math.min(100, Math.round((item.sent / item.size) * 100)) : 0
        const record = item.record
        const isReady = item.status === 'ready' && item.id !== null
        const isOpen = isReady && open.has(item.id as string)
        const fromFolder = record?.source === 'folder'
        return (
          <Fragment key={item.key}>
          {index === active.length && (
            <h2 className="h6 mt-4 mb-2">
              Awaiting mapping <span className="text-muted fw-normal">({ready.length})</span>
            </h2>
          )}
          <div className="kvuno-card p-3 mb-3">
            <div className={'d-flex justify-content-between align-items-center gap-2' + (isReady && !isOpen ? '' : ' mb-2')}>
              {isReady ? (
                <button
                  type="button"
                  className="btn btn-link p-0 text-reset text-decoration-none text-start d-flex align-items-center gap-2 kvuno-truncate"
                  onClick={() => toggle(item.id as string)}
                  aria-expanded={isOpen}
                  title={item.name}
                >
                  <i className={'bi ' + (isOpen ? 'bi-chevron-down' : 'bi-chevron-right')} aria-hidden="true" />
                  <span className="kvuno-truncate">
                    <span className="h6 mb-0 d-block kvuno-truncate">{item.name}</span>
                    <span className="small text-muted">
                      {fromFolder ? 'Data folder' : 'Upload'}
                      {record?.total_rows != null ? ' · ' + record.total_rows.toLocaleString() + ' rows' : ''}
                    </span>
                  </span>
                </button>
              ) : (
                <h3 className="h6 mb-0 kvuno-truncate" title={item.name}>
                  {item.name}
                </h3>
              )}
              <div className="d-flex align-items-center gap-2 flex-shrink-0">
                <StatusBadge tone={badge.tone} label={badge.label} icon={badge.icon} />
                {/* The server refuses to discard once submitted (409), and a
                    data-folder file is an administrator's to remove (403). */}
                {item.status !== 'submitted' && item.status !== 'processing' && (!fromFolder || isAdmin) && (
                  <button
                    className="btn btn-sm btn-outline-secondary"
                    onClick={() => void discard(item)}
                    aria-label={'Remove ' + item.name}
                  >
                    <i className="bi bi-x-lg" />
                  </button>
                )}
              </div>
            </div>

            {(item.status === 'sending' || item.status === 'paused') && (
              <>
                <div
                  className="progress mb-1"
                  role="progressbar"
                  aria-label={'Upload progress for ' + item.name}
                  aria-valuenow={percent}
                  aria-valuemin={0}
                  aria-valuemax={100}
                >
                  <div className="progress-bar" style={{ width: percent + '%' }} />
                </div>
                <p className="small text-muted mb-0">
                  {formatBytes(item.sent)} of {formatBytes(item.size)}
                  {item.status === 'paused' &&
                    ' — stopped. Select the same file again to continue from here.'}
                </p>
              </>
            )}

            {(item.status === 'received' || item.status === 'inspecting') && (
              <InspectionProgress progress={record?.progress ?? null} />
            )}

            {item.error && item.status !== 'ready' && (
              <p className="small text-danger mb-0 mt-1">{item.error}</p>
            )}

            {isOpen && record && (
              <>
                {fromFolder && (
                  <p className="small text-muted mb-2">
                    Found in the data folder. The default mapping could not find its coordinates, so it
                    was held here instead of being imported.
                  </p>
                )}
                {record.duplicate && (
                  <div className="alert alert-secondary py-2 px-3 mb-2 small">
                    <div className="fw-semibold">
                      {record.duplicate.status === 'processed'
                        ? 'Already ingested — this file will be skipped.'
                        : 'Partially ingested — processing will resume at row ' +
                          (record.duplicate.offset ?? 0).toLocaleString() +
                          '.'}
                    </div>
                    <div className="text-muted">
                      Same content as{' '}
                      <span className="fw-semibold">{record.duplicate.original_filename}</span>
                      {record.duplicate.processed_at
                        ? ', processed ' + new Date(record.duplicate.processed_at).toLocaleString()
                        : ''}
                      .
                    </div>
                  </div>
                )}
                {item.error && <p className="small text-warning mb-2">{item.error}</p>}
                <div className="row g-2 align-items-center mb-3">
                  <label className="col-auto col-form-label col-form-label-sm" htmlFor={'date-' + item.key}>
                    Data date
                  </label>
                  <div className="col-auto">
                    <input
                      id={'date-' + item.key}
                      type="date"
                      className="form-control form-control-sm"
                      value={item.dataDate}
                      min="1990-01-01"
                      max={new Date().toISOString().slice(0, 10)}
                      onChange={(e) => patch(item.key, { dataDate: e.target.value })}
                    />
                  </div>
                  <div className="col-12 small text-muted">
                    When this data was produced; every imported row gets it as its created date.
                    {record.file_modified_at && !record.data_date
                      ? " Suggested from the file's last-modified date — check it, since editing or copying a file changes that."
                      : ''}{' '}
                    Leave it empty to use the import date.
                  </div>
                </div>
                <p className="small text-muted mb-2">
                  {record.columns.length} columns
                  {record.total_rows !== null ? ', ' + record.total_rows.toLocaleString() + ' rows' : ''}.
                  Map each file column to a database column; lat and lon are required.
                </p>
                <div className="kvuno-table-fit">
                  <table className="table table-sm mb-0">
                    <thead className="table-light">
                      <tr>
                        <th>File column</th>
                        <th>Sample</th>
                        <th style={{ width: '40%' }}>Database column</th>
                      </tr>
                    </thead>
                    <tbody>
                      {record.columns.map((col) => {
                        const sample = record.rows.length ? String(record.rows[0][col] ?? '') : ''
                        return (
                          <tr key={col}>
                            <td className="fw-semibold small">{col}</td>
                            <td>
                              <code className="small text-muted">{sample}</code>
                            </td>
                            <td>
                              <select
                                className="form-select form-select-sm"
                                value={item.columnMap[col] ?? ''}
                                onChange={(e) => setColumn(item.key, col, e.target.value)}
                                aria-label={'Map ' + col}
                              >
                                {dbColumns.map((option) => (
                                  <option key={option.value} value={option.value}>
                                    {option.label}
                                  </option>
                                ))}
                              </select>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>
          </Fragment>
        )
      })}

      {ready.length > 0 && (
        <div className="d-flex flex-wrap gap-2 align-items-center">
          <button
            className="btn btn-primary"
            disabled={submitting || queue.length === 0}
            onClick={() => void processAll()}
          >
            {submitting ? 'Submitting…' : 'Process ' + queue.length + ' file(s)'}
          </button>
          {opened.length === 0 && (
            <span className="small text-muted">Open a file to check its mapping.</span>
          )}
          <Link className="btn btn-outline-secondary" to="/jobs">
            Go to jobs
          </Link>
          {skipped > 0 && (
            <span className="small text-muted">{skipped} already-ingested file(s) excluded.</span>
          )}
        </div>
      )}

      {items.length === 0 && (
        <p className="text-muted small text-center">
          Files upload in 5 MB pieces, so a dropped connection or a server restart resumes where it
          stopped.
        </p>
      )}
    </div>
  )
}
