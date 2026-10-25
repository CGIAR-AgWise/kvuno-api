"""Convert .RDS files to .parquet for faster, splittable downstream processing.

⚠️ Reading an RDS is the memory-heavy step, not the conversion: pyreadr has no
partial or chunked read, and it builds one Python object per text cell. A
7.4 MB AgWise file (2.1 M rows, five text columns) peaks at ~1.1 GB of RSS
while being read, then writes 0.9 MB of Parquet in under a second.

So uploads and scans convert in a **child process** (`_convert_in_child`):
in the 512 MB API container the kernel used to kill the gunicorn worker
mid-read, the stale-inspection recovery restarted the read on the next poll,
and the upload sat at "Reading the file…" forever. Now only the child dies,
and the upload fails with a message saying why.
"""
import os
import signal
import subprocess
import sys

import pyreadr

from app.utils.logging import get_logger

logger = get_logger(__name__)

# How long a single RDS read + Parquet write may take before it is abandoned.
CONVERT_TIMEOUT_SECONDS = 30 * 60

# The child imports only pyreadr and pandas — not the app — so it starts fast
# and its memory is the conversion's alone. Exit codes: 0 ok, 2 unreadable,
# 3 Parquet write failed (the RDS is still usable), 4 out of memory.
_CHILD = r'''
import sys
src, out = sys.argv[1], sys.argv[2]
try:
    import pyreadr
    df = pyreadr.read_r(src)[None]
except MemoryError:
    sys.exit(4)
except Exception as e:
    sys.stderr.write(f"{e.__class__.__name__}: {e}"[:500])
    sys.exit(2)
try:
    df.to_parquet(out, index=False, compression="zstd")
except MemoryError:
    sys.exit(4)
except Exception as e:
    sys.stderr.write(f"{e.__class__.__name__}: {e}"[:500])
    sys.exit(3)
'''


class ConversionError(Exception):
    """An RDS that cannot be converted. The message is meant for the user."""


class _WriteFailed(Exception):
    """The RDS was read but Parquet could not be written; the RDS is still usable."""


_OUT_OF_MEMORY = ("Reading this R file needs more memory than the server allows, so it was stopped. "
                  "Upload it as Parquet instead (in R: arrow::write_parquet(df, 'file.parquet')), "
                  "or ask an administrator to raise the API's memory limit.")


def _convert_in_child(rds_path: str, parquet_path: str) -> str:
    """Convert in a separate process; raise ConversionError / _WriteFailed on failure."""
    try:
        result = subprocess.run([sys.executable, '-c', _CHILD, rds_path, parquet_path],
                                capture_output=True, text=True, timeout=CONVERT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise ConversionError(f"Reading this R file took longer than {CONVERT_TIMEOUT_SECONDS // 60} minutes "
                              "and was stopped.") from None
    detail = (result.stderr or '').strip().splitlines()[-1:] or ['']
    if result.returncode == 0:
        return parquet_path
    # A negative code is the signal that killed it; the kernel's OOM killer
    # sends SIGKILL (Linux), and MemoryError inside the child exits 4.
    if result.returncode == 4 or result.returncode == -getattr(signal, 'SIGKILL', 9):
        logger.error("RDS conversion ran out of memory", file=os.path.basename(rds_path),
                     returncode=result.returncode)
        raise ConversionError(_OUT_OF_MEMORY)
    if result.returncode == 3:
        raise _WriteFailed(detail[0])
    logger.warning("RDS could not be read", file=os.path.basename(rds_path),
                   returncode=result.returncode, detail=detail[0])
    raise ConversionError(f"This is not a readable R data file ({detail[0] or 'exit ' + str(result.returncode)}).")


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
            _convert_in_child(rds_path, tmp_path)  # isolated: the API startup scan runs this too
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

    Runs in a child process (see the module docstring). Returns the original
    path unchanged for non-RDS input, or when the RDS was read but Parquet
    could not be written (the RDS is still usable). Raises ConversionError —
    with a message for the user — when the RDS cannot be read at all, or the
    read runs out of memory: falling back to the RDS then would only repeat
    the same whole-file read in-process.
    """
    if not path.lower().endswith('.rds'):
        return path

    out = os.path.splitext(path)[0] + '.parquet'
    tmp = out + '.tmp'
    try:
        _convert_in_child(path, tmp)
        os.replace(tmp, out)
    except _WriteFailed as e:
        logger.warning("Parquet conversion failed; keeping the RDS", file=os.path.basename(path), detail=str(e))
        return path
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    try:
        os.remove(path)
    except OSError:
        pass
    return out
