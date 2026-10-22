import { useEffect, useState } from 'react'
import type { Job } from '../api'

interface StreamPayload {
  job?: Job
  jobs?: Job[]
}

const STATUSES = ['all', 'processing', 'completed', 'error', 'stale', 'unknown'] as const
export type StatusFilter = (typeof STATUSES)[number]

/**
 * Live job list fed by the SSE stream at /ui/jobs/events.
 *
 * The server sends a full snapshot on connect and one message per changed job
 * after that, so a delta patches a single entry instead of re-rendering the
 * whole list. `connected` is tracked because a silently dead stream looks
 * identical to "nothing is running" otherwise.
 */
export function useJobStream() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    const source = new EventSource('/ui/jobs/events')

    source.onopen = () => setConnected(true)
    source.onerror = () => setConnected(false) // EventSource reconnects itself

    source.addEventListener('message', (event: MessageEvent<string>) => {
      let data: StreamPayload
      try {
        data = JSON.parse(event.data) as StreamPayload
      } catch {
        return
      }
      setJobs((current) => {
        if (data.job) {
          const job = data.job
          const index = current.findIndex((j) => j.file === job.file)
          const next = index === -1 ? [job, ...current] : current.map((j, i) => (i === index ? job : j))
          return next.sort((a, b) => (b.mtime ?? 0) - (a.mtime ?? 0))
        }
        return data.jobs ?? []
      })
    })

    return () => source.close()
  }, [])

  return { jobs, connected }
}

export { STATUSES }
export type { Job }
