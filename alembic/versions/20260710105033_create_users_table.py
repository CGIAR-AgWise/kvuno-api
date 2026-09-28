"""create users table

Revision ID: 92d64c2ee467
Revises: 9a9c7b01b1db
Create Date: 2026-07-10 10:50:33.491036

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.utils.migration_utils import get_integer_column_type

# revision identifiers, used by Alembic.
revision: str = '92d64c2ee467'
down_revision: Union[str, None] = '9a9c7b01b1db'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = 'users'


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column('id', get_integer_column_type(), primary_key=True, autoincrement=True),
        sa.Column('username', sa.String(length=100), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.UniqueConstraint('username', name='users_username_key'),
        sa.UniqueConstraint('email', name='users_email_key'),
    )


def downgrade() -> None:
    op.drop_table(TABLE_NAME)
