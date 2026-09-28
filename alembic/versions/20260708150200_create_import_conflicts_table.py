"""create_import_conflicts_table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-08 15:02:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.utils.migration_utils import get_integer_column_type

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = 'import_conflicts'


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column('id', get_integer_column_type(), primary_key=True, autoincrement=True),
        sa.Column('record_data', sa.JSON, nullable=False),
        sa.Column('country', sa.String(20)),
        sa.Column('province', sa.String(20)),
        sa.Column('lon', sa.REAL),
        sa.Column('lat', sa.REAL),
        sa.Column('variety', sa.String(20)),
        sa.Column('season_type', sa.String(20)),
        sa.Column('opt_date', sa.String(8)),
        sa.Column('check_sum', sa.String(100)),
        sa.Column('source', sa.String(50)),
        sa.Column('created_at', sa.DateTime, server_default=sa.text('now()')),
    )
    op.create_index('idx_conflict_created_at', TABLE_NAME, ['created_at'])


def downgrade() -> None:
    op.drop_table(TABLE_NAME)
