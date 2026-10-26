"""Give old planting_recommendations rows the timestamps they were stored without.

`_row_mapping` used to copy `model_dump()` straight into the INSERT, and the
pydantic model carries `created_at`/`updated_at` as None. An explicit NULL
overrides the columns' `now()` default, so rows imported before that fix have
no timestamps at all. `updated_at` drives "recently imported" displays and a
NULL sorts unpredictably.

Each row's file is known by its checksum, so `file_imports.processed_at` (the
first import time) is used for both columns when it exists. That is the same
fallback a fresh import uses for `created_at` when no data date was confirmed.
Rows with no checksum record, or whose record has no `processed_at`, get the
time the backfill runs — there is nothing better to recover. Existing values
are never overwritten.

Dry run by default: it reports what it would change. --apply writes in
batches of --batch-size, committing each.

    python scripts/backfill_timestamps.py            # report
    python scripts/backfill_timestamps.py --apply    # write
"""
import datetime
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

app = typer.Typer(name="backfill-timestamps", help=__doc__.splitlines()[0], add_completion=False)

MISSING = "(created_at IS NULL OR updated_at IS NULL)"

# The date is recovered from the checksum's file_imports row; the keyset
# (`id > :after`) keeps every batch from rescanning the rows already done.
DATABLE = f"""
    SELECT id FROM planting_recommendations
    WHERE id > :after AND {MISSING}
      AND EXISTS (SELECT 1 FROM file_imports f
                  WHERE f.check_sum = planting_recommendations.check_sum
                    AND f.processed_at IS NOT NULL)
    ORDER BY id LIMIT :n
"""

FILL_FROM_IMPORT = f"""
    UPDATE planting_recommendations SET
      created_at = COALESCE(created_at, (SELECT f.processed_at FROM file_imports f
                                         WHERE f.check_sum = planting_recommendations.check_sum)),
      updated_at = COALESCE(updated_at, (SELECT f.processed_at FROM file_imports f
                                         WHERE f.check_sum = planting_recommendations.check_sum))
    WHERE id IN ({DATABLE})
    RETURNING id
"""

# Whatever is left has no import date to recover; it happened before this run.
FILL_WITH_NOW = f"""
    UPDATE planting_recommendations SET
      created_at = COALESCE(created_at, :now),
      updated_at = COALESCE(updated_at, :now)
    WHERE id IN (SELECT id FROM planting_recommendations
                 WHERE id > :after AND {MISSING}
                 ORDER BY id LIMIT :n)
    RETURNING id
"""


def _run(session, apply: bool, batch_size: int) -> dict:
    """Backfill in place and return the counts (also used by the tests)."""
    nulls = session.execute(text(
        f"SELECT count(*) FROM planting_recommendations WHERE {MISSING}")).scalar()
    datable = session.execute(text(
        f"SELECT count(*) FROM planting_recommendations p WHERE {MISSING} "
        "AND EXISTS (SELECT 1 FROM file_imports f WHERE f.check_sum = p.check_sum "
        "            AND f.processed_at IS NOT NULL)")).scalar()
    result = {'missing': nulls, 'datable': datable, 'from_import': 0, 'from_now': 0}
    if not apply or not nulls:
        return result

    # Naive UTC, like every other timestamp the app writes (CURRENT_TIMESTAMP
    # would use the server's timezone).
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    for statement, key in ((FILL_FROM_IMPORT, 'from_import'), (FILL_WITH_NOW, 'from_now')):
        after = 0
        while True:
            params = {'after': after, 'n': batch_size}
            if key == 'from_now':
                params['now'] = now
            ids = session.execute(text(statement), params).scalars().all()
            session.commit()
            if not ids:
                break
            after = max(ids)
            result[key] += len(ids)
            typer.echo(f"  {result[key]:,} rows ({key.replace('_', ' ')})")
    return result


@app.command()
def main(
    apply: bool = typer.Option(False, "--apply", help="Write. Without it, only report."),
    batch_size: int = typer.Option(50_000, "--batch-size", help="Rows updated per transaction."),
):
    from app.models.database_conn import MyDb
    from app.worker_app import build_app

    with build_app().app_context():
        session = MyDb.get_db().session
        result = _run(session, apply, batch_size)
        typer.echo(
            f"planting_recommendations: {result['missing']:,} rows missing a timestamp; "
            f"{result['datable']:,} can use their file's first import time, "
            f"{result['missing'] - result['datable']:,} will use the run time.")
        if result['missing'] and not apply:
            typer.echo("Nothing written (dry run). Re-run with --apply.")
        elif apply and result['missing']:
            typer.echo(f"Done: {result['from_import']:,} dated from file_imports, "
                       f"{result['from_now']:,} from the run time.")


if __name__ == "__main__":
    app()
