import { useEffect, useMemo, useRef, useState } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import 'leaflet.markercluster'
import 'leaflet.markercluster/dist/MarkerCluster.css'
import 'leaflet.heat'
import {
  fetchClusters,
  fetchCoordinates,
  fetchFilters,
  fetchPlantingRecords,
  MAX_PER_PAGE,
} from '../api'
import type { Cluster, FilterColumns, LatLon, PlantingRecord } from '../api'
import Pagination from '../components/Pagination'
import { useToast } from '../context/ToastContext'

type LayerMode = 'points' | 'heatmap' | 'clusters'

const EMPTY_FILTERS = { country: '', province: '', variety: '', season_type: '' }

export default function Explore() {
  const toast = useToast()
  const [records, setRecords] = useState<PlantingRecord[]>([])
  const [meta, setMeta] = useState({ total: 0, pages: 1, current_page: 1, per_page: 200 })
  const [page, setPage] = useState(1)
  const [perPage, setPerPage] = useState(200)
  const [sort, setSort] = useState<{ col: string; dir: 'asc' | 'desc' } | null>(null)
  const [filters, setFilters] = useState(EMPTY_FILTERS)
  const [options, setOptions] = useState<FilterColumns>({
    country: [],
    province: [],
    variety: [],
    season_type: [],
  })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<LayerMode>('points')

  // Filter dropdowns page per column, so load them all up front.
  useEffect(() => {
    const controller = new AbortController()
    void fetchFilters(controller.signal)
      .then(setOptions)
      .catch(() => undefined)
    return () => controller.abort()
  }, [])

  const params = useMemo(() => {
    const p = new URLSearchParams()
    p.set('page', String(page))
    p.set('per_page', String(perPage))
    for (const [key, value] of Object.entries(filters)) if (value) p.set(key, value)
    if (sort) {
      p.set('sort_col', sort.col)
      p.set('sort_dir', sort.dir)
    }
    return p
  }, [page, perPage, filters, sort])

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError('')
    void fetchPlantingRecords(params, controller.signal)
      .then((data) => {
        setRecords(data.data ?? [])
        setMeta({
          total: data.total,
          pages: data.pages,
          current_page: data.current_page,
          per_page: data.per_page,
        })
      })
      .catch((err: Error) => {
        if (err.name !== 'AbortError') setError(err.message)
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [params])

  function updateFilter(key: keyof typeof EMPTY_FILTERS, value: string) {
    setFilters((f) => ({ ...f, [key]: value }))
    setPage(1)
  }

  function toggleSort(col: string) {
    setSort((current) =>
      current?.col === col
        ? { col, dir: current.dir === 'asc' ? 'desc' : 'asc' }
        : { col, dir: 'asc' },
    )
    setPage(1)
  }

  function exportAs(format: 'csv' | 'json') {
    // The export endpoint is paginated server-side like the rest of the API,
    // so request the ceiling explicitly — otherwise the download silently
    // contains only the first 100 rows.
    const p = new URLSearchParams(params)
    p.set('format', format)
    p.set('per_page', String(MAX_PER_PAGE))
    const a = document.createElement('a')
    a.href = '/api/v1/planting-data/export?' + p.toString()
    a.download = 'kvuno-export.' + format
    document.body.appendChild(a)
    a.click()
    a.remove()
    toast.push(
      'Export limited to ' + MAX_PER_PAGE + ' rows — narrow the filters to export a subset.',
      'warning',
    )
  }

  const sortIndicator = (col: string) => (sort?.col === col ? (sort.dir === 'asc' ? ' ▲' : ' ▼') : '')

  return (
    <div className="py-3">
      <div className="kvuno-card p-3 mb-3">
        <div className="row g-2">
          {(
            [
              ['country', 'Country'],
              ['province', 'Province'],
              ['variety', 'Variety'],
              ['season_type', 'Season'],
            ] as const
          ).map(([key, label]) => (
            <div className="col-6 col-md-3" key={key}>
              <label className="form-label small text-muted mb-1" htmlFor={'f-' + key}>
                {label}
              </label>
              <select
                id={'f-' + key}
                className="form-select form-select-sm"
                value={filters[key]}
                onChange={(e) => updateFilter(key, e.target.value)}
              >
                <option value="">All</option>
                {options[key]?.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </div>
          ))}
          <div className="col-6 col-md-3 d-flex align-items-end">
            <button
              className="btn btn-outline-secondary btn-sm w-100"
              onClick={() => {
                setFilters(EMPTY_FILTERS)
                setPage(1)
              }}
            >
              Clear filters
            </button>
          </div>
        </div>
      </div>

      <div className="kvuno-card p-3 mb-3">
        <MapView mode={mode} records={records} filters={filters} />
        <div className="btn-group btn-group-sm mt-2" role="group" aria-label="Map layer">
          {(
            [
              ['points', 'bi-geo-alt', 'Points'],
              ['heatmap', 'bi-fire', 'Heatmap'],
              ['clusters', 'bi-diagram-3', 'Clusters'],
            ] as const
          ).map(([value, icon, label]) => (
            <button
              key={value}
              className={'btn ' + (mode === value ? 'btn-primary' : 'btn-outline-secondary')}
              onClick={() => setMode(value)}
            >
              <i className={'bi ' + icon + ' me-1'} />
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="kvuno-card p-3">
        <div className="d-flex flex-wrap gap-2 align-items-center mb-3">
          <span className="text-muted small">
            {meta.total.toLocaleString()} record{meta.total !== 1 ? 's' : ''}
            {loading && ' · loading…'}
          </span>
          <div className="ms-auto d-flex gap-2">
            <select
              className="form-select form-select-sm"
              style={{ width: 'auto' }}
              value={String(perPage)}
              onChange={(e) => {
                setPerPage(Number(e.target.value))
                setPage(1)
              }}
              aria-label="Rows per page"
            >
              {[50, 100, 200, 500].map((n) => (
                <option key={n} value={n}>
                  {n} / page
                </option>
              ))}
            </select>
            <button className="btn btn-outline-secondary btn-sm" onClick={() => exportAs('csv')}>
              <i className="bi bi-filetype-csv me-1" />
              CSV
            </button>
            <button className="btn btn-outline-secondary btn-sm" onClick={() => exportAs('json')}>
              <i className="bi bi-braces me-1" />
              JSON
            </button>
          </div>
        </div>

        {error && <div className="alert alert-danger">{error}</div>}

        <div className="table-responsive">
          <table className="table table-hover align-middle mb-0">
            <thead className="table-light">
              <tr>
                {(
                  [
                    ['country', 'Country'],
                    ['province', 'Province'],
                    ['variety', 'Variety'],
                    ['lon', 'Lon'],
                    ['lat', 'Lat'],
                    ['season_type', 'Season'],
                    ['opt_date', 'Date'],
                    ['planting_option', 'Option'],
                  ] as const
                ).map(([key, label]) => (
                  <th
                    key={key}
                    className="col-sort"
                    style={{ cursor: 'pointer', whiteSpace: 'nowrap' }}
                    onClick={() => toggleSort(key)}
                  >
                    {label}
                    {sortIndicator(key)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {records.length === 0 && !loading ? (
                <tr>
                  <td colSpan={8} className="text-center text-muted small py-4">
                    No records match your filters.
                  </td>
                </tr>
              ) : (
                records.map((r) => (
                  <tr key={r.id}>
                    <td>{r.country}</td>
                    <td>{r.province}</td>
                    <td>{r.variety}</td>
                    <td className="kvuno-status">{r.lon}</td>
                    <td className="kvuno-status">{r.lat}</td>
                    <td>{r.season_type}</td>
                    <td>{r.opt_date}</td>
                    <td>{r.planting_option}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        <div className="d-flex align-items-center mt-3">
          <Pagination
            page={meta.current_page}
            pages={meta.pages}
            onChange={(next) => {
              setPage(next)
              window.scrollTo({ top: 0, behavior: 'smooth' })
            }}
          />
          <span className="text-muted small">
            Page {meta.current_page} of {meta.pages}
          </span>
        </div>
      </div>
    </div>
  )
}

/**
 * Leaflet lives outside React's render cycle: it owns its own DOM inside the
 * container and its own event listeners. The effect owns the instance and
 * tears it down on unmount, which React would otherwise leak.
 */
function MapView({
  mode,
  records,
  filters,
}: {
  mode: LayerMode
  records: PlantingRecord[]
  filters: typeof EMPTY_FILTERS
}) {
  const container = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<L.Map | null>(null)
  const markersRef = useRef<L.LayerGroup | null>(null)
  const clusterRef = useRef<L.LayerGroup | null>(null)
  const heatRef = useRef<L.Layer | null>(null)
  const toast = useToast()

  // Create once.
  useEffect(() => {
    if (!container.current || mapRef.current) return
    const map = L.map(container.current).setView([-12, 28], 5)
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap',
      maxZoom: 18,
    }).addTo(map)
    mapRef.current = map
    markersRef.current = L.markerClusterGroup({ chunkedLoading: true })
    map.addLayer(markersRef.current)

    return () => {
      map.remove()
      mapRef.current = null
    }
  }, [])

  // Points follow the table's current page.
  useEffect(() => {
    const map = mapRef.current
    const markers = markersRef.current
    if (!map || !markers || mode !== 'points') return
    markers.clearLayers()
    const bounds: [number, number][] = []
    for (const r of records) {
      const lat = Number.parseFloat(String(r.lat))
      const lon = Number.parseFloat(String(r.lon))
      if (Number.isNaN(lat) || Number.isNaN(lon)) continue
      const marker = L.circleMarker([lat, lon], {
        radius: 5,
        fillColor: '#1976d2',
        color: '#fff',
        weight: 1,
        fillOpacity: 0.8,
      })
      marker.bindTooltip(
        (r.country || '') +
          ' - ' +
          (r.variety || '') +
          '<br/>Date: ' +
          (r.opt_date || '') +
          '<br/>Option: ' +
          (r.planting_option || ''),
      )
      markers.addLayer(marker)
      bounds.push([lat, lon])
    }
    if (bounds.length) map.fitBounds(bounds, { padding: [20, 20], maxZoom: 12 })
  }, [records, mode])

  // Heatmap and clusters are their own server-side aggregations.
  useEffect(() => {
    const map = mapRef.current
    if (!map) return

    if (heatRef.current) {
      map.removeLayer(heatRef.current)
      heatRef.current = null
    }
    if (clusterRef.current) {
      map.removeLayer(clusterRef.current)
      clusterRef.current = null
    }
    if (mode === 'points') {
      markersRef.current?.addTo(map)
      return
    }
    markersRef.current?.remove()

    const controller = new AbortController()
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(filters)) if (value) query.set(key, value)

    if (mode === 'heatmap') {
      void fetchCoordinates(query, controller.signal)
        .then((res) => {
          if (!res.rows.length) {
            toast.push('No coordinate data to show.', 'warning')
            return
          }
          const points: [number, number, number][] = (res.rows as LatLon[]).map((c) => [
            c.lat,
            c.lon,
            1,
          ])
          const layer = L.heatLayer(points, {
            radius: 20,
            blur: 15,
            maxZoom: 10,
            gradient: { 0.4: '#1976d2', 0.6: '#ff9800', 0.8: '#f44336' },
          }).addTo(map)
          heatRef.current = layer
          map.fitBounds(
            points.map((p) => [p[0], p[1]] as [number, number]),
            { padding: [20, 20], maxZoom: 10 },
          )
          if (res.truncated) {
            toast.push(
              'Showing ' +
                res.rows.length.toLocaleString() +
                ' of ' +
                res.total.toLocaleString() +
                ' points.',
              'warning',
            )
          }
        })
        .catch(() => undefined)
    } else {
      const bounds = map.getBounds()
      const zoom = map.getZoom()
      query.set('zoom', String(zoom))
      query.set('ne_lat', String(bounds.getNorth()))
      query.set('ne_lng', String(bounds.getEast()))
      query.set('sw_lat', String(bounds.getSouth()))
      query.set('sw_lng', String(bounds.getWest()))

      void fetchClusters(query, controller.signal)
        .then((res) => {
          if (!res.rows.length) {
            toast.push('No cluster data.', 'warning')
            return
          }
          const items = res.rows as Cluster[]
          const layer = L.layerGroup()
          const maxCount = items.reduce((max, c) => Math.max(max, c.count), 1)
          const points: [number, number][] = []
          for (const c of items) {
            const lat = Number.parseFloat(String(c.lat))
            const lon = Number.parseFloat(String(c.lon))
            if (Number.isNaN(lat) || Number.isNaN(lon)) continue
            const radius = Math.max(4, Math.min(20, 4 + (c.count / maxCount) * 16))
            const fill =
              c.count > maxCount * 0.5
                ? '#e53935'
                : c.count > maxCount * 0.2
                  ? '#ff9800'
                  : '#1976d2'
            const marker = L.circleMarker([lat, lon], {
              radius,
              fillColor: fill,
              color: '#fff',
              weight: 1.5,
              fillOpacity: 0.75,
            })
            marker.bindTooltip(c.count + ' record' + (c.count !== 1 ? 's' : ''))
            layer.addLayer(marker)
            points.push([lat, lon])
          }
          clusterRef.current = layer
          map.addLayer(layer)
          if (points.length) map.fitBounds(points, { padding: [20, 20], maxZoom: zoom + 1 })
        })
        .catch(() => undefined)
    }

    return () => controller.abort()
  }, [mode, filters, toast])

  return <div ref={container} className="kvuno-map" role="application" aria-label="Data map" />
}
