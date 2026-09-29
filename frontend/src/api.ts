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

export function setToken(token: string): void {
  localStorage.setItem('token', token)
  // Mirrored as a cookie: require_auth() reads it when a browser is bounced to
  // the login page, and it keeps /ui/* endpoints usable during a hard reload.
  document.cookie = `token=${token}; path=/; max-age=${30 * 24 * 60 * 60}; SameSite=Lax`
}

export function clearToken(): void {
  localStorage.removeItem('token')
  document.cookie = 'token=; path=/; max-age=0; SameSite=Lax'
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
  if (response.status === 401) {
    clearToken()
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
  opt_date: string | null
  planting_option: string | null
  check_sum: string | null
  coordinates: string | null
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
}

export interface Job {
  file: string
  original_name?: string | null
  status: string
  current?: number
  total?: number
  message?: string
  mtime?: number
  started_at?: number
}

export interface TokenInfo {
  id: number
  created_at: string
  expires_at: string | null
  last_used_at: string | null
}

/* ── Endpoints ────────────────────────────────────────────────── */

export function fetchPlantingRecords(
  params: URLSearchParams,
  signal?: AbortSignal,
): Promise<{ data: PlantingRecord[] } & PageMeta> {
  return request('/api/v1/planting-data', { params, signal })
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

export const fetchCoordinates = (params: URLSearchParams, signal?: AbortSignal) =>
  fetchAllPages<LatLon>('/api/v1/planting-data/coordinates', 'coordinates', params, signal)

export const fetchClusters = (params: URLSearchParams, signal?: AbortSignal) =>
  fetchAllPages<Cluster>('/api/v1/planting-data/clusters', 'clusters', params, signal)

export const fetchConflicts = (
  params: URLSearchParams,
  page: number,
  signal?: AbortSignal,
) => fetchPage<Conflict>('/api/v1/quality/conflicts', 'data', page, DEFAULT_PER_PAGE, params, signal)

export const fetchStats = (signal?: AbortSignal) =>
  request<QualityStats>('/api/v1/quality/stats', { signal })

export async function fetchAllConflicts(signal?: AbortSignal): Promise<AllPages<Conflict>> {
  return fetchAllPages<Conflict>('/api/v1/quality/conflicts', 'data', undefined, signal)
}

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

export async function register(username: string, email: string, password: string): Promise<void> {
  await request('/api/v1/users/register', { method: 'POST', body: { username, email, password } })
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
}> {
  return request('/ui/columns', { signal })
}

export function startProcessing(file: string, columnMap: Record<string, string>) {
  return request<{ msg?: string }>('/ui/process', { method: 'POST', body: { file, column_map: columnMap } })
}

export function completeUpload(identifier: string, totalChunks: number, filename: string) {
  return request<{ file: string; columns: string[]; rows: Record<string, unknown>[]; error?: string }>(
    '/ui/upload/complete',
    { method: 'POST', body: { identifier, totalChunks, filename } },
  )
}
