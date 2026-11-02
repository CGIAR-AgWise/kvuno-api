"""add uploads.file_modified_at and uploads.data_date

file_modified_at: the file's own last-modified time — the browser's
File.lastModified (tus metadata `lastModified`) or a held data-folder file's
mtime. Only a suggestion, since edits and copies change it: it pre-fills the
mapping form's "Data date".

data_date: when the data was produced, as confirmed by the person mapping the
file. Imported rows take it as created_at; without one they take the file's
first import time. RDS and Parquet files record no creation date themselves.

Revision ID: e2a4c6b8d0f1
Revises: d9f1b3c5e7a2
Create Date: 2026-10-25 13:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e2a4c6b8d0f1'
down_revision: Union[str, None] = 'd9f1b3c5e7a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table so it also runs on SQLite.
    with op.batch_alter_table('uploads') as batch:
        batch.add_column(sa.Column('file_modified_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('data_date', sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('uploads') as batch:
        batch.drop_column('data_date')
        batch.drop_column('file_modified_at')
