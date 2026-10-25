"""Data-folder RDS conversions show on the Jobs page.

Scans used to convert silently: a long pyreadr read looked like a hung scan,
and a failed conversion was only in the log.
"""
import time

from app.services import housekeeper as hk
from app.services import progress_store as ps


def test_each_stage_becomes_a_job_update(monkeypatch):
    writes = []
    monkeypatch.setattr(hk, '_write_progress',
                        lambda path, status, current, total, message='', **kw:
                        writes.append((status, current, total, message, kw.get('error'))))

    hk.report_conversion('/data/a.RDS', 'reading', 0, 0, 75.0)
    hk.report_conversion('/data/a.RDS', 'writing', 500, 2000, 80.0)
    hk.report_conversion('/data/a.RDS', 'done', 2000, 2000, 81.0, 'Converted to a.parquet')
    hk.report_conversion('/data/b.RDS', 'failed', 0, 0, 3.0, 'This is not a readable R data file (x).')

    assert writes == [
        ('converting', 0, 0, 'Converting to Parquet: reading the R file (1m 15s)', None),
        ('converting', 500, 2000, 'Converting to Parquet: writing 500 of 2,000 rows', None),
        ('completed', 2000, 2000, 'Converted to a.parquet — 2,000 rows in 1m 21s', None),
        ('error', 0, 0, 'This is not a readable R data file (x).', 'This is not a readable R data file (x).'),
    ]


def test_a_conversion_that_stopped_reporting_is_stale():
    """The converter reports every few seconds even mid-read, so silence means it died."""
    old = time.time() - 3600
    job = ps.mark_stale({'status': 'converting', 'mtime': old})
    assert job['status'] == 'stale'
    assert ps.mark_stale({'status': 'converting', 'mtime': time.time()})['status'] == 'converting'
