"""create_file_imports_table

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-07-08 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.utils.migration_utils import get_integer_column_type

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = 'file_imports'


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column('id', get_integer_column_type(), primary_key=True, autoincrement=True),
        sa.Column('check_sum', sa.String(100), nullable=False),
        sa.Column('file_name', sa.String(120), nullable=False),
        sa.Column('processed_at', sa.DateTime, server_default=sa.text('now()')),
        sa.Column('offset', get_integer_column_type(), nullable=True),
    )
    op.create_unique_constraint('file_imports_check_sum_key', TABLE_NAME, ['check_sum'])


def downgrade() -> None:
    op.drop_table(TABLE_NAME)
