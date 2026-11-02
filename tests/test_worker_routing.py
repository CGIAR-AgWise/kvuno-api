"""Deciding where file work runs, and the processes that run it."""
from app.services import housekeeper as hk
from app.services import ingest_runner


def test_no_worker_is_assumed_without_housekeeping(monkeypatch):
    monkeypatch.setenv('HOUSEKEEPING_ENABLED', 'false')
    monkeypatch.setattr(hk, '_ask_for_worker', lambda timeout: (_ for _ in ()).throw(AssertionError('asked')))
    assert hk.celery_worker_available() is False


def test_the_answer_is_cached_between_polls(monkeypatch):
    monkeypatch.setenv('HOUSEKEEPING_ENABLED', 'true')
    monkeypatch.setattr(hk, '_worker_check', (False, 0.0))
    asked = []
    monkeypatch.setattr(hk, '_ask_for_worker', lambda timeout: asked.append(1) or True)
    assert hk.celery_worker_available() and hk.celery_worker_available()
    assert asked == [1]


def test_only_a_worker_on_our_queue_counts(monkeypatch):
    """Another project's worker on the same Redis answers a ping too."""
    from app import celery_app as ca
    monkeypatch.setattr(hk, 'celery_broker_available', lambda: True)

    class Inspect:
        def __init__(self, queues):
            self.queues = queues

        def active_queues(self):
            return self.queues

    monkeypatch.setattr(ca.celery_app.control, 'inspect',
                        lambda timeout: Inspect({'other@host': [{'name': 'celery'}]}))
    assert hk._ask_for_worker(0.1) is False
    monkeypatch.setattr(ca.celery_app.control, 'inspect',
                        lambda timeout: Inspect({'kvuno-worker': [{'name': 'kvuno'}]}))
    assert hk._ask_for_worker(0.1) is True


def test_the_runner_refuses_an_id_that_is_not_an_upload_id():
    assert ingest_runner.run('../../etc/passwd') == 2


def test_the_runner_opens_a_processing_run(monkeypatch):
    """A fallback import must show in the Runs panel like a Celery one."""
    from app import worker_app
    from app.models import database_conn, kvuno
    from app.services import progress_store, uploads

    class _Upload:
        stored_path = 'x.parquet'
        column_map = {}
        original_name = 'x.parquet'
        data_date = None
        crop_type = 'cassava'

    class _Session:
        def get(self, model, upload_id):
            return _Upload()

        def remove(self):
            pass

    class _Db:
        session = _Session()

    class _App:
        def app_context(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    started, discarded, seen = [], [], []
    monkeypatch.setattr(uploads, 'valid_id', lambda upload_id: True)
    monkeypatch.setattr(uploads, 'data_datetime', lambda upload: None)
    monkeypatch.setattr(database_conn.MyDb, 'get_db', classmethod(lambda cls: _Db()))
    monkeypatch.setattr(kvuno, 'Upload', object)
    monkeypatch.setattr(worker_app, 'build_app', lambda: _App())
    monkeypatch.setattr(hk, 'set_app', lambda app: None)
    monkeypatch.setattr(hk, 'housekeeping_settings', lambda: {})
    monkeypatch.setattr(hk, 'is_transient_db_error', lambda e: False)
    monkeypatch.setattr(hk, 'process_file', lambda *a, **kw: seen.append(kw))
    monkeypatch.setattr(progress_store, 'start_run', lambda **kw: started.append(kw) or 7)
    monkeypatch.setattr(progress_store, 'discard_run', lambda run_id: discarded.append(run_id))

    assert ingest_runner.run('a' * 32) == 0
    assert started and started[0]['source'] == 'upload'
    assert seen and seen[0]['run_id'] == 7
    assert seen[0]['crop_type'] == 'cassava', "the crop chosen on the mapping form reaches the import"
    assert discarded == [7]


def test_the_worker_records_an_rds_that_will_not_convert_and_stops(monkeypatch, tmp_path):
    from app import tasks
    bad = tmp_path / 'x.RDS'
    bad.write_bytes(b'not R data')
    reported = []
    monkeypatch.setattr(hk, 'report_conversion', lambda path, stage, *a: reported.append(stage))
    assert tasks._convert(str(bad)) is None
    assert reported[-1] == 'failed'
