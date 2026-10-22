/**
 * Minimal tus 1.0 client (core + creation) for the API's /ui/uploads/tus.
 *
 * Written in-house rather than pulling in tus-js-client: the server side is
 * ours (app/routes/uploads.py) and implements only core + creation, so the
 * client needs exactly four things — create, ask for the offset, send pieces,
 * retry. Keeping it here also keeps the bundle free of a dependency for that.
 *
 * Behaviour that makes uploads survive real networks:
 * - The upload URL is remembered in localStorage under a fingerprint of the
 *   file (name, size, mtime), so picking the same file after a reload or a
 *   crash continues it instead of starting over.
 * - Every piece is sent from the server's offset (HEAD), never from what the
 *   client thinks it sent, so a half-delivered PATCH is never duplicated.
 * - Transient failures (network, 5xx, 409 offset mismatch, 423 busy, 429) are
 *   retried with backoff; other 4xx are final.
 * - Progress comes from XHR upload events, so a 5 MB piece reports smoothly.
 */

const TUS_VERSION = '1.0.0'
const STORE_PREFIX = 'tus::'

export interface TusOptions {
  endpoint: string
  chunkSize: number
  retryDelays: number[]
  metadata: Record<string, string>
  headers: Record<string, string>
  onProgress?: (sent: number, total: number) => void
  onSuccess?: (url: string) => void
  onError?: (error: TusError) => void
}

export class TusError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
  ) {
    super(message)
    this.name = 'TusError'
  }
}

interface Reply {
  status: number
  headers: (name: string) => string | null
  body: string
}

function fingerprint(file: File, endpoint: string): string {
  return [STORE_PREFIX, endpoint, file.name, file.size, file.lastModified].join('|')
}

function encodeMetadata(meta: Record<string, string>): string {
  return Object.entries(meta)
    .map(([k, v]) => k + ' ' + btoa(String.fromCharCode(...new TextEncoder().encode(v))))
    .join(',')
}

function errorFrom(reply: Reply, fallback: string): TusError {
  let message = fallback
  try {
    message = (JSON.parse(reply.body) as { error?: string }).error ?? fallback
  } catch {
    /* not JSON */
  }
  return new TusError(message, reply.status)
}

function retryable(status: number | null): boolean {
  return status === null || status >= 500 || status === 409 || status === 423 || status === 429
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, ms)
    signal.addEventListener('abort', () => {
      window.clearTimeout(timer)
      reject(new DOMException('aborted', 'AbortError'))
    })
  })
}

export class TusUpload {
  url: string | null = null
  private readonly controller = new AbortController()
  private xhr: XMLHttpRequest | null = null

  constructor(
    private readonly file: File,
    private readonly options: TusOptions,
  ) {}

  /** Begin, or continue a previous upload of the same file. */
  start(): void {
    void this.run().catch((err: unknown) => {
      if (this.controller.signal.aborted) return
      this.options.onError?.(
        err instanceof TusError ? err : new TusError((err as Error).message, null),
      )
    })
  }

  /** Stop sending. `forget` also drops the stored URL so it cannot resume. */
  abort(forget = false): void {
    this.controller.abort()
    this.xhr?.abort()
    if (forget) this.forget()
  }

  private forget() {
    try {
      localStorage.removeItem(fingerprint(this.file, this.options.endpoint))
    } catch {
      /* storage unavailable */
    }
  }

  private send(
    method: 'POST' | 'HEAD' | 'PATCH',
    url: string,
    headers: Record<string, string>,
    body?: Blob,
  ): Promise<Reply> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      this.xhr = xhr
      xhr.open(method, url)
      xhr.setRequestHeader('Tus-Resumable', TUS_VERSION)
      for (const [k, v] of Object.entries({ ...this.options.headers, ...headers })) {
        xhr.setRequestHeader(k, v)
      }
      if (body && this.options.onProgress) {
        const base = Number(headers['Upload-Offset'] ?? 0)
        xhr.upload.onprogress = (e) => this.options.onProgress?.(base + e.loaded, this.file.size)
      }
      xhr.onload = () =>
        resolve({
          status: xhr.status,
          headers: (name) => xhr.getResponseHeader(name),
          body: xhr.responseText,
        })
      xhr.onerror = () => reject(new TusError('Network error', null))
      xhr.onabort = () => reject(new DOMException('aborted', 'AbortError'))
      xhr.send(body ?? null)
    })
  }

  /** Run `step`, retrying transient failures per `retryDelays`. */
  private async withRetry<T>(step: () => Promise<T>): Promise<T> {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await step()
      } catch (err) {
        if (this.controller.signal.aborted) throw err
        const status = err instanceof TusError ? err.status : null
        const delay = this.options.retryDelays[attempt]
        if (!retryable(status) || delay === undefined) throw err
        await sleep(delay, this.controller.signal)
      }
    }
  }

  private async create(): Promise<string> {
    const reply = await this.send('POST', this.options.endpoint, {
      'Upload-Length': String(this.file.size),
      'Upload-Metadata': encodeMetadata(this.options.metadata),
    })
    const location = reply.headers('Location')
    if (reply.status !== 201 || !location) throw errorFrom(reply, 'Could not start the upload')
    const url = new URL(location, window.location.href).pathname
    try {
      localStorage.setItem(fingerprint(this.file, this.options.endpoint), url)
    } catch {
      /* resuming after a reload is a convenience */
    }
    return url
  }

  /** The server's offset, or null when the stored upload no longer exists. */
  private async offset(url: string): Promise<number | null> {
    const reply = await this.send('HEAD', url, {})
    if (reply.status === 404 || reply.status === 410 || reply.status === 403) return null
    const value = Number(reply.headers('Upload-Offset'))
    if (reply.status !== 200 || Number.isNaN(value)) throw errorFrom(reply, 'Could not resume')
    return value
  }

  private async run(): Promise<void> {
    let stored: string | null = null
    try {
      stored = localStorage.getItem(fingerprint(this.file, this.options.endpoint))
    } catch {
      /* storage unavailable */
    }

    let offset: number | null = null
    if (stored) {
      offset = await this.withRetry(() => this.offset(stored as string))
      if (offset !== null) this.url = stored
    }
    if (offset === null) {
      this.url = await this.withRetry(() => this.create())
      offset = 0
    }
    const url = this.url as string
    this.options.onProgress?.(offset, this.file.size)

    // After any failed PATCH, ask the server where it got to before sending
    // again: part of the piece may have landed.
    let resync = false
    while (offset < this.file.size) {
      offset = await this.withRetry(async () => {
        let from = offset as number
        if (resync) {
          const current = await this.offset(url)
          if (current === null) throw new TusError('The upload expired on the server', 404)
          from = current
          resync = false
          if (from >= this.file.size) return from
        }
        const piece = this.file.slice(from, Math.min(from + this.options.chunkSize, this.file.size))
        try {
          const reply = await this.send(
            'PATCH',
            url,
            { 'Upload-Offset': String(from), 'Content-Type': 'application/offset+octet-stream' },
            piece,
          )
          const next = Number(reply.headers('Upload-Offset'))
          if (reply.status !== 204 || Number.isNaN(next)) throw errorFrom(reply, 'Upload failed')
          return next
        } catch (err) {
          resync = true
          throw err
        }
      })
      this.options.onProgress?.(offset, this.file.size)
    }

    this.forget()
    this.options.onSuccess?.(url)
  }
}
