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
import types

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from flask import Flask

from app.services import uploads as service

OWNER = types.SimpleNamespace(id=1)
STRANGER = types.SimpleNamespace(id=2)
TUS = {'Tus-Resumable': '1.0.0'}


class _InlineThread:
    """Run inspections synchronously; skip the heartbeat, which loops forever."""

    def __init__(self, target=None, args=(), name=None, daemon=None):
        self.target, self.args, self.name = target, args, name

    def start(self):
        if self.name and self.name.startswith('inspect-'):
            self.target(*self.args)


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
                                    session=MyDb.get_db().session, tmp=tmp_path)
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


class TestSubmission:
    def test_without_a_worker_the_mapping_is_kept_and_it_stays_ready(self, env):
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        resp = env.client.post(f'/ui/uploads/{upload_id}/process',
                               json={'column_map': {'lon': 'lon', 'lat': 'lat'}})
        body = resp.get_json()
        assert body['queued'] is False
        state = env.client.get(f'/ui/uploads/{upload_id}').get_json()
        assert state['status'] == 'ready'
        assert state['column_map'] == {'lon': 'lon', 'lat': 'lat'}

    def test_enqueues_with_the_users_exact_mapping(self, env, monkeypatch):
        calls = []
        monkeypatch.setenv('HOUSEKEEPING_ENABLED', 'true')
        monkeypatch.setattr('app.services.housekeeper.process_file_async',
                            lambda **kw: calls.append(kw) or True)

        upload_id = _upload(env, _parquet_bytes(env.tmp))
        resp = env.client.post(f'/ui/uploads/{upload_id}/process',
                               json={'column_map': {'lon': 'lon', 'lat': 'lat', 'Variety': ''}})
        assert resp.get_json()['queued'] is True
        # 'Variety' was set to skip, so it must not be sent at all.
        assert calls[0]['column_map'] == {'lon': 'lon', 'lat': 'lat'}
        assert calls[0]['original_filename'] == 'trial.parquet'

    def test_retry_reuses_the_saved_mapping(self, env, monkeypatch):
        calls = []
        monkeypatch.setenv('HOUSEKEEPING_ENABLED', 'true')
        monkeypatch.setattr('app.services.housekeeper.process_file_async',
                            lambda **kw: calls.append(kw) or True)

        upload_id = _upload(env, _parquet_bytes(env.tmp))
        env.client.post(f'/ui/uploads/{upload_id}/process',
                        json={'column_map': {'lon': 'lon', 'lat': 'lat'}})
        # The Jobs page's Retry sends only the stored file name.
        resp = env.client.post('/ui/process', json={'file': f'{upload_id}.parquet'})
        assert resp.status_code == 200
        assert calls[1]['column_map'] == {'lon': 'lon', 'lat': 'lat'}

    def test_unreachable_queue_is_reported_not_hidden(self, env, monkeypatch):
        monkeypatch.setenv('HOUSEKEEPING_ENABLED', 'true')
        monkeypatch.setattr('app.services.housekeeper.process_file_async', lambda **kw: False)
        upload_id = _upload(env, _parquet_bytes(env.tmp))
        resp = env.client.post(f'/ui/uploads/{upload_id}/process',
                               json={'column_map': {'lon': 'lon', 'lat': 'lat'}})
        assert resp.status_code == 503
        assert env.client.get(f'/ui/uploads/{upload_id}').get_json()['status'] == 'ready'

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
