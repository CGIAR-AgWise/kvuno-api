"""add uploads.progress: where an upload's inspection has got to

{stage, done, total, elapsed} while the file is read — stage is reading or
writing during the RDS -> Parquet conversion, then checksum and preview — so
the Upload page can show more than "Reading file…" for minutes on end.

Revision ID: c4e8a2d6f1b3
Revises: b7d3f1a9c2e4
Create Date: 2026-10-25 11:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c4e8a2d6f1b3'
down_revision: Union[str, None] = 'b7d3f1a9c2e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table so it also runs on SQLite.
    with op.batch_alter_table('uploads') as batch:
        batch.add_column(sa.Column('progress', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('uploads') as batch:
        batch.drop_column('progress')
