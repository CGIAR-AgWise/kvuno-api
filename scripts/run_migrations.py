"""Standalone migration runner.

Run before starting the application in production:
    python scripts/run_migrations.py

This script runs pending Alembic migrations and exits.
It does not start the web server.
"""
import os
import sys
from dotenv import load_dotenv

# Ensure the project root is on sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    # noinspection PyTypeChecker
    sys.path.insert(0, _project_root)



load_dotenv()

from app.__init__ import run_migrations  # noqa: E402
from app.models.database_conn import MyDb  # noqa: E402


def main():
    print("Running database migrations...")
    try:
        # Create a minimal Flask app for DB context
        from flask import Flask
        app = Flask(__name__)
        from app.config import build_db_url
        app.config['SQLALCHEMY_DATABASE_URI'] = build_db_url()
        MyDb.init_app(app)

        with app.app_context():
            run_migrations()
        print("Migrations complete.")
    except Exception as e:
        print(f"Migration failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
