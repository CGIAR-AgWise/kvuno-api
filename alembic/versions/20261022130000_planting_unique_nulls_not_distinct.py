"""make uq_planting_recommendation NULLS NOT DISTINCT

Every column of ``uq_planting_recommendation`` is nullable, and Postgres treats
NULLs as distinct in a unique constraint by default. A row with a NULL in any
key column therefore never conflicted, so ``INSERT ... ON CONFLICT DO NOTHING``
inserted it again on every replay or resume of the same file.

Upgrade (PostgreSQL 15+):
1. Deletes rows that duplicate another row on the seven key columns, comparing
   NULLs as equal, keeping the lowest id. The count is printed and logged.
2. Recreates the constraint as ``UNIQUE NULLS NOT DISTINCT``.

Downgrade recreates the plain constraint. The duplicate rows deleted by the
upgrade are NOT restored — they were copies of rows that remain.

SQLite: no-op. SQLite has no NULLS NOT DISTINCT, rebuilding the table in batch
mode to change a constraint gains nothing, and ingestion (which is what
creates the duplicates) only runs against Postgres.

Revision ID: f3a9c2d4e5b6
Revises: 7b1d0c5e2a91
Create Date: 2026-10-22 13:00:00

"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f3a9c2d4e5b6'
down_revision: Union[str, None] = '7b1d0c5e2a91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = 'planting_recommendations'
CONSTRAINT = 'uq_planting_recommendation'
KEY_COLUMNS = ['country', 'province', 'lon', 'lat', 'variety', 'season_type', 'opt_date']

log = logging.getLogger('alembic.runtime.migration')


def _supports_nulls_not_distinct(bind) -> bool:
    if bind.dialect.name != 'postgresql':
        return False
    # None in offline (--sql) mode, where there is no server to ask; the
    # documented image is postgis/postgis:17-3.5, so assume it is supported.
    version = bind.dialect.server_version_info
    if version is not None and version < (15,):
        log.warning(f"PostgreSQL {version} has no NULLS NOT DISTINCT (15+); "
                    f"{CONSTRAINT} left unchanged")
        return False
    return True


def upgrade() -> None:
    bind = op.get_bind()
    if not _supports_nulls_not_distinct(bind):
        return

    keys = ', '.join(KEY_COLUMNS)
    any_null = ' OR '.join(f'{c} IS NULL' for c in KEY_COLUMNS)
    # PARTITION BY groups NULLs together, i.e. it compares the key columns
    # with IS NOT DISTINCT FROM semantics, and unlike a self-join on seven
    # IS NOT DISTINCT FROM predicates it is one sort rather than O(n^2).
    # Only rows with a NULL key can be duplicates: the existing constraint
    # already rejects duplicates whose keys are all non-NULL.
    dedupe = sa.text(
        f"DELETE FROM {TABLE_NAME} WHERE id IN ("
        f"  SELECT id FROM ("
        f"    SELECT id, row_number() OVER (PARTITION BY {keys} ORDER BY id) AS rn"
        f"    FROM {TABLE_NAME} WHERE {any_null}"
        f"  ) ranked WHERE rn > 1"
        f")"
    )
    if op.get_context().as_sql:
        op.execute(dedupe)  # offline: emit the SQL, there is no count to report
    else:
        deleted = bind.execute(dedupe).rowcount or 0
        # Logged at WARNING so the count shows at any LOG_LEVEL: it reports
        # deleted rows. (env.py routes Alembic logging through the app's.)
        log.warning(f"Deleted {deleted} duplicate row(s) from {TABLE_NAME} (NULL-aware key match)")

    op.drop_constraint(CONSTRAINT, TABLE_NAME, type_='unique')
    op.create_unique_constraint(CONSTRAINT, TABLE_NAME, KEY_COLUMNS,
                                postgresql_nulls_not_distinct=True)


def downgrade() -> None:
    bind = op.get_bind()
    if not _supports_nulls_not_distinct(bind):
        return
    op.drop_constraint(CONSTRAINT, TABLE_NAME, type_='unique')
    op.create_unique_constraint(CONSTRAINT, TABLE_NAME, KEY_COLUMNS)
