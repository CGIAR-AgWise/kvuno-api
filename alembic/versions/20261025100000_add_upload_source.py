"""add uploads.source: where an upload came from

'upload' — sent from the Upload page (the default, and what every existing
row is). 'folder' — a file found in HOUSEKEEPING_DATA_DIR whose columns the
default mapping does not cover: instead of being imported with a mapping that
cannot find its coordinates, it is held as an upload awaiting mapping. Folder
uploads have no owner, are visible to every signed-in user, and are never
deleted as abandoned (they are the operator's files).

Revision ID: b7d3f1a9c2e4
Revises: e16b8fad5988
Create Date: 2026-10-25 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b7d3f1a9c2e4'
down_revision: Union[str, None] = 'e16b8fad5988'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table so it also runs on SQLite.
    with op.batch_alter_table('uploads') as batch:
        batch.add_column(sa.Column('source', sa.String(length=20), server_default='upload', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('uploads') as batch:
        batch.drop_column('source')
