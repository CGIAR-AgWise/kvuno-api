import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { TusUpload } from '../lib/tus'
import {
  ApiError,
  TUS_ENDPOINT,
  discardUpload,
  fetchDBColumns,
  fetchUpload,
  fetchUploads,
  getToken,
  processUpload,
} from '../api'
import type { UploadRecord } from '../api'
import { useToast } from '../context/ToastContext'

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
 */

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
  error: string
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
    error: record.error ?? '',
  }
}

function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + ' KB'
  return (bytes / 1024 / 1024).toFixed(1) + ' MB'
}

function statusBadge(item: Item): { label: string; tone: string } {
  switch (item.status) {
    case 'sending':
      return { label: 'Uploading', tone: 'primary' }
    case 'paused':
      return { label: 'Incomplete', tone: 'warning' }
    case 'receiving':
    case 'received':
    case 'inspecting':
      return { label: 'Reading file…', tone: 'info' }
    case 'ready':
      return { label: 'Ready to map', tone: 'success' }
    case 'failed':
      return { label: 'Failed', tone: 'danger' }
    case 'submitted':
      return { label: 'Submitted', tone: 'secondary' }
  }
}

export default function Upload() {
  const toast = useToast()
  const navigate = useNavigate()
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
      metadata: { filename: file.name, filetype: file.type || 'application/octet-stream' },
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
  }

  const ready = items.filter((i) => i.status === 'ready' && i.id)
  // A fully ingested duplicate is a no-op for the worker, so it is not queued.
  const queue = ready.filter((i) => i.record?.duplicate?.status !== 'processed')
  const skipped = ready.length - queue.length

  async function processAll() {
    if (queue.length === 0) return
    setSubmitting(true)
    setError('')
    let queued = 0
    const problems: string[] = []
    for (const item of queue) {
      try {
        const result = await processUpload(item.id as string, item.columnMap)
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
    <div className="py-3" style={{ maxWidth: '60rem' }}>
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

      {items.map((item) => {
        const badge = statusBadge(item)
        const percent = item.size ? Math.min(100, Math.round((item.sent / item.size) * 100)) : 0
        const record = item.record
        return (
          <div key={item.key} className="kvuno-card p-3 mb-3">
            <div className="d-flex justify-content-between align-items-center gap-2 mb-2">
              <h2 className="h6 mb-0 kvuno-truncate" title={item.name}>
                {item.name}
              </h2>
              <div className="d-flex align-items-center gap-2 flex-shrink-0">
                <span className={'badge text-bg-' + badge.tone}>{badge.label}</span>
                {item.status !== 'submitted' && (
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
              <p className="small text-muted mb-0">
                <span className="spinner-border spinner-border-sm me-2" aria-hidden="true" />
                Reading the file — large RDS files take a while to convert.
              </p>
            )}

            {item.error && item.status !== 'ready' && (
              <p className="small text-danger mb-0 mt-1">{item.error}</p>
            )}

            {item.status === 'ready' && record && (
              <>
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
                <p className="small text-muted mb-2">
                  {record.columns.length} columns
                  {record.total_rows !== null ? ', ' + record.total_rows.toLocaleString() + ' rows' : ''}.
                  Map each file column to a database column; lat and lon are required.
                </p>
                <div className="table-responsive">
                  <table className="table table-sm mb-0">
                    <thead className="table-light">
                      <tr>
                        <th>File column</th>
                        <th>Sample</th>
                        <th style={{ width: '18rem' }}>Database column</th>
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
