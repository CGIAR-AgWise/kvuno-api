"""The startup scan runs once per deployment, from one place.

create_app() runs in every gunicorn worker and again on every MAX_REQUESTS
recycle. Each used to start its own scan thread: concurrent RDS conversions
collided (FileNotFoundError on os.remove), every recycle re-listed and
re-checksummed the folder, and large files were read whole inside the
memory-capped API.
"""
import os
import threading
import time

import pytest
from flask import Flask

from app.services import housekeeper as hk


@pytest.fixture
def scans(monkeypatch, tmp_path):
    monkeypatch.setenv('HOUSEKEEPING_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('STARTUP_SCAN', 'true')
    calls = []
    monkeypatch.setattr(hk, 'load_rds_to_db', lambda **kw: calls.append(kw))
    return calls


@pytest.fixture
def app():
    return Flask(__name__)


def test_off_means_off(monkeypatch, app, scans):
    monkeypatch.setenv('STARTUP_SCAN', 'false')
    assert hk.start_startup_scan(app) == 'disabled'


def test_with_a_worker_the_scan_is_queued_not_run_in_the_api(monkeypatch, app, scans):
    monkeypatch.setenv('HOUSEKEEPING_ENABLED', 'true')
    queued = []
    monkeypatch.setattr(hk, 'process_pending', lambda: queued.append(True))
    assert hk.start_startup_scan(app) == 'queued'
    assert queued == [True]
    assert scans == []


def test_without_a_worker_one_process_scans(app, scans):
    assert hk.run_startup_scan(app) is True
    assert len(scans) == 1
    # A sibling gunicorn worker (or a recycled one) inside the cooldown skips.
    assert hk.run_startup_scan(app) is False
    assert len(scans) == 1


def test_after_the_cooldown_the_next_boot_scans_again(app, scans, tmp_path):
    assert hk.run_startup_scan(app) is True
    marker = tmp_path / hk.STARTUP_SCAN_MARKER
    old = time.time() - 3600
    os.utime(marker, (old, old))
    assert hk.run_startup_scan(app) is True
    assert len(scans) == 2


def test_a_running_scan_blocks_others_until_it_ends(monkeypatch, app, tmp_path):
    monkeypatch.setenv('HOUSEKEEPING_DATA_DIR', str(tmp_path))
    started, release = threading.Event(), threading.Event()

    def slow_scan(**kw):
        started.set()
        release.wait(5)

    monkeypatch.setattr(hk, 'load_rds_to_db', slow_scan)
    first = threading.Thread(target=hk.run_startup_scan, args=(app,))
    first.start()
    assert started.wait(5)
    assert hk.run_startup_scan(app) is False  # the live scan holds the claim
    release.set()
    first.join(5)


def test_a_failed_scan_is_logged_and_still_starts_the_cooldown(monkeypatch, app, tmp_path):
    monkeypatch.setenv('HOUSEKEEPING_DATA_DIR', str(tmp_path))

    def boom(**kw):
        raise RuntimeError('database down')

    monkeypatch.setattr(hk, 'load_rds_to_db', boom)
    assert hk.run_startup_scan(app) is True  # no exception escapes the thread
    assert (tmp_path / hk.STARTUP_SCAN_MARKER).exists()


def test_the_cooldown_has_a_floor(monkeypatch):
    from app.config import ConfigurationError, get_settings
    monkeypatch.setenv('STARTUP_SCAN_COOLDOWN_SECONDS', '10')
    with pytest.raises(ConfigurationError, match='STARTUP_SCAN_COOLDOWN_SECONDS'):
        get_settings()


def test_create_app_starts_no_scan_in_the_test_suite():
    """pytest-env sets STARTUP_SCAN=false; tests must not scan the real folder."""
    from app.config import get_settings
    assert get_settings().STARTUP_SCAN is False
