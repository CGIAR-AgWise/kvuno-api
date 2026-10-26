/**
 * Typed client for the Flask API.
 *
 * Every collection endpoint returns the same pagination envelope
 * (total, pages, current_page, per_page) alongside its rows, but each wraps
 * the rows in a differently named key — `data`, `coordinates`, `clusters`,
 * `tokens` — and /filters is shaped differently again. These types make that
 * contract explicit and checkable at compile time, which is the main reason
 * this frontend is TypeScript rather than JavaScript.
 *
 * Base URL: same-origin in production (nginx proxies to the API) and in dev
 * (`vite dev` proxies too), so paths are absolute-from-root.
 */

import { cached } from './lib/cache'

/**
 * Cache lifetimes. Short by default so a page reload still reflects newly
 * ingested data; the two expensive aggregations are held a little longer
 * because recomputing them costs the server real work.
 */
const LIST_TTL_MS = 30_000
const AGGREGATE_TTL_MS = 120_000

/** The metadata block every paginated response carries. */
export interface PageMeta {
  total: number
  pages: number
  current_page: number
  per_page: number
}

/** Server default and ceiling, mirroring app/dto/pagination.py. */
export const DEFAULT_PER_PAGE = 100
export const MAX_PER_PAGE = 500

/** Guard against a runaway page walk on a very large dataset. */
export const MAX_PAGES = 200

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export function getToken(): string | null {
  return localStorage.getItem('token')
}

/**
 * Dispatched on `window` whenever a request comes back 401. AuthProvider
 * listens for it and drops its token, so the UI cannot stay "signed in" after
 * storage was cleared underneath it.
 */
export const UNAUTHORIZED_EVENT = 'kvuno:unauthorized'

/** `; Secure` on HTTPS so the token cookie is never sent over plain HTTP. */
function cookieFlags(): string {
  return `path=/; SameSite=Lax${window.location.protocol === 'https:' ? '; Secure' : ''}`
}

export function setToken(token: string): void {
  localStorage.setItem('token', token)
  // Mirrored as a cookie: require_auth() reads it when a browser is bounced to
  // the login page, and it keeps /ui/* endpoints usable during a hard reload.
  document.cookie = `token=${token}; max-age=${30 * 24 * 60 * 60}; ${cookieFlags()}`
}

export function clearToken(): void {
  localStorage.removeItem('token')
  document.cookie = `token=; max-age=0; ${cookieFlags()}`
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'DELETE'
  params?: URLSearchParams | Record<string, string | number | undefined>
  body?: unknown
  signal?: AbortSignal
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const url = new URL(path, window.location.origin)
  if (options.params) {
    const entries =
      options.params instanceof URLSearchParams
        ? options.params.entries()
        : Object.entries(options.params)
    for (const [key, value] of entries) {
      if (value !== undefined && value !== '') url.searchParams.set(key, String(value))
    }
  }

  const token = getToken()
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'

  const response = await fetch(url.pathname + url.search, {
    method: options.method ?? 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  })

  // 401 from an /ui/* endpoint means the session lapsed; the SPA sends a
  // Bearer token so it gets JSON rather than the redirect a browser would.
  // Only when a token was actually sent: a 401 from /login with bad
  // credentials is not an expired session and should show the server's message.
  if (response.status === 401 && token) {
    clearToken()
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT))
    throw new ApiError('Your session has expired. Please sign in again.', 401)
  }

  const text = await response.text()
  let parsed: unknown = null
  if (text) {
    try {
      parsed = JSON.parse(text)
    } catch {
      parsed = null
    }
  }

  if (!response.ok) {
    const body = parsed as { msg?: string; error?: string } | null
    throw new ApiError(
      body?.msg ?? body?.error ?? `Request failed (${response.status})`,
      response.status,
    )
  }
  return parsed as T
}

interface PageResult<T> {
  rows: T[]
  meta: PageMeta
}

/**
 * Fetch a single page of a collection endpoint.
 *
 * `key` is the response property holding the rows.
 */
export async function fetchPage<T>(
  path: string,
  key: string,
  page = 1,
  perPage = MAX_PER_PAGE,
  params?: URLSearchParams | Record<string, string | number | undefined>,
  signal?: AbortSignal,
): Promise<PageResult<T>> {
  const merged = new URLSearchParams(
    params instanceof URLSearchParams ? params.toString() : undefined,
  )
  for (const [k, v] of Object.entries(params ?? {})) {
    if (typeof v !== 'object' && v !== undefined && v !== '') merged.set(k, String(v))
  }
  merged.set('page', String(page))
  merged.set('per_page', String(perPage))

  const body = await request<Record<string, unknown> & Partial<PageMeta>>(path, {
    params: merged,
    signal,
  })
  return {
    rows: (body[key] as T[] | undefined) ?? [],
    meta: {
      total: body.total ?? 0,
      pages: body.pages ?? 1,
      current_page: body.current_page ?? page,
      per_page: body.per_page ?? perPage,
    },
  }
}

/** Rows accumulated across every page, plus whether the walk was cut short. */
export interface AllPages<T> extends PageMeta {
  rows: T[]
  truncated: boolean
}

export async function fetchAllPages<T>(
  path: string,
  key: string,
  params?: URLSearchParams | Record<string, string | number | undefined>,
  signal?: AbortSignal,
): Promise<AllPages<T>> {
  const all: T[] = []
  let page = 1
  let pages = 1
  let total = 0
  let current = 1
  let perPage = MAX_PER_PAGE

  do {
    const { rows, meta } = await fetchPage<T>(path, key, page, MAX_PER_PAGE, params, signal)
    all.push(...rows)
    total = meta.total
    pages = meta.pages
    current = meta.current_page
    perPage = meta.per_page
    page += 1
  } while (page <= pages && page <= MAX_PAGES)

  return { rows: all, total, pages, current_page: current, per_page: perPage, truncated: all.length < total }
}

/* ── Domain types ─────────────────────────────────────────────── */

export interface PlantingRecord {
  id: number
  country: string | null
  province: string | null
  lon: number | null
  lat: number | null
  variety: string | null
  season_type: string | null
  /** Day-month as in the source data ('30-Nov'); there is no year. */
  opt_date: string | null
  /** The same day as ISO 8601 month-day, '--11-30'. */
  opt_month_day: string | null
  planting_option: string | null
  check_sum: string | null
  /** GeoJSON Point (RFC 7946): coordinates are [lon, lat]. */
  coordinates: { type: 'Point'; coordinates: number[] } | null
  created_at: string | null
  updated_at: string | null
}

export interface LatLon {
  lat: number
  lon: number
}

export interface Cluster {
  lat: number
  lon: number
  count: number
}

export interface FilterColumns {
  country: string[]
  province: string[]
  variety: string[]
  season_type: string[]
}

/**
 * /filters pages each column independently — the response is a dict of four
 * unrelated lists, so there is no single row axis. `pages` is a per-column
 * map, which is why this deliberately does not extend PageMeta.
 */
interface FiltersResponse extends FilterColumns {
  totals: Record<string, number>
  pages: Record<string, number>
  current_page: number
  per_page: number
}

export interface Conflict {
  id: number
  country: string | null
  province: string | null
  variety: string | null
  season_type: string | null
  opt_date: string | null
  source: string | null
  created_at: string | null
}

export interface QualityStats {
  total_records: number
  total_conflicts: number
  total_files: number
  coverage_pct: number
  conflicts_by_source: { source: string; count: number }[]
  /** INGEST_CONFLICT_LOGGING: false means new duplicates are skipped without being recorded. */
  conflict_logging?: boolean
  /** INGEST_DUPLICATE_CHECK: false means already-imported files are imported again. */
  duplicate_check?: boolean
}

export interface Job {
  file: string
  original_name?: string | null
  status: string
  current?: number
  total?: number
  message?: string
  /** Failure detail, kept separate from `message` by the server. */
  error?: string
  /** Which ingestion run this file belongs to, if any. */
  run_id?: number | null
  attempts?: number
  worker?: string
  mtime?: number
  started_at?: number
}

export interface RunCounts {
  total: number
  processing: number
  completed: number
  error: number
  stale: number
}

/** One ingestion batch: everything submitted together. */
export interface Run {
  id: number
  label: string
  source: string
  /**
   * Derived server-side from the run's jobs, not stored. A run is finished
   * only once none of its jobs are still in flight, so it cannot be left
   * claiming to be running after the work completed.
   */
  status: 'pending' | 'running' | 'completed' | 'error' | 'partial' | 'stale'
  total_files: number
  initiated_by?: string
  created_at: number
  finished_at: number
  counts: RunCounts
  rows: { current: number; total: number }
}

export interface TokenInfo {
  id: number
  created_at: string
  expires_at: string | null
  last_used_at: string | null
}

/* ── Endpoints ────────────────────────────────────────────────── */

export function fetchRuns(limit = 20, signal?: AbortSignal): Promise<Run[]> {
  return request<{ runs: Run[] }>('/ui/jobs/runs', {
    params: { limit },
    signal,
  }).then((body) => body.runs ?? [])
}

export function fetchPlantingRecords(
  params: URLSearchParams,
  signal?: AbortSignal,
): Promise<{ data: PlantingRecord[] } & PageMeta> {
  const key = 'planting:' + params.toString()
  return cached(
    key,
    (shared) => request('/api/v1/planting-data', { params, signal: shared }),
    LIST_TTL_MS,
    signal,
  )
}

export function fetchFilters(signal?: AbortSignal): Promise<FilterColumns> {
  const acc: FilterColumns = { country: [], province: [], variety: [], season_type: [] }
  const keys = Object.keys(acc) as (keyof FilterColumns)[]
  let page = 1
  let pages = 1

  const walk = async (): Promise<void> => {
    do {
      const body = await request<FiltersResponse>('/api/v1/planting-data/filters', {
        params: { page, per_page: MAX_PER_PAGE },
        signal,
      })
      for (const key of keys) {
        for (const value of body[key] ?? []) acc[key].push(value)
      }
      const reported = Object.values(body.pages ?? {})
      pages = reported.length ? Math.max(...reported) : 1
      page += 1
    } while (page <= pages && page <= MAX_PAGES)
  }

  // A failure here must not take the explorer down: the table still works
  // with empty dropdowns.
  return walk().then(() => acc).catch(() => acc)
}

/**
 * Coordinates as typed arrays.
 *
 * A plain `{lat, lon}` object costs roughly an order of magnitude more in the
 * JS heap than the two numbers it holds — for a multi-million-row dataset that
 * is hundreds of MB of objects that exist only to be read once and drawn. Two
 * Float32Arrays hold the same values in 8 bytes per point, and `lats[i]`/`lons[i]`
 * is all the render loop needs. float32 is ample: ~7 significant digits covers
 * any coordinate, and sub-metre precision is irrelevant here.
 *
 * The `format=compact` response is two flat lists, so there is no per-point
 * object allocated at any point in the pipeline.
 */
export interface CoordinateBuffers {
  lats: Float32Array
  lons: Float32Array
  total: number
  /** True when MAX_PAGES stopped the walk before the set was exhausted. */
  truncated: boolean
}

/**
 * Every matching coordinate pair in ONE request, as typed arrays.
 *
 * This replaces the page-walk the map used to do. Paging /coordinates reaches
 * the same data at one request per 500 rows, so a 200k-point map is 400 requests
 * and a full 2.2M-row dataset is 4,566 — which exhausts the per-endpoint rate
 * limit after ~2.6% of the data and can never complete. The server caps this
 * at COORDINATES_BULK_MAX_ROWS (default 500k) so the response stays bounded.
 *
 * `truncated` means the cap was hit and the map is a sample; the UI says so
 * rather than implying the map is complete.
 */
export function fetchCoordinatesBulk(
  params: URLSearchParams,
  signal?: AbortSignal,
): Promise<CoordinateBuffers> {
  return cached(
    'coords-bulk:' + params.toString(),
    async (shared) => {
      const body = await request<{
        lats?: number[]
        lons?: number[]
        total?: number
        truncated?: boolean
      }>('/api/v1/planting-data/coordinates/all', { params, signal: shared })
      return {
        lats: Float32Array.from(body.lats ?? []),
        lons: Float32Array.from(body.lons ?? []),
        total: body.total ?? 0,
        truncated: Boolean(body.truncated),
      }
    },
    AGGREGATE_TTL_MS,
    signal,
  )
}

export const fetchClusters = (params: URLSearchParams, signal?: AbortSignal) =>
  cached(
    'clusters:' + params.toString(),
    (shared) => fetchAllPages<Cluster>('/api/v1/planting-data/clusters', 'clusters', params, shared),
    AGGREGATE_TTL_MS,
    signal,
  )

export const fetchConflicts = (
  params: URLSearchParams,
  page: number,
  signal?: AbortSignal,
) => fetchPage<Conflict>('/api/v1/quality/conflicts', 'data', page, DEFAULT_PER_PAGE, params, signal)

export const fetchStats = (signal?: AbortSignal) =>
  request<QualityStats>('/api/v1/quality/stats', { signal })

/** Conflicts grouped by country/province/variety/season/date, most repeated first. */
export interface DuplicateGroup {
  country: string | null
  province: string | null
  variety: string | null
  season_type: string | null
  opt_date: string | null
  count: number
  sources: Record<string, number>
}

/**
 * One page of duplicate groups. The server does the grouping; the tab used to
 * fetch every page of /conflicts and group them here, one request per page.
 */
export const fetchDuplicates = (params: URLSearchParams, page: number, signal?: AbortSignal) =>
  fetchPage<DuplicateGroup>('/api/v1/quality/duplicates', 'data', page, DEFAULT_PER_PAGE, params, signal)

export async function fetchTokens(signal?: AbortSignal): Promise<TokenInfo[]> {
  const out: TokenInfo[] = []
  let page = 1
  let pages = 1
  do {
    const { rows, meta } = await fetchPage<TokenInfo>('/api/v1/users/tokens', 'tokens', page, 100, undefined, signal)
    out.push(...rows)
    pages = meta.pages
    page += 1
  } while (page <= pages && page <= MAX_PAGES)
  return out
}

export async function login(username: string, password: string): Promise<string> {
  const data = await request<{ access_token: string }>('/api/v1/users/login', {
    method: 'POST',
    body: { username, password },
  })
  setToken(data.access_token)
  return data.access_token
}

/** 'pending' until an administrator approves the account; admins are 'approved' at once. */
export type AccountStatus = 'pending' | 'approved' | 'rejected' | 'disabled'

export async function register(
  username: string,
  email: string,
  password: string,
): Promise<{ msg: string; status: AccountStatus }> {
  return request('/api/v1/users/register', { method: 'POST', body: { username, email, password } })
}

/** The signed-in account, from GET /api/v1/users/me. */
export interface Me {
  id: number
  username: string
  email: string
  status: AccountStatus
  role: Role
  /** May use the Accounts screens: role 'admin', or an owner (ADMIN_EMAILS). */
  is_admin: boolean
}

export type Role = 'user' | 'admin'

export function fetchMe(signal?: AbortSignal): Promise<Me> {
  return request('/api/v1/users/me', { signal })
}

/* ── Account approval (administrators) ───────────────────────── */

export interface AdminUser {
  id: number
  username: string
  email: string
  status: AccountStatus
  role: Role
  is_admin: boolean
  /** Listed in ADMIN_EMAILS: always an admin; cannot be changed from the UI. */
  is_owner: boolean
  created_at: string | null
  reviewed_at: string | null
  reviewed_by: number | null
}

export interface AdminUserList extends PageMeta {
  users: AdminUser[]
  /** Accounts per status, for the tab badges. */
  counts: Record<AccountStatus, number>
}

export function fetchAdminUsers(
  params: { status?: AccountStatus; q?: string; page?: number },
  signal?: AbortSignal,
): Promise<AdminUserList> {
  return request('/api/v1/admin/users', {
    params: { status: params.status, q: params.q, page: params.page, per_page: 50 },
    signal,
  })
}

export type ReviewAction = 'approve' | 'reject' | 'disable'

export function reviewUser(
  id: number,
  action: ReviewAction,
  body: { notify?: boolean; reason?: string } = {},
): Promise<{ user: AdminUser; emailed: boolean }> {
  return request(`/api/v1/admin/users/${id}/${action}`, { method: 'POST', body })
}

export function setUserRole(id: number, role: Role): Promise<{ user: AdminUser }> {
  return request(`/api/v1/admin/users/${id}/role`, { method: 'POST', body: { role } })
}

export async function logout(): Promise<void> {
  try {
    await request('/api/v1/users/logout', { method: 'POST' })
  } finally {
    clearToken()
  }
}

export async function createToken(expiresInDays: number | null): Promise<string> {
  const data = await request<{ access_token: string }>('/api/v1/users/tokens', {
    method: 'POST',
    body: expiresInDays ? { expires_in_days: expiresInDays } : {},
  })
  return data.access_token
}

export function revokeToken(id: number): Promise<unknown> {
  return request(`/api/v1/users/tokens/${id}`, { method: 'DELETE' })
}

/* ── Upload + jobs endpoints still under /ui/* ────────────────── */

export interface DBColumn {
  value: string
  label: string
}

export async function fetchDBColumns(signal?: AbortSignal): Promise<{
  columns: string[]
  aliases: Record<string, string>
  /** The server's MAX_FILE_SIZE in bytes, so the client checks the real limit. */
  max_file_size: number
}> {
  return request('/ui/columns', { signal })
}

/**
 * One job's progress, in the same shape as an item of /ui/jobs/data. An
 * unknown file comes back as `{status: 'unknown', current: 0, total: 0,
 * message: ''}` (no `file`), so callers should fall back to what they have.
 */
export function fetchJobProgress(file: string, signal?: AbortSignal) {
  return request<Partial<Job> & { status: string }>(
    `/ui/progress/${encodeURIComponent(file)}`,
    { signal },
  )
}

/**
 * Administrators: delete a file with its upload and its job history. The rows
 * already imported from it are kept. 409 while it is being processed.
 */
export function deleteJobFile(file: string) {
  return request<{ file: string; file_deleted: boolean; upload_deleted: boolean; jobs_deleted: number }>(
    '/ui/jobs/' + encodeURIComponent(file),
    { method: 'DELETE' },
  )
}

/**
 * Re-queue a stored file (the Jobs page's Retry).
 *
 * Omit `columnMap` to re-use the mapping saved with the upload. Sending `{}`
 * here used to overwrite it with nothing.
 */
export function startProcessing(file: string, columnMap?: Record<string, string>) {
  return request<{ msg?: string; message?: string }>('/ui/process', {
    method: 'POST',
    body: columnMap ? { file, column_map: columnMap } : { file },
  })
}

/**
 * A previously-ingested file with identical content.
 *
 * 'processed' means the worker would skip it as a no-op; 'partial' means a
 * previous run was interrupted and re-running resumes from that row rather
 * than starting over.
 */
export interface DuplicateInfo {
  status: 'processed' | 'partial'
  original_filename: string
  processed_at: string | null
  offset: number | null
}

/** tus endpoint. Files are sent here by lib/tus.ts, not by `request()`. */
export const TUS_ENDPOINT = '/ui/uploads/tus'

/** receiving -> received -> inspecting -> ready | failed -> submitted -> processing -> completed */
export type UploadStatus =
  | 'receiving'
  | 'received'
  | 'inspecting'
  | 'ready'
  | 'failed'
  | 'submitted'
  | 'processing'
  | 'completed'

/** One upload as the server tracks it (app/services/uploads.py `to_dict`). */
export interface UploadRecord {
  id: string
  name: string
  size: number
  received: number
  status: UploadStatus
  /** Stored file name — what Jobs and Retry refer to. */
  file: string | null
  columns: string[]
  rows: Record<string, unknown>[]
  total_rows: number | null
  duplicate: DuplicateInfo | null
  /** Source column -> DB column; seeded with the server's guesses. */
  column_map: Record<string, string>
  error: string | null
  /**
   * While inspecting: where it has got to. reading/writing are the RDS ->
   * Parquet conversion (reading has no total; writing counts rows), then
   * preview and checksum. elapsed is seconds since the conversion started.
   */
  progress: {
    /** queued: waiting for the worker to pick the file up. */
    stage: 'queued' | 'reading' | 'writing' | 'preview' | 'checksum'
    done: number
    total: number
    elapsed: number
  } | null
  /** The file's own last-modified time: only a suggestion for data_date. */
  file_modified_at: string | null
  /** When the data was produced (YYYY-MM-DD), confirmed on the mapping form; rows get it as created_at. */
  data_date: string | null
  /** 'upload' from the Upload page; 'folder' a data-folder file held for mapping. */
  source: 'upload' | 'folder'
  created_at: string | null
  updated_at: string | null
}

/** Dispatched after an upload is submitted or discarded, so the sidebar count refreshes. */
export const UPLOADS_CHANGED = 'kvuno:uploads-changed'

/** Files inspected and waiting for a mapping — the Upload nav badge. */
export function fetchAwaitingCount(signal?: AbortSignal) {
  return request<{ count: number }>('/ui/uploads/awaiting', { signal })
}

export function fetchUploads(signal?: AbortSignal) {
  return request<{ uploads: UploadRecord[] }>('/ui/uploads', { signal })
}

export function fetchUpload(id: string, signal?: AbortSignal) {
  return request<UploadRecord>(`/ui/uploads/${id}`, { signal })
}

/** dataDate: YYYY-MM-DD, or '' to use the file's first import time. */
export function processUpload(id: string, columnMap: Record<string, string>, dataDate: string) {
  return request<{ id: string; file: string | null; message: string; queued: boolean }>(
    `/ui/uploads/${id}/process`,
    { method: 'POST', body: { column_map: columnMap, data_date: dataDate } },
  )
}

export function discardUpload(id: string) {
  return request<null>(`/ui/uploads/${id}`, { method: 'DELETE' })
}
