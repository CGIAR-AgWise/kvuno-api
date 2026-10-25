"""Import one submitted upload in a process of its own.

    python -m app.services.ingest_runner <upload_id>

Started (detached) by the API when no Celery worker takes the job. Uploads
used to be imported on a thread inside a gunicorn worker, and gunicorn
replaces a worker after MAX_REQUESTS requests — with the Upload page polling
every 1.5 s and the Jobs stream open, every few tens of minutes. Each
recycle killed the import mid-file; it went stale and resumed from its last
checkpoint, so a 2M-row file took tens of hours or never finished. This
process is not a gunicorn worker, so recycling does not touch it.

A transient database error (connection lost, pool timeout) resumes from the
stored offset after a pause, as Celery's autoretry does for the worker; any
other failure is recorded on the job by process_file and ends the process.
"""
import sys
import time

from app.utils.logging import get_logger

logger = get_logger(__name__)

# Pauses before each retry after a transient database error.
RETRY_DELAYS_SECONDS = (15, 60, 180, 300)


def run(upload_id: str) -> int:
    from app.models.database_conn import MyDb
    from app.models.kvuno import Upload
    from app.services import uploads as upload_service
    from app.services.housekeeper import housekeeping_settings, is_transient_db_error, process_file, set_app
    from app.worker_app import build_app

    if not upload_service.valid_id(upload_id):
        logger.error("ingest runner: invalid upload id")
        return 2
    app = build_app()
    set_app(app)
    with app.app_context():
        upload = MyDb.get_db().session.get(Upload, upload_id)
        if upload is None or not upload.stored_path:
            logger.error("ingest runner: upload not found or not stored", upload_id=upload_id)
            return 2
        path, column_map, name = upload.stored_path, upload.column_map, upload.original_name
        MyDb.get_db().session.remove()

        for attempt, delay in enumerate((0, *RETRY_DELAYS_SECONDS)):
            if delay:
                logger.warning("ingest runner: database unavailable, retrying from the last checkpoint",
                               upload_id=upload_id, attempt=attempt, wait_seconds=delay)
                time.sleep(delay)
            try:
                process_file(path, column_map=column_map, original_filename=name, **housekeeping_settings())
                return 0
            except Exception as e:
                if not is_transient_db_error(e):
                    logger.exception("ingest runner failed", upload_id=upload_id)
                    return 1
                MyDb.get_db().session.remove()
        logger.error("ingest runner: database still unavailable; giving up (Retry on the Jobs page resumes)",
                     upload_id=upload_id)
        return 1


if __name__ == '__main__':
    sys.exit(run(sys.argv[1]) if len(sys.argv) == 2 else 2)
