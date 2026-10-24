"""`coordinates` in API responses is WKT, not the hex EWKB PostGIS stores."""
import struct

from app.utils.geometry import point_wkt

# Verbatim from a /api/v1/planting-data response before the fix.
FROM_PROD = '0101000020e61000006666666666063e403333333333f32cc0'


def test_ewkb_hex_with_srid_becomes_wkt():
    assert point_wkt(FROM_PROD) == 'POINT(30.025 -14.475)'


def test_geoalchemy_element_is_decoded():
    from geoalchemy2.elements import WKBElement

    element = WKBElement(bytes.fromhex(FROM_PROD), srid=4326, extended=True)
    assert point_wkt(element) == 'POINT(30.025 -14.475)'


def test_plain_wkb_big_endian_without_srid():
    data = b'\x00' + struct.pack('>I', 1) + struct.pack('>dd', 36.82, -1.29)
    assert point_wkt(data) == 'POINT(36.82 -1.29)'


def test_wkt_text_passes_through():
    # The SQLite schema stores coordinates as text.
    assert point_wkt('POINT(36.82 -1.29)') == 'POINT(36.82 -1.29)'


def test_nothing_or_not_a_point_is_none():
    assert point_wkt(None) is None
    assert point_wkt('not hex') is None
    linestring = b'\x01' + struct.pack('<I', 2) + struct.pack('<I', 0)
    assert point_wkt(linestring) is None
