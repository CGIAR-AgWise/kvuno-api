"""Remove the false import conflicts logged before the ingest fix.

The old ingest decided "this batch had duplicates" from `rowcount`, which is
-1 under psycopg 3, so every batch ran the conflict lookup — after the insert.
It found the rows it had just inserted and logged each one as a conflict
with its own file's checksum. import_conflicts therefore holds an entry for
almost every row ever imported, and the Quality page counts them.

What is removed, and why it is safe:

- only rows the old code logged: `source = 'housekeeper'` (the fixed ingest
  logs as 'ingest', so nothing it records can be touched);
- only rows that clash with *their own file* (`check_sum` equals the file's
  checksum in `record_data`). A conflict with another file is real and kept;
- per file and key, all but one of those are kept. A key that really occurs
  twice in a file was logged twice (the inserted copy and the skipped one),
  so one per group is the false entry for the inserted row and any others
  are the file's real duplicates.

Dry run by default: it reports what it would delete. --apply deletes, in
batches of --batch-size, committing each. Postgres only.

    python scripts/cleanup_conflicts.py            # report
    python scripts/cleanup_conflicts.py --apply    # delete
"""
import os
import sys

# Ensure the project root is on sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    # noinspection PyTypeChecker
    sys.path.insert(0, _project_root)

os.environ.setdefault('LOG_LEVEL', 'WARNING')

import typer  # noqa: E402
from sqlalchemy import text  # noqa: E402

app = typer.Typer(name="cleanup-conflicts", help=__doc__.splitlines()[0], add_completion=False)

# The false entries: the first of each (file, key) group of self-clashing
# rows logged by the old code. PARTITION BY treats NULLs as equal, as the
# unique constraint (NULLS NOT DISTINCT) does.
FALSE_ENTRIES = """
    SELECT id FROM (
        SELECT id, row_number() OVER (
            PARTITION BY check_sum, country, province, lon, lat, variety, season_type, opt_date
            ORDER BY id) AS n
        FROM import_conflicts
        WHERE source = 'housekeeper'
          AND check_sum IS NOT NULL
          AND check_sum = (record_data::jsonb ->> 'check_sum')
    ) grouped
    WHERE n = 1
"""


@app.command()
def main(
    apply: bool = typer.Option(False, "--apply", help="Delete. Without it, only report."),
    batch_size: int = typer.Option(50_000, "--batch-size", help="Rows deleted per transaction."),
):
    from app.models.database_conn import MyDb
    from app.worker_app import build_app

    with build_app().app_context():
        session = MyDb.get_db().session
        total = session.execute(text("SELECT count(*) FROM import_conflicts")).scalar()
        false = session.execute(text(f"SELECT count(*) FROM ({FALSE_ENTRIES}) f")).scalar()
        typer.echo(f"import_conflicts: {total:,} rows; {false:,} are false entries from the old ingest.")
        if not apply or not false:
            if false:
                typer.echo("Nothing deleted (dry run). Re-run with --apply to delete them.")
            return

        # Fix the ids first. Deleting straight from FALSE_ENTRIES in batches
        # would renumber each group after every batch, and the next batch
        # would take the group's next row — until nothing was left.
        session.execute(text(f"CREATE TEMP TABLE false_conflicts AS {FALSE_ENTRIES}"))
        session.commit()
        deleted = 0
        while True:
            taken, n = session.execute(text(
                "WITH batch AS (DELETE FROM false_conflicts WHERE id IN "
                "  (SELECT id FROM false_conflicts LIMIT :n) RETURNING id), "
                "gone AS (DELETE FROM import_conflicts WHERE id IN (SELECT id FROM batch) RETURNING 1) "
                "SELECT (SELECT count(*) FROM batch), (SELECT count(*) FROM gone)"), {'n': batch_size}).one()
            session.commit()
            if not taken:
                break
            deleted += n
            typer.echo(f"  deleted {deleted:,} of {false:,}")
        left = session.execute(text("SELECT count(*) FROM import_conflicts")).scalar()
        typer.echo(f"Done: {deleted:,} deleted; {left:,} conflicts remain.")


if __name__ == "__main__":
    app()
