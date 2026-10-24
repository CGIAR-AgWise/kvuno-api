"""
Turn a stored point into GeoJSON for API responses.

GeoAlchemy2 hands a PostGIS geometry back as a WKBElement, and `str()` of
that is the hex EWKB ("0101000020e6100000..."), which is what the API used to
return. Responses now carry an RFC 7946 GeoJSON Point —
{"type": "Point", "coordinates": [lon, lat]} — which every GIS library,
Leaflet, and `json.loads` understand. The `coordinates` column only ever holds
points, so this decodes the EWKB point directly rather than adding shapely.
"""
from __future__ import annotations

import re
import struct
from typing import Any

_WKB_POINT = 1
_SRID_FLAG = 0x20000000
_Z_FLAG = 0x80000000
_M_FLAG = 0x40000000
_WKT_POINT = re.compile(r'^\s*POINT\s*Z?\s*\(\s*([-+0-9.eE]+)\s+([-+0-9.eE]+)(?:\s+([-+0-9.eE]+))?\s*\)\s*$', re.I)


def _wkb_bytes(value: Any) -> bytes | None:
    data = getattr(value, 'data', value)  # WKBElement keeps the raw value in .data
    if isinstance(data, memoryview):
        data = data.tobytes()
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        try:
            return bytes.fromhex(data)
        except ValueError:
            return None
    return None


def point_coords(value: Any) -> tuple[float, ...] | None:
    """(lon, lat[, z]) of a stored point; None if absent or not a point.

    Accepts a GeoAlchemy2 element, raw (E)WKB bytes, a hex string, or WKT text
    (the SQLite schema stores `coordinates` as text).
    """
    if value is None:
        return None
    if isinstance(value, str):
        m = _WKT_POINT.match(value)
        if m:
            return tuple(float(g) for g in m.groups() if g is not None)

    data = _wkb_bytes(value)
    if not data or len(data) < 5:
        return None
    order = '<' if data[0] == 1 else '>'
    (geom_type,) = struct.unpack_from(order + 'I', data, 1)
    offset = 5
    if geom_type & _SRID_FLAG:
        offset += 4
    has_z = bool(geom_type & _Z_FLAG) or 1000 <= (geom_type & 0xFFFF) < 2000
    if (geom_type & 0xFFFF) % 1000 != _WKB_POINT or geom_type & _M_FLAG:
        return None
    count = 3 if has_z else 2
    if len(data) < offset + 8 * count:
        return None
    return struct.unpack_from(order + 'd' * count, data, offset)


def point_geojson(value: Any) -> dict | None:
    """{"type": "Point", "coordinates": [lon, lat]} (RFC 7946), or None."""
    coords = point_coords(value)
    if coords is None:
        return None
    return {'type': 'Point', 'coordinates': list(coords)}
