"""The Duplicates tab is one page of server-side groups, not every conflict.

It used to call /conflicts once per page and group the rows in the browser,
so opening the tab sent a request for every page of conflicts. The grouping is
now GET /api/v1/quality/duplicates, paginated by group.
"""
import types

import pytest
from flask import Flask
from sqlalchemy import text

ROWS = [
    # country, province, variety, season, date, source
    ('KE', 'Nakuru', 'H614', 'long', '0315', 'a.parquet'),
    ('KE', 'Nakuru', 'H614', 'long', '0315', 'a.parquet'),
    ('KE', 'Nakuru', 'H614', 'long', '0315', 'b.parquet'),
    ('KE', None, 'H614', 'short', '1001', 'a.parquet'),  # NULL province groups with NULLs
    ('KE', None, 'H614', 'short', '1001', 'c.parquet'),
    ('TZ', 'Arusha', 'SC403', 'long', '0301', None),     # NULL source -> 'unknown'
]


@pytest.fixture
def env(monkeypatch):
    from app.api.quality import api
    from app.models.database_conn import MyDb

    monkeypatch.setattr('app.auth.get_current_user', lambda: types.SimpleNamespace(id=1))

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    MyDb.init_app(app)
    app.register_blueprint(api)
    with app.app_context():
        session = MyDb.get_db().session
        # Plain DDL: the model's created_at default is Postgres now().
        session.execute(text(
            'CREATE TABLE import_conflicts (id INTEGER PRIMARY KEY, record_data TEXT, country TEXT, '
            'province TEXT, lon REAL, lat REAL, variety TEXT, season_type TEXT, opt_date TEXT, '
            'crop_type TEXT, check_sum TEXT, source TEXT, created_at TIMESTAMP)'))
        for row in ROWS:
            session.execute(text(
                "INSERT INTO import_conflicts (record_data, country, province, variety, season_type, "
                "opt_date, crop_type, source) VALUES ('{}', :c, :p, :v, :s, :d, :crop, :src)"),
                {**dict(zip(('c', 'p', 'v', 's', 'd', 'src'), row)),
                 'crop': 'sorghum' if row[0] == 'TZ' else 'maize'})
        session.commit()
        yield types.SimpleNamespace(client=app.test_client())
        session.remove()


def test_groups_are_counted_and_ordered_by_repetition(env):
    body = env.client.get('/api/v1/quality/duplicates').get_json()
    assert body['total'] == 3
    assert [g['count'] for g in body['data']] == [3, 2, 1]
    first, second, third = body['data']
    assert first['sources'] == {'a.parquet': 2, 'b.parquet': 1}
    assert second['province'] is None
    assert second['sources'] == {'a.parquet': 1, 'c.parquet': 1}
    assert third['sources'] == {'unknown': 1}


def test_one_page_at_a_time(env):
    page1 = env.client.get('/api/v1/quality/duplicates?per_page=2').get_json()
    page2 = env.client.get('/api/v1/quality/duplicates?per_page=2&page=2').get_json()
    assert (page1['total'], page1['pages'], page1['current_page']) == (3, 2, 1)
    assert [g['count'] for g in page1['data']] == [3, 2]
    assert [g['count'] for g in page2['data']] == [1]
    assert page2['data'][0]['country'] == 'TZ'


def test_filters_apply_to_the_groups(env):
    body = env.client.get('/api/v1/quality/duplicates?country=TZ').get_json()
    assert body['total'] == 1
    body = env.client.get('/api/v1/quality/duplicates?source=c.parquet').get_json()
    assert [(g['count'], g['sources']) for g in body['data']] == [(1, {'c.parquet': 1})]


def test_groups_carry_and_filter_by_crop(env):
    body = env.client.get('/api/v1/quality/duplicates?crop_type=Sorghum').get_json()
    assert [(g['country'], g['crop_type']) for g in body['data']] == [('TZ', 'sorghum')]


def test_past_the_last_page_is_empty_not_an_error(env):
    res = env.client.get('/api/v1/quality/duplicates?page=9')
    assert res.status_code == 200
    assert res.get_json()['data'] == []


def test_requires_auth(env, monkeypatch):
    monkeypatch.setattr('app.auth.get_current_user', lambda: None)
    assert env.client.get('/api/v1/quality/duplicates').status_code == 401
