"""Reusable watchdog handler for RDS/Parquet file ingestion."""

import os
import time

from watchdog.events import FileSystemEventHandler

from app.services.housekeeper import _get_app, hold_unmapped, logger, process_file, report_conversion
from app.utils.rds_to_parquet import ConversionError, convert_for_ingestion


class RDSFileHandler(FileSystemEventHandler):
    """Watchdog handler that processes ``.RDS`` and ``.parquet`` files.

    Fires on both *created* and *modified* events with a per-file
    cooldown to avoid duplicate processing.

    RDS files are converted to Parquet before processing for efficient
    streaming ingestion.
    """

    def __init__(self, batch_size=1000, chunk_size=10000,
                 checkpoint_interval=50, dry_run=False):
        self.batch_size = batch_size
        self.chunk_size = chunk_size
        self.checkpoint_interval = checkpoint_interval
        self.dry_run = dry_run
        self._cooldown: dict[str, float] = {}

    def _process_if_rds(self, path: str):
        now = time.time()
        last = self._cooldown.get(path, 0)
        if now - last < 5:
            return
        self._cooldown[path] = now

        if not (path.lower().endswith('.rds') or path.lower().endswith('.parquet')):
            return

        time.sleep(1)
        try:
            # Convert RDS to Parquet if needed (returns original path for Parquet)
            parquet_path = self._convert(path)
            with _get_app().app_context():
                if not hold_unmapped([parquet_path], dry_run=self.dry_run):
                    return  # held for mapping on the Upload page
            process_file(
                parquet_path,
                batch_size=self.batch_size,
                chunk_size=self.chunk_size,
                checkpoint_interval=self.checkpoint_interval,
                dry_run=self.dry_run,
            )
        except Exception as e:
            # process_file re-raises transient DB errors so Celery can retry.
            # There is no retrier here, and an exception escaping a watchdog
            # handler kills the observer thread, so log it and keep watching;
            # the file's offset is kept and the next event or run resumes it.
            logger.exception(f"Ingest of {path} failed, will resume on the next run: {e}")

    def _convert(self, path: str) -> str:
        """convert_for_ingestion, shown on the Jobs page like a scan's conversions."""
        if self.dry_run or not path.lower().endswith('.rds'):
            return convert_for_ingestion(path)
        last = {'total': 0, 'elapsed': 0.0}

        def progress(stage, done, total, elapsed):
            last.update(total=total, elapsed=elapsed)
            report_conversion(path, stage, done, total, elapsed)

        try:
            out = convert_for_ingestion(path, on_progress=progress)
        except ConversionError as e:
            report_conversion(path, 'failed', 0, last['total'], last['elapsed'], str(e))
            raise
        if out != path:
            report_conversion(path, 'done', last['total'], last['total'], last['elapsed'],
                              f"Converted to {os.path.basename(out)}")
        return out

    def on_created(self, event):
        if not event.is_directory:
            self._process_if_rds(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._process_if_rds(event.src_path)