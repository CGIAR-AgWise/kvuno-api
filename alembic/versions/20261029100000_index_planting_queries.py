"""indexes for the planting-data queries partners make

Measured on 5.5 M rows (the season's maize, soybean and cotton files):

* idx_coordinates_geography — `radius` is in metres, so the repo compares
  `CAST(coordinates AS geography(GEOMETRY,4326))` with ST_DWithin. The GiST
  index idx_coordinates is on the geometry column itself, which that cast
  cannot use: a 10 km search computed the spheroid distance to all ~900 k
  rows of a crop and variety (1.2 s; 14 ms with this index). The expression
  must stay exactly as the query writes it, or the planner silently stops
  using the index. Postgres only.
* idx_crop_variety_id and idx_country_crop_variety_id — a page is
  `WHERE crop_type = … AND variety = … [AND country = …] ORDER BY id`. With
  only single-column indexes Postgres walked the primary key and skipped
  millions of rows (cotton, stored last: 765 ms; 1 ms with the index), and
  counting combined two bitmaps of millions of rows each. The count is now an
  index-only scan.
* idx_crop_type and idx_country are dropped: each is the leading column of
  one of the new indexes, which serve the same lookups.

Building them reads the whole table (~30 s for 5.5 M rows) and blocks writes
meanwhile; reads continue.

Revision ID: a7c9e1b3d5f2
Revises: f4b6d8a0c2e3
Create Date: 2026-10-29 10:00:00

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'a7c9e1b3d5f2'
down_revision: Union[str, None] = 'f4b6d8a0c2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'planting_recommendations'
GEOGRAPHY_INDEX = 'idx_coordinates_geography'


def upgrade() -> None:
    op.create_index('idx_crop_variety_id', TABLE, ['crop_type', 'variety', 'id'])
    op.create_index('idx_country_crop_variety_id', TABLE, ['country', 'crop_type', 'variety', 'id'])
    op.drop_index('idx_crop_type', table_name=TABLE)
    op.drop_index('idx_country', table_name=TABLE)
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(f'CREATE INDEX IF NOT EXISTS {GEOGRAPHY_INDEX} ON {TABLE} '
                   f'USING gist ((CAST(coordinates AS geography(GEOMETRY,4326))))')


def downgrade() -> None:
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(f'DROP INDEX IF EXISTS {GEOGRAPHY_INDEX}')
    op.create_index('idx_country', TABLE, ['country'])
    op.create_index('idx_crop_type', TABLE, ['crop_type'])
    op.drop_index('idx_country_crop_variety_id', table_name=TABLE)
    op.drop_index('idx_crop_variety_id', table_name=TABLE)
