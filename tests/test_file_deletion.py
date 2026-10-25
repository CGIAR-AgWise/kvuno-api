"""Administrators delete a file with its upload and jobs; imported data stays."""
import datetime
import os
import socket
import types

import pytest
from flask import Flask

from app.services import progress_store as ps

ADMIN = types.SimpleNamespace(id=1)
USER = types.SimpleNamespace(id=2)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.models.database_conn import MyDb
    from app.models.kvuno import Base, FileImport, ProcessingJob, ProcessingRun, Upload
    from app.routes.main import register_app_routes

    monkeypatch.setenv('HOUSEKEEPING_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(ps, '_redis_client', lambda: None)
    current = {'user': ADMIN}
    monkeypatch.setattr('app.auth.get_current_user', lambda: current['user'])
    monkeypatch.setattr('app.api.user.get_current_user', lambda: current['user'])
    monkeypatch.setattr('app.api.user.is_admin', lambda user: user is ADMIN)

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    MyDb.init_app(app)
    register_app_routes(app)
    with app.app_context():
        Base.metadata.create_all(MyDb.get_db().engine, tables=[
            Upload.__table__, FileImport.__table__, ProcessingRun.__table__, ProcessingJob.__table__])
        session = MyDb.get_db().session
        yield types.SimpleNamespace(client=app.test_client(), session=session, tmp=tmp_path, current=current,
                                    Upload=Upload, ProcessingJob=ProcessingJob, ProcessingRun=ProcessingRun,
                                    FileImport=FileImport)
        session.remove()


def _job(env, name, status='completed', run_id=None, minutes_ago=0):
    when = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(minutes=minutes_ago)
    env.session.add(env.ProcessingJob(file_name=name, status=status, run_id=run_id, started_at=when,
                                      updated_at=when, attempts=1))
    env.session.commit()


def _run(env):
    run = env.ProcessingRun(label='r', source='upload', status='running',
                            created_at=datetime.datetime(2026, 1, 1))  # SQLite has no now() default
    env.session.add(run)
    env.session.commit()
    return run.id


def _upload(env, upload_id='a' * 32):
    folder = env.tmp / 'uploads' / upload_id
    folder.mkdir(parents=True)
    stored = folder / f'{upload_id}.parquet'
    stored.write_bytes(b'x')
    env.session.add(env.Upload(id=upload_id, original_name='trial.parquet', size=1, status='processing',
                               stored_path=str(stored)))
    env.session.commit()
    return upload_id, stored


def test_only_administrators_may_delete(env):
    (env.tmp / 'f.parquet').write_bytes(b'x')
    env.current['user'] = USER
    assert env.client.delete('/ui/jobs/f.parquet').status_code == 403
    env.current['user'] = None
    assert env.client.delete('/ui/jobs/f.parquet').status_code == 401
    assert (env.tmp / 'f.parquet').exists()


def test_an_upload_goes_with_its_files_jobs_and_empty_runs_but_not_its_data(env):
    upload_id, stored = _upload(env)
    name = stored.name
    mine, shared = _run(env), _run(env)
    _job(env, name, run_id=mine)
    _job(env, name, status='error', run_id=shared)
    _job(env, 'other.parquet', run_id=shared)
    env.session.add(env.FileImport(id=1, check_sum='sum', file_name=name, offset=None,  # BIGINT: no SQLite rowid
                                   processed_at=datetime.datetime(2026, 1, 1)))
    env.session.commit()

    resp = env.client.delete(f'/ui/jobs/{name}')

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json() == {'file': name, 'file_deleted': True, 'upload_deleted': True, 'jobs_deleted': 2}
    assert not (env.tmp / 'uploads' / upload_id).exists()
    assert env.session.get(env.Upload, upload_id) is None
    assert [j.file_name for j in env.session.query(env.ProcessingJob)] == ['other.parquet']
    assert [r.id for r in env.session.query(env.ProcessingRun)] == [shared], 'only the emptied run goes'
    assert env.session.query(env.FileImport).count() == 1, 'a re-upload is still known as imported'


def test_a_data_folder_file_goes_with_its_mapping_sidecar(env):
    (env.tmp / 'scan.parquet').write_bytes(b'x')
    (env.tmp / 'scan.parquet.map.json').write_text('{}')
    _job(env, 'scan.parquet', status='stale', minutes_ago=90)
    resp = env.client.delete('/ui/jobs/scan.parquet')
    assert resp.status_code == 200 and resp.get_json()['jobs_deleted'] == 1
    assert sorted(os.listdir(env.tmp)) == []


def test_a_job_whose_file_is_gone_can_still_be_cleared(env):
    """A finished RDS conversion: the RDS was replaced by its Parquet."""
    _job(env, 'field.RDS')
    resp = env.client.delete('/ui/jobs/field.RDS')
    assert resp.status_code == 200
    assert resp.get_json()['file_deleted'] is False and resp.get_json()['jobs_deleted'] == 1


@pytest.mark.parametrize('status', ['processing', 'converting'])
def test_a_file_being_processed_is_refused(env, status):
    (env.tmp / 'busy.parquet').write_bytes(b'x')
    _job(env, 'busy.parquet', status=status)
    assert env.client.delete('/ui/jobs/busy.parquet').status_code == 409
    assert (env.tmp / 'busy.parquet').exists()
    assert env.session.query(env.ProcessingJob).count() == 1


def test_a_file_a_live_worker_holds_is_refused(env):
    (env.tmp / 'held.parquet').write_bytes(b'x')
    _job(env, 'held.parquet')
    (env.tmp / 'held.parquet.ingest.lock').write_text(f'{socket.gethostname()} {os.getppid()}\n')
    assert env.client.delete('/ui/jobs/held.parquet').status_code == 409
    assert (env.tmp / 'held.parquet').exists()


def test_an_unknown_file_is_not_found(env):
    assert env.client.delete('/ui/jobs/nothing.parquet').status_code == 404
    assert env.client.delete('/ui/jobs/..').status_code in (400, 404)
