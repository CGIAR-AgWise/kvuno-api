"""Ingestion pipeline regressions.

Each class names the bug it guards against:

- progress writes committed the ingest's half-finished transaction;
- two workers could ingest one file at once;
- process_file swallowed every exception, so Celery never retried, and wiped
  the last known position with 0/0;
- run_id was never passed, so each attempt overwrote the last one's history;
- naive-UTC timestamps were converted as local time;
- signal handlers were installed at import time;
- process_pending enqueued one scan per gunicorn worker;
- '.PARQUET' went down the RDS path, and default-map columns missing from a
  file failed the whole pyarrow read;
- /ui/progress/<file> read a sidecar nothing writes.

Unit-only, like the rest of the suite: SQLite in memory, no Redis, no Celery.
"""
import calendar
import datetime
import os
import signal
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError, PendingRollbackError

from app.config import get_settings
from app.services import housekeeper as hk
from app.services import progress_store as ps
from app.utils.file_lock import FileLock


@pytest.fixture
def flask_app():
    from flask import Flask
    from app.models.database_conn import MyDb
    from app.models.kvuno import Base, ProcessingJob, ProcessingRun

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    MyDb.init_app(app)
    with app.app_context():
        Base.metadata.create_all(MyDb.get_db().engine,
                                 tables=[ProcessingRun.__table__, ProcessingJob.__table__])
    previous = hk._app
    hk.set_app(app)
    yield app
    hk.set_app(previous)


@pytest.fixture
def db(flask_app):
    from app.models.database_conn import MyDb
    with flask_app.app_context():
        yield MyDb.get_db().session
        MyDb.get_db().session.remove()


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    monkeypatch.setattr(ps, '_redis_client', lambda: None)
    ps._last_published.clear()
    ps._last_db_write.clear()
    yield
    ps._last_published.clear()
    ps._last_db_write.clear()


@pytest.fixture
def progress_calls(monkeypatch):
    calls = []

    def fake(file_path, status, current, total, message='', **kw):
        calls.append({'status': status, 'current': current, 'total': total,
                      'message': message, **kw})
    monkeypatch.setattr(hk, '_write_progress', fake)
    return calls


def _op_error():
    return OperationalError('SELECT 1', {}, Exception('server closed the connection'))


# ── 1. Progress writes are isolated from the ingest transaction ──────────

class TestProgressDoesNotCommitIngest:
    def test_progress_uses_its_own_session(self, db):
        with ps._db_session() as session:
            assert session is not db
            assert session is not db()

    def test_progress_write_leaves_pending_ingest_work_uncommitted(self, db):
        """The bug: _db_save committed the scoped session process_file uses."""
        from sqlalchemy.orm import Session
        from app.models.database_conn import MyDb
        from app.models.kvuno import ProcessingRun

        # Stand-in for ingest rows awaiting the next checkpoint.
        db.add(ProcessingRun(label='uncommitted ingest work', source='upload'))
        ps.save_progress('x.parquet', 'completed', 1, 1, 'done')
        db.rollback()  # what a crash before the checkpoint amounts to

        with Session(bind=MyDb.get_db().engine) as fresh:
            labels = [r.label for r in fresh.query(ProcessingRun).all()]
        assert 'uncommitted ingest work' not in labels

    def test_failed_progress_write_does_not_raise(self, db, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError('db down')
        monkeypatch.setattr(ps, '_db_save', boom)
        ps.save_progress('y.parquet', 'error', 0, 0, 'x')  # must not raise


# ── 2. Per-file ingest lock ──────────────────────────────────────────────

class TestFileLock:
    def test_second_acquire_fails_while_held(self, tmp_path):
        path = str(tmp_path / 'f.ingest.lock')
        first, second = FileLock(path, 600), FileLock(path, 600)
        assert first.acquire()
        assert not second.acquire()
        first.release()
        assert second.acquire()
        second.release()
        assert not os.path.exists(path)

    def test_stale_lock_is_taken_over(self, tmp_path):
        path = str(tmp_path / 'f.ingest.lock')
        dead = FileLock(path, 600)
        assert dead.acquire()
        old = time.time() - 3600
        os.utime(path, (old, old))
        assert FileLock(path, 600).acquire()

    def test_touch_keeps_a_live_lock_fresh(self, tmp_path):
        path = str(tmp_path / 'f.ingest.lock')
        lock = FileLock(path, 600)
        lock.acquire()
        old = time.time() - 3600
        os.utime(path, (old, old))
        lock.touch()
        assert not FileLock(path, 600).acquire()


class TestProcessFileLock:
    def test_concurrent_run_returns_early_without_progress(self, flask_app, tmp_path,
                                                           progress_calls, monkeypatch):
        data = tmp_path / 'a.parquet'
        data.write_bytes(b'x')
        holder = FileLock(str(data) + hk.INGEST_LOCK_SUFFIX, get_settings().INGEST_LOCK_STALE_SECONDS)
        assert holder.acquire()

        checksum = MagicMock()
        monkeypatch.setattr(hk, 'calculate_file_checksum', checksum)
        hk.process_file(str(data), column_map={'lat': 'lat'})

        checksum.assert_not_called()
        # No error entry: that would mark the other worker's job as failed.
        assert progress_calls == []
        # And the lock is still the other worker's.
        assert os.path.exists(holder.path)
        holder.release()

    def test_stale_lock_is_taken_over_and_released(self, flask_app, tmp_path,
                                                   progress_calls, monkeypatch):
        data = tmp_path / 'b.parquet'
        data.write_bytes(b'x')
        lock_path = str(data) + hk.INGEST_LOCK_SUFFIX
        with open(lock_path, 'w'):
            pass
        old = time.time() - get_settings().INGEST_LOCK_STALE_SECONDS - 60
        os.utime(lock_path, (old, old))

        seen = []
        monkeypatch.setattr(hk, 'calculate_file_checksum',
                            lambda *a: seen.append(os.path.exists(lock_path)) or 'sum')
        monkeypatch.setattr(hk, 'retry_db', lambda fn, **kw: fn())
        monkeypatch.setattr(hk.file_import_repo, 'get_by_checksum',
                            lambda c: SimpleNamespace(offset=None))
        hk.process_file(str(data), column_map={'lat': 'lat'})

        assert seen == [True], 'the stale lock was not taken over'
        assert not os.path.exists(lock_path), 'lock not released'
        assert progress_calls[-1]['status'] == 'completed'


# ── 3. Transient errors are retried, bad files are not ──────────────────

class TestErrorClassification:
    def test_operational_error_is_transient(self):
        assert hk.is_transient_db_error(_op_error())

    def test_pending_rollback_is_transient(self):
        assert hk.is_transient_db_error(PendingRollbackError('x'))

    def test_wrapped_cause_is_followed(self):
        try:
            try:
                raise _op_error()
            except OperationalError as inner:
                raise RuntimeError('wrapped') from inner
        except RuntimeError as outer:
            assert hk.is_transient_db_error(outer)

    def test_bad_data_is_not_transient(self):
        assert not hk.is_transient_db_error(ValueError('bad column'))
        assert not hk.is_transient_db_error(FileNotFoundError('gone'))


class TestProcessFileErrors:
    @pytest.fixture
    def parquet(self, tmp_path):
        import pyarrow as pa
        import pyarrow.parquet as pq
        path = tmp_path / 'c.PARQUET'
        pq.write_table(pa.table({'lat': [1.0] * 10, 'lon': [2.0] * 10, 'XY': ['p'] * 10}),
                       str(path))
        return str(path)

    def _resume_at(self, monkeypatch, offset):
        monkeypatch.setattr(hk, 'calculate_file_checksum', lambda *a: 'sum')
        monkeypatch.setattr(hk, 'retry_db', lambda fn, **kw: fn())
        monkeypatch.setattr(hk.file_import_repo, 'get_by_checksum',
                            lambda c: SimpleNamespace(offset=offset))

    def test_transient_error_is_reraised_with_position_kept(self, flask_app, parquet,
                                                            progress_calls, monkeypatch):
        self._resume_at(monkeypatch, 5)

        def lost_connection(**kw):
            raise _op_error()
        monkeypatch.setattr(hk, 'PlantingRecommendationCreate', lost_connection)

        with pytest.raises(OperationalError):
            hk.process_file(parquet, column_map={'lat': 'lat', 'lon': 'lon'})

        last = progress_calls[-1]
        assert last['status'] == 'error'
        # The last checkpoint and the real total, not 0/0.
        assert (last['current'], last['total']) == (5, 10)
        assert not os.path.exists(parquet + hk.INGEST_LOCK_SUFFIX)

    def test_bad_file_is_recorded_not_raised(self, flask_app, parquet,
                                             progress_calls, monkeypatch):
        self._resume_at(monkeypatch, 5)

        def bad_row(**kw):
            raise ValueError('not a number')
        monkeypatch.setattr(hk, 'PlantingRecommendationCreate', bad_row)

        hk.process_file(parquet, column_map={'lat': 'lat', 'lon': 'lon'})  # no raise
        last = progress_calls[-1]
        assert last['status'] == 'error'
        assert (last['current'], last['total']) == (5, 10)

    def test_uppercase_parquet_extension_is_streamed(self, flask_app, parquet,
                                                     progress_calls, monkeypatch):
        """'.PARQUET' used to go to pyreadr, which cannot read it."""
        self._resume_at(monkeypatch, 5)
        read_r = MagicMock(side_effect=AssertionError('pyreadr used for a parquet file'))
        monkeypatch.setattr(hk.pyreadr, 'read_r', read_r)
        monkeypatch.setattr(hk, 'PlantingRecommendationCreate',
                            MagicMock(side_effect=ValueError('stop here')))
        hk.process_file(parquet, column_map={'lat': 'lat', 'lon': 'lon'})
        read_r.assert_not_called()
        assert progress_calls[-1]['total'] == 10


class TestCeleryConfig:
    def test_visibility_timeout_and_reject_on_worker_lost(self):
        from app.celery_app import celery_app
        conf = celery_app.conf
        assert conf.task_reject_on_worker_lost is True
        assert conf.broker_transport_options['visibility_timeout'] >= 3600

    def test_a_lost_broker_is_retried_not_fatal(self):
        """A Redis blip must not leave the worker up but not consuming.

        Startup is still fail-fast (the preflight checks it); after it has
        connected, the consumer loop retries and reconnects.
        """
        from app.celery_app import celery_app
        conf = celery_app.conf
        assert conf.broker_connection_retry is True
        assert conf.broker_connection_max_retries == 1
        assert conf.broker_connection_retry_on_startup is False


# ── 4. Runs and attempts ────────────────────────────────────────────────

class TestRunsAndAttempts:
    @pytest.fixture(autouse=True)
    def unthrottled(self, monkeypatch):
        # Back-to-back writes in a test would otherwise be dropped by the
        # publish throttle before reaching the database.
        monkeypatch.setenv('JOB_PROGRESS_MIN_INTERVAL', '0')

    def test_new_run_for_a_known_file_counts_as_another_attempt(self, db):
        from app.models.kvuno import ProcessingJob
        first = ps.start_run('one', total_files=1)
        ps.save_progress('r.parquet', 'error', 1, 10, 'x', run_id=first)
        second = ps.start_run('two', total_files=1)
        ps.save_progress('r.parquet', 'processing', 1, 10, 'y', run_id=second)
        rows = {r.run_id: r.attempts for r in db.query(ProcessingJob).filter_by(file_name='r.parquet')}
        assert rows == {first: 1, second: 2}

    def test_retry_within_a_run_increments_attempts(self, db):
        from app.models.kvuno import ProcessingJob
        run = ps.start_run('one', total_files=1)
        ps.save_progress('s.parquet', 'error', 3, 10, 'db down', run_id=run)
        ps.save_progress('s.parquet', 'processing', 3, 10, 'again', run_id=run)
        row = db.query(ProcessingJob).filter_by(file_name='s.parquet').one()
        assert row.attempts == 2
        assert row.finished_at is None

    def test_process_file_async_passes_a_run_id(self, monkeypatch):
        import json
        captured = {}
        monkeypatch.setattr(ps, 'start_run', lambda **kw: 42)
        monkeypatch.setattr(hk, '_enqueue_or_warn',
                            lambda task, **kw: captured.update(kw) or True)
        assert hk.process_file_async(file_path='/tmp/data/x.parquet') is True
        assert captured['run_id'] == 42
        json.dumps(captured)

    def test_failed_enqueue_discards_the_run(self, monkeypatch):
        discarded = []
        monkeypatch.setattr(ps, 'start_run', lambda **kw: 7)
        monkeypatch.setattr(ps, 'discard_run', discarded.append)
        monkeypatch.setattr(hk, '_enqueue_or_warn', lambda task, **kw: False)
        assert hk.process_file_async(file_path='/tmp/data/x.parquet') is False
        assert discarded == [7]

    def test_run_creation_failure_still_enqueues(self, monkeypatch):
        captured = {}

        def broken(**kw):
            raise RuntimeError('no db')
        monkeypatch.setattr(ps, 'start_run', broken)
        monkeypatch.setattr(hk, '_enqueue_or_warn',
                            lambda task, **kw: captured.update(kw) or True)
        assert hk.process_file_async(file_path='/tmp/data/x.parquet') is True
        assert 'run_id' not in captured

    def test_discard_run_keeps_runs_with_jobs(self, db):
        from app.models.kvuno import ProcessingRun
        empty = ps.start_run('empty')
        used = ps.start_run('used')
        ps.save_progress('t.parquet', 'processing', 0, 1, run_id=used)
        ps.discard_run(empty)
        ps.discard_run(used)
        ids = {r.id for r in db.query(ProcessingRun).all()}
        assert empty not in ids and used in ids

    def test_scan_uses_one_run_for_all_files(self, flask_app, tmp_path, monkeypatch):
        for name in ('a.parquet', 'b.RDS', 'notes.txt'):
            (tmp_path / name).write_bytes(b'x')
        monkeypatch.setattr(hk, 'db_health_check', lambda: None)
        monkeypatch.setattr(hk, 'download_remote_files', lambda folder: [])
        monkeypatch.setattr('app.utils.rds_to_parquet.batch_convert', lambda *a, **kw: [])
        runs = []
        monkeypatch.setattr(ps, 'start_run', lambda **kw: runs.append(kw) or 99)
        seen = []
        monkeypatch.setattr(hk, 'process_file', lambda fp, *a: seen.append(a[-1]))
        hk.load_rds_to_db(str(tmp_path))
        assert len(runs) == 1 and runs[0]['source'] == 'scan' and runs[0]['total_files'] == 2
        assert seen == [99, 99]

    def test_task_accepts_run_id_and_old_shape(self):
        import inspect
        from app.tasks import process_file_task
        params = inspect.signature(process_file_task.run).parameters
        assert params['run_id'].default is None


# ── 5. Naive-UTC timestamps ─────────────────────────────────────────────

class TestUtcTimestamps:
    def test_naive_value_is_read_as_utc(self):
        naive = datetime.datetime(2026, 1, 2, 3, 4, 5)
        assert ps._epoch(naive) == calendar.timegm(naive.timetuple())

    def test_none_is_zero(self):
        assert ps._epoch(None) == 0

    def test_db_job_mtime_matches_wall_clock(self, db):
        """On a non-UTC host .timestamp() shifted this by the UTC offset."""
        ps.save_progress('u.parquet', 'processing', 1, 2)
        job = ps.get_job('u.parquet')
        assert abs(job['mtime'] - time.time()) < 60
        assert job['status'] == 'processing'


# ── 6. Signal handlers ──────────────────────────────────────────────────

class TestSignalHandlers:
    def test_import_does_not_install_handlers(self):
        assert signal.getsignal(signal.SIGINT) is not hk._handle_sigint

    def test_install_off_main_thread_does_not_raise(self):
        errors = []

        def run():
            try:
                hk.install_signal_handlers()
            except Exception as e:  # pragma: no cover - the failure mode
                errors.append(e)
        t = threading.Thread(target=run)
        t.start()
        t.join()
        assert errors == []

    def test_install_on_main_thread_sets_handlers(self):
        before = signal.getsignal(signal.SIGINT)
        try:
            hk.install_signal_handlers()
            assert signal.getsignal(signal.SIGINT) is hk._handle_sigint
        finally:
            signal.signal(signal.SIGINT, before)
            if hasattr(signal, 'SIGTERM'):
                signal.signal(signal.SIGTERM, signal.SIG_DFL)


# ── 7. process_pending de-dupe ──────────────────────────────────────────

class TestProcessPendingDedupe:
    def _redis(self, monkeypatch, set_result=None, error=None):
        client = MagicMock()
        if error:
            client.set.side_effect = error
        else:
            client.set.return_value = set_result
        monkeypatch.setattr('redis.Redis.from_url', lambda *a, **kw: client)
        return client

    def test_first_worker_enqueues(self, monkeypatch):
        client = self._redis(monkeypatch, set_result=True)
        enqueued = []
        monkeypatch.setattr(hk, '_enqueue_or_warn', lambda task, **kw: enqueued.append(task))
        hk.process_pending()
        assert len(enqueued) == 1
        _, kwargs = client.set.call_args
        assert kwargs['nx'] is True and kwargs['ex'] > 0

    def test_other_workers_skip(self, monkeypatch):
        self._redis(monkeypatch, set_result=None)
        enqueued = []
        monkeypatch.setattr(hk, '_enqueue_or_warn', lambda task, **kw: enqueued.append(task))
        hk.process_pending()
        assert enqueued == []

    def test_unreachable_redis_falls_through(self, monkeypatch):
        self._redis(monkeypatch, error=ConnectionError('down'))
        enqueued = []
        monkeypatch.setattr(hk, '_enqueue_or_warn', lambda task, **kw: enqueued.append(task))
        hk.process_pending()
        assert len(enqueued) == 1


# ── 8. Column selection ─────────────────────────────────────────────────

class TestColumnSelection:
    def test_default_map_drops_columns_absent_from_file(self):
        mapping, columns = hk._select_columns(
            {'lat': 'lat', 'lon': 'lon', 'Variety': 'variety'},
            ['lat', 'lon', 'XY'], 'f', explicit=False)
        assert mapping == {'lat': 'lat', 'lon': 'lon'}
        assert columns == ['lat', 'lon', 'XY']

    def test_explicit_map_is_left_alone(self):
        mapping, columns = hk._select_columns({'a': 'lat'}, ['a'], 'f', explicit=True)
        assert mapping == {'a': 'lat'} and columns == ['a']

    def test_xy_is_not_requested_when_absent(self):
        _, columns = hk._select_columns({'lat': 'lat'}, ['lat'], 'f', explicit=False)
        assert 'XY' not in columns

    def test_coordinate_columns_fall_back_to_lat_lon(self):
        assert hk._coordinate_columns({'y': 'lat', 'x': 'lon'}, ['y', 'x']) == ['y', 'x']
        assert hk._coordinate_columns({'y': 'lat'}, ['y', 'XY']) == ['XY']


# ── 9. /ui/progress/<file> ──────────────────────────────────────────────

class TestProgressRoute:
    @pytest.fixture
    def client(self, monkeypatch):
        from app import create_app
        # create_app's boot cleanup lists jobs; there is no jobs table here.
        monkeypatch.setattr(ps, 'load_all_jobs', lambda *a, **kw: [])
        app = create_app()
        with patch('app.auth.get_current_user', return_value=MagicMock(id=1)):
            with app.test_client() as c:
                yield c

    def test_returns_the_job_record(self, client, monkeypatch):
        job = {'file': 'a.parquet', 'status': 'processing', 'current': 5, 'total': 10,
               'message': '', 'mtime': 1, 'started_at': 1}
        monkeypatch.setattr(ps, 'get_job', lambda stem: dict(job) if stem == 'a.parquet' else None)
        resp = client.get('/ui/progress/a.parquet')
        assert resp.status_code == 200
        assert resp.get_json() == job

    def test_unknown_job(self, client, monkeypatch):
        monkeypatch.setattr(ps, 'get_job', lambda stem: None)
        resp = client.get('/ui/progress/missing.parquet')
        assert resp.status_code == 200
        assert resp.get_json() == {'status': 'unknown', 'current': 0, 'total': 0, 'message': ''}

    def test_path_separators_are_rejected(self, client, monkeypatch):
        monkeypatch.setattr(ps, 'get_job', MagicMock(side_effect=AssertionError))
        assert client.get('/ui/progress/..%5Csecret').status_code == 404


# ── 10. An upload leaves `processing` when its import ends ───────────────

class TestUploadStatusOnImportEnd:
    def test_completion_finishes_the_upload(self, monkeypatch):
        calls = []
        monkeypatch.setattr(hk, 'save_progress', lambda *a, **kw: None)
        monkeypatch.setattr('app.services.uploads.mark_processed',
                            lambda *a, **kw: calls.append((a, kw)))
        hk._write_progress('C:/data/uploads/' + 'a' * 32 + '/' + 'a' * 32 + '.parquet',
                           'completed', 5, 5)
        assert calls and calls[0][0][1] == 'completed'

    def test_in_flight_progress_does_not_finish_it(self, monkeypatch):
        calls = []
        monkeypatch.setattr(hk, 'save_progress', lambda *a, **kw: None)
        monkeypatch.setattr('app.services.uploads.mark_processed',
                            lambda *a, **kw: calls.append((a, kw)))
        hk._write_progress('C:/data/uploads/' + 'a' * 32 + '/' + 'a' * 32 + '.parquet',
                           'processing', 2, 5)
        assert calls == []
