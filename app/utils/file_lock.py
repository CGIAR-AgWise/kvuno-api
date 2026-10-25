"""A cross-process lock held as an O_EXCL file, with stale takeover.

Same approach as the upload ``_Lock`` in ``app/services/uploads.py``: creating
the file with ``O_CREAT | O_EXCL`` is atomic on every filesystem the data
directory lives on (local disk or a shared volume), so it works across
processes and containers without Redis. A holder that dies cannot release it,
so a lock whose mtime is older than ``stale_seconds`` is taken over; a live
holder must ``touch()`` it more often than that.

The file records "<hostname> <pid>". The worker uses the hostname at startup
to clear locks its previous run left behind (``release_dead_locks``), instead
of waiting out ``stale_seconds`` — during which a resumed job would find the
dead lock "held" and skip the file.
"""
import os
import socket
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
                    os.write(fd, f"{socket.gethostname()} {os.getpid()}\n".encode())
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

    @staticmethod
    def holder_host(path: str) -> str | None:
        """The hostname recorded in a lock file; None if unreadable or old-format (pid only)."""
        try:
            with open(path) as f:
                parts = f.read().split()
        except OSError:
            return None
        return parts[0] if len(parts) == 2 else None

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
