"""add_original_filename_column_to_file_imports

Revision ID: 9a9c7b01b1db
Revises: c3d4e5f6a7b8
Create Date: 2026-07-08 19:22:24.494562

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9a9c7b01b1db'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None



def upgrade() -> None:
    op.add_column('file_imports', sa.Column('original_filename', sa.String(255)))


def downgrade() -> None:
    op.drop_column('file_imports', 'original_filename')
