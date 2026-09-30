/**
 * Tiny request cache for GET responses.
 *
 * Two behaviours, both aimed at cutting round trips rather than just latency:
 *
 * 1. **In-flight de-duplication.** If the same key is requested while an
 *    identical request is already running, the second caller awaits the first
 *    instead of firing a duplicate. On the explorer this is the difference
 *    between one request and two whenever a filter, sort, or map layer
 *    changes while a fetch is still open.
 * 2. **TTL**, so revisiting a filter set (going back a page, toggling
 *    points→heatmap→points) does not refetch. Short by default: this is a
 *    latency and load optimisation, not a source of truth, and ingestion can
 *    change the row counts underneath us.
 *
 * Deliberately not react-query. The explorer is the only consumer, needs two
 * behaviours rather than a full query cache, and the alternative adds ~13 KB
 * gzip for cache-invalidation semantics this page does not use.
 *
 * Bounded by entry count with least-recently-used eviction, so a user paging
 * through a large result set cannot grow the cache without limit.
 */

interface Entry<T> {
  value: T
  expires: number
}

const DEFAULT_TTL_MS = 60_000
const MAX_ENTRIES = 60

const cache = new Map<string, Entry<unknown>>()
const inflight = new Map<string, Promise<unknown>>()

/** Exposed for tests and for an explicit refresh button. */
export function cacheSize(): number {
  return cache.size
}

export function invalidateAll(): void {
  cache.clear()
}

function evictIfNeeded(): void {
  while (cache.size > MAX_ENTRIES) {
    // Map preserves insertion order, so the first key is the oldest.
    const oldest = cache.keys().next()
    if (oldest.done) break
    cache.delete(oldest.value)
  }
}

/**
 * Fetch `key`, reusing a cached value or an in-flight request when possible.
 *
 * `loader` is only called on a genuine miss. `signal` is intentionally not
 * forwarded to the shared request: if the caller that started it goes away, the
 * others waiting on the same promise should still get their result.
 */
export async function cached<T>(
  key: string,
  loader: () => Promise<T>,
  ttlMs: number = DEFAULT_TTL_MS,
): Promise<T> {
  const now = Date.now()
  const hit = cache.get(key)
  if (hit && hit.expires > now) {
    // Refresh recency for the LRU eviction order.
    cache.delete(key)
    cache.set(key, hit)
    return hit.value as T
  }
  if (hit) cache.delete(key)

  const existing = inflight.get(key)
  if (existing) return existing as Promise<T>

  const promise = loader()
    .then((value) => {
      cache.set(key, { value, expires: Date.now() + ttlMs })
      evictIfNeeded()
      return value
    })
    .finally(() => {
      inflight.delete(key)
    })

  inflight.set(key, promise)
  return promise
}
