"""`/health` stays a cheap database probe; `?deep=1` adds the ingest stack."""
import types

import pytest
from flask import Flask


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.routes.main import register_app_routes

    monkeypatch.setenv('HOUSEKEEPING_DATA_DIR', str(tmp_path))
    (tmp_path / 'uploads').mkdir()
    monkeypatch.setattr('app.routes.main.MyDb.check_db_connection', lambda: True)
    app = Flask(__name__)
    register_app_routes(app)
    return types.SimpleNamespace(client=app.test_client(), tmp=tmp_path, monkeypatch=monkeypatch)


def test_the_default_stays_a_database_only_probe(env):
    body = env.client.get('/health').get_json()
    assert body['status'] == 'healthy'
    assert set(body) == {'status', 'name', 'version', 'database'}


def test_deep_reports_disabled_services_as_such(env):
    """Housekeeping off means Redis is unused, not down."""
    body = env.client.get('/health?deep=1').get_json()
    assert body['housekeeping'] is False
    assert body['redis'] == body['worker'] == 'DISABLED'
    assert body['data_dir'] == 'UP'
    assert body['status'] == 'healthy'


def test_deep_degrades_when_the_worker_stack_is_down(env):
    env.monkeypatch.setenv('HOUSEKEEPING_ENABLED', 'true')
    env.monkeypatch.setattr('app.config.celery_broker_available', lambda: False)
    env.monkeypatch.setattr('app.services.housekeeper.celery_worker_available', lambda: False)

    body = env.client.get('/health?deep=1').get_json()
    assert body['redis'] == body['worker'] == 'DOWN'
    assert body['status'] == 'degraded'


def test_deep_flags_a_missing_data_dir(env):
    (env.tmp / 'uploads').rmdir()
    body = env.client.get('/health?deep=1').get_json()
    assert body['data_dir'] == 'MISSING'
    assert body['status'] == 'degraded'


def test_a_database_outage_is_unhealthy_and_500(env):
    env.monkeypatch.setattr('app.routes.main.MyDb.check_db_connection', lambda: False)
    resp = env.client.get('/health?deep=1')
    assert resp.status_code == 500
    assert resp.get_json()['status'] == 'unhealthy'
