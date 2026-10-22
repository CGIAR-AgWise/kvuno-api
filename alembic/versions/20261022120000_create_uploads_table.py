"""create uploads table

Replaces the on-disk state of the chunked upload flow (`.chunks/` plus the
`.meta.json` / `.map.json` / `.preview.json` sidecars) with one row per upload.

Revision ID: 7b1d0c5e2a91
Revises: 00318c15e700
Create Date: 2026-10-22 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.utils.migration_utils import get_integer_column_type

revision: str = '7b1d0c5e2a91'
down_revision: Union[str, None] = '00318c15e700'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'uploads',
        sa.Column('id', sa.String(32), nullable=False),
        sa.Column('owner_id', get_integer_column_type(), nullable=True),
        sa.Column('original_name', sa.String(255), nullable=False),
        sa.Column('size', get_integer_column_type(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='receiving'),
        sa.Column('stored_path', sa.String(512), nullable=True),
        sa.Column('sha256', sa.String(64), nullable=True),
        sa.Column('preview', sa.JSON, nullable=True),
        sa.Column('duplicate', sa.JSON, nullable=True),
        sa.Column('column_map', sa.JSON, nullable=True),
        sa.Column('error', sa.String(2000), nullable=True),
        # CURRENT_TIMESTAMP, not now(): this project also migrates on SQLite.
        sa.Column('created_at', sa.DateTime, nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime, nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('id', name='uploads_pkey'),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='SET NULL'),
    )
    op.create_index('ix_uploads_owner_id', 'uploads', ['owner_id'])
    op.create_index('ix_uploads_status', 'uploads', ['status'])


def downgrade() -> None:
    op.drop_index('ix_uploads_status', table_name='uploads')
    op.drop_index('ix_uploads_owner_id', table_name='uploads')
    op.drop_table('uploads')
