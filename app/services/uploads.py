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

ERROR_MAX = 2000

_ID_RE = re.compile(r'^[0-9a-f]{32}$')

# Statuses that still accept a mapping and a submit. `failed` is excluded:
# there is nothing to map when the file could not be read.
SUBMITTABLE = ('ready', 'submitted', 'processing')
# Uploads that never reached a worker; cleaned up once abandoned.
UNFINISHED = ('receiving', 'received', 'inspecting', 'ready', 'failed', 'processing')

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

def create(owner_id: int | None, original_name: str, size: int, max_size: int) -> Upload:
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


def get_owned(upload_id: str, owner_id: int | None) -> Upload:
    """Load an upload the caller owns. Anyone else's is reported as not found."""
    if not valid_id(upload_id):
        raise UploadError('Upload not found', 404)
    upload = _session().get(Upload, upload_id)
    if upload is None or upload.owner_id != owner_id:
        raise UploadError('Upload not found', 404)
    return upload


def list_owned(owner_id: int | None, statuses=UNFINISHED) -> list[Upload]:
    return (_session().query(Upload)
            .filter(Upload.owner_id == owner_id, Upload.status.in_(statuses))
            .order_by(Upload.created_at.desc())
            .all())


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


def _claim_inspection(upload_id: str) -> bool:
    """Atomically take the inspection, so two pollers never run it twice."""
    stale_before = _now() - datetime.timedelta(seconds=get_settings().UPLOAD_INSPECT_STALE_SECONDS)
    session = _session()
    claimed = session.execute(
        update(Upload)
        .where(
            Upload.id == upload_id,
            or_(Upload.status == 'received',
                (Upload.status == 'inspecting') & (Upload.updated_at < stale_before)),
        )
        .values(status='inspecting', updated_at=_now(), error=None)
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


def inspect(upload_id: str, app=None):
    """Convert, preview and fingerprint a received upload. Runs off the request path."""
    from app.utils.preview import build_preview
    from app.utils.rds_to_parquet import convert_for_ingestion
    from app.utils import calculate_file_checksum

    if not _claim_inspection(upload_id):
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

        stored = convert_for_ingestion(source)
        ext = _extension(stored)
        preview = build_preview(stored, ext, use_cache=False)

        total_rows = None
        if ext == '.parquet':
            import pyarrow.parquet as pq
            total_rows = pq.ParquetFile(stored).metadata.num_rows

        upload.stored_path = stored
        upload.sha256 = calculate_file_checksum(stored, logger)
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
        # ConversionError already says, for the user, what went wrong.
        upload.error = _clip(str(e) if isinstance(e, ConversionError) else f'Could not read the file: {e}')
    finally:
        stop.set()
    upload.updated_at = _now()
    session.commit()


def ensure_inspection(app, upload: Upload) -> None:
    """Start inspection on a thread if the upload needs it.

    Called after the last byte arrives and on every status poll. The second
    call site is what recovers an inspection whose worker was recycled: its
    heartbeat stops, the row goes stale, and the next poll re-claims it.
    """
    if upload.status not in ('received', 'inspecting'):
        return
    stale_before = _now() - datetime.timedelta(seconds=get_settings().UPLOAD_INSPECT_STALE_SECONDS)
    if upload.status == 'inspecting' and upload.updated_at and upload.updated_at >= stale_before:
        return  # alive: its heartbeat is recent

    def run():
        with app.app_context():
            try:
                inspect(upload.id, app)
            except Exception:
                logger.error(f'Upload {upload.id}: inspection crashed', exc_info=True)

    threading.Thread(target=run, name=f'inspect-{upload.id}', daemon=True).start()


# ── Submission and removal ─────────────────────────────────────

def submit(upload: Upload, column_map=None) -> Upload:
    """Validate and store the mapping, ready to process. `None` re-uses the saved one (Retry)."""
    if upload.status not in SUBMITTABLE:
        raise UploadError(f'Upload is {upload.status}, not ready to process', 409)
    columns = (upload.preview or {}).get('columns', [])
    upload.column_map = validate_column_map(
        upload.column_map if column_map is None else column_map, columns)
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
    """The upload whose stored file is `file_name` (`<id>.parquet`), if any."""
    upload_id = os.path.splitext(os.path.basename(file_name))[0]
    if not valid_id(upload_id):
        return None
    upload = _session().get(Upload, upload_id)
    if upload is None or os.path.basename(upload.stored_path or '') != os.path.basename(file_name):
        return None
    return upload


def discard(upload: Upload) -> None:
    shutil.rmtree(upload_dir(upload.id), ignore_errors=True)
    session = _session()
    session.delete(upload)
    session.commit()


def cleanup_abandoned(max_age_seconds: int) -> int:
    """Delete uploads that never reached a worker and have been idle too long."""
    cutoff = _now() - datetime.timedelta(seconds=max_age_seconds)
    stale = (_session().query(Upload)
             .filter(Upload.status.in_(UNFINISHED), Upload.updated_at < cutoff)
             .all())
    for upload in stale:
        discard(upload)
    return len(stale)


def to_dict(upload: Upload) -> dict:
    preview = upload.preview or {}
    return {
        'id': upload.id,
        'name': upload.original_name,
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
        'created_at': upload.created_at.isoformat() if upload.created_at else None,
        'updated_at': upload.updated_at.isoformat() if upload.updated_at else None,
    }
