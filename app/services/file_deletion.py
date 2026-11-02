"""An administrator deletes a file: the stored file, its upload, and its jobs.

What goes:
- the file on disk — a data-folder file with its `.map.json` sidecar, or an
  upload's whole `uploads/<id>/` directory;
- the `uploads` row, when the file came from an upload;
- every `processing_jobs` row for it (all runs), its Redis entry, and runs
  left with no jobs.

What stays by default: the rows already imported into
planting_recommendations, and the `file_imports` record of the file's
checksum — so uploading the same file again is still recognised as already
imported. `purge_rows=True` ("Undo import") removes both: every
planting_recommendations row whose checksum is the file's, its
import_conflicts rows, and the file_imports record, so the same file can be
imported afresh. That is irreversible and is never the default.

A file that is being processed is refused (409): its job is live, or its
ingest lock is held by a live process. The deletion holds the ingest lock
itself while it runs, so nothing can start the file meanwhile.
"""
import os

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.cache import invalidate_planting_data_cache
from app.config import get_settings
from app.models.database_conn import MyDb
from app.services import progress_store
from app.services import uploads as upload_service
from app.services.uploads import UploadError
from app.utils.file_lock import FileLock
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Statuses of a job that is still running (stale ones are not: nothing has
# reported for JOB_STALE_AFTER_SECONDS).
ACTIVE_STATUSES = ('processing', 'converting')

# Imported rows are deleted in batches: a single file can be millions of
# rows, and one transaction that large holds locks and bloats WAL.
PURGE_BATCH_SIZE = 10_000


def _purge_imported_rows(name: str) -> dict:
    """Delete the planting rows and checksum record one file imported.

    Uses its own Session, like progress_store: the request's scoped session
    must not be committed here. Batches commit as they go, so a failure
    mid-way can be retried (the remaining rows still carry the checksum).
    """
    from app.models.kvuno import FileImport, ImportConflict, PlantingRecommendation

    names = [name]
    if name.lower().endswith('.rds'):
        names.append(os.path.splitext(name)[0] + '.parquet')

    with Session(bind=MyDb.get_db().engine) as session:
        record = (session.query(FileImport).filter(FileImport.file_name.in_(names))
                  .order_by(FileImport.id).first())
        if record is None:
            return {'checksum': None, 'rows_deleted': 0, 'conflicts_deleted': 0}

        checksum = record.check_sum
        rows_deleted = conflicts_deleted = 0
        for model, counter in ((PlantingRecommendation, 'rows'),
                               (ImportConflict, 'conflicts')):
            while True:
                ids = select(model.id).where(model.check_sum == checksum).limit(PURGE_BATCH_SIZE)
                n = session.execute(delete(model).where(model.id.in_(ids))).rowcount
                session.commit()
                if not n:
                    break
                if counter == 'rows':
                    rows_deleted += n
                else:
                    conflicts_deleted += n

        session.delete(record)
        session.commit()

    invalidate_planting_data_cache()
    return {'checksum': checksum, 'rows_deleted': rows_deleted,
            'conflicts_deleted': conflicts_deleted}


def _data_path(name: str) -> str:
    return os.path.join(get_settings().HOUSEKEEPING_DATA_DIR, name)


def delete_file(name: str, actor_id: int | None, purge_rows: bool = False) -> dict:
    """Delete one file and its upload and jobs. Raises UploadError (404/409/400).

    With `purge_rows`, also delete everything the file imported ("Undo
    import") — see the module docstring.
    """
    if not name or name != os.path.basename(name) or name in ('.', '..'):
        raise UploadError('Not a file name', 400)

    job = progress_store.get_job(name)
    upload = upload_service.find_by_stored_name(name)
    if job is None and upload is None and not os.path.isfile(_data_path(name)):
        raise UploadError('No such file', 404)
    if job is not None and job.get('status') in ACTIVE_STATUSES:
        raise UploadError('The file is being processed; wait for it to finish or stall', 409)

    stored = upload.stored_path if upload is not None and upload.stored_path else _data_path(name)
    lock = FileLock(stored + '.ingest.lock', get_settings().INGEST_LOCK_STALE_SECONDS)
    if os.path.exists(os.path.dirname(stored) or '.') and not lock.acquire():
        raise UploadError('The file is being processed by a worker; try again when it stops', 409)
    try:
        # Before the file goes: if this fails, the rows and the file are both
        # still there and the administrator can retry (batches already deleted
        # are gone, but the checksum is kept until the last batch).
        purged = _purge_imported_rows(name) if purge_rows else {
            'checksum': None, 'rows_deleted': 0, 'conflicts_deleted': 0}

        file_deleted = False
        if upload is not None:
            upload_id = upload.id
            upload_service.discard(upload)  # its directory: data, converted file, lock
            file_deleted = True
        else:
            upload_id = None
            for path in (stored, stored + '.map.json'):
                try:
                    os.remove(path)
                    file_deleted = file_deleted or path == stored
                except FileNotFoundError:
                    pass
        jobs_deleted = progress_store.delete_file_history(name)
    finally:
        lock.release()  # a no-op once discard() removed the directory

    logger.info("file deleted by an administrator", file=name, actor_id=actor_id, upload_id=upload_id,
                file_deleted=file_deleted, jobs_deleted=jobs_deleted, purge_rows=purge_rows,
                rows_deleted=purged['rows_deleted'], conflicts_deleted=purged['conflicts_deleted'])
    return {'file': name, 'file_deleted': file_deleted, 'upload_deleted': upload_id is not None,
            'jobs_deleted': jobs_deleted, 'rows_deleted': purged['rows_deleted'],
            'conflicts_deleted': purged['conflicts_deleted']}
