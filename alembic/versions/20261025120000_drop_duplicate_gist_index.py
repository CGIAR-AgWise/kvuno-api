"""drop idx_planting_recommendations_coordinates, a second GiST index on coordinates

idx_coordinates is the same GiST index on the same column. Every inserted row
updated both, and the planner only ever uses one.

Revision ID: d9f1b3c5e7a2
Revises: c4e8a2d6f1b3
Create Date: 2026-10-25 12:00:00

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'd9f1b3c5e7a2'
down_revision: Union[str, None] = 'c4e8a2d6f1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Postgres only: on SQLite `coordinates` is Text and has no GiST index.
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('DROP INDEX IF EXISTS idx_planting_recommendations_coordinates')


def downgrade() -> None:
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('CREATE INDEX IF NOT EXISTS idx_planting_recommendations_coordinates '
                   'ON planting_recommendations USING gist (coordinates)')
