"""A cross-process lock held as an O_EXCL file, with stale takeover.

Same approach as the upload ``_Lock`` in ``app/services/uploads.py``: creating
the file with ``O_CREAT | O_EXCL`` is atomic on every filesystem the data
directory lives on (local disk or a shared volume), so it works across
processes and containers without Redis. A holder that dies cannot release it,
so a lock whose mtime is older than ``stale_seconds`` is taken over; a live
holder must ``touch()`` it more often than that.
"""
import os
import time


class FileLock:
    def __init__(self, path: str, stale_seconds: float):
        self.path = path
        self.stale_seconds = stale_seconds
        self.held = False

    def acquire(self) -> bool:
        # Two passes: the second is the retry after removing a stale lock.
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    # Who holds it, for whoever finds it on disk later.
                    os.write(fd, f"{os.getpid()}\n".encode())
                finally:
                    os.close(fd)
                self.held = True
                return True
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) < self.stale_seconds:
                        return False
                    os.remove(self.path)  # holder died; take it over
                except OSError:
                    pass
        return False

    def touch(self):
        if self.held:
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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
