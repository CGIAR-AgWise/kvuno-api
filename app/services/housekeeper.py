"""
Housekeeping — RDS/Parquet file ingestion service.

Used as the primary processing module by both the Flask app (upload API,
startup background job) and the standalone CLI (``python housekeeping.py``).
"""
import concurrent.futures
import gc
import json
import os
import signal
import socket
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

# How long process_pending()'s de-dupe key lives. create_app() calls it once
# per gunicorn worker, so without this N workers enqueued N identical scans.
PROCESS_PENDING_DEDUPE_KEY = 'kvuno:process_pending'
PROCESS_PENDING_DEDUPE_SECONDS = 300


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


def _write_progress(file_path: str, status: str, current: int, total: int, message: str = "",
                    run_id: int | None = None, error: str | None = None,
                    original_filename: str | None = None):
    """Record progress for one file.

    ``run_id`` groups the files submitted together so the UI can report batch
    progress; ``error`` is stored separately from ``message`` so routine
    progress updates cannot erase why a job failed.
    """
    from app.services.progress_store import save_progress
    save_progress(
        file_path, status, current, total, message,
        run_id=run_id, error=error,
        # HOSTNAME is the container's, set by the runtime: not a setting.
        worker=os.getenv('HOSTNAME') or socket.gethostname(),
        original_filename=original_filename or os.path.basename(file_path),
    )


# ── DB helpers ─────────────────────────────────────────────────

def retry_db(fn, attempts: int = 3, base_delay: float = 1.0):
    """Call *fn* with retries on SQLAlchemyError using exponential backoff."""
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except SQLAlchemyError as e:
            last_exc = e
            if attempt < attempts:
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


# ── Remote download ────────────────────────────────────────────

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


# ── Conflict logging ───────────────────────────────────────────

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


def _log_batch_conflicts(session, mappings):
    from sqlalchemy import and_, or_
    unique_cols = ['country', 'province', 'lon', 'lat', 'variety', 'season_type', 'opt_date']
    key_tuples = {
        tuple(m.get(c) for c in unique_cols)
        for m in mappings
    }
    # IS NOT DISTINCT FROM, not a tuple IN: `NULL = NULL` is never true, so a
    # key with any NULL never matched and its conflict went unlogged — even
    # though the constraint (NULLS NOT DISTINCT) does reject it.
    existing = session.query(PlantingRecommendation).filter(or_(*[
        and_(*[getattr(PlantingRecommendation, c).is_not_distinct_from(v)
               for c, v in zip(unique_cols, key)])
        for key in key_tuples
    ])).all()
    existing_keys = {
        tuple(getattr(e, c) for c in unique_cols): e.check_sum
        for e in existing
    }
    for m in mappings:
        key = tuple(m.get(c) for c in unique_cols)
        if key in existing_keys:
            record_data = {
                k: str(v) if not isinstance(v, (str, int, float, bool, list, dict)) and v is not None else v
                for k, v in m.items()
            }
            conflict = ImportConflict(
                record_data=record_data,
                country=m.get('country'),
                province=m.get('province'),
                lon=m.get('lon'),
                lat=m.get('lat'),
                variety=m.get('variety'),
                season_type=m.get('season_type'),
                opt_date=m.get('opt_date'),
                check_sum=existing_keys[key],
                source='housekeeper',
            )
            session.add(conflict)


# ── File processing ────────────────────────────────────────────

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

            existing = retry_db(lambda: file_import_repo.get_by_checksum(checksum))
            resume_offset = 0
            if existing and existing.offset is None:
                logger.warning(f"File {file_name} is already fully processed. Checksum: {checksum}")
                # Report it, or a submitted duplicate shows nothing at all.
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
            lock.touch()  # a large RDS can take minutes to deserialise

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

            failed_batches = 0
            completed_batches = 0
            batches_since_checkpoint = 0
            last_committed_row = resume_offset

            remaining = num_rows - resume_offset
            pbar = tqdm(
                total=remaining,
                unit="rows",
                desc=f"{file_name} (resumed at row {resume_offset})" if resume_offset else file_name,
                leave=False,
                file=__import__('sys').stderr,
            )

            try:
                for chunk_start, chunk in chunk_source:
                    if shutdown_requested:
                        logger.warning("Shutdown requested — breaking after current chunk")
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
                        if shutdown_requested:
                            logger.warning("Shutdown requested — breaking after current batch")
                            break

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
                                    stmt = insert(PlantingRecommendation).values(mappings).on_conflict_do_nothing()
                                    result = session.execute(stmt)
                                    if result.rowcount < len(mappings):
                                        _log_batch_conflicts(session, mappings)
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

                    if not shutdown_requested:
                        last_committed_row = chunk_end
                        progress('processing', last_committed_row, num_rows)

                    if batches_since_checkpoint >= checkpoint_interval and not shutdown_requested:
                        # Rows and offset in ONE transaction. Committing the
                        # rows first left a window where a crash kept rows
                        # past the stored offset, which a resume re-inserted.
                        file_import_repo.upsert_offset(checksum, file_name, chunk_end,
                                                       original_filename, commit=False)
                        session.commit()
                        checkpoint_row = chunk_end
                        batches_since_checkpoint = 0
                        logger.debug(f"Checkpoint committed at row {chunk_end}")
                    if shutdown_requested:
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

            if shutdown_requested:
                file_import_repo.upsert_offset(checksum, file_name, last_committed_row,
                                               original_filename, commit=False)
                session.commit()
                logger.warning(
                    f"Graceful shutdown — committed {completed_batches} batches from {file_name}, "
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
                       failed_batches=failed_batches, shutdown=shutdown_requested)

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
    """
    os.makedirs(data_folder, exist_ok=True)
    global_start_time = time.time()

    db_health_check()
    download_remote_files(data_folder)

    file_paths = [
        os.path.join(data_folder, f)
        for f in os.listdir(data_folder)
        if f.lower().endswith(('.rds', '.parquet'))
    ]

    logger.info(f"Starting to process {len(file_paths)} file(s) from {data_folder}")
    emit_event("housekeeping.start", file_count=len(file_paths), data_folder=data_folder)

    failed_count = 0
    with _get_app().app_context():
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
    """Watch *data_folder* for new files and process them as they arrive."""
    try:
        from watchdog.observers.polling import PollingObserver as Observer
    except ImportError:
        logger.error("watchdog is required for --watch mode: poetry add watchdog")
        raise

    os.makedirs(data_folder, exist_ok=True)

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


# ── Celery helpers ─────────────────────────────────────────────

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


# ── Async wrappers (Flask integration) ─────────────────────────

def process_file_async(file_path: str, app=None, column_map: dict | None = None,
                       original_filename: str | None = None) -> bool:
    """Enqueue a single file for processing via Celery (non-blocking).

    Used by the upload API endpoint.

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
    # history. Best effort: no run is better than no ingest.
    run_id = None
    try:
        from app.services.progress_store import start_run
        run_id = start_run(label=original_filename or os.path.basename(str(file_path)),
                           source='upload', total_files=1)
    except Exception as e:
        logger.warning(f"Could not open a processing run for {file_path}: {e}")
    if run_id is not None:
        kwargs['run_id'] = int(run_id)

    queued = bool(_enqueue_or_warn(process_file_task, **kwargs))
    if not queued and run_id is not None:
        # Nothing will ever report into it, and an empty run reads as
        # 'pending' forever.
        try:
            from app.services.progress_store import discard_run
            discard_run(run_id)
        except Exception as e:
            logger.warning(f"Could not discard unused run {run_id}: {e}")
    return queued


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
                               nx=True, ex=PROCESS_PENDING_DEDUPE_SECONDS))
    except Exception as e:
        logger.debug(f"process_pending de-dupe unavailable, not de-duplicating: {e}")
        return True


def process_pending(app=None):
    """Enqueue background processing of unprocessed files via Celery.

    Called once at Flask app startup — once per gunicorn worker, hence the
    de-dupe guard.
    """
    from app.tasks import process_pending_task
    if not _claim_process_pending():
        logger.info("Startup scan already enqueued by another worker; skipping")
        return
    _enqueue_or_warn(process_pending_task)


# ── CLI dispatcher ─────────────────────────────────────────────

def cli_run(args):
    """Entry point for the ``python housekeeping.py`` CLI.

    Merges CLI args (which take precedence) over env-var sourced defaults.
    """
    settings = housekeeping_settings()
    for key in ('batch_size', 'chunk_size', 'checkpoint_interval'):
        cli_val = getattr(args, key, None)
        if cli_val is not None:
            settings[key] = cli_val

    # Only here: the CLI is the one entry point that owns its process. See
    # install_signal_handlers() for why this is not done at import time.
    install_signal_handlers()

    data_folder = args.data_folder or get_settings().HOUSEKEEPING_DATA_DIR
    os.makedirs(data_folder, exist_ok=True)
    set_app(_get_app())

    if args.watch:
        watch_directory(data_folder, dry_run=args.dry_run, **settings)
    else:
        with _get_app().app_context():
            load_rds_to_db(
                data_folder=data_folder,
                dry_run=args.dry_run,
                max_workers=get_settings().HOUSEKEEPING_MAX_WORKERS,
                **settings,
            )
