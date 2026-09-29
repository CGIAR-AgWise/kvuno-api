/** leaflet.heat ships no types; declares the factory the explorer uses. */
import 'leaflet'

declare module 'leaflet' {
  interface HeatMapOptions {
    minOpacity?: number
    maxZoom?: number
    max?: number
    radius?: number
    blur?: number
    gradient?: Record<number, string>
  }

  function heatLayer(
    latlngs: Array<[number, number] | [number, number, number]>,
    options?: HeatMapOptions,
  ): Layer & { setLatLngs(latlngs: Array<[number, number, number]>): void }

  export { heatLayer }
}
