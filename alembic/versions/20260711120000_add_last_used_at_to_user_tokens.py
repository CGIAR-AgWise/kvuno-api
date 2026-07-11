"""Add last_used_at to user_tokens

Revision ID: 20260711120000
Revises: 20260710111125_add_expires_at_to_user_tokens
Create Date: 2026-07-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '20260711120000'
down_revision: Union[str, None] = '20260710111125_add_expires_at_to_user_tokens'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_tokens', sa.Column('last_used_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('user_tokens', 'last_used_at')
