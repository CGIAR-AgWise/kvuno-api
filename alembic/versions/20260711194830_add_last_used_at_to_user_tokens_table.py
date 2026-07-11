"""add last used at to user tokens table

Revision ID: 022ecd9e3632
Revises: f0436b79c8b5
Create Date: 2026-07-11 19:48:30.733065

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa




# revision identifiers, used by Alembic.
revision: str = '022ecd9e3632'
down_revision: Union[str, None] = 'f0436b79c8b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_tokens', sa.Column('last_used_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('user_tokens', 'last_used_at')