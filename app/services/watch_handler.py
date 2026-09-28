"""Reusable watchdog handler for RDS/Parquet file ingestion."""

import time

from watchdog.events import FileSystemEventHandler

from app.services.housekeeper import process_file


class RDSFileHandler(FileSystemEventHandler):
    """Watchdog handler that processes ``.RDS`` and ``.parquet`` files.

    Fires on both *created* and *modified* events with a per-file
    cooldown to avoid duplicate processing.
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
        process_file(
            path,
            batch_size=self.batch_size,
            chunk_size=self.chunk_size,
            checkpoint_interval=self.checkpoint_interval,
            dry_run=self.dry_run,
        )

    def on_created(self, event):
        if not event.is_directory:
            self._process_if_rds(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._process_if_rds(event.src_path)
