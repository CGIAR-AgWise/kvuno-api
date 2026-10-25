from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from sqlalchemy.exc import OperationalError


class MyDb:
    # ONE extension instance, created without an app and then bound per app.
    #
    # The previous version did `cls.db = SQLAlchemy(app)` on every call, which
    # silently replaced the binding. The worker (app/tasks.py) builds its own
    # Flask app, so a process that touched both the API app and the worker app
    # ended up with the first app's sessions orphaned — surfacing as the
    # confusing "The current Flask app is not registered with this
    # 'SQLAlchemy' instance" RuntimeError partway through a job.
    #
    # flask-sqlalchemy 3.x supports exactly this pattern: one SQLAlchemy object
    # init_app()'d to several apps, each getting its own engine and session
    # scope keyed by app context.
    db = SQLAlchemy()

    # Every app (the API and the worker's own) gets these. Without
    # pool_pre_ping a pooled connection that died while idle — database
    # restart, idle timeout, network blip — was handed out as live; the first
    # query failed, and reusing that session gave "Can't reconnect until
    # invalid transaction is rolled back". pool_recycle retires connections
    # before typical server/proxy idle timeouts close them.
    ENGINE_OPTIONS = {'pool_pre_ping': True, 'pool_recycle': 1800}

    @classmethod
    def init_app(cls, app: Flask):
        options = app.config.setdefault('SQLALCHEMY_ENGINE_OPTIONS', {})
        for key, value in cls.ENGINE_OPTIONS.items():
            options.setdefault(key, value)
        cls.db.init_app(app)

    @classmethod
    def get_db(cls):
        if cls.db is None:
            raise RuntimeError("Database is not initialized. Call init_app() with a Flask app first.")
        return cls.db

    @classmethod
    def check_db_connection(cls):
        if cls.db is None:
            raise RuntimeError("Database is not initialized.")

        try:
            # Execute a simple query to check the database connection

            connection = cls.db.session()
            connection.execute(text("SELECT 1"))
            connection.close()
            return True
        except OperationalError:
            return False
