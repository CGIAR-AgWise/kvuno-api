"""A lock left by a killed process must not hold up the retry.

Production: an out-of-memory kill of a Celery child (WorkerLostError, signal
9) left the file's ingest lock behind. Celery requeued the task at once, the
new attempt found the lock "held" (touched under 10 minutes ago), skipped the
file silently, and the job sat at "processing" until it went stale and
someone pressed Retry.
"""
import os
import socket
import subprocess
import sys

import pytest

from app.utils import file_lock
from app.utils.file_lock import FileLock


def _left_behind(path, host, pid):
    """A fresh lock file as a holder that never released it would leave."""
    path.write_text(f'{host} {pid}\n')


def _dead_pid() -> int:
    """The pid of a process that has exited."""
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    return proc.pid


@pytest.mark.skipif(os.name == 'nt', reason='pid probing is POSIX-only; Windows waits for staleness')
def test_a_dead_holder_on_this_host_is_taken_over_at_once(tmp_path):
    lock_path = tmp_path / 'f.parquet.ingest.lock'
    _left_behind(lock_path, socket.gethostname(), _dead_pid())
    lock = FileLock(str(lock_path), stale_seconds=600)
    assert lock.acquire(), 'the holder is gone; the retry must not wait 10 minutes'
    assert FileLock.holder(str(lock_path)) == (socket.gethostname(), os.getpid())
    lock.release()


def test_a_live_holder_keeps_its_lock(tmp_path):
    lock_path = tmp_path / 'f.parquet.ingest.lock'
    _left_behind(lock_path, socket.gethostname(), os.getppid())  # our parent: alive
    assert not FileLock(str(lock_path), stale_seconds=600).acquire()


def test_another_hosts_holder_is_left_to_go_stale(tmp_path, monkeypatch):
    """Its pid means nothing here, so only the lock's age counts."""
    monkeypatch.setattr(file_lock, '_pid_alive', lambda pid: False)
    lock_path = tmp_path / 'f.parquet.ingest.lock'
    _left_behind(lock_path, 'some-other-host', 12345)
    assert not FileLock(str(lock_path), stale_seconds=600).acquire()


def test_an_old_format_lock_is_left_to_go_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(file_lock, '_pid_alive', lambda pid: False)
    lock_path = tmp_path / 'f.parquet.ingest.lock'
    lock_path.write_text('12345\n')  # pid only, as locks were written before
    assert not FileLock(str(lock_path), stale_seconds=600).acquire()


def test_on_this_host_a_dead_pid_is_detected_through_the_probe(tmp_path, monkeypatch):
    """The decision itself, platform-independent: dead pid on this host → taken over."""
    monkeypatch.setattr(file_lock, '_pid_alive', lambda pid: False)
    lock_path = tmp_path / 'f.parquet.ingest.lock'
    _left_behind(lock_path, socket.gethostname(), 12345)
    assert FileLock(str(lock_path), stale_seconds=600).acquire()
