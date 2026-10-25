"""An administrator deletes a file: the stored file, its upload, and its jobs.

What goes:
- the file on disk — a data-folder file with its `.map.json` sidecar, or an
  upload's whole `uploads/<id>/` directory;
- the `uploads` row, when the file came from an upload;
- every `processing_jobs` row for it (all runs), its Redis entry, and runs
  left with no jobs.

What stays: the rows already imported into planting_recommendations, and the
`file_imports` record of the file's checksum — so uploading the same file
again is still recognised as already imported. Removing imported data is a
different, larger operation.

A file that is being processed is refused (409): its job is live, or its
ingest lock is held by a live process. The deletion holds the ingest lock
itself while it runs, so nothing can start the file meanwhile.
"""
import os

from app.config import get_settings
from app.services import progress_store
from app.services import uploads as upload_service
from app.services.uploads import UploadError
from app.utils.file_lock import FileLock
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Statuses of a job that is still running (stale ones are not: nothing has
# reported for JOB_STALE_AFTER_SECONDS).
ACTIVE_STATUSES = ('processing', 'converting')


def _data_path(name: str) -> str:
    return os.path.join(get_settings().HOUSEKEEPING_DATA_DIR, name)


def delete_file(name: str, actor_id: int | None) -> dict:
    """Delete one file and its upload and jobs. Raises UploadError (404/409/400)."""
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
                file_deleted=file_deleted, jobs_deleted=jobs_deleted)
    return {'file': name, 'file_deleted': file_deleted, 'upload_deleted': upload_id is not None,
            'jobs_deleted': jobs_deleted}
