import { useEffect, useMemo, useRef, useState } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import 'leaflet.markercluster'
import 'leaflet.markercluster/dist/MarkerCluster.css'
// The Default sheet is what draws the coloured cluster bubbles. Without it a
// cluster is a bare number floating on the map, which says how many points
// there are but not where they are.
import 'leaflet.markercluster/dist/MarkerCluster.Default.css'
import 'leaflet.heat'
import {
  fetchClusters,
  fetchCoordinatesBulk,
  fetchFilters,
  fetchPlantingRecords,
  MAX_PER_PAGE,
} from '../api'
import type { Cluster, FilterColumns, PlantingRecord } from '../api'
import Pagination from '../components/Pagination'
import { useToast } from '../context/ToastContext'
import { invalidateAll } from '../lib/cache'
import { createPointCloud } from '../lib/pointCloud'

type LayerMode = 'points' | 'heatmap' | 'clusters'

const EMPTY_FILTERS = { crop_type: '', country: '', province: '', variety: '', season_type: '' }

/**
 * Point fill for both point layers. Magenta with a white outline reads against
 * every OpenStreetMap background; the previous mid-blue matched water and
 * faded into the land colours, so the data was hard to find.
 */
const POINT_COLOR = '#d81b60'

/** Columns dropped below the md breakpoint so the table fits without scrolling. */
const NARROW_HIDDEN = new Set(['crop_type', 'season_type', 'planting_option'])

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
    crop_type: [],
  })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<LayerMode>('points')
  // Off by default: loading everything is ceil(total/500) sequential requests
  // and renders the whole result set in the DOM.
  const [loadAll, setLoadAll] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)

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
      .finally(() => {
        // An aborted fetch has been superseded; the newer one owns `loading`.
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [params, reloadKey])

  function updateFilter(key: keyof typeof EMPTY_FILTERS, value: string) {
    setFilters((f) => ({ ...f, [key]: value }))
    setPage(1)
  }

  function toggleLoadAll(next: boolean) {
    setLoadAll(next)
    if (next) {
      toast.push(
        'Map will plot a random sample of matching points instead of only ' +
          "the current page's rows.",
        'info',
      )
    }
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
    <div className="pb-3">
      <div className="kvuno-card p-3 mb-3">
        <div className="row g-2">
          {(
            [
              ['crop_type', 'Crop'],
              ['country', 'Country'],
              ['province', 'Province'],
              ['variety', 'Variety'],
              ['season_type', 'Season'],
            ] as const
          ).map(([key, label]) => (
            <div className="col-6 col-md-2" key={key}>
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
          <div className="col-6 col-md-2 d-flex align-items-end">
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

      {/* Table left, map right. The map is a reference for the rows, so keeping
          both on screen means scanning a page of rows no longer means scrolling
          down and back up. Below `lg` the columns stack, where two side-by-side
          panes would leave neither usable. */}
      <div className="row g-3">
        <div className="col-lg-7 order-2 order-lg-1">
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
            <button
              className="btn btn-outline-secondary btn-sm"
              onClick={() => {
                invalidateAll()
                // Bump a counter so the effect re-runs even when page is
                // unchanged — invalidating the cache alone changes no state.
                setReloadKey((n) => n + 1)
                toast.push('Cache cleared — refetching.', 'info')
              }}
              title="Clear cached responses and refetch"
            >
              <i className="bi bi-arrow-clockwise me-1" />
              Refresh
            </button>
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

        <div className="kvuno-table-fit kvuno-table-scroll">
          <table className="table table-hover align-middle mb-0 sticky-top">
            <thead className="table-light">
              <tr>
                {(
                  [
                    ['crop_type', 'Crop'],
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
                    className={'col-sort' + (NARROW_HIDDEN.has(key) ? ' d-none d-md-table-cell' : '')}
                    style={{ cursor: 'pointer' }}
                    tabIndex={0}
                    aria-sort={
                      sort?.col === key
                        ? sort.dir === 'asc'
                          ? 'ascending'
                          : 'descending'
                        : 'none'
                    }
                    onClick={() => toggleSort(key)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        toggleSort(key)
                      }
                    }}
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
                  <td colSpan={9} className="text-center text-muted small py-4">
                    No records match your filters.
                  </td>
                </tr>
              ) : (
                records.map((r) => (
                  <tr key={r.id}>
                    <td className="d-none d-md-table-cell">{r.crop_type}</td>
                    <td>{r.country}</td>
                    <td>{r.province}</td>
                    <td>{r.variety}</td>
                    <td className="kvuno-status">{r.lon}</td>
                    <td className="kvuno-status">{r.lat}</td>
                    <td className="d-none d-md-table-cell">{r.season_type}</td>
                    <td>{r.opt_date}</td>
                    <td className="d-none d-md-table-cell">{r.planting_option}</td>
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

        <div className="col-lg-5 order-1 order-lg-2">
          <div className="kvuno-card p-3">
            <div className="kvuno-map mb-2">
              <MapView mode={mode} records={records} filters={filters} loadAll={loadAll} />
            </div>
            <div className="d-flex align-items-center justify-content-between gap-2 mb-2">
              <span className="small text-muted">Map layer</span>
              <div className="form-check form-switch d-flex align-items-center gap-2 mb-0">
                <input
                  className="form-check-input mt-0"
                  type="checkbox"
                  role="switch"
                  id="load-all"
                  checked={loadAll}
                  onChange={(e) => toggleLoadAll(e.target.checked)}
                />
                <label className="form-check-label small text-nowrap" htmlFor="load-all">
                  Sample
                </label>
              </div>
            </div>
            <div className="btn-group btn-group-sm w-100" role="group" aria-label="Map layer">
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
  loadAll,
}: {
  mode: LayerMode
  records: PlantingRecord[]
  filters: typeof EMPTY_FILTERS
  /** Plot every matching point instead of only the table's current page. */
  loadAll: boolean
}) {
  const container = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<L.Map | null>(null)
  const markersRef = useRef<L.LayerGroup | null>(null)
  const clusterRef = useRef<L.LayerGroup | null>(null)
  const heatRef = useRef<L.Layer | null>(null)
  const cloudRef = useRef<L.Layer | null>(null)
  const toast = useToast()

  // Create once.
  useEffect(() => {
    if (!container.current || mapRef.current) return
    // preferCanvas is the enabling change for plotting many points. The default
    // renderer is SVG, which creates one DOM node per vector layer — roughly a
    // gigabyte of DOM for a multi-million-row dataset, which crashes the tab
    // regardless of how the data is stored. A canvas renderer draws all of them
    // into a single element. It does not reduce the number of Leaflet layer
    // objects, so the typed-array change below is what handles the heap.
    const map = L.map(container.current, { preferCanvas: true }).setView([-12, 28], 5)
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

  // Points. Off `loadAll` these come from the table's current page, which is
  // cheap but caps the map at one page. On, they come from the coordinates
  // endpoint, which the client walks page by page (and the cache serves
  // repeat visits), so the map shows the whole filtered set.
  useEffect(() => {
    const map = mapRef.current
    const markers = markersRef.current
    if (!map || !markers || mode !== 'points') return
    markers.clearLayers()
    const bounds: [number, number][] = []

    const addPoint = (lat: number, lon: number, label: string) => {
      if (Number.isNaN(lat) || Number.isNaN(lon)) return
      const marker = L.circleMarker([lat, lon], {
        radius: 6,
        fillColor: POINT_COLOR,
        color: '#fff',
        weight: 1.5,
        fillOpacity: 0.9,
      })
      if (label) marker.bindTooltip(label)
      markers.addLayer(marker)
      bounds.push([lat, lon])
    }

    const fit = () => {
      if (bounds.length) map.fitBounds(bounds, { padding: [20, 20], maxZoom: 12 })
    }

    if (!loadAll) {
      for (const r of records) {
        addPoint(
          Number.parseFloat(String(r.lat)),
          Number.parseFloat(String(r.lon)),
          (r.country || '') +
            ' - ' +
            (r.variety || '') +
            '<br/>Date: ' +
            (r.opt_date || '') +
            '<br/>Option: ' +
            (r.planting_option || ''),
        )
      }
      fit()
      return
    }

    const controller = new AbortController()
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(filters)) if (value) query.set(key, value)
    void fetchCoordinatesBulk(query, controller.signal)
      .then((res) => {
        markers.clearLayers()
        bounds.length = 0
        const { lats, lons } = res

        // One canvas layer for every point. Adding a circleMarker per point
        // would put a Leaflet layer object back on the heap for each row, which
        // is the thing typed arrays were meant to avoid.
        if (cloudRef.current) {
          map.removeLayer(cloudRef.current)
          cloudRef.current = null
        }
        cloudRef.current = createPointCloud(lats, lons, { radius: 3.5, color: POINT_COLOR })
        map.addLayer(cloudRef.current)

        // Fit to the returned extent, computed from the arrays rather than by
        // building a bounds list of Leaflet points.
        if (lats.length > 0) {
          let minLat = Infinity
          let maxLat = -Infinity
          let minLon = Infinity
          let maxLon = -Infinity
          for (let i = 0; i < lats.length; i += 1) {
            if (lats[i] < minLat) minLat = lats[i]
            if (lats[i] > maxLat) maxLat = lats[i]
            if (lons[i] < minLon) minLon = lons[i]
            if (lons[i] > maxLon) maxLon = lons[i]
          }
          map.fitBounds(
            L.latLngBounds(L.latLng(minLat, minLon), L.latLng(maxLat, maxLon)),
            { padding: [20, 20], maxZoom: 12 },
          )
        }
        if (res.truncated) {
          // Be explicit: this is a random sample, not a clipped view of the
          // first N rows. "Plotted 5,000 of 2,282,356" alone reads as a bug.
          toast.push(
            'Showing a random sample of ' +
              lats.length.toLocaleString() +
              ' points from ' +
              res.total.toLocaleString() +
              ' matches. Narrow the filters for full detail.',
            'info',
          )
        }
      })
      .catch(() => undefined)

    return () => controller.abort()
  }, [records, mode, loadAll, toast])

  // Heatmap and clusters are their own server-side aggregations.
  useEffect(() => {
    const map = mapRef.current
    if (!map) return

    if (heatRef.current) {
      map.removeLayer(heatRef.current)
      heatRef.current = null
    }
    if (cloudRef.current && mode !== 'points') {
      map.removeLayer(cloudRef.current)
      cloudRef.current = null
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
      // The bulk sample, NOT /coordinates paged at 500 rows per request. That
      // walk was still wired to the heatmap and cost 4,566 requests on this
      // dataset, so the layer exhausted the rate limit long before it rendered
      // (HTTP 429 on page 1 of thousands). A random sample of the filtered set
      // is the right input for a heatmap anyway — it encodes density, and every
      // point carries weight 1 regardless of how many rows matched.
      void fetchCoordinatesBulk(query, controller.signal)
        .then((res) => {
          if (!res.lats.length) {
            toast.push('No coordinate data to show.', 'warning')
            return
          }
          const points: [number, number, number][] = []
          for (let i = 0; i < res.lats.length; i += 1) {
            points.push([res.lats[i], res.lons[i], 1])
          }
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
              'Heatmap built from a random sample of ' +
                res.lats.length.toLocaleString() +
                ' points from ' +
                res.total.toLocaleString() +
                ' matches.',
              'info',
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
