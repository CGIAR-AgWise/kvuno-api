"""Duplicate detection for uploads.

The worker already skips a file whose checksum is fully ingested
(`existing.offset is None` -> return), but that happened *after* the job was
enqueued. The user was told "Processing started", a job ran, and it silently did
nothing. The check now happens at /ui/upload/complete, where the file is on disk
and already being read for the preview.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.routes.main import _describe_duplicate


def _import(checksum='abc', offset=None, original='orig.parquet', processed=True):
    record = MagicMock()
    record.offset = offset
    record.original_filename = original
    record.file_name = 'stored.parquet'
    record.processed_at = datetime(2026, 1, 1) if processed else None
    return record


@pytest.fixture
def known(monkeypatch):
    """Control what the checksum resolves to and what the repo finds."""
    state = {'checksum': 'abc', 'record': None}

    monkeypatch.setattr(
        'app.utils.calculate_file_checksum', lambda path, logger, algorithm='sha256': state['checksum']
    )
    monkeypatch.setattr(
        'app.repo.file_import.FileImportRepo.get_by_checksum',
        lambda self, cs: state['record'],
    )
    return state


def test_unknown_file_is_not_a_duplicate(known):
    known['record'] = None
    assert _describe_duplicate('/tmp/x.parquet') is None


def test_fully_processed_file_is_reported_as_processed(known):
    known['record'] = _import(offset=None)
    result = _describe_duplicate('/tmp/x.parquet')
    assert result is not None
    # 'processed' means the worker would no-op, so the UI must say it is skipped.
    assert result['status'] == 'processed'
    assert result['original_filename'] == 'orig.parquet'
    assert result['offset'] is None
    assert result['processed_at'] is not None


def test_partially_processed_file_is_reported_as_partial(known):
    known['record'] = _import(offset=9317)
    result = _describe_duplicate('/tmp/x.parquet')
    # 'partial' is a resume, not a duplicate — the file is worth submitting.
    assert result['status'] == 'partial'
    assert result['offset'] == 9317


def test_missing_checksum_is_not_a_duplicate(known):
    known['checksum'] = None
    assert _describe_duplicate('/tmp/x.parquet') is None


def test_lookup_failure_does_not_block_the_upload(known):
    """A failed dedupe check must not turn an upload into an error."""
    known['record'] = None
    with patch('app.repo.file_import.FileImportRepo.get_by_checksum',
               side_effect=RuntimeError('db down')):
        assert _describe_duplicate('/tmp/x.parquet') is None
