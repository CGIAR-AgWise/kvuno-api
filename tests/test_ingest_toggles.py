"""INGEST_DUPLICATE_CHECK: an already-imported file is skipped, or imported again.

The ingest is paused before its first chunk (the worker-stop flag), so the
test sees where it would have started without needing PostGIS.
"""
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.services import housekeeper as hk


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    from flask import Flask
    from app.models.database_conn import MyDb

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    MyDb.init_app(app)
    previous = hk._app
    hk.set_app(app)

    data = tmp_path / 'done.parquet'
    pq.write_table(pa.table({'lat': [1.0] * 4, 'lon': [2.0] * 4}), str(data))
    state = SimpleNamespace(path=str(data), progress=[], offsets=[])
    monkeypatch.setattr(hk, '_write_progress',
                        lambda fp, status, current, total, message='', **kw:
                        state.progress.append((status, current, total, message)))
    monkeypatch.setattr(hk, 'calculate_file_checksum', lambda *a: 'sum')
    monkeypatch.setattr(hk, 'retry_db', lambda fn, **kw: fn())
    # offset=None: the file was imported completely before.
    monkeypatch.setattr(hk.file_import_repo, 'get_by_checksum', lambda c: SimpleNamespace(offset=None))
    monkeypatch.setattr(hk.file_import_repo, 'upsert_offset',
                        lambda checksum, name, offset, original, commit=True: state.offsets.append(offset))
    hk.request_worker_stop()
    yield state
    hk.clear_worker_stop()
    hk.set_app(previous)


def _run(state):
    hk.process_file(state.path, column_map={'lat': 'lat', 'lon': 'lon'}, chunk_size=2)


def test_an_imported_file_is_skipped_by_default(ingest):
    _run(ingest)
    assert ingest.offsets == []
    assert ingest.progress[-1][0] == 'completed'


def test_with_the_check_off_it_is_imported_again_from_the_start(ingest, monkeypatch):
    monkeypatch.setenv('INGEST_DUPLICATE_CHECK', 'false')
    _run(ingest)
    status, current, total, _ = ingest.progress[-1]
    assert (status, current, total) == ('processing', 0, 4)  # paused at row 0, not skipped
