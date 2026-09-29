import { useEffect, useRef, useState } from 'react'
import Resumable from 'resumablejs'
import { completeUpload, fetchDBColumns, startProcessing } from '../api'
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

  async function processAll() {
    setPhase('submitting')
    setError('')
    let ok = 0
    let fail = 0
    for (const file of filesRef.current) {
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
          <div className="d-flex gap-2">
            <button
              className="btn btn-primary"
              disabled={phase === 'submitting'}
              onClick={() => void processAll()}
            >
              {phase === 'submitting' ? 'Submitting…' : 'Process ' + files.length + ' file(s)'}
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
