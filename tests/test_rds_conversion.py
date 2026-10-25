"""RDS conversion runs in a child process, and fails with a reason.

Reading an RDS is memory-heavy (pyreadr reads whole files; a 7.4 MB AgWise
file peaks at ~1.1 GB). In the 512 MB API container the kernel killed the
gunicorn worker mid-read, the stale-inspection recovery restarted the read on
the next poll, and the upload sat at "Reading the file…" forever.
"""
import os
import signal
import subprocess

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


def _child_exits(monkeypatch, returncode, stderr=''):
    monkeypatch.setattr(conv.subprocess, 'run',
                        lambda *a, **kw: subprocess.CompletedProcess(a, returncode, '', stderr))


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
    def hang(*a, **kw):
        raise subprocess.TimeoutExpired(a, conv.CONVERT_TIMEOUT_SECONDS)

    monkeypatch.setattr(conv.subprocess, 'run', hang)
    with pytest.raises(conv.ConversionError, match='took longer than'):
        conv.convert_for_ingestion(rds)
