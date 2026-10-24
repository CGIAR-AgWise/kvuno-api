"""`coordinates` in API responses is a GeoJSON Point, not the hex EWKB PostGIS stores."""
import struct

from app.utils.geometry import point_geojson

# Verbatim from a /api/v1/planting-data response before the fix.
FROM_PROD = '0101000020e61000006666666666063e403333333333f32cc0'
POINT = {'type': 'Point', 'coordinates': [30.025, -14.475]}


def test_ewkb_hex_with_srid_becomes_geojson_lon_first():
    assert point_geojson(FROM_PROD) == POINT


def test_geoalchemy_element_is_decoded():
    from geoalchemy2.elements import WKBElement

    element = WKBElement(bytes.fromhex(FROM_PROD), srid=4326, extended=True)
    assert point_geojson(element) == POINT


def test_plain_wkb_big_endian_without_srid():
    data = b'\x00' + struct.pack('>I', 1) + struct.pack('>dd', 36.82, -1.29)
    assert point_geojson(data) == {'type': 'Point', 'coordinates': [36.82, -1.29]}


def test_wkt_text_is_converted():
    # The SQLite schema stores coordinates as text.
    assert point_geojson('POINT(30.025 -14.475)') == POINT


def test_nothing_or_not_a_point_is_none():
    assert point_geojson(None) is None
    assert point_geojson('not hex') is None
    linestring = b'\x01' + struct.pack('<I', 2) + struct.pack('<I', 0)
    assert point_geojson(linestring) is None
