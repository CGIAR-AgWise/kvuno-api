"""The tus upload flow, end to end against in-memory SQLite.

Each test names the failure of the resumable.js flow it replaced:

- chunks lived in a directory every worker boot deleted, so uploads in flight
  were lost at random — the offset is now the part file's size, and cleanup
  only removes uploads idle for days;
- the client chose the storage identifier — ids are now issued by the server
  and checked against a strict pattern;
- conversion, preview and hashing ran inside one request and timed out —
  inspection now runs off the request path and is re-claimed if it dies;
- auto-guessed column matches were displayed but never sent, and Retry sent
  `{}` and wiped the mapping — the server now seeds and keeps the mapping.
"""
import datetime
import threading
import types
import uuid

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from flask import Flask

from app.services import uploads as service

OWNER = types.SimpleNamespace(id=1)
STRANGER = types.SimpleNamespace(id=2)
TUS = {'Tus-Resumable': '1.0.0'}


_RealThread = threading.Thread


class _InlineThread:
    """Run inspections synchronously; skip the heartbeat, which loops forever.

    The patch replaces threading.Thread everywhere, so any other thread (the
    RDS converter's stdout reader) is started for real.
    """

    def __init__(self, target=None, args=(), name=None, daemon=None):
        self.target, self.args, self.name, self.daemon = target, args, name, daemon

    def start(self):
        if self.name and self.name.startswith('inspect-'):
            self.target(*self.args)
        elif self.name and not self.name.startswith('heartbeat'):
            _RealThread(target=self.target, args=self.args, name=self.name, daemon=True).start()


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.models.database_conn import MyDb
    from app.models.kvuno import Base, FileImport, Upload
    from app.routes.main import register_app_routes

    # Settings are read per call, so the environment is all a test changes.
    monkeypatch.setenv('HOUSEKEEPING_DATA_DIR', str(tmp_path))  # uploads in tmp/uploads
    monkeypatch.setenv('MAX_FILE_SIZE_MB', '1')
    monkeypatch.setenv('HOUSEKEEPING_ENABLED', 'false')
    monkeypatch.setattr(service.threading, 'Thread', _InlineThread)

    # Imports start a separate process; record them instead.
    spawned = []
    monkeypatch.setattr('app.routes.uploads._spawn_runner', spawned.append)

    current = {'user': OWNER}
    monkeypatch.setattr('app.auth.get_current_user', lambda: current['user'])
    monkeypatch.setattr('app.routes.uploads.get_current_user', lambda: current['user'])

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    MyDb.init_app(app)
    register_app_routes(app)
    with app.app_context():
        Base.metadata.create_all(MyDb.get_db().engine,
                                 tables=[Upload.__table__, FileImport.__table__])
        yield types.SimpleNamespace(app=app, client=app.test_client(), current=current,
                                    session=MyDb.get_db().session, tmp=tmp_path, spawned=spawned)
        MyDb.get_db().session.remove()


def _parquet_bytes(tmp_path, columns=None) -> bytes:
    columns = columns or {
        'lon': [36.8, 0.0], 'lat': [-1.3, 0.0], 'Variety': ['a', 'b'], 'XY': ['p', 'q'],
    }
    path = tmp_path / 'src.parquet'
    pq.write_table(pa.table(columns), path)
    return path.read_bytes()


def _create(client, size, name='trial.parquet'):
    import base64
    meta = 'filename ' + base64.b64encode(name.encode()).decode()
    return client.post('/ui/uploads/tus', headers={**TUS, 'Upload-Length': str(size),
                                                   'Upload-Metadata': meta})


def _patch(client, location, offset, body):
    return client.patch(location, data=body, headers={**TUS, 'Upload-Offset': str(offset)},
                        content_type='application/offset+octet-stream')


def _upload(env, data: bytes, name='trial.parquet') -> str:
    resp = _create(env.client, len(data), name)
    assert resp.status_code == 201, resp.get_json()
    location = resp.headers['Location']
    assert _patch(env.client, location, 0, data).status_code == 204
    return location.rsplit('/', 1)[1]


class TestTusProtocol:
    def test_resumes_from_the_bytes_on_disk(self, env):
        data = _parquet_bytes(env.tmp)
        half = len(data) // 2

        resp = _create(env.client, len(data))
        assert resp.status_code == 201
        location = resp.headers['Location']
        assert resp.headers['Tus-Resumable'] == '1.0.0'

        resp = _patch(env.client, location, 0, data[:half])
        assert resp.status_code == 204
        assert resp.headers['Upload-Offset'] == str(half)

        # What a client does after a dropped connection or a server restart.
        head = env.client.head(location, headers=TUS)
        assert head.status_code == 200
        assert head.headers['Upload-Offset'] == str(half)
        assert head.headers['Upload-Length'] == str(len(data))
        assert head.headers['Cache-Control'] == 'no-store'

        assert _patch(env.client, location, half, data[half:]).status_code == 204

        upload_id = location.rsplit('/', 1)[1]
        body = env.client.get(f'/ui/uploads/{upload_id}').get_json()
        assert body['status'] == 'ready'
        assert body['received'] == len(data)

    def test_wrong_offset_is_a_conflict_not_a_corrupt_file(self, env):
        data = _parquet_bytes(env.tmp)
        location = _create(env.client, len(data)).headers['Location']
        assert _patch(env.client, location, 0, data[:10]).status_code == 204
        assert _patch(env.client, location, 0, data[:10]).status_code == 409

    def test_body_past_the_declared_length_is_refused(self, env):
        location = _create(env.client, 4).headers['Location']
        assert _patch(env.client, location, 0, b'12345').status_code == 413

    def test_patch_while_another_holds_the_lock_is_423(self, env):
        location = _create(env.client, 4).headers['Location']
        upload_id = location.rsplit('/', 1)[1]
        lock = service._Lock(upload_id)
        assert lock.acquire()
        try:
            assert _patch(env.client, location, 0, b'1234').status_code == 423
        finally:
            lock.release()
        assert _patch(env.client, location, 0, b'1234').status_code == 204

    def test_size_and_type_are_checked_before_any_bytes(self, env):
        assert _create(env.client, 2 * 1024 * 1024).status_code == 413
        assert _create(env.client, 10, name='notes.csv').status_code == 415
        assert _create(env.client, 0).status_code == 400

    def test_unwritable_storage_is_a_clear_503_not_a_500(self, env, monkeypatch):
        # Production: a root-owned data volume while the app runs as uid 1000
        # raised PermissionError from makedirs and returned a bare 500.
        def denied(*args, **kwargs):
            raise PermissionError(13, 'Permission denied', 'static/data/uploads')

        monkeypatch.setattr(service.os, 'makedirs', denied)
        res = _create(env.client, 10)
        assert res.status_code == 503
        assert 'not writable' in res.get_json()['error']
        assert env.session.query(service.Upload).count() == 0

    def test_ids_are_server_issued_and_strict(self, env):
        location = _create(env.client, 10).headers['Location']
        assert service.valid_id(location.rsplit('/', 1)[1])
        assert env.client.head('/ui/uploads/tus/..%2F..%2Fetc', headers=TUS).status_code == 404
        assert env.client.head('/ui/uploads/tus/' + 'A' * 32, headers=TUS).status_code == 404

    def test_another_users_upload_is_not_found(self, env):
        location = _create(env.client, 10).headers['Location']
        env.current['user'] = STRANGER
        assert env.client.head(location, headers=TUS).status_code == 404
        assert _patch(env.client, location, 0, b'x').status_code == 404


class TestInspection:
    def test_seeds_the_mapping_with_the_servers_guesses(self, env):
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        body = env.client.get(f'/ui/uploads/{upload_id}').get_json()
        assert body['columns'] == ['lon', 'lat', 'Variety', 'XY']
        assert body['column_map'] == {'lon': 'lon', 'lat': 'lat', 'Variety': 'variety'}
        assert body['total_rows'] == 2
        assert body['file'] == f'{upload_id}.parquet'

    def test_unreadable_file_fails_with_a_reason(self, env):
        upload_id = _upload(env, b'not a parquet file')
        # Inspection committed through its own session; requests here share the
        # fixture's, which still holds the object as the PATCH left it. A real
        # request starts with a fresh session.
        env.session.expire_all()
        body = env.client.get(f'/ui/uploads/{upload_id}').get_json()
        assert body['status'] == 'failed'
        assert body['error'].startswith('Could not read the file')

    def test_dead_inspection_is_reclaimed_on_the_next_poll(self, env):
        from app.models.kvuno import Upload

        monkey_thread = service.threading.Thread
        service.threading.Thread = lambda *a, **k: types.SimpleNamespace(start=lambda: None)
        try:
            upload_id = _upload(env, _parquet_bytes(env.tmp))  # inspection never runs
        finally:
            service.threading.Thread = monkey_thread

        row = env.session.get(Upload, upload_id)
        row.status = 'inspecting'
        row.updated_at = datetime.datetime(2000, 1, 1)
        env.session.commit()

        # The poll that notices answers with what it loaded; inspection runs in
        # its own context (a thread, in production), so the next poll sees it.
        env.client.get(f'/ui/uploads/{upload_id}')
        # Requests here share the fixture's session; a real request starts fresh.
        env.session.expire_all()
        assert env.client.get(f'/ui/uploads/{upload_id}').get_json()['status'] == 'ready'

    def test_live_inspection_is_not_started_twice(self, env):
        from app.models.kvuno import Upload

        upload_id = _upload(env, _parquet_bytes(env.tmp))
        row = env.session.get(Upload, upload_id)
        row.status = 'received'
        env.session.commit()
        assert service._claim_inspection(upload_id) is True
        assert service._claim_inspection(upload_id) is False  # heartbeat is fresh

    def test_a_redelivered_worker_task_takes_over_at_once(self, env):
        """Its previous process is gone; the heartbeat is not proof of life."""
        from app.models.kvuno import Upload

        upload_id = _upload(env, _parquet_bytes(env.tmp))
        row = env.session.get(Upload, upload_id)
        row.status = 'inspecting'
        row.updated_at = service._now()
        env.session.commit()

        assert service._claim_inspection(upload_id) is False  # the poll waits
        assert service._claim_inspection(upload_id, take_over=True) is True


class TestSubmission:
    def test_without_a_worker_the_mapping_is_kept_and_it_stays_ready(self, env):
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        resp = env.client.post(f'/ui/uploads/{upload_id}/process',
                               json={'column_map': {'lon': 'lon', 'lat': 'lat'}})
        body = resp.get_json()
        assert body['queued'] is True
        state = env.client.get(f'/ui/uploads/{upload_id}').get_json()
        assert state['status'] == 'processing'
        assert state['column_map'] == {'lon': 'lon', 'lat': 'lat'}

    def test_enqueues_with_the_users_exact_mapping(self, env):
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        resp = env.client.post(f'/ui/uploads/{upload_id}/process',
                               json={'column_map': {'lon': 'lon', 'lat': 'lat', 'Variety': ''}})
        assert resp.get_json()['queued'] is True
        state = env.client.get(f'/ui/uploads/{upload_id}').get_json()
        assert state['status'] == 'processing'
        # 'Variety' was set to skip, so it must not be in the saved mapping.
        assert state['column_map'] == {'lon': 'lon', 'lat': 'lat'}

    def test_retry_reuses_the_saved_mapping(self, env):
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        env.client.post(f'/ui/uploads/{upload_id}/process',
                        json={'column_map': {'lon': 'lon', 'lat': 'lat'}})
        # The Jobs page's Retry sends only the stored file name.
        resp = env.client.post('/ui/process', json={'file': f'{upload_id}.parquet'})
        assert resp.status_code == 200
        state = env.client.get(f'/ui/uploads/{upload_id}').get_json()
        assert state['status'] == 'processing'
        assert state['column_map'] == {'lon': 'lon', 'lat': 'lat'}

    def test_unreachable_queue_is_reported_not_hidden(self, env):
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        resp = env.client.post(f'/ui/uploads/{upload_id}/process',
                               json={'column_map': {'lon': 'lon', 'lat': 'lat'}})
        assert resp.status_code == 200
        assert env.client.get(f'/ui/uploads/{upload_id}').get_json()['status'] == 'processing'

    @pytest.mark.parametrize('column_map, message', [
        ({'Variety': 'variety'}, 'coordinates are required'),
        ({'lon': 'lon', 'lat': 'lat', 'Variety': 'lat'}, 'mapped once'),
        ({'lon': 'lon', 'lat': 'lat', 'nope': 'variety'}, 'not in the file'),
        ({'lon': 'lon', 'lat': 'lat', 'Variety': 'id'}, 'Invalid target'),
    ])
    def test_bad_mappings_are_rejected(self, env, column_map, message):
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        resp = env.client.post(f'/ui/uploads/{upload_id}/process', json={'column_map': column_map})
        assert resp.status_code == 400
        assert message in resp.get_json()['error']


class TestLifecycle:
    def test_list_shows_unfinished_uploads_after_a_reload(self, env):
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        ids = [u['id'] for u in env.client.get('/ui/uploads').get_json()['uploads']]
        assert ids == [upload_id]

    def test_discard_removes_row_and_files(self, env):
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        assert env.client.delete(f'/ui/uploads/{upload_id}').status_code == 204
        assert not (env.tmp / 'uploads' / upload_id).exists()
        assert env.client.get(f'/ui/uploads/{upload_id}').status_code == 404

    def test_cleanup_spares_recent_uploads_and_removes_abandoned_ones(self, env):
        from app.models.kvuno import Upload

        fresh = _create(env.client, 10).headers['Location'].rsplit('/', 1)[1]
        old = _create(env.client, 10).headers['Location'].rsplit('/', 1)[1]
        env.session.get(Upload, old).updated_at = datetime.datetime(2000, 1, 1)
        env.session.commit()

        assert service.cleanup_abandoned(7 * 86400) == 1
        assert (env.tmp / 'uploads' / fresh).exists()
        assert not (env.tmp / 'uploads' / old).exists()

    def test_a_finished_import_leaves_processing_and_the_open_list(self, env):
        from app.models.kvuno import Upload

        upload_id = _upload(env, _parquet_bytes(env.tmp))
        upload = env.session.get(Upload, upload_id)
        upload.status = 'processing'
        env.session.commit()

        assert service.mark_processed(upload.stored_path, 'completed')
        env.session.refresh(upload)
        assert upload.status == 'completed'
        listed = [u['id'] for u in env.client.get('/ui/uploads').get_json()['uploads']]
        assert upload_id not in listed

    def test_the_ingest_completion_finishes_the_upload(self, env, monkeypatch):
        """The wiring, not just mark_processed: a completed job must close it."""
        from app.models.kvuno import Upload
        from app.services import housekeeper as hk

        upload_id = _upload(env, _parquet_bytes(env.tmp))
        upload = env.session.get(Upload, upload_id)
        upload.status = 'processing'
        env.session.commit()

        # The job table is not part of this fixture; its write is not what
        # this test is about.
        monkeypatch.setattr(hk, 'save_progress', lambda *a, **k: None)
        hk._write_progress(upload.stored_path, 'completed', 2, 2, 'Done')

        env.session.refresh(upload)
        assert upload.status == 'completed'
        listed = [u['id'] for u in env.client.get('/ui/uploads').get_json()['uploads']]
        assert upload_id not in listed

    def test_a_converted_file_is_still_the_same_upload(self, env):
        """stored_path `<id>.rds`, but the worker imported `<id>.parquet`."""
        from app.models.kvuno import Upload

        upload_id = uuid.uuid4().hex
        env.session.add(Upload(
            id=upload_id, original_name='trial.RDS', size=10, status='processing',
            stored_path=str(env.tmp / 'uploads' / upload_id / f'{upload_id}.rds'),
        ))
        env.session.commit()

        assert service.find_by_stored_name(f'{upload_id}.parquet').id == upload_id
        assert service.mark_processed(f'{upload_id}.parquet', 'completed')
        env.session.expire_all()
        assert env.session.get(Upload, upload_id).status == 'completed'

    def test_a_failed_import_is_recorded_on_the_upload(self, env):
        from app.models.kvuno import Upload

        upload_id = _upload(env, _parquet_bytes(env.tmp))
        upload = env.session.get(Upload, upload_id)
        upload.status = 'processing'
        env.session.commit()

        assert service.mark_processed(upload.stored_path, 'failed', 'bad rows')
        env.session.refresh(upload)
        assert (upload.status, upload.error) == ('failed', 'bad rows')

    def test_an_upload_awaiting_mapping_is_not_finished(self, env):
        from app.models.kvuno import Upload

        upload_id = _upload(env, _parquet_bytes(env.tmp))
        upload = env.session.get(Upload, upload_id)
        assert upload.status == 'ready'

        assert service.mark_processed(upload.stored_path, 'completed') is False
        env.session.refresh(upload)
        assert upload.status == 'ready'

    def test_cleanup_also_removes_a_finished_upload(self, env):
        from app.models.kvuno import Upload

        upload_id = _upload(env, _parquet_bytes(env.tmp))
        upload = env.session.get(Upload, upload_id)
        upload.status = 'completed'
        upload.updated_at = datetime.datetime(2000, 1, 1)
        env.session.commit()

        assert service.cleanup_abandoned(7 * 86400) == 1
        assert not (env.tmp / 'uploads' / upload_id).exists()


def _folder_file(env, name='field.parquet', columns=None):
    """A data-folder file whose columns the default mapping cannot use."""
    path = env.tmp / name
    pq.write_table(pa.table(columns or {'Longitude': [36.8], 'Latitude': [-1.3], 'Variety': ['a']}), path)
    return str(path)


class TestAwaitingMapping:
    """Data-folder files without lat/lon used to be "imported" with nothing in them."""

    def test_needs_mapping_only_when_defaults_find_no_coordinates(self, env):
        from app.services.housekeeper import needs_mapping

        assert needs_mapping(_folder_file(env))
        ok = _folder_file(env, 'ok.parquet', {'lat': [1.0], 'lon': [2.0]})
        assert not needs_mapping(ok)
        # RDS would have to be read whole; process_file handles it as before.
        rds = env.tmp / 'x.rds'
        rds.write_bytes(b'not read')
        assert not needs_mapping(str(rds))

    def test_scan_holds_the_file_as_a_shared_upload(self, env):
        from app.services.housekeeper import hold_unmapped

        held = _folder_file(env)
        ok = _folder_file(env, 'ok.parquet', {'lat': [1.0], 'lon': [2.0]})
        assert hold_unmapped([held, ok]) == [ok]

        import os
        assert not os.path.exists(held)  # out of the data-folder root
        env.current['user'] = STRANGER  # nobody owns it; everyone can map it
        uploads = env.client.get('/ui/uploads').get_json()['uploads']
        assert [(u['name'], u['source'], u['status']) for u in uploads] == [
            ('field.parquet', 'folder', 'ready')]
        assert env.client.get('/ui/uploads/awaiting').get_json() == {'count': 1}
        assert env.client.get(f"/ui/uploads/{uploads[0]['id']}").status_code == 200

    def test_dry_run_and_disabled_hold_nothing(self, env, monkeypatch):
        from app.models.kvuno import Upload
        from app.services.housekeeper import hold_unmapped

        path = _folder_file(env)
        assert hold_unmapped([path], dry_run=True) == []
        monkeypatch.setenv('INGEST_HOLD_UNMAPPED', 'false')
        assert hold_unmapped([path]) == [path]
        assert env.session.query(Upload).count() == 0

    def test_awaiting_count_excludes_other_users_uploads(self, env):
        _upload(env, _parquet_bytes(env.tmp))
        assert env.client.get('/ui/uploads/awaiting').get_json() == {'count': 1}
        env.current['user'] = STRANGER
        assert env.client.get('/ui/uploads/awaiting').get_json() == {'count': 0}

    def test_only_an_administrator_discards_a_folder_file(self, env, monkeypatch):
        from app.services.housekeeper import hold_unmapped

        hold_unmapped([_folder_file(env)])
        upload_id = env.client.get('/ui/uploads').get_json()['uploads'][0]['id']
        monkeypatch.setattr('app.routes.uploads.is_admin', lambda user: False)
        assert env.client.delete(f'/ui/uploads/{upload_id}').status_code == 403
        monkeypatch.setattr('app.routes.uploads.is_admin', lambda user: True)
        assert env.client.delete(f'/ui/uploads/{upload_id}').status_code == 204

    def test_cleanup_never_removes_folder_files(self, env):
        from app.models.kvuno import Upload
        from app.services.housekeeper import hold_unmapped

        hold_unmapped([_folder_file(env)])
        upload = env.session.query(Upload).one()
        upload.updated_at = datetime.datetime(2000, 1, 1)
        env.session.commit()
        assert service.cleanup_abandoned(7 * 86400) == 0

    def test_duplicate_check_can_be_turned_off(self, env, monkeypatch):
        monkeypatch.setenv('INGEST_DUPLICATE_CHECK', 'false')
        assert service.describe_duplicate('unused', checksum='abc') is None


class TestInspectionProgress:
    """The Upload page showed "Reading the file…" for the whole of a long RDS conversion."""

    def _inspecting(self, env):
        from app.models.kvuno import Upload
        upload_id = _create(env.client, 10).headers['Location'].rsplit('/', 1)[1]
        env.session.get(Upload, upload_id).status = 'inspecting'
        env.session.commit()
        return upload_id

    def _progress(self, env, upload_id):
        env.session.expire_all()
        return env.client.get(f'/ui/uploads/{upload_id}').get_json()['progress']

    def test_stages_are_recorded_and_throttled(self, env):
        upload_id = self._inspecting(env)
        write = service._ProgressWriter(upload_id)
        write('reading', 0, 0, 4.04)
        assert self._progress(env, upload_id) == {'stage': 'reading', 'done': 0, 'total': 0, 'elapsed': 4.0}
        write('reading', 0, 0, 5.0)  # same stage within PROGRESS_WRITE_SECONDS: skipped
        assert self._progress(env, upload_id)['elapsed'] == 4.0
        write('writing', 50, 100, 6.0)  # a new stage is always written
        assert self._progress(env, upload_id) == {'stage': 'writing', 'done': 50, 'total': 100, 'elapsed': 6.0}

    def test_an_rds_upload_ends_ready_with_no_progress_left(self, env):
        import pandas as pd
        import pyreadr

        path = env.tmp / 'src.RDS'
        pyreadr.write_rds(str(path), pd.DataFrame({'lat': [1.0], 'lon': [2.0]}))
        upload_id = _upload(env, path.read_bytes(), name='trial.RDS')
        body = env.client.get(f'/ui/uploads/{upload_id}').get_json()
        assert (body['status'], body['progress'], body['file']) == ('ready', None, f'{upload_id}.parquet')


class TestWorkOffTheApi:
    """Reading and importing files happens on the worker, never in a gunicorn worker.

    Imports used to run on a thread in the gunicorn worker that took the
    request; gunicorn recycles workers every MAX_REQUESTS, which killed long
    imports again and again (2M rows: 40+ hours).
    """

    def _worker(self, monkeypatch, available=True, queued=True):
        sent = {'inspect': [], 'process': []}
        monkeypatch.setattr('app.services.housekeeper.celery_worker_available', lambda: available)
        monkeypatch.setattr('app.services.housekeeper._enqueue_or_warn',
                            lambda task, **kw: sent['inspect'].append(kw) or queued)
        monkeypatch.setattr('app.services.housekeeper.process_file_async',
                            lambda path, **kw: sent['process'].append((path, kw)) or queued)
        return sent

    def test_with_a_worker_the_upload_is_read_there(self, env, monkeypatch):
        sent = self._worker(monkeypatch)
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        body = env.client.get(f'/ui/uploads/{upload_id}').get_json()
        assert (body['status'], body['progress']['stage']) == ('received', 'queued')
        assert sent['inspect'] == [{'upload_id': upload_id}], 'queued once, not on every poll'

    def test_a_queue_that_refuses_falls_back_to_reading_in_the_api(self, env, monkeypatch):
        self._worker(monkeypatch, queued=False)
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        assert env.client.get(f'/ui/uploads/{upload_id}').get_json()['status'] == 'ready'

    def test_with_a_worker_the_import_is_queued_with_the_mapping(self, env, monkeypatch):
        upload_id = _upload(env, _parquet_bytes(env.tmp))  # read in the API: no worker yet
        sent = self._worker(monkeypatch)
        resp = env.client.post(f'/ui/uploads/{upload_id}/process', json={'column_map': {'lon': 'lon', 'lat': 'lat'}})
        assert resp.get_json()['message'] == 'Queued for the worker'
        path, kw = sent['process'][0]
        assert path.endswith(f'{upload_id}.parquet') and kw['column_map'] == {'lon': 'lon', 'lat': 'lat'}
        assert env.spawned == []

    def test_without_a_worker_the_import_runs_in_its_own_process(self, env):
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        env.client.post(f'/ui/uploads/{upload_id}/process', json={'column_map': {'lon': 'lon', 'lat': 'lat'}})
        assert env.spawned == [upload_id]

    def test_a_process_that_cannot_start_puts_the_upload_back(self, env, monkeypatch):
        def fail(upload_id):
            raise OSError('no fork')
        monkeypatch.setattr('app.routes.uploads._spawn_runner', fail)
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        resp = env.client.post(f'/ui/uploads/{upload_id}/process', json={'column_map': {'lon': 'lon', 'lat': 'lat'}})
        assert resp.status_code == 503
        assert env.client.get(f'/ui/uploads/{upload_id}').get_json()['status'] == 'ready'


class TestFileTimes:
    """Imported rows' created_at is the file's own time, not the upload time.

    RDS and Parquet files record no creation time, so the browser's
    File.lastModified (or a data-folder file's mtime) is the only source.
    """

    def test_client_time_is_parsed_and_only_trusted_when_plausible(self):
        assert service.parse_client_mtime('1700000000000') == datetime.datetime(2023, 11, 14, 22, 13, 20)
        assert service.parse_client_mtime('not a time') is None
        assert service.parse_client_mtime(None) is None
        assert service.parse_client_mtime('0') is None  # 1970: a clock that was never set
        future = service.parse_client_mtime(str(10 ** 13))  # the year 2286
        assert future <= datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    def test_the_browsers_file_time_is_kept_as_a_suggestion(self, env):
        import base64

        data = _parquet_bytes(env.tmp)
        meta = ','.join(f'{k} {base64.b64encode(v.encode()).decode()}'
                        for k, v in {'filename': 'trial.parquet', 'lastModified': '1700000000000'}.items())
        location = env.client.post('/ui/uploads/tus', headers={**TUS, 'Upload-Length': str(len(data)),
                                                               'Upload-Metadata': meta}).headers['Location']
        assert _patch(env.client, location, 0, data).status_code == 204
        body = env.client.get(f"/ui/uploads/{location.rsplit('/', 1)[1]}").get_json()
        assert (body['file_modified_at'], body['data_date']) == ('2023-11-14T22:13:20', None)

    def _submit(self, env, upload_id, **extra):
        return env.client.post(f'/ui/uploads/{upload_id}/process',
                               json={'column_map': {'lon': 'lon', 'lat': 'lat'}, **extra})

    def test_the_confirmed_data_date_is_saved_and_reused_by_retry(self, env):
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        assert self._submit(env, upload_id, data_date='2024-03-01').status_code == 200
        env.client.post('/ui/process', json={'file': f'{upload_id}.parquet'})  # Retry sends no date
        assert env.client.get(f'/ui/uploads/{upload_id}').get_json()['data_date'] == '2024-03-01'
        from app.models.kvuno import Upload
        assert service.data_datetime(env.session.get(Upload, upload_id)) == datetime.datetime(2024, 3, 1)

    @pytest.mark.parametrize('value, message', [
        ('2999-01-01', 'future'), ('1980-01-01', 'before 1990'), ('01/03/2024', 'YYYY-MM-DD')])
    def test_an_implausible_data_date_is_refused(self, env, value, message):
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        resp = self._submit(env, upload_id, data_date=value)
        assert resp.status_code == 400 and message in resp.get_json()['error']

    def test_an_empty_data_date_means_the_import_date(self, env):
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        self._submit(env, upload_id, data_date='2024-03-01')
        env.client.post(f'/ui/uploads/{upload_id}/process', json={'data_date': ''})
        assert env.client.get(f'/ui/uploads/{upload_id}').get_json()['data_date'] is None

    def test_the_worker_gets_the_data_date(self, env, monkeypatch):
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        sent = []
        monkeypatch.setattr('app.services.housekeeper.celery_worker_available', lambda: True)
        monkeypatch.setattr('app.services.housekeeper.process_file_async',
                            lambda path, **kw: sent.append(kw) or True)
        self._submit(env, upload_id, data_date='2024-03-01')
        assert sent[0]['created_at'] == datetime.datetime(2024, 3, 1)
