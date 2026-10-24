"""add user approval status

New accounts start `pending` and can do nothing until an administrator
approves them. Every existing account is backfilled `approved` (the column's
server default) so nobody is locked out by the upgrade.

The `role` column follows in e16b8fad5988.

Revision ID: a8c4e2f1d9b3
Revises: f3a9c2d4e5b6
Create Date: 2026-10-23 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.utils.migration_utils import get_integer_column_type

revision: str = 'a8c4e2f1d9b3'
down_revision: Union[str, None] = 'f3a9c2d4e5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table: SQLite cannot ALTER TABLE ADD COLUMN ... with every
    # constraint form; batch mode copies the table there and is a plain ALTER
    # on PostgreSQL.
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('status', sa.String(20), nullable=False, server_default='approved'))
        batch.add_column(sa.Column('reviewed_at', sa.DateTime, nullable=True))
        batch.add_column(sa.Column('reviewed_by', get_integer_column_type(), nullable=True))
    op.create_index('ix_users_status', 'users', ['status'])


def downgrade() -> None:
    op.drop_index('ix_users_status', table_name='users')
    with op.batch_alter_table('users') as batch:
        batch.drop_column('reviewed_by')
        batch.drop_column('reviewed_at')
        batch.drop_column('status')
