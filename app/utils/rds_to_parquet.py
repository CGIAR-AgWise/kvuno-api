"""Convert .RDS files to .parquet for faster, splittable downstream processing."""
import os
import pyreadr

from app.utils.logging import get_logger

logger = get_logger(__name__)


def rds_to_parquet(rds_path: str, parquet_path: str | None = None, compression: str = "zstd") -> str:
    """Read an RDS file and write it as Parquet.

    Args:
        rds_path: Path to the .RDS file.
        parquet_path: Output path (defaults to ``rds_path`` with ``.parquet`` extension).
        compression: Parquet compression codec (default ``zstd``).

    Returns:
        Path to the written Parquet file.
    """
    if parquet_path is None:
        parquet_path = os.path.splitext(rds_path)[0] + ".parquet"

    result = pyreadr.read_r(rds_path)
    df = result[None]
    df.to_parquet(parquet_path, index=False, compression=compression)
    return parquet_path


CONVERT_LOCK_SUFFIX = '.convert.lock'


def batch_convert(data_dir: str, pattern: str = ".RDS", delete_originals: bool = False) -> list[str]:
    """Convert all matching files in *data_dir* to Parquet.

    Safe to run from several processes at once — the startup scan runs in
    every gunicorn worker, and the Celery worker and CLI scan the same
    directory. Previously two of them converted the same file, the first
    deleted the RDS, and the second's ``os.remove`` raised FileNotFoundError
    and aborted its whole scan (one could also ingest the other's half-written
    Parquet). Now:

    - a per-file ``<file>.convert.lock`` lets one process convert it; the
      others skip it (they will see the Parquet on their file listing);
    - the Parquet is written to ``<name>.parquet.tmp`` and renamed into place,
      so no one ever reads a partial file (``.tmp`` is not picked up as data);
    - an RDS that is already gone was converted by someone else, not an error;
    - one bad file is logged and skipped instead of failing the rest.

    Returns:
        List of created Parquet paths.
    """
    from app.config import get_settings
    from app.utils.file_lock import FileLock

    created = []
    for fname in sorted(os.listdir(data_dir)):
        if not fname.upper().endswith(pattern.upper()):
            continue
        rds_path = os.path.join(data_dir, fname)
        lock = FileLock(rds_path + CONVERT_LOCK_SUFFIX, get_settings().INGEST_LOCK_STALE_SECONDS)
        if not lock.acquire():
            logger.info("RDS conversion already running in another process; skipping", file=fname)
            continue
        parquet_path = os.path.splitext(rds_path)[0] + ".parquet"
        tmp_path = parquet_path + ".tmp"
        try:
            if not os.path.exists(rds_path):
                continue  # converted (and removed) by another process meanwhile
            rds_to_parquet(rds_path, tmp_path)
            os.replace(tmp_path, parquet_path)
            created.append(parquet_path)
            if delete_originals:
                try:
                    os.remove(rds_path)
                except FileNotFoundError:
                    pass
        except Exception:
            logger.exception("RDS conversion failed; the file is left as it is", file=fname)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        finally:
            lock.release()
    return created


def convert_for_ingestion(path: str) -> str:
    """Return a Parquet path for *path*, converting an RDS file if needed.

    Uploading an RDS means every later step re-parses it in full: the preview
    has to show 5 rows, and the worker has to read the whole thing again. A
    Parquet file answers both from the footer and a single batch, so
    converting once at upload time removes the repeated full parses.

    Returns the original path unchanged for non-RDS input, or if conversion
    fails — a bad file should surface as a slow upload, not a failed one.
    """
    if not path.lower().endswith('.rds'):
        return path

    out = os.path.splitext(path)[0] + '.parquet'
    try:
        rds_to_parquet(path, out)
    except Exception as e:  # noqa: BLE001 - conversion is best-effort
        logger.warning(f"Parquet conversion failed for {os.path.basename(path)}; keeping the RDS: {e}",
                       exc_info=True)
        return path

    try:
        os.remove(path)
    except OSError:
        pass
    return out
