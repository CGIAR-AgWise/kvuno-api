"""
Housekeeping — RDS/Parquet file ingestion service.

Used as the primary processing module by both the Flask app (upload API,
startup background job) and the standalone CLI (``python housekeeping.py``).
"""
import concurrent.futures
import json
import os
import signal
import threading
import time

import pandas as pd
import pyreadr
from dotenv import load_dotenv
from geoalchemy2 import WKTElement
from sqlalchemy import text as db_text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from tqdm import tqdm

from app import create_app
from app.config import (
    CELERY_BROKER_URL,
    HOUSEKEEPING_BATCH_SIZE,
    HOUSEKEEPING_CHECKPOINT_INTERVAL,
    HOUSEKEEPING_CHUNK_SIZE,
    HOUSEKEEPING_DATA_DIR,
    HOUSEKEEPING_MAX_WORKERS,
    celery_broker_available,
)
from app.dto.planting_recommendation import PlantingRecommendationCreate
from app.models.database_conn import MyDb
from app.models.kvuno import PlantingRecommendation, ImportConflict
from app.repo.planting_recommendation import PlantingRecommendationRepo
from app.repo.file_import import FileImportRepo
from app.utils import calculate_file_checksum
from app.utils.downloader import RDSDownloader
from app.utils.logging import SharedLogger

load_dotenv()

shared_logger = SharedLogger()
logger = shared_logger.get_logger()

_app = None
DATA_DIR = HOUSEKEEPING_DATA_DIR


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
    """Return current housekeeping parameters (read from centralized config)."""
    return {
        'batch_size': HOUSEKEEPING_BATCH_SIZE,
        'chunk_size': HOUSEKEEPING_CHUNK_SIZE,
        'checkpoint_interval': HOUSEKEEPING_CHECKPOINT_INTERVAL,
        'max_workers': HOUSEKEEPING_MAX_WORKERS,
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


try:
    signal.signal(signal.SIGTERM, _handle_sigterm)
except (ValueError, AttributeError):
    pass
signal.signal(signal.SIGINT, _handle_sigint)


# ── Telemetry ──────────────────────────────────────────────────

def emit_event(event: str, **kwargs):
    """Write a structured JSON event line to stderr for monitoring ingestion."""
    record = {"event": event, "timestamp": time.time()}
    record.update(kwargs)
    print(json.dumps(record), file=__import__('sys').stderr, flush=True)


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
    raw = os.getenv('RDS_COLUMN_MAP')
    if raw:
        try:
            overrides = json.loads(raw)
            default_map.update(overrides)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Invalid RDS_COLUMN_MAP JSON, using defaults: {e}")

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


def _write_progress(file_path: str, status: str, current: int, total: int, message: str = ""):
    from app.services.progress_store import save_progress
    save_progress(file_path, status, current, total, message)


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

    allowed_domains_raw = os.getenv("REMOTE_RDS_ALLOWED_DOMAINS", "").strip()
    allowed_domains = [d.strip() for d in allowed_domains_raw.split(",") if d.strip()] or None
    require_https = os.getenv("REMOTE_RDS_ALLOW_HTTP", "").lower() != "true"
    configure_ssrf(allowed_domains=allowed_domains, require_https=require_https)

    urls_raw = os.getenv("REMOTE_RDS_URLS", "").strip()
    if not urls_raw:
        logger.info("No REMOTE_RDS_URLS defined, skipping remote download")
        return []

    urls = [u.strip() for u in urls_raw.split(";") if u.strip()]
    logger.info(f"Found {len(urls)} remote RDS URL(s) to download")

    token = os.getenv("REMOTE_RDS_TOKEN")
    cookies_raw = os.getenv("REMOTE_RDS_COOKIES")
    headers_raw = os.getenv("REMOTE_RDS_HEADERS")

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

def _log_batch_conflicts(session, mappings):
    from sqlalchemy import tuple_
    unique_cols = ['country', 'province', 'lon', 'lat', 'variety', 'season_type', 'opt_date']
    key_tuples = [
        tuple(m.get(c) for c in unique_cols)
        for m in mappings
    ]
    existing = session.query(PlantingRecommendation).filter(
        tuple_(*[getattr(PlantingRecommendation, c) for c in unique_cols]).in_(key_tuples)
    ).all()
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
):
    """
    Processes a single file by reading its contents in chunks, converting data to
    :class:`PlantingDataRecord` instances, and inserting records into the database.
    Supports resumable processing via checkpoint-based commits.

    Accepts both ``.RDS`` (via pyreadr) and ``.parquet`` files.
    """
    start_time = time.time()
    file_name = os.path.basename(file_path)

    original_filename = ""
    meta_path = file_path + '.meta.json'
    if os.path.isfile(meta_path):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            original_filename = meta.get('original_name', '')
        except (json.JSONDecodeError, OSError):
            pass

    column_map = load_column_map(file_path=file_path)

    with _get_app().app_context():
        checksum = None
        try:
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
                return
            if existing and existing.offset is not None:
                resume_offset = existing.offset
                logger.info(f"Resuming {file_name} from row {resume_offset} (checksum: {checksum})")

            column_names = list(column_map.keys())
            if file_path.endswith('.parquet'):
                data = pd.read_parquet(file_path, columns=column_names + ['XY'])
            else:
                result = pyreadr.read_r(file_path)
                data = result[None]
            num_rows = len(data)
            _write_progress(file_path, 'processing', resume_offset, num_rows, 'Processing…')

            if resume_offset >= num_rows:
                logger.warning(f"File {file_name} offset ({resume_offset}) >= total rows ({num_rows}), skipping")
                return

            emit_event("file.processing_start", file=file_name, checksum=checksum,
                       total_rows=num_rows, resume_offset=resume_offset)
            logger.info(f"Processing file {file_name} in chunks of size {chunk_size}")

            session = MyDb.get_db().session
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
                for chunk_start in range(resume_offset, num_rows, chunk_size):
                    if shutdown_requested:
                        logger.warning("Shutdown requested — breaking after current chunk")
                        break

                    chunk_end = min(chunk_start + chunk_size, num_rows)
                    pbar.update(chunk_end - chunk_start)
                    chunk = data.iloc[chunk_start:chunk_end]

                    logger.debug(f"Processing chunk from rows {chunk_start} to {chunk_end} of {file_name}")

                    filtered = chunk.dropna(subset=['XY'])
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
                                            if r.lat and r.lon
                                            else None
                                        }
                                        for r in rows
                                    ]
                                    stmt = insert(PlantingRecommendation).values(mappings).on_conflict_do_nothing()
                                    result = session.execute(stmt)
                                    if result.rowcount < len(mappings):
                                        _log_batch_conflicts(session, mappings)
                            retry_db(lambda: _flush_batch(sub), attempts=2)
                            completed_batches += 1
                            batches_since_checkpoint += 1
                        except SQLAlchemyError:
                            failed_batches += 1
                            logger.error(f"Batch {i // batch_size + 1} failed in {file_name}, skipping")

                    if not shutdown_requested:
                        last_committed_row = chunk_end
                        _write_progress(file_path, 'processing', last_committed_row, num_rows)

                    if batches_since_checkpoint >= checkpoint_interval and not shutdown_requested:
                        session.commit()
                        file_import_repo.upsert_offset(checksum, file_name, chunk_end, original_filename)
                        batches_since_checkpoint = 0
                        logger.debug(f"Checkpoint committed at row {chunk_end}")
                    if shutdown_requested:
                        break

            except BaseException:
                _write_progress(file_path, 'error', last_committed_row, num_rows, "Interrupted")
                pbar.close()
                session.rollback()
                raise

            pbar.close()
            session.commit()

            del data

            elapsed = time.time() - start_time
            emit_event("file.processing_end", file=file_name, checksum=checksum,
                       elapsed=round(elapsed, 2), completed_batches=completed_batches,
                       failed_batches=failed_batches, shutdown=shutdown_requested)

            if shutdown_requested:
                file_import_repo.upsert_offset(checksum, file_name, last_committed_row, original_filename)
                logger.warning(
                    f"Graceful shutdown — committed {completed_batches} batches from {file_name}, "
                    f"offset {last_committed_row} persisted"
                )
            else:
                file_import_repo.upsert_offset(checksum, file_name, num_rows, original_filename)
                _write_progress(file_path, 'completed', num_rows, num_rows, "Done")
                logger.info(f"File {file_name} fully processed and recorded")

            if failed_batches:
                logger.warning(f"File {file_name} processed with {failed_batches} failed batch(es)")

        except FileNotFoundError as e:
            _write_progress(file_path, 'error', 0, 0, str(e))
            logger.error(f"Failed to process file {file_name}: {e}")
        except Exception as e:
            _write_progress(file_path, 'error', 0, 0, str(e))
            logger.error(f"Unexpected error processing file {file_name}: {e}")
        finally:
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
        failed_files = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(process_file, fp, batch_size, chunk_size, checkpoint_interval, dry_run): fp
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
                    logger.error(f"Failed to process {os.path.basename(fp)}: {e}")

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

def _enqueue_or_warn(task, **kwargs):
    """Enqueue a Celery task, or log a warning if the broker is unreachable."""
    if not celery_broker_available():
        logger.warning(
            f"Cannot enqueue {task.__name__} — Redis at "
            f"{CELERY_BROKER_URL} "
            f"is not reachable. Start Redis or disable HOUSEKEEPING_ENABLED."
        )
        return

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
            f"{CELERY_BROKER_URL}. "
            f"Run the worker in Docker for housekeeping, or set HOUSEKEEPING_ENABLED=false."
        )
        return

    if result and isinstance(result[0], Exception):
        logger.warning(f"Failed to enqueue {task.__name__}: {result[0]}")


# ── Async wrappers (Flask integration) ─────────────────────────

def process_file_async(file_path: str, app=None):
    """Enqueue a single file for processing via Celery (non-blocking).

    Used by the upload API endpoint.
    """
    from app.tasks import process_file_task
    _enqueue_or_warn(process_file_task, file_path=file_path)


def process_pending(app=None):
    """Enqueue background processing of unprocessed files via Celery.

    Called once at Flask app startup.
    """
    from app.tasks import process_pending_task
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

    data_folder = args.data_folder
    os.makedirs(data_folder, exist_ok=True)
    set_app(_get_app())

    if args.watch:
        watch_directory(data_folder, dry_run=args.dry_run, **settings)
    else:
        with _get_app().app_context():
            load_rds_to_db(data_folder=data_folder, dry_run=args.dry_run, **settings)
