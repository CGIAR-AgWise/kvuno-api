"""The default column map matches a data-folder file's columns whatever their case.

The AllCountries files name them 'Country'/'Province'; the built-in map says
'country'/'province', so scanned rows were stored with neither.
"""
import pyarrow as pa
import pyarrow.parquet as pq

from app.services import housekeeper as hk

ALL_COUNTRIES = ['Country', 'Province', 'lon', 'lat', 'Variety', 'Season_type', 'Opt_date', 'Planting_Option']


def test_capitalised_columns_are_read():
    mapping, columns = hk._select_columns(hk.load_column_map(), ALL_COUNTRIES, 'f.parquet', explicit=False)
    assert mapping['Country'] == 'country' and mapping['Province'] == 'province'
    assert set(columns) <= set(ALL_COUNTRIES), 'only names the file has are asked of pyarrow'


def test_an_exact_name_wins_over_a_case_match():
    mapping, missing = hk._match_case({'lat': 'lat', 'LAT': 'lon'}, ['lat', 'LAT'])
    assert mapping == {'lat': 'lat', 'LAT': 'lon'} and missing == []


def test_a_column_is_read_once_even_if_two_keys_match_it():
    mapping, _ = hk._match_case({'Crop': 'crop_type', 'crop': 'crop_type'}, ['CROP'])
    assert mapping == {'CROP': 'crop_type'}


def test_an_explicit_upload_map_is_left_alone():
    mapping, _ = hk._select_columns({'Country': 'country'}, ['Country'], 'f.parquet', explicit=True)
    assert mapping == {'Country': 'country'}


def test_upper_case_coordinates_are_not_held_for_mapping(tmp_path):
    path = tmp_path / 'upper.parquet'
    pq.write_table(pa.table({'LAT': [1.0], 'LON': [2.0]}), str(path))
    assert hk.needs_mapping(str(path)) is False
    pq.write_table(pa.table({'x': [1.0], 'y': [2.0]}), str(path))
    assert hk.needs_mapping(str(path)) is True
