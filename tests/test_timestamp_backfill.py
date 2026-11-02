"""The one-off timestamp backfill: file import time, then the run time."""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from scripts.backfill_timestamps import _run


@pytest.fixture
def session():
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE file_imports "
                          "(id INTEGER PRIMARY KEY, check_sum VARCHAR(100) UNIQUE, processed_at DATETIME)"))
        conn.execute(text("CREATE TABLE planting_recommendations "
                          "(id INTEGER PRIMARY KEY, check_sum VARCHAR(100), "
                          " created_at DATETIME, updated_at DATETIME)"))
        conn.execute(text("INSERT INTO file_imports (id, check_sum, processed_at) VALUES "
                          "(1, 'sum-a', '2026-01-01 00:00:00'), (2, 'sum-null', NULL)"))
        conn.execute(text("INSERT INTO planting_recommendations (id, check_sum, created_at, updated_at) VALUES "
                          "(1, 'sum-a', NULL, NULL), "        # datable
                          "(2, 'sum-a', '2025-05-05 00:00:00', NULL), "  # only updated_at missing
                          "(3, 'sum-b', NULL, NULL), "        # no file_imports row
                          "(4, 'sum-c', '2025-01-01 00:00:00', '2025-01-02 00:00:00'), "  # untouched
                          "(5, 'sum-null', NULL, NULL)"))     # file_imports row has no date
    with Session(engine) as session:
        yield session


def _rows(session):
    return {row[0]: (row[1], row[2]) for row in session.execute(text(
        'SELECT id, created_at, updated_at FROM planting_recommendations'))}


def test_a_dry_run_reports_and_writes_nothing(session):
    result = _run(session, apply=False, batch_size=2)
    assert result == {'missing': 4, 'datable': 2, 'from_import': 0, 'from_now': 0}
    assert all(created is None and updated is None for i, (created, updated) in _rows(session).items()
               if i in (1, 3, 5))


def test_the_file_import_time_wins_then_the_run_time(session):
    result = _run(session, apply=True, batch_size=2)

    assert result == {'missing': 4, 'datable': 2, 'from_import': 2, 'from_now': 2}
    rows = _rows(session)
    assert rows[1] == ('2026-01-01 00:00:00', '2026-01-01 00:00:00')
    assert rows[2] == ('2025-05-05 00:00:00', '2026-01-01 00:00:00'), 'existing values are kept'
    assert rows[4] == ('2025-01-01 00:00:00', '2025-01-02 00:00:00'), 'nothing to do'
    assert rows[3][0] is not None and rows[3][1] is not None
    assert rows[5][0] is not None and rows[5][1] is not None


def test_running_it_again_changes_nothing(session):
    _run(session, apply=True, batch_size=2)
    before = _rows(session)
    assert _run(session, apply=True, batch_size=2) == {
        'missing': 0, 'datable': 0, 'from_import': 0, 'from_now': 0}
    assert _rows(session) == before
