"""Crop type: chosen when a file is mapped, or read from a mapped column."""
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.dto.data_filters import PlantingDataFilter
from app.dto.planting_recommendation import PlantingRecommendationCreate
from app.services import housekeeper as hk
from app.services import uploads
from app.utils import crop_type as crops


@pytest.mark.parametrize('raw, stored', [
    ('Maize', 'maize'), ('  sweet   Potato ', 'sweet potato'), ('', None), (None, None), ('NaN', None),
])
def test_one_spelling_per_crop(raw, stored):
    assert crops.normalise(raw) == stored
    assert PlantingRecommendationCreate(crop_type=raw).crop_type == stored


def test_a_long_or_odd_cell_never_fails_its_row():
    assert crops.normalise('x' * 50) == 'x' * crops.MAX_LENGTH
    assert PlantingRecommendationCreate(crop_type=12).crop_type == '12'


@pytest.mark.parametrize('raw', ['x' * 31, 'maize; drop', '<b>'])
def test_the_form_refuses_what_it_cannot_store(raw):
    with pytest.raises(uploads.UploadError):
        uploads.parse_crop_type(raw)


def test_the_api_filter_matches_whatever_the_case():
    assert PlantingDataFilter(crop_type='Maize').crop_type == 'maize'


# ── Submitting a mapping ─────────────────────────────────────────────────

@pytest.fixture
def upload(monkeypatch):
    monkeypatch.setattr(uploads, '_session', lambda: SimpleNamespace(commit=lambda: None))
    return SimpleNamespace(status='ready', preview={'columns': ['lat', 'lon']},
                           column_map={'lat': 'lat', 'lon': 'lon'}, data_date=None, crop_type=None,
                           error=None, updated_at=None)


def test_submit_stores_the_crop_type(upload):
    uploads.submit(upload, crop_type=' Cassava ')
    assert upload.crop_type == 'cassava'


def test_retry_keeps_it_and_an_empty_value_clears_it(upload):
    uploads.submit(upload, crop_type='rice')
    uploads.submit(upload)  # Jobs-page Retry sends no crop type
    assert upload.crop_type == 'rice'
    uploads.submit(upload, crop_type='')
    assert upload.crop_type is None


def test_crop_type_is_a_mappable_column():
    assert 'crop_type' in uploads.DB_TARGET_COLUMNS
    assert uploads.suggest_column_map(['Crop', 'lat', 'lon'])['Crop'] == 'crop_type'


# ── Importing ────────────────────────────────────────────────────────────

class _Session:
    """Records what the ingest would insert; RETURNs nothing."""

    def __init__(self):
        self.rows = []

    def begin_nested(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, rows=None):
        self.rows.extend(rows or [])
        return SimpleNamespace(all=lambda: [])

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    from flask import Flask
    from app.models.database_conn import MyDb

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    MyDb.init_app(app)
    previous = hk._app
    hk.set_app(app)

    session = _Session()
    monkeypatch.setattr(MyDb, 'get_db', classmethod(lambda cls: SimpleNamespace(session=session)))
    monkeypatch.setenv('INGEST_CONFLICT_LOGGING', 'false')
    monkeypatch.setattr(hk, '_write_progress', lambda *a, **kw: None)
    monkeypatch.setattr(hk, 'calculate_file_checksum', lambda *a: 'sum')
    monkeypatch.setattr(hk, 'retry_db', lambda fn, **kw: fn())
    monkeypatch.setattr(hk, '_insert_statement', lambda: 'INSERT')
    monkeypatch.setattr(hk.file_import_repo, 'get_by_checksum', lambda c: None)
    monkeypatch.setattr(hk.file_import_repo, 'upsert_offset', lambda *a, **kw: None)

    path = tmp_path / 'trial.parquet'
    pq.write_table(pa.table({'lat': [1.0, 2.0], 'lon': [3.0, 4.0], 'Crop': ['Rice', None]}), str(path))
    yield SimpleNamespace(path=str(path), session=session)
    hk.set_app(previous)


def test_every_row_gets_the_chosen_crop(ingest):
    hk.process_file(ingest.path, column_map={'lat': 'lat', 'lon': 'lon'}, crop_type='maize')
    assert [r['crop_type'] for r in ingest.session.rows] == ['maize', 'maize']


def test_a_mapped_column_wins_and_the_choice_fills_its_blanks(ingest):
    hk.process_file(ingest.path, column_map={'lat': 'lat', 'lon': 'lon', 'Crop': 'crop_type'},
                    crop_type='maize')
    assert [r['crop_type'] for r in ingest.session.rows] == ['rice', 'maize']


def test_without_either_the_crop_is_unknown(ingest):
    hk.process_file(ingest.path, column_map={'lat': 'lat', 'lon': 'lon'})
    assert [r['crop_type'] for r in ingest.session.rows] == [None, None]


def test_the_worker_task_receives_it(monkeypatch):
    sent = []
    monkeypatch.setattr(hk, '_enqueue_or_warn', lambda task, **kw: sent.append(kw) or True)
    monkeypatch.setattr('app.services.progress_store.start_run', lambda **kw: 1)
    hk.process_file_async('x.parquet', crop_type='maize')
    assert sent[0]['crop_type'] == 'maize'
