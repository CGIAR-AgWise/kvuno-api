"""Reusable watchdog handler for RDS/Parquet file ingestion."""

import time

from watchdog.events import FileSystemEventHandler

from app.services.housekeeper import logger, process_file
from app.utils.rds_to_parquet import convert_for_ingestion


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
            parquet_path = convert_for_ingestion(path)
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

    def on_created(self, event):
        if not event.is_directory:
            self._process_if_rds(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._process_if_rds(event.src_path)