"""A dead pooled connection no longer fails a job with
"Can't reconnect until invalid transaction is rolled back".

The engines had no pool_pre_ping, so a connection that died while idle was
handed out as live; the first query failed and retry_db retried on the same,
now-invalid session, so every attempt failed the same way.
"""
from unittest.mock import MagicMock

import pytest
from flask import Flask
from sqlalchemy.exc import OperationalError

from app.services import housekeeper as hk


def _dead_connection():
    return OperationalError('SELECT 1', {}, Exception('server closed the connection unexpectedly'))


def test_engines_ping_and_recycle_connections():
    from app.models.database_conn import MyDb

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    MyDb.init_app(app)
    options = app.config['SQLALCHEMY_ENGINE_OPTIONS']
    assert options['pool_pre_ping'] is True
    assert options['pool_recycle'] > 0
    with app.app_context():
        assert MyDb.get_db().engine.pool._pre_ping is True


def test_explicit_engine_options_are_kept():
    from app.models.database_conn import MyDb

    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_recycle': 60}
    MyDb.init_app(app)
    assert app.config['SQLALCHEMY_ENGINE_OPTIONS'] == {'pool_recycle': 60, 'pool_pre_ping': True}


def test_retry_rolls_the_session_back_before_trying_again(monkeypatch):
    monkeypatch.setattr(hk.time, 'sleep', lambda s: None)
    session = MagicMock()
    calls = []

    def query():
        calls.append(session.rollback.call_count)
        if len(calls) == 1:
            raise _dead_connection()
        return 'row'

    assert hk.retry_db(query, session=session) == 'row'
    assert calls == [0, 1], 'the second attempt must run on a rolled-back session'


def test_without_a_session_nothing_is_rolled_back(monkeypatch):
    """Mid-ingest: a rollback would drop rows written since the last checkpoint."""
    monkeypatch.setattr(hk.time, 'sleep', lambda s: None)

    def query():
        raise _dead_connection()

    with pytest.raises(OperationalError):
        hk.retry_db(query, attempts=2)
