"""RDS conversion runs in a child process, and fails with a reason.

Reading an RDS is memory-heavy (pyreadr reads whole files; a 7.4 MB AgWise
file peaks at ~1.1 GB). In the 512 MB API container the kernel killed the
gunicorn worker mid-read, the stale-inspection recovery restarted the read on
the next poll, and the upload sat at "Reading the file…" forever.
"""
import io
import os
import signal

import pandas as pd
import pyarrow.parquet as pq
import pyreadr
import pytest

from app.utils import rds_to_parquet as conv


@pytest.fixture
def rds(tmp_path):
    path = tmp_path / 'small.RDS'
    pyreadr.write_rds(str(path), pd.DataFrame({'lat': [1.0, 2.0], 'lon': [3.0, 4.0], 'Variety': ['a', 'b']}))
    return str(path)


class _Child:
    """Stand-in for the conversion child: its stdout lines, exit code and stderr."""

    def __init__(self, returncode, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.killed = False

    def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True


def _child_exits(monkeypatch, returncode, stderr='', stdout=''):
    child = _Child(returncode, stdout, stderr)
    monkeypatch.setattr(conv.subprocess, 'Popen', lambda *a, **kw: child)
    return child


def test_a_real_rds_is_converted_in_a_child_and_replaced(rds):
    out = conv.convert_for_ingestion(rds)
    assert out.endswith('.parquet')
    assert pq.ParquetFile(out).schema_arrow.names == ['lat', 'lon', 'Variety']
    assert not os.path.exists(rds)
    assert not os.path.exists(out + '.tmp')


def test_parquet_is_passed_through(tmp_path):
    path = str(tmp_path / 'x.parquet')
    assert conv.convert_for_ingestion(path) == path


def test_a_file_that_is_not_r_data_fails_with_a_reason(tmp_path):
    bad = tmp_path / 'notes.RDS'
    bad.write_bytes(b'this is not an R data file')
    with pytest.raises(conv.ConversionError, match='not a readable R data file'):
        conv.convert_for_ingestion(str(bad))
    assert bad.exists(), 'the upload is kept for the user to see what failed'


@pytest.mark.parametrize('code', [4, -getattr(signal, 'SIGKILL', 9)])
def test_running_out_of_memory_is_a_clear_failure_not_a_loop(monkeypatch, rds, code):
    _child_exits(monkeypatch, code)
    with pytest.raises(conv.ConversionError, match='more memory than the server allows'):
        conv.convert_for_ingestion(rds)


def test_a_failed_parquet_write_keeps_the_usable_rds(monkeypatch, rds):
    _child_exits(monkeypatch, 3, 'ArrowTypeError: mixed types')
    assert conv.convert_for_ingestion(rds) == rds


def test_a_conversion_that_hangs_is_abandoned(monkeypatch, rds):
    child = _child_exits(monkeypatch, 0)
    monkeypatch.setattr(conv, 'CONVERT_TIMEOUT_SECONDS', -1)  # already over time
    with pytest.raises(conv.ConversionError, match='took longer than'):
        conv.convert_for_ingestion(rds)
    assert child.killed


# ── Progress ────────────────────────────────────────────────────────────
# Uploads and scans used to show "Reading the file…" for as long as the
# conversion took, with no sign of whether it was moving.

def test_a_real_conversion_reports_reading_then_rows_written(rds):
    seen = []
    conv.convert_for_ingestion(rds, on_progress=lambda *event: seen.append(event))
    assert seen[0][:3] == ('reading', 0, 0)
    assert seen[-1][:3] == ('writing', 2, 2)


def test_a_quiet_child_still_reports_how_long_it_has_been_reading(monkeypatch, rds):
    import queue
    real_get = queue.Queue.get
    calls = {'n': 0}

    def first_wait_times_out(self, block=True, timeout=None):
        calls['n'] += 1
        if calls['n'] == 1:
            raise queue.Empty
        return real_get(self, block, timeout)

    monkeypatch.setattr(queue.Queue, 'get', first_wait_times_out)
    _child_exits(monkeypatch, 0, stdout='read 3\nwrite 3 3\n')
    seen = []
    conv._convert_in_child(rds, rds + '.out', on_progress=lambda *event: seen.append(event[:3]))
    assert seen == [('reading', 0, 0), ('reading', 0, 0), ('writing', 0, 3), ('writing', 3, 3)]


def test_a_failing_progress_callback_does_not_fail_the_conversion(rds):
    def boom(*event):
        raise RuntimeError('db down')
    assert conv.convert_for_ingestion(rds, on_progress=boom).endswith('.parquet')


def test_batch_convert_reports_each_file_done_or_failed(tmp_path):
    good = tmp_path / 'good.RDS'
    pyreadr.write_rds(str(good), pd.DataFrame({'lat': [1.0], 'lon': [2.0]}))
    (tmp_path / 'bad.RDS').write_bytes(b'not R data')
    events = []
    conv.batch_convert(str(tmp_path), pattern='.rds', delete_originals=True,
                       on_file=lambda path, stage, done, total, elapsed, message:
                       events.append((os.path.basename(path), stage, message)))
    finals = [e for e in events if e[1] in ('done', 'failed')]
    assert finals[0][:2] == ('bad.RDS', 'failed') and 'not a readable R data file' in finals[0][2]
    assert finals[1] == ('good.RDS', 'done', 'Converted to good.parquet')
