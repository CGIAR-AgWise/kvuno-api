from app.celery_app import celery_app
from app.config import get_settings
from app.worker_app import build_app as _get_worker_app

# Task options are fixed when the task is registered, i.e. at worker start.
_settings = get_settings()


@celery_app.task(bind=True, max_retries=_settings.CELERY_TASK_MAX_RETRIES,
                 default_retry_delay=_settings.CELERY_TASK_RETRY_DELAY, autoretry_for=(Exception,))
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
        if file_path.lower().endswith('.rds'):
            # The API no longer converts in the request (it used to, needing
            # ~150x the file size in memory there); the worker does it here,
            # shown on the Jobs page like a scan's conversions.
            file_path = _convert(file_path)
            if file_path is None:
                return  # recorded on the job; it would not convert on a retry either
        process_file(file_path, column_map=column_map, original_filename=original_filename,
                     run_id=run_id, **housekeeping_settings())


def _convert(rds_path: str) -> str | None:
    """The Parquet path for an RDS, or None when it cannot be converted."""
    import os

    from app.services.housekeeper import report_conversion
    from app.utils.rds_to_parquet import ConversionError, convert_for_ingestion
    last = {'total': 0, 'elapsed': 0.0}

    def progress(stage, done, total, elapsed):
        last.update(total=total, elapsed=elapsed)
        report_conversion(rds_path, stage, done, total, elapsed)

    try:
        out = convert_for_ingestion(rds_path, on_progress=progress)
    except ConversionError as e:
        report_conversion(rds_path, 'failed', 0, last['total'], last['elapsed'], str(e))
        return None
    if out != rds_path:
        report_conversion(rds_path, 'done', last['total'], last['total'], last['elapsed'],
                          f"Converted to {os.path.basename(out)}")
    return out


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def inspect_upload_task(self, upload_id: str):
    """Read a received upload: RDS -> Parquet, preview, checksum, suggested mapping.

    Runs on the worker so the API never reads files: an RDS read needs ~150x
    the file size in memory, which the API's limit could not give. The claim
    in inspect() makes a duplicate task (a re-queue by the status poll) a no-op.
    """
    app = _get_worker_app()
    with app.app_context():
        from app.services import uploads
        uploads.inspect(upload_id, app)


@celery_app.task(bind=True, max_retries=_settings.CELERY_TASK_MAX_RETRIES,
                 default_retry_delay=_settings.CELERY_TASK_RETRY_DELAY, autoretry_for=(Exception,))
def process_pending_task(self):
    app = _get_worker_app()
    with app.app_context():
        from app.services.housekeeper import load_rds_to_db, set_app, housekeeping_settings
        set_app(app)
        settings = get_settings()
        # max_workers is load_rds_to_db-only; housekeeping_settings() omits it
        # because the same dict also feeds process_file and watch_directory.
        load_rds_to_db(
            data_folder=settings.HOUSEKEEPING_DATA_DIR,
            max_workers=settings.HOUSEKEEPING_MAX_WORKERS,
            **housekeeping_settings(),
        )
