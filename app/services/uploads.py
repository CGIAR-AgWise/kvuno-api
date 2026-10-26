"""
Uploads — resumable receipt, inspection and submission of data files.

Why this replaced the resumable.js chunk flow
---------------------------------------------
The old flow stored numbered chunk files under `.chunks/<identifier>/`, merged
them in one request, then converted, previewed and hashed the result in that
same request. It failed in several independent ways:

- `create_app()` wiped `.chunks/` on every boot, and gunicorn boots a worker on
  every `max_requests` recycle and every timeout — so in-flight uploads lost
  their chunks at random ("Missing chunk — upload may have failed").
- Each chunk cost two requests against the 600/hour default rate limit.
- The merge + RDS conversion + preview could outlive the 120s worker timeout.
- The identifier came from the client (`<size>-<filename>`), so two users with
  the same file name collided, and `os.path.join` with it was a traversal hole.
- State lived in sidecar files, so nothing knew where a failed upload stopped.

Now the browser speaks the tus protocol (https://tus.io, core + creation): the
server issues the id, bytes are appended to one file at a byte offset (the
file's size on disk *is* the offset, so a restart loses nothing), and an
`uploads` row tracks the lifecycle. Inspection — conversion, preview, checksum
— runs in the background and the client polls for it.

Inspection runs on a thread in the API process, not on Celery, so uploads keep
working with `HOUSEKEEPING_ENABLED=false`. A thread dies with its gunicorn
worker, so inspection keeps a heartbeat on `updated_at`; a row stuck in
`inspecting` without one is re-claimed on the next status poll.
"""
from __future__ import annotations

import datetime
import os
import re
import shutil
import threading
import time
import uuid

from sqlalchemy import or_, update

from app.config import ALLOWED_EXTENSIONS, get_settings
from app.models.database_conn import MyDb
from app.models.kvuno import PlantingRecommendation, Upload
from app.utils.logging import get_logger

logger = get_logger(__name__)

TUS_VERSION = '1.0.0'


def upload_root() -> str:
    # Under the data directory but in a subdirectory on purpose: the startup
    # scan (`load_rds_to_db`) ingests every .rds/.parquet in the data
    # directory's root with the default mapping, which used to pick up uploads
    # still waiting for the user to map their columns.
    return os.path.join(get_settings().HOUSEKEEPING_DATA_DIR, 'uploads')


PART_NAME = 'data.part'
LOCK_NAME = '.lock'

# A PATCH refreshes its lock every block it writes, so a lock this old belongs
# to a request that died (client gone, worker killed) and may be taken over.
LOCK_STALE_SECONDS = 30
WRITE_BLOCK = 64 * 1024

INSPECT_HEARTBEAT_SECONDS = 30
# Inspection progress is polled every 1.5 s by the Upload page; writing it
# more often than this would only add database writes.
PROGRESS_WRITE_SECONDS = 2

ERROR_MAX = 2000

_ID_RE = re.compile(r'^[0-9a-f]{32}$')

# Statuses that still accept a mapping and a submit. `failed` is excluded:
# there is nothing to map when the file could not be read. `completed` is
# excluded too: the import already ran.
SUBMITTABLE = ('ready', 'submitted', 'processing')
# Uploads still in the lifecycle, so the Upload page lists them. `completed`
# is not here: the import is over and the file is only waiting for cleanup.
UNFINISHED = ('receiving', 'received', 'inspecting', 'ready', 'failed', 'processing')
# Everything cleanup may remove once idle: the open lifecycle and finished
# imports (their file is still on disk until then).
CLEANABLE = (*UNFINISHED, 'completed')

_EXCLUDED = {'id', 'check_sum', 'coordinates', 'created_at', 'updated_at'}
DB_TARGET_COLUMNS = {
    col.name for col in PlantingRecommendation.__table__.columns
    if col.name not in _EXCLUDED
}

COLUMN_ALIASES = {
    'cultivar': 'variety',
    'season': 'season_type',
    'sowing_date': 'opt_date',
    'planting_date': 'opt_date',
    'plantingdate': 'opt_date',
    'date': 'opt_date',
    'longitude': 'lon',
    'long': 'lon',
    'latitude': 'lat',
    'lat_y': 'lat',
    'lon_x': 'lon',
}

# Without these the coordinates column (NOT NULL) cannot be built, so every
# batch would fail and the file would be reported done with nothing inserted.
REQUIRED_TARGETS = ('lat', 'lon')


class UploadError(Exception):
    """A request the client can fix. `status` is the HTTP status to return."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── Paths ──────────────────────────────────────────────────────

def valid_id(upload_id: str) -> bool:
    return bool(_ID_RE.match(upload_id or ''))


def upload_dir(upload_id: str) -> str:
    # Callers validate first; asserting here keeps a bad id from ever becoming
    # a path, whatever the call site.
    if not valid_id(upload_id):
        raise UploadError('Invalid upload id', 404)
    return os.path.join(upload_root(), upload_id)


def part_path(upload_id: str) -> str:
    return os.path.join(upload_dir(upload_id), PART_NAME)


def _extension(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def received_path(upload: Upload) -> str:
    return os.path.join(upload_dir(upload.id), upload.id + _extension(upload.original_name))


def _now() -> datetime.datetime:
    # Naive UTC, matching every other timestamp column in this schema.
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _clip(text: str, limit: int = ERROR_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + '…'


def _session():
    return MyDb.get_db().session


# ── Lifecycle ──────────────────────────────────────────────────

def parse_client_mtime(raw: str | None) -> datetime.datetime | None:
    """The tus `lastModified` metadata (ms since the epoch) as naive UTC, or None.

    It comes from the browser, so it is only trusted as far as it is
    plausible: unparsable or before 1990 is ignored, a future time is now.
    """
    try:
        stamp = datetime.datetime.fromtimestamp(int(raw) / 1000, datetime.timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if stamp < datetime.datetime(1990, 1, 1):
        return None
    return min(stamp, _now())


def parse_data_date(raw) -> datetime.date:
    """The mapping form's "Data date" (YYYY-MM-DD). Raises UploadError if not plausible."""
    try:
        value = datetime.date.fromisoformat(str(raw))
    except ValueError:
        raise UploadError('Data date must be a date (YYYY-MM-DD)')
    if value > _now().date():
        raise UploadError('Data date cannot be in the future')
    if value < datetime.date(1990, 1, 1):
        raise UploadError('Data date is before 1990; check it')
    return value


def data_datetime(upload: Upload) -> datetime.datetime | None:
    """The upload's confirmed data date as the rows' created_at (midnight), or None."""
    if upload.data_date is None:
        return None
    return datetime.datetime.combine(upload.data_date, datetime.time())


def create(owner_id: int | None, original_name: str, size: int, max_size: int,
           file_modified_at: datetime.datetime | None = None) -> Upload:
    """Register a new upload and its empty part file. Validates before any bytes arrive."""
    name = os.path.basename(original_name or '').strip()
    if not name:
        raise UploadError('A file name is required (tus metadata "filename")')
    if _extension(name) not in ALLOWED_EXTENSIONS:
        raise UploadError(f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}", 415)
    if size <= 0:
        raise UploadError('The file is empty')
    if size > max_size:
        raise UploadError(
            f'File too large ({size / 1024 / 1024:.1f} MB). '
            f'Maximum allowed: {max_size / 1024 / 1024:.0f} MB', 413)

    upload = Upload(
        id=uuid.uuid4().hex,
        owner_id=owner_id,
        original_name=_clip(name, 255),
        size=size,
        status='receiving',
        file_modified_at=file_modified_at,
        created_at=_now(),
        updated_at=_now(),
    )
    try:
        os.makedirs(upload_dir(upload.id), exist_ok=True)
        open(part_path(upload.id), 'wb').close()
    except OSError as e:
        # Almost always the data volume's ownership: the container runs as the
        # non-root `app` user (UID 1000) and a volume created before the image
        # made the directory is owned by root. A server problem, not the
        # client's — 503 with a stable message, and one clear log line.
        logger.error('upload storage is not writable', path=upload_root(), error=e.__class__.__name__,
                     errno=e.errno, hint='chown the HOUSEKEEPING_DATA_DIR volume to 1000:1000')
        raise UploadError('Upload storage on the server is not writable. Ask an administrator to fix '
                          'the data volume permissions.', 503) from None

    session = _session()
    session.add(upload)
    session.commit()
    return upload


def _visible_to(owner_id: int | None):
    """The caller's own uploads, plus data-folder files held for mapping (no owner)."""
    return or_(Upload.owner_id == owner_id, Upload.source == 'folder')


def get_owned(upload_id: str, owner_id: int | None) -> Upload:
    """Load an upload the caller may see. Anyone else's is reported as not found.

    Data-folder files held for mapping belong to nobody, so every signed-in
    user can see and map them.
    """
    if not valid_id(upload_id):
        raise UploadError('Upload not found', 404)
    upload = _session().get(Upload, upload_id)
    if upload is None or (upload.owner_id != owner_id and upload.source != 'folder'):
        raise UploadError('Upload not found', 404)
    return upload


def list_owned(owner_id: int | None, statuses=UNFINISHED) -> list[Upload]:
    return (_session().query(Upload)
            .filter(_visible_to(owner_id), Upload.status.in_(statuses))
            .order_by(Upload.created_at.desc())
            .all())


def count_awaiting_mapping(owner_id: int | None) -> int:
    """Uploads the caller can see that are inspected and waiting for a mapping."""
    return (_session().query(Upload)
            .filter(_visible_to(owner_id), Upload.status == 'ready')
            .count())


def hold_from_folder(file_path: str) -> Upload:
    """Take a data-folder file the default mapping cannot import, and hold it for mapping.

    The file moves into ``uploads/<id>/`` (so the next scan no longer sees it
    in the data folder root) and is inspected like an upload: preview,
    checksum, duplicate check and suggested mapping. It then waits, as
    status ``ready``, for someone to map it on the Upload page.
    """
    name = os.path.basename(file_path)
    upload = Upload(
        id=uuid.uuid4().hex,
        owner_id=None,
        source='folder',
        original_name=_clip(name, 255),
        size=os.path.getsize(file_path),
        status='received',
        # os.replace below keeps the file's time; recorded for the page.
        file_modified_at=datetime.datetime.fromtimestamp(
            os.path.getmtime(file_path), datetime.timezone.utc).replace(tzinfo=None),
        created_at=_now(),
        updated_at=_now(),
    )
    # Row first, then the move: if the move fails the row is removed and the
    # file is still where the caller can import it the old way.
    session = _session()
    session.add(upload)
    session.commit()
    try:
        os.makedirs(upload_dir(upload.id), exist_ok=True)
        os.replace(file_path, received_path(upload))
    except OSError:
        session.delete(upload)
        session.commit()
        raise
    logger.info("data-folder file held for mapping", upload_id=upload.id, file=name)
    inspect(upload.id)
    return session.get(Upload, upload.id)


def current_offset(upload: Upload) -> int:
    """Bytes received so far. The part file's size is the source of truth."""
    if upload.status != 'receiving':
        return upload.size
    try:
        return os.path.getsize(part_path(upload.id))
    except OSError:
        return 0


# ── Receiving bytes ────────────────────────────────────────────

class _Lock:
    """A cross-process lock on one upload, as an O_EXCL file in its directory.

    tus clients send one PATCH at a time, but a retry can arrive while the
    previous request is still draining on another worker. Two writers at the
    same offset would interleave bytes, so the second gets 423 and retries.
    """

    def __init__(self, upload_id: str):
        self.path = os.path.join(upload_dir(upload_id), LOCK_NAME)
        self.held = False

    def acquire(self) -> bool:
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                self.held = True
                return True
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) < LOCK_STALE_SECONDS:
                        return False
                    os.remove(self.path)  # holder died; take it over
                except OSError:
                    pass
        return False

    def touch(self):
        try:
            os.utime(self.path)
        except OSError:
            pass

    def release(self):
        if self.held:
            try:
                os.remove(self.path)
            except OSError:
                pass
            self.held = False


def append(upload: Upload, offset: int, stream, content_length: int | None) -> int:
    """Append a PATCH body at `offset`. Returns the new offset.

    A body cut off mid-request keeps every byte that arrived: the offset is the
    file size, so the client's next HEAD resumes from exactly there.
    """
    if upload.status != 'receiving':
        # Already complete — a retried final PATCH. Report the full length.
        if offset == upload.size:
            return upload.size
        raise UploadError('Upload is already complete', 409)

    lock = _Lock(upload.id)
    if not lock.acquire():
        raise UploadError('Upload is busy, retry shortly', 423)
    try:
        path = part_path(upload.id)
        on_disk = os.path.getsize(path)
        if offset != on_disk:
            raise UploadError(f'Upload-Offset {offset} does not match {on_disk}', 409)

        remaining = upload.size - offset
        if content_length is not None and content_length > remaining:
            raise UploadError('Body exceeds the declared Upload-Length', 413)

        written = 0
        with open(path, 'r+b') as fh:
            fh.seek(offset)
            while True:
                block = stream.read(WRITE_BLOCK)
                if not block:
                    break
                if written + len(block) > remaining:
                    raise UploadError('Body exceeds the declared Upload-Length', 413)
                fh.write(block)
                written += len(block)
                lock.touch()
            fh.flush()
            os.fsync(fh.fileno())
        return offset + written
    finally:
        lock.release()


def finish_receiving(upload: Upload) -> bool:
    """Move a fully received part file into place. Idempotent; True if this call did it."""
    session = _session()
    claimed = session.execute(
        update(Upload)
        .where(Upload.id == upload.id, Upload.status == 'receiving')
        .values(status='received', updated_at=_now())
    ).rowcount
    session.commit()
    if claimed:
        os.replace(part_path(upload.id), received_path(upload))
    session.refresh(upload)
    return bool(claimed)


# ── Inspection ─────────────────────────────────────────────────

def suggest_column_map(columns: list[str]) -> dict[str, str]:
    """Best-guess source -> DB mapping: exact (case-insensitive) name, then alias."""
    suggested = {}
    used = set()
    for col in columns:
        lowered = col.lower()
        target = lowered if lowered in DB_TARGET_COLUMNS else COLUMN_ALIASES.get(lowered)
        if target and target not in used:
            suggested[col] = target
            used.add(target)
    return suggested


def validate_column_map(column_map, columns: list[str]) -> dict[str, str]:
    """Return a clean mapping or raise UploadError naming what is wrong."""
    if not isinstance(column_map, dict):
        raise UploadError('column_map must be an object of {file column: database column}')
    clean = {str(k): str(v) for k, v in column_map.items() if v}
    unknown_sources = sorted(set(clean) - set(columns))
    if unknown_sources:
        raise UploadError(f'Columns not in the file: {unknown_sources}')
    invalid = sorted(set(clean.values()) - DB_TARGET_COLUMNS)
    if invalid:
        raise UploadError(f'Invalid target columns: {invalid}')
    targets = list(clean.values())
    doubled = sorted({t for t in targets if targets.count(t) > 1})
    if doubled:
        raise UploadError(f'Each database column can be mapped once: {doubled}')
    missing = [t for t in REQUIRED_TARGETS if t not in targets]
    if missing:
        raise UploadError(f'Map a file column to {" and ".join(missing)} — coordinates are required')
    return clean


def describe_duplicate(file_path: str, total_rows: int | None = None, checksum: str | None = None) -> dict | None:
    """Describe a previously-ingested file with the same content, if any.

    Returns None, or {status, original_filename, processed_at, offset} where
    status is 'processed' (fully ingested; re-running is a no-op) or 'partial'
    (interrupted; re-running resumes from that row). A recorded offset at or
    past the file's row count is also 'processed': files completed before the
    worker started clearing the offset were left with offset == row count.
    """
    if not get_settings().INGEST_DUPLICATE_CHECK:
        return None
    try:
        from app.utils import calculate_file_checksum
        from app.repo.file_import import FileImportRepo

        # Inspection passes the checksum it already computed: hashing twice
        # was a second full read of the file.
        checksum = checksum or calculate_file_checksum(file_path, logger)
        if not checksum:
            return None
        existing = FileImportRepo().get_by_checksum(checksum)
        if existing is None:
            return None
        done = existing.offset is None or (total_rows is not None and existing.offset >= total_rows)
        return {
            'status': 'processed' if done else 'partial',
            'original_filename': existing.original_filename or existing.file_name,
            'processed_at': existing.processed_at.isoformat() if existing.processed_at else None,
            'offset': existing.offset,
        }
    except Exception:
        # A failed lookup must not block the upload; the worker still guards.
        logger.warning('Duplicate check failed', exc_info=True)
        return None


def _claim_inspection(upload_id: str, take_over: bool = False) -> bool:
    """Atomically take the inspection, so two pollers never run it twice.

    ``take_over`` is for the worker task: a redelivered task means the previous
    delivery ended (the process was killed), but the row's heartbeat can still
    be inside the stale window. Waiting for it meant a killed reader was only
    recovered by the next Upload-page poll, minutes later. The visibility
    timeout is far longer than CONVERT_TIMEOUT_SECONDS, so a redelivery while
    the original is still reading is not expected.
    """
    stale_before = _now() - datetime.timedelta(seconds=get_settings().UPLOAD_INSPECT_STALE_SECONDS)
    if take_over:
        state = Upload.status.in_(('received', 'inspecting'))
    else:
        state = or_(Upload.status == 'received',
                    (Upload.status == 'inspecting') & (Upload.updated_at < stale_before))
    session = _session()
    claimed = session.execute(
        update(Upload)
        .where(Upload.id == upload_id, state)
        .values(status='inspecting', updated_at=_now(), error=None, progress=None)
    ).rowcount
    session.commit()
    return bool(claimed)


def _heartbeat(app, upload_id: str, stop: threading.Event):
    while not stop.wait(INSPECT_HEARTBEAT_SECONDS):
        try:
            with app.app_context():
                session = _session()
                session.execute(
                    update(Upload)
                    .where(Upload.id == upload_id, Upload.status == 'inspecting')
                    .values(updated_at=_now()))
                session.commit()
        except Exception:
            logger.warning(f'Upload {upload_id}: heartbeat failed', exc_info=True)


class _ProgressWriter:
    """Records where an inspection has got to on the upload row (Upload page).

    Called by the RDS conversion every couple of seconds; written at most once
    per PROGRESS_WRITE_SECONDS unless the stage changes. Uses its own Session,
    like progress_store, so it never commits the inspection's pending changes.
    It also bumps updated_at, which is what the stale-inspection check reads.
    """

    def __init__(self, upload_id: str):
        self.upload_id = upload_id
        self.elapsed = 0.0
        self._stage = None
        self._written = 0.0

    def __call__(self, stage: str, done: int, total: int, elapsed: float, force: bool = False):
        self.elapsed = elapsed
        now = time.monotonic()
        if not force and stage == self._stage and now - self._written < PROGRESS_WRITE_SECONDS:
            return
        self._stage, self._written = stage, now
        from sqlalchemy.orm import Session
        try:
            with Session(bind=MyDb.get_db().engine) as session:
                session.execute(
                    update(Upload)
                    .where(Upload.id == self.upload_id, Upload.status == 'inspecting')
                    .values(progress={'stage': stage, 'done': int(done), 'total': int(total),
                                      'elapsed': round(elapsed, 1)},
                            updated_at=_now()))
                session.commit()
        except Exception:
            logger.warning('upload progress write failed', upload_id=self.upload_id, exc_info=True)


def inspect(upload_id: str, app=None, take_over: bool = False):
    """Convert, preview and fingerprint a received upload. Runs off the request path.

    ``take_over`` lets the worker task reclaim an inspection whose process was
    killed, even before its heartbeat looks stale (see `_claim_inspection`).
    """
    from app.utils.preview import build_preview
    from app.utils.rds_to_parquet import convert_for_ingestion
    from app.utils import calculate_file_checksum

    if not _claim_inspection(upload_id, take_over):
        return

    stop = threading.Event()
    if app is not None:
        threading.Thread(target=_heartbeat, args=(app, upload_id, stop), daemon=True).start()

    session = _session()
    upload = session.get(Upload, upload_id)
    if upload is None:  # discarded between the claim and here
        stop.set()
        return
    try:
        source = received_path(upload)
        converted = os.path.splitext(source)[0] + '.parquet'
        if not os.path.isfile(source) and os.path.isfile(converted):
            source = converted  # a previous attempt converted it, then died
        if not os.path.isfile(source):
            raise UploadError('The uploaded file is missing from storage')

        progress = _ProgressWriter(upload_id)
        stored = convert_for_ingestion(source, on_progress=progress)
        ext = _extension(stored)
        progress('preview', 0, 0, progress.elapsed, force=True)
        preview = build_preview(stored, ext, use_cache=False)

        total_rows = None
        if ext == '.parquet':
            import pyarrow.parquet as pq
            total_rows = pq.ParquetFile(stored).metadata.num_rows

        progress('checksum', 0, 0, progress.elapsed, force=True)
        upload.stored_path = stored
        upload.sha256 = calculate_file_checksum(stored, logger)
        upload.progress = None
        upload.preview = {**preview, 'total_rows': total_rows}
        upload.duplicate = describe_duplicate(stored, total_rows, checksum=upload.sha256)
        upload.column_map = suggest_column_map(preview['columns'])
        upload.status = 'ready'
        upload.error = None
    except Exception as e:
        from app.utils.rds_to_parquet import ConversionError
        logger.exception(f'Upload {upload_id}: inspection failed: {e}')
        session.rollback()
        upload = session.get(Upload, upload_id)
        upload.status = 'failed'
        upload.progress = None
        # ConversionError already says, for the user, what went wrong.
        upload.error = _clip(str(e) if isinstance(e, ConversionError) else f'Could not read the file: {e}')
    finally:
        stop.set()
    upload.updated_at = _now()
    session.commit()


def _queue_inspection(upload: Upload) -> bool:
    """Send the inspection to the worker; True if it was queued.

    Marked queued *before* the task is sent: the worker can claim it at once,
    and marking it afterwards would turn its `inspecting` back to `received`.
    (Only a `received` row, or an `inspecting` one whose process died, gets
    here — ensure_inspection returns early for a live one.)
    """
    from app.services.housekeeper import _enqueue_or_warn
    from app.tasks import inspect_upload_task
    session = _session()
    session.execute(
        update(Upload)
        .where(Upload.id == upload.id, Upload.status.in_(('received', 'inspecting')))
        .values(status='received', progress={'stage': 'queued', 'done': 0, 'total': 0, 'elapsed': 0},
                updated_at=_now()))
    session.commit()
    if not _enqueue_or_warn(inspect_upload_task, upload_id=upload.id):
        session.execute(update(Upload)
                        .where(Upload.id == upload.id, Upload.status == 'received')
                        .values(progress=None))
        session.commit()
        session.refresh(upload)
        return False
    session.refresh(upload)
    logger.info("upload inspection queued for the worker", upload_id=upload.id)
    return True


def ensure_inspection(app, upload: Upload) -> None:
    """Start inspection if the upload needs it: on the worker, else on a thread.

    Called after the last byte arrives and on every status poll. The second
    call site is what recovers an inspection whose process died: its
    heartbeat stops, the row goes stale, and the next poll re-claims it.

    With a Celery worker on our queue the inspection is a task there, so the
    API never reads a file (an RDS read needs ~150x its size in memory). The
    upload stays `received` with progress stage `queued` until the worker
    claims it; a poll re-queues only once that is older than the stale
    window (the worker may be busy with an import), and inspect()'s claim
    makes any duplicate a no-op.
    """
    if upload.status not in ('received', 'inspecting'):
        return
    stale_before = _now() - datetime.timedelta(seconds=get_settings().UPLOAD_INSPECT_STALE_SECONDS)
    fresh = upload.updated_at is not None and upload.updated_at >= stale_before
    if upload.status == 'inspecting' and fresh:
        return  # alive: its heartbeat is recent
    queued = (upload.progress or {}).get('stage') == 'queued'
    if upload.status == 'received' and queued and fresh:
        return  # waiting for the worker

    from app.services.housekeeper import celery_worker_available
    if celery_worker_available() and _queue_inspection(upload):
        return

    def run():
        with app.app_context():
            try:
                inspect(upload.id, app)
            except Exception:
                logger.error(f'Upload {upload.id}: inspection crashed', exc_info=True)

    threading.Thread(target=run, name=f'inspect-{upload.id}', daemon=True).start()


# ── Submission and removal ─────────────────────────────────────

def submit(upload: Upload, column_map=None, data_date=None) -> Upload:
    """Validate and store the mapping and data date, ready to process.

    `None` re-uses the saved value (Retry sends neither). An empty data date
    clears it: the rows then take the file's first import time.
    """
    if upload.status not in SUBMITTABLE:
        raise UploadError(f'Upload is {upload.status}, not ready to process', 409)
    columns = (upload.preview or {}).get('columns', [])
    mapping = validate_column_map(upload.column_map if column_map is None else column_map, columns)
    if data_date is not None:
        upload.data_date = parse_data_date(data_date) if str(data_date).strip() else None
    upload.column_map = mapping
    # If already processing, keep that status; otherwise mark as submitted
    if upload.status != 'processing':
        upload.status = 'submitted'
    upload.error = None
    upload.updated_at = _now()
    _session().commit()
    return upload


def mark_ready(upload: Upload, message: str) -> None:
    """Undo a submit that could not be enqueued, so the user can try again."""
    upload.status = 'ready'
    upload.error = _clip(message)
    upload.updated_at = _now()
    _session().commit()


def find_by_stored_name(file_name: str) -> Upload | None:
    """The upload whose stored file has this name's id (`<id>.<ext>`), if any.

    The extension is not compared: an inspection whose Parquet write failed
    keeps the RDS as `stored_path`, and the worker's later conversion is
    `<id>.parquet` — the id is what identifies the upload.
    """
    upload_id = os.path.splitext(os.path.basename(file_name))[0]
    if not valid_id(upload_id):
        return None
    upload = _session().get(Upload, upload_id)
    if upload is None:
        return None
    if os.path.splitext(os.path.basename(upload.stored_path or ''))[0] != upload_id:
        return None
    return upload


def mark_processed(file_name: str, status: str, error: str | None = None) -> bool:
    """Finish an upload's lifecycle when its import ends.

    `processing` was the last status an import ever left behind, so the Upload
    page kept listing a finished file as Processing until the cleanup task
    deleted it (CLEANUP_AGE, a day by default). Only a `processing` row is
    changed: a folder file waiting for a mapping, or an upload already
    discarded, must not move.

    Uses its own short-lived Session, like progress_store, so it can never
    commit rows the ingest session is holding between checkpoints.
    """
    upload_id = os.path.splitext(os.path.basename(file_name))[0]
    if not valid_id(upload_id):
        return False
    from sqlalchemy.orm import Session

    with Session(bind=MyDb.get_db().engine) as session:
        upload = session.get(Upload, upload_id)
        # The id is the identity; the extension may differ (an RDS converted
        # by the worker is `<id>.parquet` while stored_path is `<id>.rds`).
        if upload is None or os.path.splitext(os.path.basename(upload.stored_path or ''))[0] != upload_id:
            return False
        updated = session.execute(
            update(Upload)
            .where(Upload.id == upload_id, Upload.status == 'processing')
            .values(status=status, error=_clip(error) if error else None, updated_at=_now())
        ).rowcount
        session.commit()
        return bool(updated)


def discard(upload: Upload) -> None:
    shutil.rmtree(upload_dir(upload.id), ignore_errors=True)
    session = _session()
    session.delete(upload)
    session.commit()


def cleanup_abandoned(max_age_seconds: int) -> int:
    """Delete uploads that never reached a worker and have been idle too long.

    Never data-folder files held for mapping: they are the operator's files,
    not an abandoned upload, and would otherwise vanish after a week.
    """
    cutoff = _now() - datetime.timedelta(seconds=max_age_seconds)
    stale = (_session().query(Upload)
             .filter(Upload.status.in_(CLEANABLE), Upload.updated_at < cutoff,
                     Upload.source != 'folder')
             .all())
    for upload in stale:
        discard(upload)
    return len(stale)


def to_dict(upload: Upload) -> dict:
    preview = upload.preview or {}
    return {
        'id': upload.id,
        'name': upload.original_name,
        'source': upload.source or 'upload',
        'size': upload.size,
        'received': current_offset(upload),
        'status': upload.status,
        'file': os.path.basename(upload.stored_path) if upload.stored_path else None,
        'columns': preview.get('columns', []),
        'rows': preview.get('rows', []),
        'total_rows': preview.get('total_rows'),
        'duplicate': upload.duplicate,
        'column_map': upload.column_map or {},
        'error': upload.error,
        'progress': upload.progress if upload.status in ('received', 'inspecting') else None,
        'file_modified_at': upload.file_modified_at.isoformat() if upload.file_modified_at else None,
        'data_date': upload.data_date.isoformat() if upload.data_date else None,
        'created_at': upload.created_at.isoformat() if upload.created_at else None,
        'updated_at': upload.updated_at.isoformat() if upload.updated_at else None,
    }
