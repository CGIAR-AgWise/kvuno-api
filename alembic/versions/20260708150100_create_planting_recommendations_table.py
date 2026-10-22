"""create_planting_recommendations_table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-08 15:01:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry

from app.utils.migration_utils import get_integer_column_type, is_sqlite, now_default

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = 'planting_recommendations'


def upgrade() -> None:
    sqlite = is_sqlite()

    if not sqlite:
        op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    # SQLite has no geometry type without the SpatiaLite extension, and
    # geoalchemy2 calls SpatiaLite functions (RecoverGeometryColumn) for a
    # Geometry column there. The zero-infra SQLite setup gets a plain Text
    # column instead, so the schema migrates end to end; spatial queries and
    # ingestion (which uses the Postgres INSERT ... ON CONFLICT) need Postgres.
    coordinates_type = sa.Text() if sqlite else Geometry('POINT', srid=4326)

    op.create_table(
        TABLE_NAME,
        sa.Column('id', get_integer_column_type(), primary_key=True, autoincrement=True),
        sa.Column('check_sum', sa.String(100), nullable=False),
        sa.Column('country', sa.String(20)),
        sa.Column('province', sa.String(20)),
        sa.Column('coordinates', coordinates_type, nullable=False),
        sa.Column('lon', sa.REAL),
        sa.Column('lat', sa.REAL),
        sa.Column('variety', sa.String(20)),
        sa.Column('season_type', sa.String(20)),
        sa.Column('opt_date', sa.String(8)),
        sa.Column('planting_option', sa.Integer),
        sa.Column('created_at', sa.DateTime, server_default=now_default()),
        sa.Column('updated_at', sa.DateTime, server_default=now_default()),
        # Inline: SQLite cannot ALTER a constraint onto an existing table.
        sa.UniqueConstraint(
            'country', 'province', 'lon', 'lat', 'variety', 'season_type', 'opt_date',
            name='uq_planting_recommendation',
        ),
    )
    op.create_index('idx_check_sum', TABLE_NAME, ['check_sum'])
    if not sqlite:
        # A GiST index on a geometry; meaningless on the SQLite Text stand-in.
        op.create_index('idx_coordinates', TABLE_NAME, ['coordinates'], postgresql_using='gist')
    op.create_index('idx_country', TABLE_NAME, ['country'])
    op.create_index('idx_lat', TABLE_NAME, ['lat'])
    op.create_index('idx_lon', TABLE_NAME, ['lon'])
    op.create_index('idx_opt_date', TABLE_NAME, ['opt_date'])
    op.create_index('idx_planting_option', TABLE_NAME, ['planting_option'])
    op.create_index('idx_province', TABLE_NAME, ['province'])
    op.create_index('idx_season_type', TABLE_NAME, ['season_type'])
    op.create_index('idx_variety', TABLE_NAME, ['variety'])


def downgrade() -> None:
    op.drop_table(TABLE_NAME)
