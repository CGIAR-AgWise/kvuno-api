"""create job_progress table

Revision ID: j0bpr0gr3ss
Revises: 022ecd9e3632
Create Date: 2026-07-13 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.utils.migration_utils import get_integer_column_type

revision: str = 'j0bpr0gr3ss'
down_revision: Union[str, None] = '022ecd9e3632'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'job_progress',
        sa.Column('id', get_integer_column_type(), primary_key=True, autoincrement=True),
        sa.Column('file_name', sa.String(255), nullable=False, unique=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='unknown'),
        sa.Column('current_row', get_integer_column_type(), nullable=False, server_default='0'),
        sa.Column('total_rows', get_integer_column_type(), nullable=False, server_default='0'),
        sa.Column('message', sa.String(500), nullable=True),
        sa.Column('updated_at', sa.DateTime, server_default=sa.text('now()')),
    )


def downgrade() -> None:
    op.drop_table('job_progress')
