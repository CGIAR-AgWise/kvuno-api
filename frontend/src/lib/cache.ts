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

/**
 * A shared request plus how many callers are still waiting on it. The request
 * has its own AbortController: it is only aborted once *every* subscriber has
 * gone away, never because one of them did.
 */
interface InFlight<T> {
  promise: Promise<T>
  controller: AbortController
  subscribers: number
}

const cache = new Map<string, Entry<unknown>>()
const inflight = new Map<string, InFlight<unknown>>()

function abortError(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException('The operation was aborted.', 'AbortError')
}

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
 * `loader` is only called on a genuine miss, and receives the shared request's
 * OWN AbortSignal — never a caller's. The caller's `signal` only stops that
 * caller waiting: it rejects with an AbortError while the shared request keeps
 * running for anyone else subscribed. The shared request is aborted only when
 * no subscriber is left, and that check is deferred a macrotask so React
 * StrictMode's unmount/remount (which aborts and immediately re-subscribes)
 * joins the same request instead of killing it. An aborted or failed request
 * is evicted and never cached.
 */
export function cached<T>(
  key: string,
  loader: (signal: AbortSignal) => Promise<T>,
  ttlMs: number = DEFAULT_TTL_MS,
  signal?: AbortSignal,
): Promise<T> {
  if (signal?.aborted) return Promise.reject(abortError(signal))

  const now = Date.now()
  const hit = cache.get(key)
  if (hit && hit.expires > now) {
    // Refresh recency for the LRU eviction order.
    cache.delete(key)
    cache.set(key, hit)
    return Promise.resolve(hit.value as T)
  }
  if (hit) cache.delete(key)

  let entry = inflight.get(key) as InFlight<T> | undefined
  if (!entry) {
    const controller = new AbortController()
    const created: InFlight<T> = {
      controller,
      subscribers: 0,
      promise: loader(controller.signal).then(
        (value) => {
          if (inflight.get(key) === created) inflight.delete(key)
          if (!controller.signal.aborted) {
            cache.set(key, { value, expires: Date.now() + ttlMs })
            evictIfNeeded()
          }
          return value
        },
        (error: unknown) => {
          if (inflight.get(key) === created) inflight.delete(key)
          throw error
        },
      ),
    }
    // Callers that all abort leave the rejection unobserved otherwise.
    created.promise.catch(() => undefined)
    inflight.set(key, created)
    entry = created
  }

  const shared = entry
  shared.subscribers += 1
  if (!signal) return shared.promise

  return new Promise<T>((resolve, reject) => {
    let settled = false
    const release = () => {
      shared.subscribers -= 1
      if (shared.subscribers > 0) return
      setTimeout(() => {
        if (shared.subscribers > 0 || shared.controller.signal.aborted) return
        if (inflight.get(key) === shared) inflight.delete(key)
        shared.controller.abort()
      }, 0)
    }
    const onAbort = () => {
      if (settled) return
      settled = true
      release()
      reject(abortError(signal))
    }
    signal.addEventListener('abort', onAbort, { once: true })
    shared.promise.then(
      (value) => {
        if (settled) return
        settled = true
        signal.removeEventListener('abort', onAbort)
        shared.subscribers -= 1
        resolve(value)
      },
      (error: unknown) => {
        if (settled) return
        settled = true
        signal.removeEventListener('abort', onAbort)
        shared.subscribers -= 1
        reject(error)
      },
    )
  })
}
