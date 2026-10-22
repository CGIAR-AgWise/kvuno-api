from dotenv import load_dotenv

from app.celery_app import celery_app
from app.config import build_db_url, CELERY_TASK_MAX_RETRIES, CELERY_TASK_RETRY_DELAY

load_dotenv()

_worker_app = None


def _get_worker_app():
    global _worker_app
    if _worker_app is None:
        import os
        from flask import Flask
        app = Flask(__name__)
        app.config['SQLALCHEMY_DATABASE_URI'] = build_db_url()
        app.config['SQLALCHEMY_ECHO'] = os.getenv('DEBUG_DB', 'false').lower() == 'true'
        from app.models.database_conn import MyDb
        MyDb.init_app(app)
        _worker_app = app
    return _worker_app


@celery_app.task(bind=True, max_retries=CELERY_TASK_MAX_RETRIES, default_retry_delay=CELERY_TASK_RETRY_DELAY,
                 autoretry_for=(Exception,))
def process_file_task(self, file_path: str, column_map: dict | None = None,
                      original_filename: str | None = None, run_id: int | None = None):
    # run_id is optional so tasks queued by an older API (without it) still run.
    # process_file only raises for transient DB errors — a bad file is recorded
    # as an error and returns — so autoretry_for retries exactly those, and the
    # retry resumes from the stored offset.
    app = _get_worker_app()
    with app.app_context():
        from app.services.housekeeper import process_file, housekeeping_settings, set_app
        set_app(app)
        process_file(file_path, column_map=column_map, original_filename=original_filename,
                     run_id=run_id, **housekeeping_settings())


@celery_app.task(bind=True, max_retries=CELERY_TASK_MAX_RETRIES, default_retry_delay=CELERY_TASK_RETRY_DELAY,
                 autoretry_for=(Exception,))
def process_pending_task(self):
    app = _get_worker_app()
    with app.app_context():
        from app.services.housekeeper import load_rds_to_db, set_app, DATA_DIR, housekeeping_settings
        from app.config import HOUSEKEEPING_MAX_WORKERS
        set_app(app)
        # max_workers is load_rds_to_db-only; housekeeping_settings() omits it
        # because the same dict also feeds process_file and watch_directory.
        load_rds_to_db(
            data_folder=DATA_DIR,
            max_workers=HOUSEKEEPING_MAX_WORKERS,
            **housekeeping_settings(),
        )
