"""Telling inserted rows from skipped ones, which made ingests slow and wrong.

`result.rowcount` is -1 for the ingest INSERT under psycopg 3, so the old
`rowcount < len(rows)` check sent every batch through the conflict lookup:
a 2,000-way OR of IS NOT DISTINCT FROM (a full table scan, slower as the table
grew; 2M rows took ~45 min) that ran after the insert and so recorded every
row just inserted as a conflict. The batch now uses RETURNING.
"""
import numpy as np
from sqlalchemy.dialects import postgresql

from app.services import housekeeper as hk


def _row(lon, lat, variety='v'):
    return {'country': 'Nigeria', 'province': 'Oyo', 'lon': lon, 'lat': lat,
            'variety': variety, 'season_type': 'long', 'opt_date': '30-Nov', 'check_sum': 'new'}


def _returned(row):
    """A key as RETURNING gives it back: lon/lat stored as float4 (REAL)."""
    return tuple(float(np.float32(row[c])) if c in ('lon', 'lat') else row[c] for c in hk.KEY_COLUMNS)


def test_rows_returned_with_float4_coordinates_count_as_inserted():
    rows = [_row(36.817223, -1.286389), _row(3.379206, 6.524379)]
    assert rows[0]['lon'] != _returned(rows[0])[2], 'the stored value really differs'
    assert hk._not_inserted(rows, [_returned(r) for r in rows]) == []


def test_only_rows_not_returned_are_conflicts():
    rows = [_row(1.1, 2.2), _row(3.3, 4.4), _row(5.5, 6.6)]
    assert hk._not_inserted(rows, [_returned(rows[0]), _returned(rows[2])]) == [rows[1]]


def test_a_key_twice_in_one_batch_inserts_once():
    rows = [_row(1.1, 2.2), _row(1.1, 2.2)]
    assert hk._not_inserted(rows, [_returned(rows[0])]) == [rows[1]]


def test_the_insert_returns_keys_and_compiles_once():
    sql = str(hk._insert_statement().compile(dialect=postgresql.psycopg.dialect()))
    assert 'ON CONFLICT DO NOTHING' in sql
    assert 'RETURNING planting_recommendations.country' in sql
    # executemany form: no per-batch VALUES list baked into the statement
    assert sql.count('VALUES') == 1 and '%(country_m1)s' not in sql


def test_lookup_compares_coordinates_as_real_and_nulls_as_not_distinct():
    keys = {hk._key(_row(1.1, 2.2)), ('Nigeria', None, 1.0, 2.0, 'v', 'long', '30-Nov')}
    sql = str(hk._existing_rows_query(keys).compile(dialect=postgresql.psycopg.dialect()))
    # A REAL column compared with float8 cannot use the index (and never matches).
    assert 'CAST(%(param_3)s AS REAL)' in sql
    assert ') IN (' in sql  # the NULL-free key goes through the indexed tuple IN
    assert 'IS NOT DISTINCT FROM' in sql  # only the key with a NULL needs this


# ── Timestamps ───────────────────────────────────────────────────────────

def test_rows_always_get_both_timestamps():
    """model_dump() carries created_at/updated_at as None, and an explicit NULL beat the now() default."""
    import datetime
    from app.dto.planting_recommendation import PlantingRecommendationCreate

    made = datetime.datetime(2023, 11, 14, 22, 13, 20)
    imported = datetime.datetime(2026, 10, 25, 12, 0, 0)
    row = hk._row_mapping(PlantingRecommendationCreate(lon=0.0, lat=0.0, check_sum='c'), made, imported)
    assert (row['created_at'], row['updated_at']) == (made, imported)
    assert row['coordinates'] is not None, '0,0 is a real place'
