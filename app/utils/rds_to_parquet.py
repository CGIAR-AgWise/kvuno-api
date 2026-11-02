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

The child reports as it goes (see `_CHILD`), so callers can show where a
conversion has got to: uploads on their row (Upload page), scans and the
folder watcher as a job on the Jobs page.
"""
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

import pyreadr

from app.utils.logging import get_logger

logger = get_logger(__name__)

# How long a single RDS read + Parquet write may take before it is abandoned.
CONVERT_TIMEOUT_SECONDS = 30 * 60

# How often the parent reports while the child is quiet. pyreadr cannot say
# how far it has read, so during the read the only news is "still reading,
# for this long" — which is still more than a spinner.
PROGRESS_TICK_SECONDS = 2

# on_progress(stage, done, total, elapsed): stage is 'reading' (done/total
# 0, nothing to count) or 'writing' (rows written of total); elapsed is
# seconds since the conversion started.
ProgressCallback = Callable[[str, int, int, float], None]

# The child imports only pyreadr and pyarrow — not the app — so it starts fast
# and its memory is the conversion's alone. Exit codes: 0 ok, 2 unreadable,
# 3 Parquet write failed (the RDS is still usable), 4 out of memory.
# It reports on stdout: `read <rows>` once the RDS is in memory, then
# `write <done> <total>` after each slice of rows is written. The Parquet is
# written in slices (one row group each) so the write has a real percentage.
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
print(f"read {len(df)}", flush=True)
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    table = pa.Table.from_pandas(df, preserve_index=False)
    del df
    total = table.num_rows
    step = max(100_000, -(-total // 20))
    with pq.ParquetWriter(out, table.schema, compression="zstd") as writer:
        for start in range(0, total, step):
            writer.write_table(table.slice(start, step))
            print(f"write {min(start + step, total)} {total}", flush=True)
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


def keep_mtime(source: str, target: str) -> None:
    """Give *target* the modification time of *source*.

    A held data-folder file's time pre-fills the "Data date" on the mapping
    form, so a converted Parquet carries the RDS's time rather than the
    moment it was written. Best effort: a file system that refuses it keeps
    "now".
    """
    try:
        stat = os.stat(source)
        os.utime(target, (stat.st_atime, stat.st_mtime))
    except OSError:
        logger.warning("could not copy the file time to the converted file", file=os.path.basename(target))


def _parse(line: str) -> tuple[str, int, int] | None:
    """A child progress line as (stage, done, total), or None if it is not one."""
    parts = line.split()
    try:
        if parts[0] == 'read' and len(parts) == 2:
            return 'writing', 0, int(parts[1])
        if parts[0] == 'write' and len(parts) == 3:
            return 'writing', int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        pass
    return None


def _report(on_progress: Optional[ProgressCallback], stage: str, done: int, total: int, elapsed: float):
    """Progress is news, never a reason to fail the conversion."""
    if on_progress is None:
        return
    try:
        on_progress(stage, done, total, elapsed)
    except Exception:
        logger.warning("conversion progress callback failed", exc_info=True)


def _convert_in_child(rds_path: str, parquet_path: str,
                      on_progress: Optional[ProgressCallback] = None) -> str:
    """Convert in a separate process; raise ConversionError / _WriteFailed on failure.

    Reports through on_progress as it goes: 'reading' every
    PROGRESS_TICK_SECONDS until the child has the file in memory, then
    'writing' with rows written of the total.
    """
    started = time.monotonic()
    proc = subprocess.Popen([sys.executable, '-c', _CHILD, rds_path, parquet_path],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # stdout is read on a thread so the loop below can also wake on a timer:
    # the read is silent for as long as pyreadr takes.
    lines: queue.Queue = queue.Queue()

    def pump():
        for raw in proc.stdout:
            lines.put(raw)
        lines.put(None)

    threading.Thread(target=pump, name='convert-progress', daemon=True).start()

    stage, done, total = 'reading', 0, 0
    _report(on_progress, stage, done, total, 0.0)
    while True:
        elapsed = time.monotonic() - started
        if elapsed > CONVERT_TIMEOUT_SECONDS:
            proc.kill()
            proc.wait()
            raise ConversionError(f"Reading this R file took longer than {CONVERT_TIMEOUT_SECONDS // 60} minutes "
                                  "and was stopped.")
        try:
            line = lines.get(timeout=PROGRESS_TICK_SECONDS)
        except queue.Empty:
            _report(on_progress, stage, done, total, time.monotonic() - started)
            continue
        if line is None:
            break
        parsed = _parse(line)
        if parsed is not None:
            stage, done, total = parsed
            _report(on_progress, stage, done, total, time.monotonic() - started)

    returncode = proc.wait()
    stderr = proc.stderr.read() if proc.stderr else ''
    detail = (stderr or '').strip().splitlines()[-1:] or ['']
    if returncode == 0:
        logger.info("RDS converted to Parquet", file=os.path.basename(rds_path), rows=total,
                    seconds=round(time.monotonic() - started, 1))
        return parquet_path
    # A negative code is the signal that killed it; the kernel's OOM killer
    # sends SIGKILL (Linux), and MemoryError inside the child exits 4.
    if returncode == 4 or returncode == -getattr(signal, 'SIGKILL', 9):
        logger.error("RDS conversion ran out of memory", file=os.path.basename(rds_path),
                     returncode=returncode)
        raise ConversionError(_OUT_OF_MEMORY)
    if returncode == 3:
        raise _WriteFailed(detail[0])
    logger.warning("RDS could not be read", file=os.path.basename(rds_path),
                   returncode=returncode, detail=detail[0])
    raise ConversionError(f"This is not a readable R data file ({detail[0] or 'exit ' + str(returncode)}).")


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


def convert(rds_path: str, parquet_path: str | None = None,
            on_progress: Optional[ProgressCallback] = None) -> str:
    """Convert one RDS to Parquet in a child process, keeping the RDS.

    The Parquet is written to ``<out>.tmp`` and renamed into place, so a
    failed or interrupted run never leaves a partial file. Raises
    ConversionError (with a message for a person) on any failure. Used by
    ``scripts/convert_rds.py`` to convert files on a machine with more memory
    than the server, before uploading them.
    """
    if parquet_path is None:
        parquet_path = os.path.splitext(rds_path)[0] + ".parquet"
    tmp = parquet_path + ".tmp"
    try:
        _convert_in_child(rds_path, tmp, on_progress=on_progress)
        keep_mtime(rds_path, tmp)
        os.replace(tmp, parquet_path)
    except _WriteFailed as e:
        raise ConversionError(f"The file was read, but writing Parquet failed: {e}") from None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return parquet_path


CONVERT_LOCK_SUFFIX = '.convert.lock'


def _notify(on_file, rds_path, stage, done, total, elapsed, message):
    if on_file is None:
        return
    try:
        on_file(rds_path, stage, done, total, elapsed, message)
    except Exception:
        logger.warning("conversion progress callback failed", exc_info=True)


def batch_convert(data_dir: str, pattern: str = ".RDS", delete_originals: bool = False,
                  on_file: Optional[Callable[[str, str, int, int, float, str], None]] = None) -> list[str]:
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

    ``on_file(rds_path, stage, done, total, elapsed, message)`` follows each
    conversion: the stages of ProgressCallback, then 'done' or 'failed' (with
    a message for the user). The scan uses it to show conversions on the Jobs
    page; it used to convert silently and only log a failure.

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
        last = {'total': 0, 'elapsed': 0.0}

        def progress(stage, done, total, elapsed, _path=rds_path):
            last.update(total=total, elapsed=elapsed)
            if on_file is not None:
                on_file(_path, stage, done, total, elapsed, '')

        try:
            if not os.path.exists(rds_path):
                continue  # converted (and removed) by another process meanwhile
            # Isolated in a child: the API startup scan runs this too.
            _convert_in_child(rds_path, tmp_path, on_progress=progress)
            keep_mtime(rds_path, tmp_path)
            os.replace(tmp_path, parquet_path)
            created.append(parquet_path)
            if delete_originals:
                try:
                    os.remove(rds_path)
                except FileNotFoundError:
                    pass
            _notify(on_file, rds_path, 'done', last['total'], last['total'], last['elapsed'],
                    f"Converted to {os.path.basename(parquet_path)}")
        except Exception as e:
            logger.exception("RDS conversion failed; the file is left as it is", file=fname)
            _notify(on_file, rds_path, 'failed', 0, last['total'], last['elapsed'],
                    str(e) if isinstance(e, (ConversionError, _WriteFailed)) else f"Could not convert: {e}")
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        finally:
            lock.release()
    return created


def convert_for_ingestion(path: str, on_progress: Optional[ProgressCallback] = None) -> str:
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
    the same whole-file read in-process. on_progress: see ProgressCallback.
    """
    if not path.lower().endswith('.rds'):
        return path

    out = os.path.splitext(path)[0] + '.parquet'
    tmp = out + '.tmp'
    try:
        _convert_in_child(path, tmp, on_progress=on_progress)
        keep_mtime(path, tmp)
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
