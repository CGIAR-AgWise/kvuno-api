import os

from dotenv import load_dotenv
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

load_dotenv(verbose=True)


class MyDb:
    db = None
    db_url: str = os.getenv("DB_URL")
    debug_db = os.getenv('DEBUG_DB', 'false').lower() == 'true'

    @classmethod
    def init_app(cls, app: Flask):
        cls.db = SQLAlchemy(app)

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
