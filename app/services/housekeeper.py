"""
Housekeeping — RDS/Parquet file ingestion service.

Used as the primary processing module by both the Flask app (upload API,
startup background job) and the standalone CLI (``python housekeeping.py``).

The Celery worker imports this module for ``process_file`` and
``load_rds_to_db``; the API imports it for background-thread processing
and the standalone CLI.
"""
import concurrent.futures
import gc
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time

import pandas as pd
import pyreadr
from geoalchemy2 import WKTElement
from sqlalchemy import text as db_text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    InterfaceError,
    OperationalError,
    PendingRollbackError,
    SQLAlchemyError,
    TimeoutError as PoolTimeoutError,
)
from tqdm import tqdm

from app import create_app
from app.config import celery_broker_available, get_settings
from app.dto.planting_recommendation import PlantingRecommendationCreate
from app.models.database_conn import MyDb
from app.models.kvuno import PlantingRecommendation, ImportConflict
from app.repo.planting_recommendation import PlantingRecommendationRepo
from app.repo.file_import import FileImportRepo
from app.services.progress_store import save_progress
from app.utils import calculate_file_checksum
from app.utils.downloader import RDSDownloader
from app.utils.file_lock import FileLock
from app.utils.logging import get_logger

logger = get_logger(__name__)

_app = None

# Per-file ingest lock, next to the data file. Two workers could ingest the
# same file at once (a double-click enqueues twice; a Redis redelivery after
# the visibility timeout runs a task that is still running), interleaving
# commits and offsets. The holder refreshes it every chunk, so it only goes
# stale when the holder is dead; set INGEST_LOCK_STALE_SECONDS well above the
# slowest chunk.
INGEST_LOCK_SUFFIX = '.ingest.lock'

# process_pending()'s de-dupe key. create_app() runs once per gunicorn worker,
# so without it N workers enqueued N identical scans. It lives for
# STARTUP_SCAN_COOLDOWN_SECONDS.
PROCESS_PENDING_DEDUPE_KEY = 'kvuno:process_pending'


# ── App management ─────────────────────────────────────────────

def _get_app():
    global _app
    if _app is None:
        _app = create_app()
    return _app


def set_app(app_instance):
    """Override the module-level app (used when integrating into the Flask server)."""
    global _app
    _app = app_instance


# ── Settings ───────────────────────────────────────────────────

def housekeeping_settings() -> dict:
    """Return the housekeeping parameters shared by every entry point.

    Deliberately excludes ``max_workers``. Only ``load_rds_to_db`` — the
    multi-file thread-pool orchestrator — has a thread pool; ``process_file``
    (one file) and ``watch_directory`` (one file per event) have nothing to
    parallelise. This dict is splatted into all three, so including it here
    raised ``TypeError: process_file() got an unexpected keyword argument
    'max_workers'`` and failed every Celery ingest. Pass it explicitly at the
    ``load_rds_to_db`` call sites instead.
    """
    settings = get_settings()
    return {
        'batch_size': settings.HOUSEKEEPING_BATCH_SIZE,
        'chunk_size': settings.HOUSEKEEPING_CHUNK_SIZE,
        'checkpoint_interval': settings.HOUSEKEEPING_CHECKPOINT_INTERVAL,
    }


# ── Repos ──────────────────────────────────────────────────────

file_import_repo = FileImportRepo()
planting_recommendation_repo = PlantingRecommendationRepo()


# ── Graceful shutdown ──────────────────────────────────────────

shutdown_requested = False

# Set by the Celery worker's main process when it is told to stop (see
# app/celery_app.py). The ingest runs in a pool CHILD, which never sees the
# SIGTERM Docker sends to PID 1 — and Celery's warm shutdown would otherwise
# wait for the whole file while Docker kills the container after its grace
# period, leaving the job "processing" with an unacked message Redis only
# redelivers after CELERY_VISIBILITY_TIMEOUT (6 h). A file in the container's
# temp directory is visible to parent and children alike.
WORKER_STOP_FILE = os.path.join(tempfile.gettempdir(), 'kvuno-worker-stopping')


def request_worker_stop():
    try:
        with open(WORKER_STOP_FILE, 'w'):
            pass
    except OSError:
        logger.warning("Could not signal running ingests to stop", exc_info=True)


def clear_worker_stop():
    try:
        os.remove(WORKER_STOP_FILE)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not clear the worker stop flag", exc_info=True)


def stop_requested() -> bool:
    """The CLI got SIGINT/SIGTERM, or the Celery worker is shutting down."""
    return shutdown_requested or os.path.exists(WORKER_STOP_FILE)


def _handle_sigterm(signum, frame):
    global shutdown_requested
    if shutdown_requested:
        raise SystemExit(1)
    shutdown_requested = True
    logger.warning("SIGTERM received — shutting down after current batch")


def _handle_sigint(signum, frame):
    global shutdown_requested
    if shutdown_requested:
        raise KeyboardInterrupt()
    shutdown_requested = True
    logger.warning("KeyboardInterrupt received — shutting down after current batch")


def install_signal_handlers():
    """Install graceful-shutdown handlers — for the standalone CLI only.

    These used to be installed at import time, which replaced gunicorn's and
    Celery's own SIGTERM handlers in any process that merely imported this
    module (the API with HOUSEKEEPING_ENABLED, and every worker), so neither
    shut down the way it expects. Celery's warm shutdown already lets a task
    finish, and an interrupted ingest resumes from its offset.

    ``signal.signal`` raises ValueError off the main thread, so both are
    guarded; SIGTERM may also be missing on some platforms (AttributeError).
    """
    for sig_name, handler in (('SIGTERM', _handle_sigterm), ('SIGINT', _handle_sigint)):
        try:
            signal.signal(getattr(signal, sig_name), handler)
        except (ValueError, AttributeError):
            pass


# ── Telemetry ──────────────────────────────────────────────────

def emit_event(event: str, **kwargs):
    """Log a structured ingestion event (file.processing_start, …).

    These used to be hand-built JSON printed to stderr — a second stream in a
    second format. They now go through the shared logger, so they carry the
    task id and timestamp like every other line and render as JSON fields in
    production.
    """
    logger.info(event, **kwargs)


# ── Column mapping ─────────────────────────────────────────────

def needs_mapping(file_path: str) -> bool:
    """Whether the default mapping cannot import this file: it finds no lat/lon in it.

    The default mapping is the built-ins, overridden by RDS_COLUMN_MAP and a
    per-file ``.map.json`` (load_column_map). Without both coordinates every
    row is dropped (the coordinates column is NOT NULL), so such a file used
    to be "imported" with nothing in it. Parquet only — its columns come from
    the footer; an RDS would have to be read whole, so it is left to
    process_file as before (scans convert RDS to Parquet first anyway).
    """
    if not file_path.lower().endswith('.parquet'):
        return False
    try:
        import pyarrow.parquet as pq
        columns = set(pq.ParquetFile(file_path).schema_arrow.names)
    except Exception:
        return False  # unreadable: process_file reports it
    targets = {target for source, target in load_column_map(file_path).items() if source in columns}
    return not {'lat', 'lon'} <= targets


def hold_unmapped(file_paths: list[str], dry_run: bool = False) -> list[str]:
    """Hold the files the default mapping cannot import; return the rest.

    A held file becomes an upload awaiting mapping (source='folder'), shown on
    the Upload page and counted in the sidebar. INGEST_HOLD_UNMAPPED=false
    keeps the old behaviour. Needs an app context.
    """
    if not get_settings().INGEST_HOLD_UNMAPPED:
        return list(file_paths)
    remaining = []
    for path in file_paths:
        if not needs_mapping(path):
            remaining.append(path)
            continue
        name = os.path.basename(path)
        if dry_run:
            logger.info("[DRY RUN] would hold for mapping: the default mapping finds no lat/lon", file=name)
            continue
        try:
            from app.services import uploads as upload_service
            upload_service.hold_from_folder(path)
        except Exception:
            logger.exception("could not hold the file for mapping; importing it with the default mapping",
                             file=name)
            remaining.append(path)
    return remaining


def load_column_map(file_path: str | None = None) -> dict[str, str]:
    """Load column name mapping from env var, overridden by a per-file .map.json if present."""
    default_map = {
        'country': 'country',
        'province': 'province',
        'lon': 'lon',
        'lat': 'lat',
        'Variety': 'variety',
        'Season_type': 'season_type',
        'Opt_date': 'opt_date',
        'Planting_Option': 'planting_option',
    }
    raw = get_settings().RDS_COLUMN_MAP
    if raw:
        # Validated as a JSON object when the settings were loaded.
        default_map.update(json.loads(raw))

    if file_path:
        mapping_path = file_path + '.map.json'
        if os.path.isfile(mapping_path):
            try:
                with open(mapping_path) as f:
                    per_file = json.load(f)
                default_map.update(per_file)
                logger.info(f"Applied per-file column mapping from {mapping_path}")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load per-file mapping {mapping_path}: {e}")

    return default_map


def _already_reported_complete(file_name: str, run_id: int | None) -> bool:
    try:
        from app.services.progress_store import get_job
        job = get_job(file_name)
    except Exception:
        return False
    return bool(job and job.get('status') == 'completed' and job.get('run_id') == run_id
                and (job.get('total') or 0) > 0)


def worker_name() -> str:
    """This worker's name on its jobs.

    Returns the container hostname for Celery workers, or 'api-background'
    when called from the API's background processing thread.
    """
    # If called from API background thread, HOSTNAME won't be set meaningfully.
    # Check if we're in a Celery worker context.
    if os.getenv('CELERY_WORKER'):
        return os.getenv('HOSTNAME') or socket.gethostname()
    return os.getenv('HOSTNAME') or socket.gethostname() or 'api-background'


def _write_progress(file_path: str, status: str, current: int, total: int, message: str = "",
                    run_id: int | None = None, error: str | None = None,
                    original_filename: str | None = None):
    """Record progress for one file.

    ``run_id`` groups the files submitted together so the UI can report batch
    progress; ``error`` is stored separately from ``message`` so routine
    progress updates cannot erase why a job failed.
    """
    save_progress(
        file_path, status, current, total, message,
        run_id=run_id, error=error,
        worker=worker_name(),
        original_filename=original_filename or os.path.basename(file_path),
    )


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def report_conversion(rds_path: str, stage: str, done: int, total: int, elapsed: float, message: str = ''):
    """Show a data-folder RDS -> Parquet conversion on the Jobs page.

    It is a job of its own, under the RDS name: 'converting' while it runs
    (the read cannot be measured, so it reports how long it has been reading;
    the write reports rows), then 'completed' or 'error'. Scans used to
    convert silently, so a long read looked like a hung scan and a failure was
    only in the log. Called by batch_convert's on_file and the folder watcher.
    """
    with _get_app().app_context():
        if stage == 'done':
            _write_progress(rds_path, 'completed', total, total,
                            f"{message} — {total:,} rows in {_duration(elapsed)}")
        elif stage == 'failed':
            _write_progress(rds_path, 'error', 0, total, message, error=message)
        elif stage == 'reading':
            _write_progress(rds_path, 'converting', 0, 0,
                            f"Converting to Parquet: reading the R file ({_duration(elapsed)})")
        else:
            _write_progress(rds_path, 'converting', done, total,
                            f"Converting to Parquet: writing {done:,} of {total:,} rows")


# ── DB helpers ─────────────────────────────────────────────────

def retry_db(fn, attempts: int = 3, base_delay: float = 1.0, session=None):
    """Call *fn* with retries on SQLAlchemyError using exponential backoff.

    Pass ``session`` only where nothing uncommitted is at stake (e.g. before
    an ingest starts): it is rolled back before each retry. A failed query —
    a dropped connection above all — leaves the session's transaction
    invalid, and retrying on it without a rollback could never succeed
    ("Can't reconnect until invalid transaction is rolled back"). Mid-ingest
    it must NOT be rolled back here: that would discard the rows written
    since the last checkpoint while the offset carried on past them; there
    the error propagates and the file resumes from its checkpoint instead.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except SQLAlchemyError as e:
            last_exc = e
            if attempt < attempts:
                if session is not None:
                    try:
                        session.rollback()
                    except Exception:
                        logger.warning("rollback before retry failed", exc_info=True)
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(f"DB error (attempt {attempt}/{attempts}): {e}. Retrying in {delay}s")
                time.sleep(delay)
    logger.error(f"DB operation failed after {attempts} attempts: {last_exc}")
    raise last_exc


_TRANSIENT_DB_ERRORS = (
    OperationalError, InterfaceError, DisconnectionError, PoolTimeoutError,
    # Raised on every use of a session whose transaction could not be rolled
    # back — in an ingest that means the connection went away mid-savepoint
    # (a constraint failure inside a savepoint rolls back cleanly instead).
    PendingRollbackError,
)


def is_transient_db_error(exc: BaseException) -> bool:
    """Whether *exc* is the database being unavailable, not the file being bad.

    Transient errors are re-raised so Celery's ``autoretry_for`` retries the
    task, which resumes from the stored offset. The cause chain is followed
    because retry_db and SQLAlchemy both wrap the original error.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, _TRANSIENT_DB_ERRORS):
            return True
        if isinstance(exc, DBAPIError) and getattr(exc, 'connection_invalidated', False):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _select_columns(column_map: dict[str, str], available: list[str], file_name: str,
                    explicit: bool) -> tuple[dict[str, str], list[str]]:
    """The mapping restricted to the file's columns, and the columns to read.

    The default/sidecar map names columns a given file may not have, and
    asking pyarrow for a missing column fails the whole read. An explicit
    upload map was already validated against the file, so it is left alone.
    'XY' (the coordinate presence check) is read whenever the file has it.
    """
    if not explicit:
        missing = [c for c in column_map if c not in available]
        if missing:
            logger.warning(f"{file_name}: mapped column(s) absent from the file, skipped: {missing}")
            column_map = {k: v for k, v in column_map.items() if k in available}
    columns = list(column_map)
    if 'XY' in available and 'XY' not in columns:
        columns.append('XY')
    return column_map, columns


def _coordinate_columns(column_map: dict[str, str], frame_columns) -> list[str]:
    """Columns a row must have to be ingestable: XY if present, else lat/lon.

    ``coordinates`` is NOT NULL, so a row without them would fail its batch.
    """
    if 'XY' in frame_columns:
        return ['XY']
    return [src for src, target in column_map.items()
            if target in ('lat', 'lon') and src in frame_columns]


def db_health_check():
    """Run a quick query to confirm the database is reachable. Raises on failure."""
    with _get_app().app_context():
        session = MyDb.get_db().session
        session.execute(db_text("SELECT 1"))
        logger.info("Database health check passed")


# ── Remote download ─────────────────────────────────────────────

def download_remote_files(data_folder: str) -> list[str]:
    """
    Downloads remote RDS files defined in the REMOTE_RDS_URLS environment variable.

    URLs should be semicolon-delimited. Optional auth can be configured via:
      - REMOTE_RDS_TOKEN: Bearer token
      - REMOTE_RDS_COOKIES: Comma-separated key=value pairs
      - REMOTE_RDS_HEADERS: Comma-separated key:value pairs

    SSRF protection is enforced:
      - Only HTTPS URLs are allowed (override with REMOTE_RDS_ALLOW_HTTP=true)
      - Private/internal IP addresses are blocked
      - Cloud metadata endpoints are blocked
      - Redirects are not followed unless redirect targets pass validation

    Download errors are logged and skipped gracefully.
    """
    from app.utils.downloader import configure_ssrf

    settings = get_settings()
    allowed_domains = [d.strip() for d in settings.REMOTE_RDS_ALLOWED_DOMAINS.split(",") if d.strip()] or None
    require_https = not settings.REMOTE_RDS_ALLOW_HTTP
    configure_ssrf(allowed_domains=allowed_domains, require_https=require_https)

    urls_raw = settings.REMOTE_RDS_URLS.strip()
    if not urls_raw:
        logger.info("No REMOTE_RDS_URLS defined, skipping remote download")
        return []

    urls = [u.strip() for u in urls_raw.split(";") if u.strip()]
    logger.info(f"Found {len(urls)} remote RDS URL(s) to download")

    token = settings.REMOTE_RDS_TOKEN.get_secret_value() or None
    cookies_raw = settings.REMOTE_RDS_COOKIES.get_secret_value()
    headers_raw = settings.REMOTE_RDS_HEADERS.get_secret_value()

    cookies = {}
    if cookies_raw:
        for pair in cookies_raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                cookies[k.strip()] = v.strip()

    headers = {}
    if headers_raw:
        for pair in headers_raw.split(","):
            if ":" in pair:
                k, v = pair.split(":", 1)
                headers[k.strip()] = v.strip()

    downloader = RDSDownloader(data_dir=data_folder, logger=logger)
    if token:
        downloader.set_bearer_token(token)
    if cookies:
        downloader.set_cookies(cookies)
    if headers:
        downloader.set_headers(headers)

    downloaded = []
    emit_event("file.download_start", url_count=len(urls))
    for url in urls:
        try:
            path = downloader.download(url)
            downloaded.append(path)
        except Exception as e:
            logger.warning(f"Skipping failed download {url}: {e}")

    emit_event("file.download_end", downloaded=len(downloaded), total=len(urls))
    logger.info(f"Downloaded {len(downloaded)}/{len(urls)} remote file(s)")
    return downloaded


# ── Conflict logging ─────────────────────────────────────────────

def _iter_frame_chunks(data, start: int, chunk_size: int):
    """Yield (offset, DataFrame) slices from an in-memory frame (RDS path)."""
    total = len(data)
    for offset in range(start, total, chunk_size):
        yield offset, data.iloc[offset:min(offset + chunk_size, total)]


def _iter_parquet_chunks(parquet_file, columns, start: int, chunk_size: int):
    """Yield (offset, DataFrame) from a Parquet file one batch at a time.

    Memory stays flat regardless of file size: only `chunk_size` rows are
    decoded at once, and pyarrow releases each batch before the next. `start`
    is a row offset so a partially-ingested file can resume; batches wholly
    before it are skipped without being converted to pandas at all.
    """
    offset = 0
    for batch in parquet_file.iter_batches(batch_size=chunk_size, columns=columns):
        frame = batch.to_pandas()
        length = len(frame)
        if length == 0:
            continue
        end = offset + length
        if end <= start:
            offset = end
            continue
        if offset < start:
            yield start, frame.iloc[start - offset:]
        else:
            yield offset, frame
        offset = end


# The columns of uq_planting_recommendation, in its order.
KEY_COLUMNS = ('country', 'province', 'lon', 'lat', 'variety', 'season_type', 'opt_date')


def _insert_statement():
    """INSERT … ON CONFLICT DO NOTHING RETURNING the key of each row inserted.

    Executed with a list of rows (executemany), so SQLAlchemy compiles it once
    and batches it itself. It used to be ``.values(rows)``: one statement with
    ~24,000 bind parameters, compiled afresh for every batch (~250 ms each).

    RETURNING is how the batch learns which rows went in. ``rowcount`` is -1
    for this statement under psycopg 3, so the old ``rowcount < len(rows)``
    check was always true: every batch ran the conflict lookup — a full table
    scan that grew with the table — and, because that lookup ran after the
    insert, recorded every row it had just inserted as a conflict.
    """
    return (insert(PlantingRecommendation)
            .on_conflict_do_nothing()
            .returning(*[getattr(PlantingRecommendation, c) for c in KEY_COLUMNS]))


def _as_real(value):
    """A coordinate as the float4 Postgres stores (lon/lat are REAL columns).

    36.817223 is stored as 36.81722259521484375, so the value RETURNING gives
    back is not the one sent, and a lookup with the float8 that was sent
    neither matches nor uses the index. Both sides are compared as float4.
    """
    import numpy as np
    return None if value is None else float(np.float32(value))


def _key(row) -> tuple:
    return tuple(_as_real(row.get(c)) if c in ('lon', 'lat') else row.get(c) for c in KEY_COLUMNS)


def _not_inserted(mappings, inserted) -> list[dict]:
    """The rows of a batch that ON CONFLICT skipped: those not RETURNed.

    A multiset, not a set: a key twice in one batch inserts once, and the
    second copy is a conflict.
    """
    from collections import Counter
    remaining = Counter(_key(dict(zip(KEY_COLUMNS, r))) for r in inserted)
    skipped = []
    for m in mappings:
        key = _key(m)
        if remaining[key]:
            remaining[key] -= 1
        else:
            skipped.append(m)
    return skipped


def _existing_rows_query(keys):
    """SELECT the key columns + check_sum of the rows already holding *keys*."""
    from sqlalchemy import REAL, and_, cast, literal, or_, select, tuple_
    columns = [getattr(PlantingRecommendation, c) for c in KEY_COLUMNS]

    def values(key):
        # lon/lat as REAL, or Postgres widens the column to float8 to compare
        # and the unique index cannot be used.
        return [cast(literal(v), REAL) if c in ('lon', 'lat') and v is not None else literal(v)
                for c, v in zip(KEY_COLUMNS, key)]

    conditions = []
    plain = [k for k in keys if None not in k]
    if plain:
        conditions.append(tuple_(*columns).in_([tuple_(*values(k)) for k in plain]))
    conditions.extend(
        and_(*[col.is_not_distinct_from(v) for col, v in zip(columns, values(key))])
        for key in keys if None in key)
    return select(*columns, PlantingRecommendation.check_sum).where(or_(*conditions))


def _log_batch_conflicts(session, skipped):
    """Record the rows ON CONFLICT skipped, with the checksum of the row they clash with.

    Only the skipped rows are looked up, and keys without NULLs go through a
    tuple IN that uses the unique index. Keys with a NULL need IS NOT DISTINCT
    FROM (`NULL = NULL` is never true, but the constraint is NULLS NOT
    DISTINCT), which no index serves — so only those few take that path.
    """
    if not skipped:
        return
    existing = {
        _key(dict(zip(KEY_COLUMNS, row[:-1]))): row[-1]
        for row in session.execute(_existing_rows_query({_key(m) for m in skipped}))
    }

    conflicts = []
    for m in skipped:
        record_data = {
            k: str(v) if not isinstance(v, (str, int, float, bool, list, dict)) and v is not None else v
            for k, v in m.items()
        }
        conflicts.append({
            'record_data': record_data,
            **{c: m.get(c) for c in KEY_COLUMNS},
            'check_sum': existing.get(_key(m)),
            'source': 'housekeeper',
        })
    session.execute(insert(ImportConflict), conflicts)


# ── File processing ─────────────────────────────────────────────

def process_file(
    file_path: str,
    batch_size: int = 1000,
    chunk_size: int = 10000,
    checkpoint_interval: int = 50,
    dry_run: bool = False,
    run_id: int | None = None,
    column_map: dict[str, str] | None = None,
    original_filename: str | None = None,
):
    """
    Processes a single file by reading its contents in chunks, converting data to
    :class:`PlantingDataRecord` instances, and inserting records into the database.
    Supports resumable processing via checkpoint-based commits.

    Accepts both ``.RDS`` (via pyreadr) and ``.parquet`` files.

    ``run_id`` associates this file with a :class:`ProcessingRun` so a batch of
    files submitted together can be tracked and reported as a unit.

    ``column_map`` comes from an upload the user mapped, and is used *as is*.
    Merging it over the defaults (what the sidecar path does) meant a column the
    user chose to skip was mapped anyway, and a default whose source column was
    absent from the file made pyarrow fail the whole read.
    """
    start_time = time.time()
    file_name = os.path.basename(file_path)

    if original_filename is None:
        original_filename = ""
        meta_path = file_path + '.meta.json'
        if os.path.isfile(meta_path):
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
                original_filename = meta.get('original_name', '')
            except (json.JSONDecodeError, OSError):
                pass

    # An explicit map (from an upload) was validated against the file; the
    # default/sidecar map was not, so only that one is trimmed to the schema.
    explicit_map = column_map is not None
    if column_map is None:
        column_map = load_column_map(file_path=file_path)

    def progress(status, current, total, message="", **kw):
        _write_progress(file_path, status, current, total, message, run_id=run_id,
                        original_filename=original_filename or None, **kw)

    lock = FileLock(file_path + INGEST_LOCK_SUFFIX, get_settings().INGEST_LOCK_STALE_SECONDS)
    # Initialised before the try so every error path can report the last
    # known position. Reporting 0/0 used to wipe it from the Jobs page.
    num_rows = 0
    checkpoint_row = 0

    with _get_app().app_context():
        session = MyDb.get_db().session
        checksum = None
        try:
            if not dry_run and not lock.acquire():
                # Another live worker owns this file. Writing an error here
                # would mark *its* healthy job as failed, so say nothing.
                logger.warning(f"{file_name} is already being ingested by another worker "
                               f"({lock.path}); skipping this run")
                return

            checksum = calculate_file_checksum(file_path, logger)

            if dry_run:
                existing = file_import_repo.get_by_checksum(checksum)
                if existing and existing.offset is None:
                    logger.info(f"[DRY RUN] {file_name} — already processed, would skip")
                elif existing and existing.offset is not None:
                    logger.info(f"[DRY RUN] {file_name} — partially processed at offset {existing.offset}, would resume")
                else:
                    logger.info(f"[DRY RUN] {file_name} — would process {os.path.getsize(file_path)} bytes")
                return

            # Nothing is uncommitted yet, so a retry may roll the session back
            # (a stale connection used to leave it unusable for every retry).
            existing = retry_db(lambda: file_import_repo.get_by_checksum(checksum), session=session)
            resume_offset = 0
            if existing and existing.offset is None and not get_settings().INGEST_DUPLICATE_CHECK:
                # Duplicate checking is off: import it again from the start.
                # Rows already stored are still skipped by the unique constraint.
                logger.info("file was imported before; importing again (INGEST_DUPLICATE_CHECK=false)",
                            file=file_name)
                existing = None
            if existing and existing.offset is None:
                logger.warning(f"File {file_name} is already fully processed. Checksum: {checksum}")
                # Report it, or a submitted duplicate shows nothing at all —
                # unless this file's job already says completed: then this is
                # a late redelivery of a task a resumed job already finished,
                # and writing 0/0 over it would erase the real result.
                if not _already_reported_complete(file_name, run_id):
                    progress('completed', 0, 0, 'Already ingested — nothing to do')
                return
            if existing and existing.offset is not None:
                resume_offset = existing.offset
                logger.info(f"Resuming {file_name} from row {resume_offset} (checksum: {checksum})")
            checkpoint_row = resume_offset

            # Case-insensitive: 'X.PARQUET' went down the pyreadr path and failed.
            is_parquet = file_path.lower().endswith('.parquet')

            if is_parquet:
                # Stream row groups instead of materialising the whole file.
                # A 3M-row upload would otherwise sit in memory in full, which is
                # what OOM-kills the worker container. Uploads are converted to
                # Parquet on arrival precisely so this path is the common one.
                import pyarrow.parquet as pq
                parquet_file = pq.ParquetFile(file_path)
                num_rows = parquet_file.metadata.num_rows
                column_map, columns = _select_columns(
                    column_map, parquet_file.schema_arrow.names, file_name, explicit_map)
                chunk_source = _iter_parquet_chunks(
                    parquet_file, columns, resume_offset, chunk_size
                )
            else:
                # pyreadr has no chunksize parameter, so an RDS cannot be read
                # incrementally — it must be fully deserialised. Uploads are
                # converted to Parquet at upload time, so this is the rarer path.
                result = pyreadr.read_r(file_path)
                data = result[None]
                num_rows = len(data)
                column_map, _ = _select_columns(
                    column_map, list(data.columns), file_name, explicit_map)
                chunk_source = _iter_frame_chunks(data, resume_offset, chunk_size)

            # Lock is held for the entire ingestion; no need to touch during deserialisation.

            if resume_offset >= num_rows:
                # Completed before the offset was cleared on success; finish
                # the bookkeeping so dedupe recognises it from now on.
                logger.warning(f"File {file_name} offset ({resume_offset}) >= total rows ({num_rows}), skipping")
                file_import_repo.upsert_offset(checksum, file_name, None, original_filename)
                progress('completed', num_rows, num_rows, 'Already ingested — nothing to do')
                return

            progress('processing', resume_offset, num_rows, 'Processing…')

            emit_event("file.processing_start", file=file_name, checksum=checksum,
                       total_rows=num_rows, resume_offset=resume_offset)
            logger.info(f"Processing file {file_name} in chunks of size {chunk_size}")

            # INGEST_CONFLICT_LOGGING=false still skips duplicates (the unique
            # constraint does that) but stops recording each one in
            # import_conflicts — an extra query per batch that had any.
            log_conflicts = get_settings().INGEST_CONFLICT_LOGGING
            insert_stmt = _insert_statement()
            failed_batches = 0
            completed_batches = 0
            batches_since_checkpoint = 0
            last_committed_row = resume_offset

            remaining = num_rows - resume_offset
            # Disable tqdm in background threads (no TTY); enable only for CLI runs.
            pbar = tqdm(
                total=remaining,
                unit="rows",
                desc=f"{file_name} (resumed at row {resume_offset})" if resume_offset else file_name,
                leave=False,
                disable=not sys.stdout.isatty(),
            )

            # Stops only between chunks: a chunk's rows and the offset after
            # it are committed together, so a resume never re-inserts rows.
            stopped = False
            try:
                for chunk_start, chunk in chunk_source:
                    if stop_requested():
                        stopped = True
                        break

                    lock.touch()
                    chunk_end = chunk_start + len(chunk)
                    pbar.update(len(chunk))

                    logger.debug(f"Processing chunk from rows {chunk_start} to {chunk_end} of {file_name}")

                    required = _coordinate_columns(column_map, chunk.columns)
                    filtered = chunk.dropna(subset=required) if required else chunk
                    skipped = len(chunk) - len(filtered)
                    if skipped:
                        logger.warning(f"Skipped {skipped} row(s) in chunk {chunk_start}-{chunk_end} due to empty coordinates")

                    if filtered.empty:
                        continue

                    records_data = filtered.replace({pd.NA: None, pd.NaT: None}).to_dict('records')

                    batch = []
                    for rd in records_data:
                        kwargs = {'check_sum': checksum}
                        for rds_col, target_attr in column_map.items():
                            value = rd.get(rds_col)
                            if target_attr == 'planting_option' and value is not None:
                                value = int(value)
                            kwargs[target_attr] = value
                        batch.append(PlantingRecommendationCreate(**kwargs))

                    for i in range(0, len(batch), batch_size):
                        sub = batch[i:i + batch_size]
                        try:
                            def _flush_batch(rows):
                                with session.begin_nested():
                                    mappings = [
                                        {
                                            **r.model_dump(),
                                            'coordinates': WKTElement(f"POINT({r.lon} {r.lat})", srid=4326)
                                            # `is not None`: 0 is a real
                                            # coordinate, and a NULL here fails
                                            # the NOT NULL column and with it the
                                            # whole batch.
                                            if r.lat is not None and r.lon is not None
                                            else None
                                        }
                                        for r in rows
                                    ]
                                    # No conflict target: any unique violation
                                    # is a duplicate. uq_planting_recommendation
                                    # is NULLS NOT DISTINCT on Postgres (see
                                    # migration f3a9c2d4e5b6), so a row with a
                                    # NULL key column now conflicts as well
                                    # instead of being inserted on every replay.
                                    inserted = session.execute(insert_stmt, mappings).all()
                                    if log_conflicts and len(inserted) < len(mappings):
                                        _log_batch_conflicts(session, _not_inserted(mappings, inserted))
                            retry_db(lambda: _flush_batch(sub), attempts=2)
                            completed_batches += 1
                            batches_since_checkpoint += 1
                        except SQLAlchemyError as e:
                            # A lost connection is not a bad batch: skipping
                            # it would advance the offset past rows that were
                            # never written. Abort the file and let the task
                            # retry from the last checkpoint instead.
                            if is_transient_db_error(e):
                                raise
                            failed_batches += 1
                            logger.exception(f"Batch {i // batch_size + 1} failed in {file_name}, skipping")

                    last_committed_row = chunk_end
                    progress('processing', last_committed_row, num_rows)

                    if batches_since_checkpoint >= checkpoint_interval:
                        # Rows and offset in ONE transaction. Committing the
                        # rows first left a window where a crash kept rows
                        # past the stored offset, which a resume re-inserted.
                        file_import_repo.upsert_offset(checksum, file_name, chunk_end,
                                                       original_filename, commit=False)
                        session.commit()
                        checkpoint_row = chunk_end
                        batches_since_checkpoint = 0
                        logger.debug(f"Checkpoint committed at row {chunk_end}")
                    if stop_requested():
                        stopped = True
                        break

            except BaseException as exc:
                pbar.close()
                # Roll back first, so nothing past the last checkpoint is kept.
                try:
                    session.rollback()
                except Exception:
                    pass
                # Exceptions are reported by the handlers below; this covers
                # KeyboardInterrupt/SystemExit, which they do not catch.
                if not isinstance(exc, Exception):
                    progress('error', checkpoint_row, num_rows, "Interrupted", error="Interrupted")
                raise

            pbar.close()

            # Drop references to the bulk frame before returning. Only the RDS
            # path holds one — Parquet streams batches — so a bare `del data`
            # would raise NameError there. gc.collect() matters more than the
            # delete: pandas/pyarrow return memory to the allocator in arenas,
            # so a large file's peak otherwise sticks around in the worker.
            data = None
            chunk_source = None
            gc.collect()

            if stopped:
                file_import_repo.upsert_offset(checksum, file_name, last_committed_row,
                                               original_filename, commit=False)
                session.commit()
                # Still 'processing' — it is not done and not failed. The
                # worker re-enqueues it on start (resume_interrupted_jobs); if
                # none starts, it goes stale and gets a Retry button.
                progress('processing', last_committed_row, num_rows,
                         f"Paused for a worker restart; resumes from row {last_committed_row:,}")
                logger.warning(
                    f"Stopping — committed {completed_batches} batches from {file_name}, "
                    f"offset {last_committed_row} persisted"
                )
            else:
                # offset=None is what every "already processed" check tests
                # for. Storing num_rows here meant no file was ever recognised
                # as done: re-uploads were reported "partial" and re-read.
                # Committed together with the final rows, as at a checkpoint.
                file_import_repo.upsert_offset(checksum, file_name, None,
                                               original_filename, commit=False)
                session.commit()
                if failed_batches:
                    note = f"{failed_batches} batch(es) failed and were skipped — see worker logs"
                    progress('completed', num_rows, num_rows, f"Done with errors: {note}", error=note)
                else:
                    progress('completed', num_rows, num_rows, "Done")
                # The API caches filters/coordinates/clusters in Redis for up
                # to 5 minutes; without this, newly ingested rows stayed
                # invisible until the cache expired.
                try:
                    from app.cache import invalidate_planting_data_cache
                    invalidate_planting_data_cache()
                except Exception:
                    logger.warning("Could not invalidate the planting data cache", exc_info=True)
                logger.info(f"File {file_name} fully processed and recorded")

            elapsed = time.time() - start_time
            emit_event("file.processing_end", file=file_name, checksum=checksum,
                       elapsed=round(elapsed, 2), completed_batches=completed_batches,
                       failed_batches=failed_batches, shutdown=stopped)

            if failed_batches:
                logger.warning(f"File {file_name} processed with {failed_batches} failed batch(es)")

        except Exception as e:
            try:
                session.rollback()
            except Exception:
                pass
            # The position reported is the last checkpoint: after the rollback
            # nothing past it exists, and it is where a retry will resume.
            if is_transient_db_error(e):
                msg = f"Database unavailable, will retry from row {checkpoint_row}: {e}"
                progress('error', checkpoint_row, num_rows, msg, error=str(e))
                logger.error(f"Transient database error processing {file_name}: {e}")
                # Re-raised so Celery's autoretry_for fires. Swallowing every
                # exception here meant retries never happened at all.
                raise
            progress('error', checkpoint_row, num_rows, str(e), error=str(e))
            if isinstance(e, FileNotFoundError):
                logger.exception(f"Failed to process file {file_name}: {e}")
            else:
                logger.exception(f"Unexpected error processing file {file_name}: {e}")
        finally:
            lock.release()
            elapsed_time = time.time() - start_time
            if elapsed_time > 60:
                minutes, seconds = divmod(elapsed_time, 60)
                logger.info(f"Processing file {file_name} took {int(minutes)} minutes and {seconds:.2f} seconds")
            else:
                logger.info(f"Processing file {file_name} took {elapsed_time:.2f} seconds")


# ── Batch processing ───────────────────────────────────────────

def load_rds_to_db(data_folder: str, batch_size: int = 1000, chunk_size: int = 5000,
                   checkpoint_interval: int = 50, max_workers: int = 1, dry_run: bool = False):
    """
    Loads and processes all RDS/Parquet files from *data_folder* using a thread pool.

    Remote RDS files defined in the ``REMOTE_RDS_URLS`` env var are downloaded first.
    RDS files in the data directory are converted to Parquet for efficient streaming.
    """
    os.makedirs(data_folder, exist_ok=True)
    global_start_time = time.time()

    db_health_check()
    download_remote_files(data_folder)

    # Convert any RDS files in the data directory to Parquet for efficient streaming.
    # This matches the upload flow where RDS is converted at inspection time.
    from app.utils.rds_to_parquet import batch_convert
    converted = batch_convert(data_folder, pattern=".rds", delete_originals=True,
                              on_file=None if dry_run else report_conversion)
    if converted:
        logger.info(f"Converted {len(converted)} RDS file(s) to Parquet")

    file_paths = [
        os.path.join(data_folder, f)
        for f in os.listdir(data_folder)
        if f.lower().endswith(('.rds', '.parquet'))
    ]

    logger.info(f"Starting to process {len(file_paths)} file(s) from {data_folder}")
    emit_event("housekeeping.start", file_count=len(file_paths), data_folder=data_folder)

    failed_count = 0
    with _get_app().app_context():
        # Files whose columns the default mapping cannot use wait for a person
        # to map them on the Upload page, instead of importing nothing.
        file_paths = hold_unmapped(file_paths, dry_run=dry_run)

        # ONE run for the whole scan, so its files are reported as a batch and
        # each gets its own history row instead of overwriting the last one.
        run_id = None
        if file_paths and not dry_run:
            try:
                from app.services.progress_store import start_run
                run_id = start_run(label=f"Scan of {os.path.basename(os.path.normpath(data_folder))}",
                                   source='scan', total_files=len(file_paths))
            except Exception as e:
                logger.warning(f"Could not open a processing run for the scan: {e}")

        failed_files = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(process_file, fp, batch_size, chunk_size, checkpoint_interval,
                                dry_run, run_id): fp
                for fp in file_paths
            }
            for future in concurrent.futures.as_completed(futures):
                fp = futures[future]
                try:
                    future.result()
                    logger.info(f"Completed processing {os.path.basename(fp)}")
                except Exception as e:
                    failed_files.append((fp, e))
                    failed_count += 1
                    logger.exception(f"Failed to process {os.path.basename(fp)}: {e}")

        if failed_files:
            logger.error(
                f"Processed {len(file_paths) - len(failed_files)}/{len(file_paths)} file(s), "
                f"{len(failed_files)} failed"
            )
            for fp, exc in failed_files:
                logger.error(f"  {os.path.basename(fp)}: {exc}")

    global_elapsed_time = time.time() - global_start_time
    emit_event("housekeeping.end", elapsed=round(global_elapsed_time, 2), failed=failed_count)
    if global_elapsed_time > 60:
        minutes, seconds = divmod(global_elapsed_time, 60)
        logger.info(f"Processing all files took {int(minutes)} minutes and {seconds:.2f} seconds")
    else:
        logger.info(f"Processing all files took {global_elapsed_time:.2f} seconds")


# ── File watcher ───────────────────────────────────────────────

def watch_directory(
    data_folder: str,
    batch_size: int = 1000,
    chunk_size: int = 10000,
    checkpoint_interval: int = 50,
    dry_run: bool = False,
):
    """Watch *data_folder* for new files and process them as they arrive.

    RDS files are converted to Parquet on startup and when new ones appear.
    """
    try:
        from watchdog.observers.polling import PollingObserver as Observer
    except ImportError:
        logger.error("watchdog is required for --watch mode: poetry add watchdog")
        raise

    os.makedirs(data_folder, exist_ok=True)

    # Convert any existing RDS files to Parquet before starting the watcher.
    from app.utils.rds_to_parquet import batch_convert
    converted = batch_convert(data_folder, pattern=".rds", delete_originals=True,
                              on_file=None if dry_run else report_conversion)
    if converted:
        logger.info(f"Converted {len(converted)} existing RDS file(s) to Parquet")

    from app.services.watch_handler import RDSFileHandler

    event_handler = RDSFileHandler(
        batch_size=batch_size,
        chunk_size=chunk_size,
        checkpoint_interval=checkpoint_interval,
        dry_run=dry_run,
    )
    observer = Observer(timeout=1)
    observer.schedule(event_handler, data_folder, recursive=False)
    observer.start()
    logger.info(f"Watching {data_folder} for new files (polling every 1s)...")
    logger.info(f"Existing files: {[f for f in os.listdir(data_folder) if f.endswith(('.RDS', '.rds', '.parquet'))]}")

    try:
        while True:
            time.sleep(1)
            if shutdown_requested:
                logger.warning("Shutdown requested — stopping file watcher")
                break
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


# ── Celery restart helpers (used by worker) ─────────────────────

def release_dead_locks(data_folder: str, hostname: str) -> int:
    """Remove ingest locks this host left behind. Call before any task runs.

    A worker killed mid-ingest (container restart past its grace period, OOM)
    cannot release its lock, and a lock looks live for
    INGEST_LOCK_STALE_SECONDS — so the resumed job found it "held by another
    worker" and silently did nothing. When this host's worker is only just
    starting, nothing on it can be holding one.
    """
    removed = 0
    for root, _dirs, files in os.walk(data_folder):
        for name in files:
            if not name.endswith(INGEST_LOCK_SUFFIX):
                continue
            path = os.path.join(root, name)
            if FileLock.holder_host(path) == hostname:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    logger.warning(f"Could not remove dead ingest lock {path}", exc_info=True)
    if removed:
        logger.info("released dead ingest locks", count=removed, host=hostname)
    return removed


def resume_interrupted_jobs(hostname: str) -> int:
    """Re-enqueue the jobs this host was running when its worker stopped.

    Called once the worker is up. A job still 'processing' (or gone 'stale')
    with worker == this host cannot be running: this worker has just started.
    It continues in its own run, from its stored offset, with the mapping
    saved with its upload — what the Jobs page's Retry does, done for you.
    Covers both a cooperative stop ("Paused for a worker restart") and a hard
    kill, whose unacked message Redis would only redeliver hours later.
    """
    from app.services import uploads as upload_service
    from app.services.progress_store import load_all_jobs

    resumed = 0
    for job in load_all_jobs():
        if job.get('worker') != hostname or job.get('status') not in ('processing', 'stale'):
            continue
        name = job.get('file') or ''
        try:
            upload = upload_service.find_by_stored_name(name)
            if upload is not None and upload.stored_path and os.path.isfile(upload.stored_path):
                from app.services.housekeeper import process_file_async
                queued = process_file_async(upload.stored_path, column_map=upload.column_map,
                                            original_filename=upload.original_name, run_id=job.get('run_id'))
            else:
                path = os.path.join(get_settings().HOUSEKEEPING_DATA_DIR, name)
                if not os.path.isfile(path):
                    logger.warning("interrupted job's file is gone; not resuming", file=name)
                    continue
                from app.services.housekeeper import process_file_async
                queued = process_file_async(path, original_filename=job.get('original_name'),
                                            run_id=job.get('run_id'))
        except Exception:
            logger.exception("could not resume interrupted job", file=name)
            continue
        if queued:
            resumed += 1
            logger.info("resumed interrupted job", file=name, run_id=job.get('run_id'), row=job.get('current'))
    return resumed


# ── Celery helpers (used by worker) ─────────────────────────────

def _enqueue_or_warn(task, **kwargs) -> bool:
    """Enqueue a Celery task, or log a warning if the broker is unreachable.

    Returns whether the task was published, so a caller can tell the user the
    truth instead of "Processing started" regardless.
    """
    if not celery_broker_available():
        logger.warning(
            f"Cannot enqueue {task.__name__} — Redis at "
            f"{get_settings().CELERY_BROKER_URL} "
            f"is not reachable. Start Redis or disable HOUSEKEEPING_ENABLED."
        )
        return False

    result = []

    def _send():
        try:
            r = task.delay(**kwargs)
            result.append(r)
        except Exception as e:
            result.append(e)

    t = threading.Thread(target=_send, daemon=True)
    t.start()
    t.join(timeout=2)

    if t.is_alive():
        logger.warning(
            f"Timed out enqueuing {task.__name__} — "
            f"Kombu/Celery cannot connect to Redis at "
            f"{get_settings().CELERY_BROKER_URL}. "
            f"Run the worker in Docker for housekeeping, or set HOUSEKEEPING_ENABLED=false."
        )
        return False

    if result and isinstance(result[0], Exception):
        logger.warning(f"Failed to enqueue {task.__name__}: {result[0]}")
        return False
    return True


# (answer, valid until): asking Celery takes up to a second, and the Upload
# page polls every 1.5 s.
WORKER_CHECK_CACHE_SECONDS = 30
_worker_check = (False, 0.0)


def celery_worker_available(timeout: float = 1.0) -> bool:
    """Is a Celery worker consuming this app's queue right now? (cached briefly)

    A reachable broker is not enough: production's worker ran
    HOUSEKEEPING_MODE=oneshot (no Celery), so a task sent there waited
    forever. And another project's worker on the same Redis would answer a
    plain ping, so this asks which queues the workers consume.
    """
    global _worker_check
    if not get_settings().HOUSEKEEPING_ENABLED:
        return False
    answer, valid_until = _worker_check
    if time.time() < valid_until:
        return answer
    answer = _ask_for_worker(timeout)
    _worker_check = (answer, time.time() + WORKER_CHECK_CACHE_SECONDS)
    return answer


def _ask_for_worker(timeout: float) -> bool:
    if not celery_broker_available():
        return False
    queue = get_settings().CELERY_TASK_DEFAULT_QUEUE
    found = []

    def ask():
        try:
            from app.celery_app import celery_app
            active = celery_app.control.inspect(timeout=timeout).active_queues() or {}
            found.append(any(q.get('name') == queue for queues in active.values() for q in queues or []))
        except Exception:
            found.append(False)

    t = threading.Thread(target=ask, daemon=True)
    t.start()
    t.join(timeout + 2)
    return bool(found and found[0])


# ── Async wrappers (Flask integration, Celery only) ──────────────

def process_file_async(file_path: str, app=None, column_map: dict | None = None,
                       original_filename: str | None = None, run_id: int | None = None) -> bool:
    """Enqueue a single file for processing via Celery (non-blocking).

    Used by the upload API endpoint and worker restart recovery.

    ``file_path`` is coerced to ``str`` here, at the Celery boundary, rather
    than trusted from callers. Both call sites build a ``pathlib.Path`` (and
    both are annotated ``str``, which nothing checked), and Celery serialises
    task arguments as JSON — so a ``Path`` reached the broker and failed with
    "Object of type WindowsPath is not JSON serializable". The upload still
    returned 200, so the file was stored and silently never ingested.
    """
    from app.tasks import process_file_task
    kwargs = {'file_path': str(file_path)}
    # Only when given, so tasks already queued by an older API keep the same
    # shape and the worker falls back to the sidecar mapping for them.
    if column_map is not None:
        kwargs['column_map'] = column_map
    if original_filename:
        kwargs['original_filename'] = original_filename

    # A run per submission. Without a run_id every attempt at a file matched
    # the same progress row, so each one overwrote the previous attempt's
    # history. Best effort: no run is better than no ingest. A given run_id
    # continues that run (resuming an interrupted job), so it is not discarded.
    resuming = run_id is not None
    if not resuming:
        try:
            from app.services.progress_store import start_run
            run_id = start_run(label=original_filename or os.path.basename(str(file_path)),
                               source='upload', total_files=1)
        except Exception as e:
            logger.warning(f"Could not open a processing run for {file_path}: {e}")
    if run_id is not None:
        kwargs['run_id'] = int(run_id)

    queued = bool(_enqueue_or_warn(process_file_task, **kwargs))
    if not queued and run_id is not None and not resuming:
        # Nothing will ever report into it, and an empty run reads as
        # 'pending' forever.
        try:
            from app.services.progress_store import discard_run
            discard_run(run_id)
        except Exception as e:
            logger.warning(f"Could not discard unused run {run_id}: {e}")
    return queued


# ── Celery startup scan (used when HOUSEKEEPING_ENABLED=true) ───

def _claim_process_pending() -> bool:
    """Whether this process should enqueue the startup scan.

    create_app() calls process_pending() in every gunicorn worker, so two
    workers enqueued two identical scans of the data directory. A Redis
    ``SET NX EX`` lets the first one through and drops the rest for a few
    minutes. If Redis cannot be reached, fall through: the enqueue below will
    hit the same broker and warn about it properly.
    """
    try:
        from redis import Redis
        client = Redis.from_url(get_settings().CELERY_BROKER_URL, socket_connect_timeout=2, socket_timeout=2)
        return bool(client.set(PROCESS_PENDING_DEDUPE_KEY, str(os.getpid()),
                               nx=True, ex=get_settings().STARTUP_SCAN_COOLDOWN_SECONDS))
    except Exception as e:
        logger.debug(f"process_pending de-dupe unavailable, not de-duplicating: {e}")
        return True


def process_pending(app=None):
    """Enqueue background processing of unprocessed files via Celery.

    Called once at Flask app startup — once per gunicorn worker, hence the
    de-dupe guard. Only runs when HOUSEKEEPING_ENABLED=true.
    """
    from app.tasks import process_pending_task
    if not _claim_process_pending():
        logger.info("Startup scan already enqueued by another worker; skipping")
        return
    _enqueue_or_warn(process_pending_task)


STARTUP_SCAN_MARKER = '.startup-scan'
_STARTUP_SCAN_HEARTBEAT_SECONDS = 30


def start_startup_scan(app) -> str:
    """Scan the data folder once per deployment, from one place.

    Called by create_app(), i.e. in every gunicorn worker and again whenever
    one is recycled (MAX_REQUESTS). Each of them used to run its own scan on a
    thread: concurrent RDS conversions collided, every recycle re-listed and
    re-checksummed the folder, and large files were read whole inside the
    memory-capped API container.

    - HOUSEKEEPING_ENABLED: the scan is queued for the Celery worker instead
      (process_pending, de-duplicated across processes through Redis), so the
      API never imports files itself.
    - Otherwise one API process runs it on a thread (run_startup_scan).

    Returns what happened, for logs and tests: 'disabled', 'queued' or 'thread'.
    """
    settings = get_settings()
    if not settings.STARTUP_SCAN:
        return 'disabled'
    if settings.HOUSEKEEPING_ENABLED:
        process_pending()
        return 'queued'
    threading.Thread(target=run_startup_scan, args=(app,), name='startup-scan', daemon=True).start()
    return 'thread'


def run_startup_scan(app) -> bool:
    """Run the startup scan here unless another process ran or runs it.

    The claim is ``<data dir>/.startup-scan``, an O_EXCL FileLock whose staleness
    is STARTUP_SCAN_COOLDOWN_SECONDS. A heartbeat keeps it fresh while the scan
    runs, so no other process starts one meanwhile; afterwards the file is left
    in place on purpose, so a process booting within the cooldown — a sibling
    gunicorn worker, a recycled one — skips its scan. After the cooldown the
    marker counts as stale and the next boot scans again.
    """
    settings = get_settings()
    folder = settings.HOUSEKEEPING_DATA_DIR
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        logger.exception("startup scan skipped: data folder unavailable", folder=folder)
        return False
    claim = FileLock(os.path.join(folder, STARTUP_SCAN_MARKER), settings.STARTUP_SCAN_COOLDOWN_SECONDS)
    if not claim.acquire():
        logger.info("startup scan skipped: another process ran it recently or is running it")
        return False

    stop = threading.Event()

    def heartbeat():
        while not stop.wait(_STARTUP_SCAN_HEARTBEAT_SECONDS):
            claim.touch()

    threading.Thread(target=heartbeat, name='startup-scan-heartbeat', daemon=True).start()
    try:
        set_app(app)
        with app.app_context():
            load_rds_to_db(
                data_folder=folder,
                max_workers=settings.HOUSEKEEPING_MAX_WORKERS,
                **housekeeping_settings(),
            )
    except Exception:
        logger.exception("startup scan failed")
    finally:
        stop.set()
        claim.touch()  # the cooldown starts when the scan ends; the file stays
    return True


# ── Housekeeping runner ──────────────────────────────────────────

def run(
    data_folder: str | None = None,
    *,
    watch: bool = False,
    dry_run: bool = False,
    batch_size: int | None = None,
    chunk_size: int | None = None,
    checkpoint_interval: int | None = None,
) -> None:
    """Run housekeeping using the supplied configuration.

    ``None`` values fall back to the corresponding housekeeping settings.
    Explicit values always take precedence over environment configuration.

    This function is intentionally independent of any CLI framework so it can
    be called directly by the CLI, tests, scheduled jobs, or other application
    entry points.
    """
    settings = housekeeping_settings()

    if batch_size is not None:
        settings['batch_size'] = batch_size

    if chunk_size is not None:
        settings['chunk_size'] = chunk_size

    if checkpoint_interval is not None:
        settings['checkpoint_interval'] = checkpoint_interval

    if data_folder is None:
        data_folder = get_settings().HOUSEKEEPING_DATA_DIR

    os.makedirs(data_folder, exist_ok=True)

    # The housekeeping runner owns this process only when called directly.
    # Do not install handlers at module import time because this module is also
    # imported by Flask and Celery.
    install_signal_handlers()

    set_app(_get_app())

    if watch:
        watch_directory(
            data_folder,
            dry_run=dry_run,
            **settings,
        )
        return

    with _get_app().app_context():
        load_rds_to_db(
            data_folder=data_folder,
            dry_run=dry_run,
            max_workers=get_settings().HOUSEKEEPING_MAX_WORKERS,
            **settings,
        )