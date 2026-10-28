"""add crop_type to planting_recommendations, import_conflicts and uploads

planting_recommendations.crop_type: the crop a row is about, lower-case
('maize'). Taken from a file column mapped to crop_type, else from the crop
type chosen when the file was mapped (uploads.crop_type). Existing rows stay
NULL: nothing recorded their crop.

It joins uq_planting_recommendation, so the same place, variety, season and
date for two different crops are two rows instead of the second being dropped
as a duplicate. Existing rows all have NULL there, and the constraint is NULLS
NOT DISTINCT, so they keep colliding with each other exactly as before.

Postgres only for the constraint, like f3a9c2d4e5b6: SQLite cannot alter a
constraint in place, and ingestion only runs against Postgres. Rebuilding the
constraint builds its index again — on a table of millions of rows that takes
a while and blocks writes meanwhile; reads are unaffected.

Downgrade fails if two rows now differ only by crop_type: delete one of each
pair first.

Revision ID: f4b6d8a0c2e3
Revises: e2a4c6b8d0f1
Create Date: 2026-10-28 10:00:00

"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f4b6d8a0c2e3'
down_revision: Union[str, None] = 'e2a4c6b8d0f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = 'planting_recommendations'
CONSTRAINT = 'uq_planting_recommendation'
OLD_KEY = ['country', 'province', 'lon', 'lat', 'variety', 'season_type', 'opt_date']
NEW_KEY = [*OLD_KEY, 'crop_type']

log = logging.getLogger('alembic.runtime.migration')


def _nulls_not_distinct(bind) -> bool:
    # None in offline (--sql) mode; the documented image is PostGIS 17.
    version = bind.dialect.server_version_info
    return version is None or version >= (15,)


def _recreate_constraint(columns) -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    log.warning(f"Rebuilding {CONSTRAINT} on ({', '.join(columns)}); on a large table this takes a while")
    op.drop_constraint(CONSTRAINT, TABLE_NAME, type_='unique')
    if _nulls_not_distinct(bind):
        op.create_unique_constraint(CONSTRAINT, TABLE_NAME, columns, postgresql_nulls_not_distinct=True)
    else:
        op.create_unique_constraint(CONSTRAINT, TABLE_NAME, columns)


def upgrade() -> None:
    # batch_alter_table so it also runs on SQLite.
    with op.batch_alter_table(TABLE_NAME) as batch:
        batch.add_column(sa.Column('crop_type', sa.String(30), nullable=True))
    op.create_index('idx_crop_type', TABLE_NAME, ['crop_type'])
    with op.batch_alter_table('import_conflicts') as batch:
        batch.add_column(sa.Column('crop_type', sa.String(30), nullable=True))
    with op.batch_alter_table('uploads') as batch:
        batch.add_column(sa.Column('crop_type', sa.String(30), nullable=True))
    _recreate_constraint(NEW_KEY)


def downgrade() -> None:
    _recreate_constraint(OLD_KEY)
    with op.batch_alter_table('uploads') as batch:
        batch.drop_column('crop_type')
    with op.batch_alter_table('import_conflicts') as batch:
        batch.drop_column('crop_type')
    op.drop_index('idx_crop_type', table_name=TABLE_NAME)
    with op.batch_alter_table(TABLE_NAME) as batch:
        batch.drop_column('crop_type')
