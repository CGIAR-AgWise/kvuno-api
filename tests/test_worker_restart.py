"""Restarting the worker mid-ingest must not leave the job hanging.

Before: Docker's SIGTERM reached only Celery's main process, the warm shutdown
waited for the whole file, Docker SIGKILLed the container, the task message
stayed unacked for CELERY_VISIBILITY_TIMEOUT (6 h), and the dead ingest lock
made a manual Retry skip the file. The job showed "processing", then "stale".
"""
import datetime
import os
import socket
from types import SimpleNamespace

import pytest

from app.services import housekeeper as hk
from app.services import progress_store as ps
from app.utils.file_lock import FileLock


@pytest.fixture(autouse=True)
def clean_stop_flag():
    hk.clear_worker_stop()
    yield
    hk.clear_worker_stop()


@pytest.fixture
def flask_app():
    from flask import Flask
    from app.models.database_conn import MyDb

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    MyDb.init_app(app)
    previous = hk._app
    hk.set_app(app)
    yield app
    hk.set_app(previous)


# ── Stopping ──────────────────────────────────────────────────────────────

class TestStopRequest:
    def test_the_flag_is_a_file_shared_with_pool_children(self):
        assert not hk.stop_requested()
        hk.request_worker_stop()
        assert os.path.exists(hk.WORKER_STOP_FILE)
        assert hk.stop_requested()
        hk.clear_worker_stop()
        assert not hk.stop_requested()

    def test_celery_signals_set_and_clear_it(self, monkeypatch):
        from app import celery_app as ca
        monkeypatch.setattr(hk, 'release_dead_locks', lambda *a: 0)
        ca._on_worker_shutting_down()
        assert hk.stop_requested()
        ca._on_worker_init()  # a restarted container keeps its /tmp
        assert not hk.stop_requested()

    def test_an_ingest_told_to_stop_pauses_at_its_offset(self, flask_app, tmp_path, monkeypatch):
        import pyarrow as pa
        import pyarrow.parquet as pq

        data = tmp_path / 'big.parquet'
        pq.write_table(pa.table({'lat': [1.0] * 10, 'lon': [2.0] * 10}), str(data))
        progress, offsets = [], []
        monkeypatch.setattr(hk, '_write_progress',
                            lambda fp, status, current, total, message='', **kw:
                            progress.append((status, current, total, message)))
        monkeypatch.setattr(hk, 'calculate_file_checksum', lambda *a: 'sum')
        monkeypatch.setattr(hk, 'retry_db', lambda fn, **kw: fn())
        monkeypatch.setattr(hk.file_import_repo, 'get_by_checksum', lambda c: SimpleNamespace(offset=4))
        monkeypatch.setattr(hk.file_import_repo, 'upsert_offset',
                            lambda checksum, name, offset, original, commit=True: offsets.append(offset))

        hk.request_worker_stop()
        hk.process_file(str(data), column_map={'lat': 'lat', 'lon': 'lon'}, chunk_size=2)

        assert offsets == [4], 'the offset must be kept where it was, not cleared as done'
        status, current, total, message = progress[-1]
        assert (status, current, total) == ('processing', 4, 10)
        assert 'Paused' in message
        assert not os.path.exists(str(data) + hk.INGEST_LOCK_SUFFIX), 'lock not released'


# ── Starting again ────────────────────────────────────────────────────────

class TestDeadLocks:
    def test_only_this_hosts_locks_are_released(self, tmp_path):
        nested = tmp_path / 'uploads' / 'abc'
        nested.mkdir(parents=True)
        mine = nested / ('a.parquet' + hk.INGEST_LOCK_SUFFIX)
        theirs = tmp_path / ('b.parquet' + hk.INGEST_LOCK_SUFFIX)
        legacy = tmp_path / ('c.parquet' + hk.INGEST_LOCK_SUFFIX)
        mine.write_text(f'{socket.gethostname()} 7\n')
        theirs.write_text('other-host 7\n')
        legacy.write_text('7\n')  # old format: pid only, left to go stale

        assert hk.release_dead_locks(str(tmp_path), socket.gethostname()) == 1
        assert not mine.exists()
        assert theirs.exists() and legacy.exists()

    def test_a_new_lock_records_its_host(self, tmp_path):
        lock = FileLock(str(tmp_path / 'x.lock'), 600)
        assert lock.acquire()
        assert FileLock.holder_host(lock.path) == socket.gethostname()
        lock.release()


class TestResume:
    @pytest.fixture
    def enqueued(self, monkeypatch):
        calls = []
        monkeypatch.setattr(hk, 'process_file_async', lambda path, **kw: calls.append((path, kw)) or True)
        return calls

    def test_this_hosts_unfinished_jobs_continue_in_their_runs(self, tmp_path, monkeypatch, enqueued):
        monkeypatch.setenv('HOUSEKEEPING_DATA_DIR', str(tmp_path))
        (tmp_path / 'scan.parquet').write_bytes(b'x')
        uploaded = tmp_path / 'up.parquet'
        uploaded.write_bytes(b'x')
        jobs = [
            {'file': 'up.parquet', 'status': 'processing', 'worker': 'w1', 'run_id': 3, 'current': 40},
            {'file': 'scan.parquet', 'status': 'stale', 'worker': 'w1', 'run_id': 4,
             'original_name': 'Scan.RDS'},
            {'file': 'done.parquet', 'status': 'completed', 'worker': 'w1', 'run_id': 5},
            {'file': 'other.parquet', 'status': 'processing', 'worker': 'w2', 'run_id': 6},
            {'file': 'gone.parquet', 'status': 'processing', 'worker': 'w1', 'run_id': 7},
        ]
        monkeypatch.setattr(ps, 'load_all_jobs', lambda *a, **kw: jobs)
        upload = SimpleNamespace(stored_path=str(uploaded), column_map={'Lat': 'lat'}, original_name='Up.RDS',
                                 data_date=datetime.date(2024, 3, 1), crop_type='maize')
        monkeypatch.setattr('app.services.uploads.find_by_stored_name',
                            lambda name: upload if name == 'up.parquet' else None)

        assert hk.resume_interrupted_jobs('w1') == 2
        assert enqueued == [
            (str(uploaded), {'column_map': {'Lat': 'lat'}, 'original_filename': 'Up.RDS', 'run_id': 3,
                             'created_at': datetime.datetime(2024, 3, 1), 'crop_type': 'maize'}),
            (os.path.join(str(tmp_path), 'scan.parquet'), {'original_filename': 'Scan.RDS', 'run_id': 4}),
        ]

    def test_a_stalled_job_from_any_host_is_resumed(self, tmp_path, monkeypatch, enqueued):
        """Imports the API ran on a gunicorn thread were killed by worker recycling and left stalled."""
        monkeypatch.setenv('HOUSEKEEPING_DATA_DIR', str(tmp_path))
        (tmp_path / 'big.parquet').write_bytes(b'x')
        (tmp_path / 'live.parquet').write_bytes(b'x')
        jobs = [
            {'file': 'big.parquet', 'status': 'stale', 'worker': 'kvuno', 'run_id': None},
            # Another host's job that is still reporting may well be running: left alone.
            {'file': 'live.parquet', 'status': 'processing', 'worker': 'kvuno', 'run_id': None},
        ]
        monkeypatch.setattr(ps, 'load_all_jobs', lambda *a, **kw: jobs)
        monkeypatch.setattr('app.services.uploads.find_by_stored_name', lambda name: None)

        assert hk.resume_interrupted_jobs('kvuno-worker') == 1
        assert [os.path.basename(path) for path, _ in enqueued] == ['big.parquet']

    def test_resuming_reuses_the_run_and_never_discards_it(self, monkeypatch):
        started, discarded, sent = [], [], []
        monkeypatch.setattr(ps, 'start_run', lambda **kw: started.append(kw) or 99)
        monkeypatch.setattr(ps, 'discard_run', discarded.append)
        monkeypatch.setattr(hk, '_enqueue_or_warn', lambda task, **kw: sent.append(kw) or False)

        assert hk.process_file_async('/data/a.parquet', run_id=3) is False
        assert started == [] and discarded == []
        assert sent[0]['run_id'] == 3


class TestLateRedelivery:
    def test_a_finished_job_is_not_overwritten_with_zero_rows(self, monkeypatch):
        """After a hard kill the old message is redelivered hours later; the file is done by then."""
        monkeypatch.setattr(ps, 'get_job', lambda name: {'status': 'completed', 'run_id': 3, 'total': 10})
        assert hk._already_reported_complete('a.parquet', 3)
        assert not hk._already_reported_complete('a.parquet', 4)
