"""Paging planting data: the page's ids first, then the rows; the count apart.

Measured on 5.5 M rows, a plain `WHERE … ORDER BY id LIMIT n` walked the
primary key past millions of rows (a file's rows are stored together), so the
repo now selects the page's ids from an index and then loads those rows. These
run the real queries on SQLite.
"""
import types

import pytest
from flask import Flask
from sqlalchemy import text

from app.dto.data_filters import PlantingDataFilter
from app.repo.planting_recommendation import PlantingRecommendationRepo

# id, crop_type, variety, country, lat
ROWS = [(i, 'maize' if i <= 6 else 'cotton', 'medium' if i % 2 else 'short', 'Zambia' if i <= 3 else 'Kenya',
         float(10 - i)) for i in range(1, 10)]


@pytest.fixture
def repo():
    from app.models.database_conn import MyDb

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    MyDb.init_app(app)
    with app.app_context():
        from sqlalchemy import event
        # GeoAlchemy reads the geometry column through PostGIS's AsEWKB().
        event.listen(MyDb.get_db().engine, 'connect',
                     lambda conn, _: conn.create_function('AsEWKB', 1, lambda value: value))
        session = MyDb.get_db().session
        # Plain DDL: the model's coordinates column is PostGIS geometry.
        session.execute(text(
            'CREATE TABLE planting_recommendations (id INTEGER PRIMARY KEY, check_sum TEXT, coordinates TEXT, '
            'country TEXT, province TEXT, lon REAL, lat REAL, variety TEXT, season_type TEXT, opt_date TEXT, '
            'planting_option INTEGER, crop_type TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)'))
        for row_id, crop, variety, country, lat in ROWS:
            session.execute(text(
                "INSERT INTO planting_recommendations (id, check_sum, coordinates, crop_type, variety, country, "
                "lat, lon) VALUES (:id, 'c', NULL, :crop, :variety, :country, :lat, 1.0)"),
                dict(id=row_id, crop=crop, variety=variety, country=country, lat=lat))
        session.commit()
        yield types.SimpleNamespace(repo=PlantingRecommendationRepo())
        session.remove()


def _ids(page):
    return [r.id for r in page.items]


def test_without_filters_the_whole_table_is_counted(repo):
    """A bare count() had no FROM without a WHERE, and counted 1."""
    page = repo.repo.get_paginated_data(PlantingDataFilter(), 1, 4)
    assert (page.total, page.pages, _ids(page)) == (9, 3, [1, 2, 3, 4])


def test_a_later_page_of_a_filtered_set(repo):
    page = repo.repo.get_paginated_data(PlantingDataFilter(crop_type='maize'), 2, 4)
    assert (page.total, page.pages, page.page, _ids(page)) == (6, 2, 2, [5, 6])


def test_every_filter_reaches_both_the_page_and_the_count(repo):
    page = repo.repo.get_paginated_data(
        PlantingDataFilter(crop_type='Maize', variety='medium', country='Zambia'), 1, 10)
    assert (page.total, _ids(page)) == (2, [1, 3])


def test_the_chosen_sort_is_kept_with_id_as_tie_break(repo):
    page = repo.repo.get_paginated_data(PlantingDataFilter(sort_col='lat', sort_dir='asc'), 1, 3)
    assert _ids(page) == [9, 8, 7]  # lat = 10 - id


def test_past_the_end_is_empty_not_an_error(repo):
    page = repo.repo.get_paginated_data(PlantingDataFilter(crop_type='cotton'), 5, 10)
    assert (page.total, page.pages, _ids(page)) == (3, 1, [])
