"""batch_convert is safe when several processes scan the same directory.

Production: the startup scan runs in every gunicorn worker. Two converted the
same RDS; the first deleted it and the second's os.remove raised
FileNotFoundError: 'static/data/useCase_..._season.RDS', aborting its scan.
"""
import os

import pytest

from app.utils import rds_to_parquet as conv
from app.utils.file_lock import FileLock


@pytest.fixture
def fake_convert(monkeypatch):
    """Stand-in for pyreadr: writes the output, records what it converted."""
    seen = []

    def convert(rds_path, parquet_path=None, on_progress=None):
        seen.append(os.path.basename(rds_path))
        with open(parquet_path, 'w') as f:
            f.write('parquet')
        return parquet_path

    monkeypatch.setattr(conv, '_convert_in_child', convert)
    return seen


def test_converts_atomically_and_removes_the_original(tmp_path, fake_convert):
    (tmp_path / 'a.RDS').write_text('rds')
    created = conv.batch_convert(str(tmp_path), pattern='.rds', delete_originals=True)
    assert created == [str(tmp_path / 'a.parquet')]
    assert sorted(os.listdir(tmp_path)) == ['a.parquet']  # no .tmp, no lock, no RDS


def test_original_removed_by_another_process_is_not_an_error(tmp_path, monkeypatch):
    rds = tmp_path / 'a.RDS'
    rds.write_text('rds')

    def convert_while_the_other_worker_deletes_it(rds_path, parquet_path=None, on_progress=None):
        with open(parquet_path, 'w') as f:
            f.write('parquet')
        os.remove(rds_path)  # the other gunicorn worker finished first
        return parquet_path

    monkeypatch.setattr(conv, '_convert_in_child', convert_while_the_other_worker_deletes_it)
    assert conv.batch_convert(str(tmp_path), pattern='.rds', delete_originals=True) == [
        str(tmp_path / 'a.parquet')]


def test_a_file_being_converted_elsewhere_is_skipped(tmp_path, fake_convert):
    (tmp_path / 'a.RDS').write_text('rds')
    other = FileLock(str(tmp_path / ('a.RDS' + conv.CONVERT_LOCK_SUFFIX)), 600)
    assert other.acquire()
    assert conv.batch_convert(str(tmp_path), pattern='.rds', delete_originals=True) == []
    assert fake_convert == []
    assert (tmp_path / 'a.RDS').exists()
    other.release()


def test_one_bad_file_does_not_stop_the_rest(tmp_path, monkeypatch):
    (tmp_path / 'bad.RDS').write_text('rds')
    (tmp_path / 'good.RDS').write_text('rds')

    def convert(rds_path, parquet_path=None, on_progress=None):
        with open(parquet_path, 'w') as f:
            f.write('partial')
        if os.path.basename(rds_path) == 'bad.RDS':
            raise ValueError('not an R data file')
        return parquet_path

    monkeypatch.setattr(conv, '_convert_in_child', convert)
    created = conv.batch_convert(str(tmp_path), pattern='.rds', delete_originals=True)
    assert created == [str(tmp_path / 'good.parquet')]
    # The bad file is untouched and leaves no partial output behind.
    assert sorted(os.listdir(tmp_path)) == ['bad.RDS', 'good.parquet']
