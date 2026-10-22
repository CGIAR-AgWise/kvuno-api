import L from 'leaflet'

/**
 * Draws a large point set in one pass, with no Leaflet layer per point.
 *
 * `L.circleMarker` allocates a layer object for every point. That is fine for a
 * page of rows and hopeless for millions: a layer costs orders of magnitude more
 * than the two numbers it holds, so the JS heap blows up even after the
 * coordinates have been moved into typed arrays and the renderer is canvas.
 *
 * This layer instead owns a single <canvas> in the overlay pane and paints
 * every point itself. Combined with `preferCanvas` on the map there is no DOM
 * node per point and no Leaflet object per point — the only per-point state is
 * the two Float32Arrays.
 *
 * Trade-off: no hit-testing, so points here have no tooltips or click
 * behaviour. The paged "points" layer still uses real circle markers, which do
 * have tooltips. This is the dense overview mode.
 *
 * Redraw happens on moveend/zoomend/resize rather than every animation frame,
 * so panning stays responsive at the cost of the cloud lagging slightly behind
 * during a drag.
 */
export interface PointCloudOptions {
  radius?: number
  color?: string
  opacity?: number
}

export function createPointCloud(
  lats: Float32Array,
  lons: Float32Array,
  options: PointCloudOptions = {},
): L.Layer {
  const radius = options.radius ?? 2
  const color = options.color ?? '#1976d2'
  const opacity = options.opacity ?? 0.75

  const Layer = L.Layer.extend({
    _canvas: null as HTMLCanvasElement | null,
    _ctx: null as CanvasRenderingContext2D | null,
    _onViewChange: null as (() => void) | null,

    onAdd(map: L.Map) {
      const canvas = L.DomUtil.create('canvas', 'leaflet-point-cloud') as HTMLCanvasElement
      // Purely decorative: keep it out of hit-testing and off the tab order.
      canvas.style.pointerEvents = 'none'
      canvas.style.position = 'absolute'
      canvas.style.top = '0'
      canvas.style.left = '0'
      map.getPanes().overlayPane.appendChild(canvas)
      this._canvas = canvas
      this._ctx = canvas.getContext('2d')

      this._onViewChange = () => this._reset()
      map.on('moveend zoomend resize', this._onViewChange)
      this._reset()
    },

    onRemove(map: L.Map) {
      if (this._onViewChange) map.off('moveend zoomend resize', this._onViewChange)
      this._canvas?.remove()
      this._canvas = null
      this._ctx = null
      this._onViewChange = null
    },

    _reset(): void {
      const canvas = this._canvas
      const ctx = this._ctx
      const map = this._map
      if (!canvas || !ctx || !map) return

      const size = map.getSize()
      canvas.width = size.x
      canvas.height = size.y
      L.DomUtil.setPosition(canvas, map.containerPointToLayerPoint([0, 0]))
      this._draw()
    },

    _draw(): void {
      const ctx = this._ctx
      const map = this._map
      if (!ctx || !map) return

      ctx.clearRect(0, 0, this._canvas!.width, this._canvas!.height)
      if (lats.length === 0) return

      ctx.fillStyle = color
      ctx.globalAlpha = opacity

      const d = radius * 2
      const project = map.latLngToContainerPoint.bind(map)
      let minX = Infinity
      let minY = Infinity
      let maxX = -Infinity
      let maxY = -Infinity

      for (let i = 0; i < lats.length; i += 1) {
        // toNonNull: L.latLng returns LatLng; project returns a Point.
        const p = project(L.latLng(lats[i], lons[i]))
        const x = p.x
        const y = p.y
        ctx.fillRect(x - radius, y - radius, d, d)
        if (x < minX) minX = x
        if (y < minY) minY = y
        if (x > maxX) maxX = x
        if (y > maxY) maxY = y
      }

      ctx.globalAlpha = 1
      this._bounds =
        minX === Infinity
          ? null
          : L.latLngBounds(map.containerPointToLatLng([minX, maxY]), map.containerPointToLatLng([maxX, minY]))
    },
  })

  return new Layer()
}
