"""A minimal Flask app for processes that import files outside gunicorn.

The Celery worker (`app/tasks.py`) and the upload runner
(`app/services/ingest_runner.py`) need an app context for the database, but
not routes, the startup scan or the preflight that `create_app()` brings.
"""
from flask import Flask

from app.config import build_db_url

_app = None


def build_app() -> Flask:
    """The process's one app (created on first use)."""
    global _app
    if _app is None:
        app = Flask(__name__)
        app.config['SQLALCHEMY_DATABASE_URI'] = build_db_url()
        # DEBUG_DB is handled by app/utils/logging.py (through the shared
        # handler); echo would add SQLAlchemy's own stdout handler and bypass
        # LOG_LEVEL.
        app.config['SQLALCHEMY_ECHO'] = False
        from app.models.database_conn import MyDb
        MyDb.init_app(app)
        _app = app
    return _app
