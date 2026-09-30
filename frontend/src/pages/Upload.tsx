import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import Resumable from 'resumablejs'
import { completeUpload, fetchDBColumns, startProcessing } from '../api'
import type { DuplicateInfo } from '../api'
import { useToast } from '../context/ToastContext'

interface ColumnOption {
  value: string
  label: string
}

interface CompletedFile {
  key: string
  name: string
  /**
   * Server-side path after ingestion-ready conversion. NOT the uploaded name:
   * the API converts RDS to Parquet on arrival, so a .rds upload comes back
   * as .parquet and this is the field that must be submitted.
   */
  file: string
  columns: string[]
  rows: Record<string, unknown>[]
  columnMap: Record<string, string>
  /**
   * Set when this file's content was already ingested. The worker skips a
   * fully-processed duplicate as a no-op, so submitting it wastes a job and
   * shows a spinner that resolves to nothing. Surfaced so the user is told
   * before that happens rather than after.
   */
  duplicate: DuplicateInfo | null
  /** User opted in to re-ingest a known duplicate. */
  reprocess: boolean
}

type Phase = 'idle' | 'uploading' | 'mapping' | 'submitting' | 'done'

const EMPTY: ColumnOption[] = [{ value: '', label: '— skip —' }]

/** resumablejs ships no accurate types; these match the runtime we use. */
interface ResumableFile {
  fileName: string
  uniqueIdentifier: string
  chunks: unknown[]
  progress(): number
}
interface TypedResumable {
  files: ResumableFile[]
  assignDrop(element: HTMLElement): void
  assignBrowse(element: HTMLElement, isDirectory?: boolean): void
  upload(): void
  on(event: 'fileAdded', handler: (file: ResumableFile) => void | boolean): void
  on(event: 'fileSuccess', handler: (file: ResumableFile) => void): void
  on(event: 'fileError', handler: (file: ResumableFile, message: string) => void): void
  on(event: 'fileProgress', handler: (file: ResumableFile) => void): void
}

export default function Upload() {
  const toast = useToast()
  const navigate = useNavigate()
  const [dbColumns, setDbColumns] = useState<ColumnOption[]>(EMPTY)
  const [aliases, setAliases] = useState<Record<string, string>>({})
  const [files, setFiles] = useState<CompletedFile[]>([])
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState('')
  const [maxFileSizeMb, setMaxFileSizeMb] = useState(20)

  const dropRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const resumableRef = useRef<TypedResumable | null>(null)
  // `files` is read inside resumable callbacks, which are registered once.
  const filesRef = useRef<CompletedFile[]>([])
  filesRef.current = files

  useEffect(() => {
    const controller = new AbortController()
    void fetchDBColumns(controller.signal)
      .then(({ columns, aliases: a }) => {
        setDbColumns([...EMPTY, ...columns.map((c) => ({ value: c, label: c }))])
        setAliases(a ?? {})
      })
      .catch(() => undefined)
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const drop = dropRef.current
    const input = inputRef.current
    if (!drop || !input) return

    const maxFileSize = 20 * 1024 * 1024
    setMaxFileSizeMb(Math.round(maxFileSize / (1024 * 1024)))

    const options = {
      target: '/ui/upload/resumable',
      fileType: ['rds', 'parquet'],
      maxFileSize,
      chunkSize: 2 * 1024 * 1024,
      simultaneousUploads: 3,
      testChunks: true,
      throttleProgressCallbacks: true,
      maxFilesErrorCallback: () => undefined,
      maxFileSizeErrorCallback: (file: { fileName: string }) => {
        setError(file.fileName + ' is too large — max ' + maxFileSizeMb + ' MB.')
      },
      fileTypeErrorCallback: () => setError('Only .rds and .parquet files are supported.'),
    }

    const instance = new Resumable(options as never) as unknown as TypedResumable
    resumableRef.current = instance
    instance.assignDrop(drop)
    instance.assignBrowse(input, false)

    instance.on('fileAdded', (file) => {
      setError('')
      const ext = file.fileName.split('.').pop()?.toLowerCase() ?? ''
      if (!['rds', 'parquet'].includes(ext)) {
        setError('Only .rds and .parquet files are supported.')
        return false
      }
      setPhase('uploading')
      instance.upload()
      return true
    })

    instance.on('fileSuccess', (file) => {
      void (async () => {
        try {
          const data = await completeUpload(file.uniqueIdentifier, file.chunks.length, file.fileName)
          if (data.error) {
            setError(file.fileName + ': ' + data.error)
            return
          }
          setFiles((current) => [
            ...current,
            {
              key: file.uniqueIdentifier,
              name: file.fileName,
              file: data.file,
              columns: data.columns,
              rows: data.rows ?? [],
              columnMap: {},
              duplicate: data.duplicate ?? null,
              reprocess: false,
            },
          ])
          setPhase('mapping')
        } catch (err) {
          setError(file.fileName + ': ' + (err as Error).message)
        }
      })()
    })

    instance.on('fileError', (file, message) => {
      setError(file.fileName + ': ' + message)
    })

    return () => {
      instance.files.length = 0
    }
  }, [maxFileSizeMb])

  function guessMatch(col: string): ColumnOption | undefined {
    const direct = dbColumns.find((c) => c.value && c.value.toLowerCase() === col.toLowerCase())
    if (direct) return direct
    const alias = aliases[col.toLowerCase()]
    return alias ? dbColumns.find((c) => c.value === alias) : undefined
  }

  function setColumn(key: string, col: string, value: string) {
    setFiles((current) =>
      current.map((f) =>
        f.key === key
          ? { ...f, columnMap: { ...f.columnMap, ...(value ? { [col]: value } : {}) } }
          : f,
      ),
    )
  }

  /** Files that will actually be submitted: duplicates are opt-in only. */
  const queue = files.filter((f) => !f.duplicate || f.reprocess)
  const skipped = files.filter((f) => f.duplicate && !f.reprocess)

  async function processAll() {
    if (queue.length === 0) {
      toast.push('Nothing to process — every file is already ingested.', 'warning')
      return
    }
    setPhase('submitting')
    setError('')
    let ok = 0
    let fail = 0
    for (const file of queue) {
      try {
        await startProcessing(file.file, file.columnMap)
        ok += 1
      } catch (err) {
        fail += 1
        setError(file.name + ': ' + (err as Error).message)
      }
    }
    setPhase('done')
    if (fail === 0) {
      toast.push('All ' + ok + ' file(s) sent for processing.', 'success')
    } else {
      toast.push(ok + ' submitted, ' + fail + ' failed.', 'danger')
    }
    // Hand off to the jobs overview so progress is visible. A short delay
    // first: the toast and the disabled button state are the confirmation the
    // user just did something, and a client-side route change does not reload
    // the SPA, so the toast survives.
    if (ok > 0) {
      window.setTimeout(() => navigate('/jobs'), 1200)
    }
  }

  return (
    <div className="py-3" style={{ maxWidth: '60rem' }}>
      <div
        ref={dropRef}
        className="kvuno-card p-5 text-center mb-3"
        style={{ borderStyle: 'dashed', cursor: 'pointer' }}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') inputRef.current?.click()
        }}
      >
        <i className="bi bi-cloud-arrow-up fs-1 text-muted" />
        <p className="mb-1 mt-2">Drop .rds or .parquet files here, or click to browse</p>
        <p className="text-muted small mb-0">Up to {maxFileSizeMb} MB each, uploaded in 2 MB chunks.</p>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".rds,.parquet"
          className="d-none"
          aria-label="Choose files"
        />
      </div>

      {error && <div className="alert alert-danger">{error}</div>}

      {files.length > 0 && (
        <div className="kvuno-card p-3 mb-3">
          <h2 className="h6 mb-3">Map columns to the database schema</h2>
          {files.map((file) => (
            <div key={file.key} className="mb-4">
              <div className="d-flex justify-content-between align-items-center mb-2">
                <h3 className="h6 mb-0 kvuno-truncate">{file.name}</h3>
                <span className="badge text-bg-secondary">{file.columns.length} columns</span>
              </div>
              {file.duplicate && (
                <div
                  className={
                    'alert alert-' + (file.reprocess ? 'warning' : 'secondary') + ' py-2 px-3 mb-2 small'
                  }
                >
                  <div className="fw-semibold">
                    {file.duplicate.status === 'processed'
                      ? 'Already ingested — this file will be skipped.'
                      : 'Partially ingested — processing will resume at row ' +
                        (file.duplicate.offset ?? 0).toLocaleString() +
                        '.'}
                  </div>
                  <div className="text-muted">
                    Same content as{' '}
                    <span className="fw-semibold">{file.duplicate.original_filename}</span>
                    {file.duplicate.processed_at
                      ? ', processed ' + new Date(file.duplicate.processed_at).toLocaleString()
                      : ''}
                    .
                  </div>
                  <div className="form-check mt-1">
                    <input
                      className="form-check-input"
                      type="checkbox"
                      id={'reprocess-' + file.key}
                      checked={file.reprocess}
                      onChange={(e) =>
                        setFiles((current) =>
                          current.map((f) =>
                            f.key === file.key ? { ...f, reprocess: e.target.checked } : f,
                          ),
                        )
                      }
                    />
                    <label
                      className="form-check-label"
                      htmlFor={'reprocess-' + file.key}
                    >
                      Process it anyway
                    </label>
                  </div>
                </div>
              )}
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
                    {file.columns.map((col) => {
                      const match = guessMatch(col)
                      const sample = file.rows.length ? String(file.rows[0][col] ?? '') : ''
                      return (
                        <tr key={col}>
                          <td className="fw-semibold small">{col}</td>
                          <td>
                            <code className="small text-muted">{sample}</code>
                          </td>
                          <td>
                            <select
                              className="form-select form-select-sm"
                              value={file.columnMap[col] ?? match?.value ?? ''}
                              onChange={(e) => setColumn(file.key, col, e.target.value)}
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
            </div>
          ))}
          {skipped.length > 0 && (
            <p className="small text-muted mb-2">
              {skipped.length} file(s) already ingested and excluded. Tick
              &ldquo;Process it anyway&rdquo; on a file to include it.
            </p>
          )}
          <div className="d-flex gap-2">
            <button
              className="btn btn-primary"
              disabled={phase === 'submitting' || queue.length === 0}
              title={
                queue.length === 0
                  ? 'Every file here is already ingested. Tick "Process it anyway" to override.'
                  : undefined
              }
              onClick={() => void processAll()}
            >
              {phase === 'submitting'
                ? 'Submitting…'
                : skipped.length > 0
                  ? 'Process ' + queue.length + ' of ' + files.length + ' file(s)'
                  : 'Process ' + files.length + ' file(s)'}
            </button>
            <a className="btn btn-outline-secondary" href="/jobs">
              Go to jobs
            </a>
          </div>
        </div>
      )}

      {phase === 'idle' && (
        <p className="text-muted small text-center">
          Files are uploaded in chunks, so an interrupted transfer resumes where it stopped.
        </p>
      )}
    </div>
  )
}
