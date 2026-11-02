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
dead lock "held" and skip the file. And while running, a lock whose holder is
a dead process on this host is taken over at once (``_holder_is_dead``).
"""
import os
import socket
import time


def _pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists. Unknown counts as alive (the safe side).

    POSIX only: on Windows os.kill(pid, 0) does not probe, it terminates the
    process, so there every holder is assumed alive and only staleness counts
    (production runs on Linux).
    """
    if os.name == 'nt':
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # e.g. PermissionError: it exists, owned by someone else
    return True


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
                    fresh = time.time() - os.path.getmtime(self.path) < self.stale_seconds
                    if fresh and not self._holder_is_dead():
                        return False
                    os.remove(self.path)  # holder died; take it over
                except OSError:
                    pass
        return False

    def _holder_is_dead(self) -> bool:
        """Whether the holder is a process on this host that no longer exists.

        A process that dies mid-ingest (an out-of-memory kill of a Celery
        child, a crash) leaves its lock behind, and the lock looked live for
        stale_seconds (10 min). Celery requeues such a task at once, so the
        new attempt found the lock "held", skipped the file silently, and the
        job sat at "processing" until it went stale and someone pressed Retry.
        On the same host the recorded pid answers the question at once.
        Another host's holder cannot be checked, so only staleness counts.
        """
        host, pid = self.holder(self.path)
        if host != socket.gethostname() or pid is None or pid == os.getpid():
            return False
        return not _pid_alive(pid)

    @staticmethod
    def holder(path: str) -> tuple[str | None, int | None]:
        """(hostname, pid) recorded in a lock file; (None, None) if unreadable or old-format."""
        try:
            with open(path) as f:
                parts = f.read().split()
        except OSError:
            return None, None
        if len(parts) != 2:
            return None, None
        try:
            return parts[0], int(parts[1])
        except ValueError:
            return parts[0], None

    @staticmethod
    def holder_host(path: str) -> str | None:
        """The hostname recorded in a lock file; None if unreadable or old-format (pid only)."""
        return FileLock.holder(path)[0]

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
