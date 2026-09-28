"""create user_tokens table

Revision ID: 1cb31978bb89
Revises: 92d64c2ee467
Create Date: 2026-07-10 10:50:40.238167

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.utils.migration_utils import get_integer_column_type

# revision identifiers, used by Alembic.
revision: str = '1cb31978bb89'
down_revision: Union[str, None] = '92d64c2ee467'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = 'user_tokens'


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column('id', get_integer_column_type(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('token', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    )
    op.create_index('idx_user_tokens_token', TABLE_NAME, ['token'])


def downgrade() -> None:
    op.drop_index('idx_user_tokens_token', table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
