"""Tests for the progress store.

The behaviours guarded here are ones that were previously wrong, so each test
names the bug it prevents from returning:

- ``updated_at`` did not refresh on UPDATE (``server_default=now()`` applies on
  INSERT only), which broke mtime ordering and made healthy jobs look stalled.
- ``started_at`` was not persisted at all, so the rate/ETA the UI computes
  disappeared whenever a job was read from the database.
- A job was keyed by filename alone with a UNIQUE constraint, so re-running a
  file overwrote its history.
- In-flight jobs were only ever written when Redis was *absent*, so a broker
  restart lost every running job.
"""

import time

import pytest

from app.config import get_settings
from app.services import progress_store as ps


@pytest.fixture
def db(monkeypatch):
    """An in-memory SQLite session with the new tables created."""
    from flask import Flask
    from app.models.database_conn import MyDb
    from app.models.kvuno import Base, ProcessingJob, ProcessingRun

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    MyDb.init_app(app)
    with app.app_context():
        # Only the two progress tables. create_all() on the full metadata would
        # also try to build planting_recommendations, whose PostGIS geometry
        # column has no SQLite equivalent (RecoverGeometryColumn).
        tables = [ProcessingRun.__table__, ProcessingJob.__table__]
        Base.metadata.create_all(MyDb.get_db().engine, tables=tables)
        yield MyDb.get_db().session
        MyDb.get_db().session.remove()


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    """Force the Postgres path, so the durable-write behaviour is what is tested."""
    monkeypatch.setattr(ps, '_redis_client', lambda: None)
    ps._last_published.clear()
    ps._last_db_write.clear()
    yield
    ps._last_published.clear()
    ps._last_db_write.clear()


class TestUpdatedAtRefreshes:
    def test_updated_at_moves_on_update(self, db):
        """The original bug: DEFAULT now() never fired on UPDATE."""
        from app.models.kvuno import ProcessingJob

        ps.save_progress('a.parquet', 'processing', 1, 10, 'start')
        first = db.query(ProcessingJob).filter_by(file_name='a.parquet').one().updated_at
        time.sleep(0.02)
        ps.save_progress('a.parquet', 'completed', 10, 10, 'done')
        db.expire_all()
        second = db.query(ProcessingJob).filter_by(file_name='a.parquet').one().updated_at
        assert second > first, 'updated_at did not refresh on update'

    def test_started_at_is_preserved_across_updates(self, db):
        """started_at must stay put or the computed rate is wrong.

        Uses a terminal write for the second update, because an intermediate
        one is throttled by JOB_DB_CHECKPOINT_SECONDS and would not reach the
        database at all (see the throttling test below).
        """
        from app.models.kvuno import ProcessingJob

        ps.save_progress('b.parquet', 'processing', 1, 10)
        first = db.query(ProcessingJob).filter_by(file_name='b.parquet').one().started_at
        time.sleep(0.02)
        ps.save_progress('b.parquet', 'completed', 10, 10, 'done')
        db.expire_all()
        row = db.query(ProcessingJob).filter_by(file_name='b.parquet').one()
        assert row.started_at == first
        assert row.updated_at > first

    def test_finished_at_only_on_terminal(self, db):
        from app.models.kvuno import ProcessingJob

        ps.save_progress('c.parquet', 'processing', 1, 10)
        row = db.query(ProcessingJob).filter_by(file_name='c.parquet').one()
        assert row.finished_at is None
        ps.save_progress('c.parquet', 'error', 1, 10, 'boom')
        db.expire_all()
        assert db.query(ProcessingJob).filter_by(file_name='c.parquet').one().finished_at is not None


class TestStartedAtSurvivesRead:
    def test_db_read_carries_started_at(self, db):
        """A DB-sourced job must still be able to show rate and ETA."""
        ps.save_progress('d.parquet', 'processing', 100, 1000, 'half')
        job = next(j for j in ps._db_load_all() if j['file'] == 'd.parquet')
        assert job['started_at'] > 0, 'started_at missing — rate/ETA would be omitted'


class TestHistoryIsNotOverwritten:
    def test_same_file_in_two_runs_is_two_rows(self, db):
        """Reprocessing a file must add history, not replace it."""
        from app.models.kvuno import ProcessingJob

        first_run = ps.start_run('batch one', total_files=1)
        ps.save_progress('e.parquet', 'completed', 10, 10, 'ok', run_id=first_run)

        second_run = ps.start_run('batch two', total_files=1)
        ps.save_progress('e.parquet', 'error', 0, 10, 'failed', run_id=second_run,
                         error='boom')

        rows = db.query(ProcessingJob).filter_by(file_name='e.parquet').all()
        assert len(rows) == 2, f'expected 2 rows, got {len(rows)}'
        assert {r.run_id for r in rows} == {first_run, second_run}
        assert {r.status for r in rows} == {'completed', 'error'}

    def test_error_is_stored_separately_from_message(self, db):
        """Routine progress must not erase why a job failed."""
        from app.models.kvuno import ProcessingJob

        run = ps.start_run('b', total_files=1)
        ps.save_progress('f.parquet', 'error', 3, 10, 'short read', run_id=run,
                         error='ValueError: bad column type')
        row = db.query(ProcessingJob).filter_by(file_name='f.parquet').one()
        assert row.error == 'ValueError: bad column type'
        assert row.message == 'short read'


class TestInFlightJobsAreDurable:
    def test_processing_is_persisted_not_just_terminal(self, db):
        """Previously only terminal states reached Postgres when Redis was up."""
        from app.models.kvuno import ProcessingJob

        ps.save_progress('g.parquet', 'processing', 50, 100, 'half')
        assert db.query(ProcessingJob).filter_by(file_name='g.parquet').count() == 1

    def test_db_writes_are_throttled_but_terminal_always_writes(self, db, monkeypatch):
        from app.models.kvuno import ProcessingJob

        monkeypatch.setenv('JOB_DB_CHECKPOINT_SECONDS', '60')
        ps.save_progress('h.parquet', 'processing', 1, 10, 'first')   # writes
        ps.save_progress('h.parquet', 'processing', 2, 10, 'second')  # throttled
        row = db.query(ProcessingJob).filter_by(file_name='h.parquet').one()
        assert row.current_row == 1, 'second progress write should have been throttled'

        # A terminal state must get through regardless of the throttle.
        ps.save_progress('h.parquet', 'completed', 10, 10, 'done')
        db.expire_all()
        row = db.query(ProcessingJob).filter_by(file_name='h.parquet').one()
        assert row.status == 'completed'
        assert row.current_row == 10


class TestRuns:
    def test_run_aggregates_job_counts(self, db):
        run = ps.start_run('five files', source='upload', total_files=2)
        ps.save_progress('i.parquet', 'completed', 10, 10, 'ok', run_id=run)
        ps.save_progress('j.parquet', 'error', 0, 10, 'no', run_id=run, error='e')

        runs = {r['id']: r for r in ps.list_runs()}
        assert run in runs
        summary = runs[run]
        assert summary['label'] == 'five files'
        assert summary['counts']['total'] == 2
        assert summary['counts']['completed'] == 1
        assert summary['counts']['error'] == 1
        assert summary['rows'] == {'current': 10, 'total': 20}

    def test_finish_run_stamps_finished_at(self, db):
        from app.models.kvuno import ProcessingRun

        run = ps.start_run('batch')
        ps.finish_run(run, 'completed')
        db.expire_all()
        row = db.get(ProcessingRun, run)
        assert row.status == 'completed'
        assert row.finished_at is not None


class _FakeRedis:
    """Just enough of redis-py for the read paths: a ZSET index and hashes."""

    def __init__(self, hashes=None):
        self.hashes = hashes or {}

    def zrevrange(self, key, start, end):
        stems = sorted(self.hashes, key=lambda s: float(self.hashes[s]['mtime']), reverse=True)
        return [s.encode() for s in stems]

    def hgetall(self, key):
        return self.hashes.get(key[len(ps.KEY_PREFIX):], {})

    def exists(self, key):
        return 0

    def keys(self, pattern):
        return []


class TestListingMergesRedisAndPostgres:
    """The Jobs page used to be empty whenever Redis answered but held nothing.

    load_all_jobs returned Redis *instead of* Postgres, so a flushed or
    restarted Redis — or an API and worker on different Redis DBs — hid every
    job that was safely recorded in the database.
    """

    def test_reachable_but_empty_redis_still_lists_db_jobs(self, db, monkeypatch):
        ps.save_progress('m.parquet', 'completed', 5, 5, 'done')
        monkeypatch.setattr(ps, '_redis_client', lambda: _FakeRedis())

        assert [j['file'] for j in ps.load_all_jobs()] == ['m.parquet']
        assert ps.get_job('m.parquet')['status'] == 'completed'

    def test_fresher_redis_state_wins_over_the_db_checkpoint(self, db, monkeypatch):
        ps.save_progress('n.parquet', 'processing', 10, 100, 'checkpoint')
        live = {'status': 'processing', 'current': '60', 'total': '100',
                'message': 'live', 'mtime': str(time.time() + 5), 'started_at': '1'}
        monkeypatch.setattr(ps, '_redis_client', lambda: _FakeRedis({'n.parquet': live}))

        (job,) = ps.load_all_jobs()
        assert job['current'] == 60

    def test_one_entry_per_file_even_with_several_runs(self, db):
        first = ps.start_run('first')
        second = ps.start_run('retry')
        ps.save_progress('o.parquet', 'error', 3, 9, 'failed', run_id=first, error='x')
        ps.save_progress('o.parquet', 'completed', 9, 9, 'done', run_id=second)

        jobs = [j for j in ps.load_all_jobs() if j['file'] == 'o.parquet']
        assert len(jobs) == 1
        assert jobs[0]['status'] == 'completed'


class TestClipping:
    def test_overlong_message_and_error_are_truncated(self, db):
        """Postgres would raise DataError on an oversized value mid-ingest."""
        from app.models.kvuno import ProcessingJob

        ps.save_progress('k.parquet', 'error', 0, 0, 'x' * 5000,
                         error='y' * 9000)
        row = db.query(ProcessingJob).filter_by(file_name='k.parquet').one()
        assert len(row.message) <= ps.MESSAGE_MAX
        assert len(row.error) <= ps.ERROR_MAX


class TestMarkStaleStillWorks:
    def test_long_silent_processing_job_goes_stale(self):
        old = time.time() - (get_settings().JOB_STALE_AFTER_SECONDS + 10)
        job = {'status': 'processing', 'mtime': old, 'message': ''}
        assert ps.mark_stale(job)['status'] == 'stale'

    def test_terminal_jobs_are_never_marked_stale(self):
        old = time.time() - (get_settings().JOB_STALE_AFTER_SECONDS + 10)
        for status in ('completed', 'error'):
            assert ps.mark_stale({'status': status, 'mtime': old})['status'] == status


class TestRedisHashDecoding:
    """Redis returns bytes unless decode_responses is set.

    The progress client is built without it, so hgetall yields
    {b'status': b'processing', ...}. Looking those up with str keys misses every
    field, which silently degraded a job to status='unknown' with zero rows —
    so the Jobs page showed everything as "Pending" while ingestion ran fine,
    and started_at was lost too, which is why the rate/ETA never appeared.
    """

    def test_bytes_hash_is_decoded(self):
        raw = {
            b'status': b'processing',
            b'current': b'605000',
            b'total': b'2025504',
            b'message': b'halfway',
            b'mtime': b'1790762111.7',
            b'started_at': b'1790761795.6',
        }
        job = ps._to_job('x.parquet', raw)
        assert job['status'] == 'processing'
        assert job['current'] == 605000
        assert job['total'] == 2025504
        assert job['message'] == 'halfway'
        # started_at drives the rows/sec and ETA; it must survive.
        assert job['started_at'] == 1790761795

    def test_str_hash_still_works(self):
        job = ps._to_job('x.parquet', {
            'status': 'completed', 'current': '10', 'total': '10',
            'message': 'Done', 'mtime': '1.0', 'started_at': '0.5',
        })
        assert job['status'] == 'completed'
        assert job['current'] == 10

    def test_missing_fields_still_default(self):
        job = ps._to_job('x.parquet', {b'status': b'processing'})
        assert job['status'] == 'processing'
        assert job['current'] == 0
        assert job['started_at'] == 0


class TestHousekeepingSettingsContract:
    """housekeeping_settings() is splatted into three callables.

    They do not share a signature, so a parameter only one of them accepts
    raises TypeError in the other two. max_workers belongs to load_rds_to_db's
    thread pool; including it broke process_file and watch_directory, failing
    every Celery ingest with "process_file() got an unexpected keyword argument".
    """

    CONSUMERS = ('process_file', 'load_rds_to_db', 'watch_directory')

    def test_every_consumer_accepts_the_shared_settings(self):
        import inspect
        from app.services import housekeeper

        settings = housekeeper.housekeeping_settings()
        for name in self.CONSUMERS:
            fn = getattr(housekeeper, name)
            params = inspect.signature(fn).parameters
            accepts_kwargs = any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            unknown = [k for k in settings if k not in params]
            assert not unknown or accepts_kwargs, (
                f'{name} would raise TypeError for {unknown}'
            )

    def test_max_workers_is_not_in_the_shared_settings(self):
        from app.services.housekeeper import housekeeping_settings
        assert 'max_workers' not in housekeeping_settings()

    def test_max_workers_is_still_reachable_for_the_batch_task(self):
        """Dropping it from the shared dict must not silently disable the pool."""
        from app.config import get_settings
        HOUSEKEEPING_MAX_WORKERS = get_settings().HOUSEKEEPING_MAX_WORKERS
        import inspect
        from app.services.housekeeper import load_rds_to_db
        assert 'max_workers' in inspect.signature(load_rds_to_db).parameters
        assert isinstance(HOUSEKEEPING_MAX_WORKERS, int)
