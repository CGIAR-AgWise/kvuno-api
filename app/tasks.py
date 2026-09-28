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
def process_file_task(self, file_path: str):
    app = _get_worker_app()
    with app.app_context():
        from app.services.housekeeper import process_file, housekeeping_settings, set_app
        set_app(app)
        process_file(file_path, **housekeeping_settings())


@celery_app.task(bind=True, max_retries=CELERY_TASK_MAX_RETRIES, default_retry_delay=CELERY_TASK_RETRY_DELAY,
                 autoretry_for=(Exception,))
def process_pending_task(self):
    app = _get_worker_app()
    with app.app_context():
        from app.services.housekeeper import load_rds_to_db, set_app, DATA_DIR, housekeeping_settings
        set_app(app)
        load_rds_to_db(data_folder=DATA_DIR, **housekeeping_settings())
