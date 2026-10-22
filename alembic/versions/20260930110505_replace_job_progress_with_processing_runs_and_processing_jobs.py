"""replace job_progress with processing_runs and processing_jobs

Supersedes the old ``job_progress`` table.

Why it was replaced rather than extended:
- ``job_progress.file_name`` was UNIQUE, so there could only ever be ONE row per
  filename. Re-processing a file overwrote its history outright.
- It had no ``started_at``, so the rows/sec + ETA feature depended entirely on
  the Redis hash and vanished whenever a job was read from the database.
- ``updated_at`` was ``server_default=now()``, which applies on INSERT only.
  Nothing refreshed it on UPDATE, so for any existing row it kept the creation
  timestamp — breaking mtime ordering and making healthy jobs look stalled to
  ``mark_stale()``. ``updated_at`` is now written explicitly on every update.

Revision ID: 00318c15e700
Revises: j0bpr0gr3ss
Create Date: 2026-09-30 11:05:05.490821

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.utils.migration_utils import get_integer_column_type, is_specified_db, now_default

revision: str = '00318c15e700'
down_revision: Union[str, None] = 'j0bpr0gr3ss'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'processing_runs',
        sa.Column('id', get_integer_column_type(), primary_key=True, autoincrement=True),
        sa.Column('label', sa.String(255), nullable=True),
        sa.Column('source', sa.String(20), nullable=False, server_default='upload'),
        sa.Column('status', sa.String(20), nullable=False, server_default='running'),
        sa.Column('total_files', sa.Integer, nullable=False, server_default='0'),
        sa.Column('initiated_by', sa.String(120), nullable=True),
        sa.Column('created_at', sa.DateTime, server_default=now_default(), nullable=True),
        sa.Column('started_at', sa.DateTime, nullable=True),
        sa.Column('finished_at', sa.DateTime, nullable=True),
    )
    op.create_index('ix_processing_runs_created_at', 'processing_runs', ['created_at'])

    op.create_table(
        'processing_jobs',
        sa.Column('id', get_integer_column_type(), primary_key=True, autoincrement=True),
        sa.Column('run_id', get_integer_column_type(), nullable=True),
        sa.Column('file_name', sa.String(255), nullable=False),
        # Deliberately NOT unique: the same file may be processed in several
        # runs, and each must be its own row.
        sa.Column('original_filename', sa.String(255), nullable=True),
        sa.Column('stored_path', sa.String(512), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='unknown'),
        sa.Column('current_row', get_integer_column_type(), nullable=False, server_default='0'),
        sa.Column('total_rows', get_integer_column_type(), nullable=False, server_default='0'),
        sa.Column('message', sa.String(500), nullable=True),
        sa.Column('error', sa.String(2000), nullable=True),
        sa.Column('attempts', sa.Integer, nullable=False, server_default='0'),
        sa.Column('worker', sa.String(120), nullable=True),
        sa.Column('started_at', sa.DateTime, nullable=True),
        sa.Column('updated_at', sa.DateTime, nullable=False, server_default=now_default()),
        sa.Column('finished_at', sa.DateTime, nullable=True),
        sa.ForeignKeyConstraint(['run_id'], ['processing_runs.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_processing_jobs_run_id', 'processing_jobs', ['run_id'])
    op.create_index('ix_processing_jobs_status', 'processing_jobs', ['status'])
    op.create_index('ix_processing_jobs_updated_at', 'processing_jobs', ['updated_at'])

    # ── Backfill ──────────────────────────────────────────────────
    # The old table holds at most one row per filename and no run grouping, so
    # everything is folded into a single synthetic "legacy" run. Without this,
    # upgrading would silently discard every job record already in the table.
    legacy_rows = op.get_bind().execute(
        sa.text('SELECT file_name, status, current_row, total_rows, message, updated_at '
                'FROM job_progress')
    ).fetchall()

    if legacy_rows:
        run_id = op.get_bind().execute(
            # CURRENT_TIMESTAMP, not now(): `now()` is Postgres-only and this
            # project also runs migrations on SQLite.
            sa.text("INSERT INTO processing_runs "
                    "(label, source, status, total_files, created_at) "
                    "VALUES (:label, 'legacy', 'running', :total, CURRENT_TIMESTAMP) "
                    "RETURNING id"),
            {'label': 'Migrated from job_progress', 'total': len(legacy_rows)},
        ).scalar()

        op.get_bind().execute(
            sa.text(
                'INSERT INTO processing_jobs '
                '(run_id, file_name, original_filename, status, current_row, '
                ' total_rows, message, attempts, started_at, updated_at) '
                'VALUES (:run_id, :file_name, :file_name, :status, :current_row, '
                '        :total_rows, :message, 1, :started_at, :updated_at)'
            ),
            [
                {
                    'run_id': run_id,
                    'file_name': r.file_name,
                    'status': r.status,
                    'current_row': r.current_row,
                    'total_rows': r.total_rows,
                    'message': r.message,
                    # updated_at is the only timestamp the old table had; use it
                    # for both so the row is not dated to the epoch.
                    'started_at': r.updated_at,
                    'updated_at': r.updated_at,
                }
                for r in legacy_rows
            ],
        )

    op.drop_table('job_progress')


def downgrade() -> None:
    op.create_table(
        'job_progress',
        sa.Column('id', get_integer_column_type(), primary_key=True, autoincrement=True),
        sa.Column('file_name', sa.String(255), nullable=False, unique=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='unknown'),
        sa.Column('current_row', get_integer_column_type(), nullable=False, server_default='0'),
        sa.Column('total_rows', get_integer_column_type(), nullable=False, server_default='0'),
        sa.Column('message', sa.String(500), nullable=True),
        sa.Column('updated_at', sa.DateTime, server_default=now_default()),
    )

    # Carry the most recent row per filename back, since the old schema could
    # only hold one row per file.
    if is_specified_db('postgresql'):
        op.get_bind().execute(sa.text(
            "INSERT INTO job_progress (file_name, status, current_row, total_rows, message, updated_at) "
            "SELECT DISTINCT ON (file_name) file_name, status, current_row, total_rows, message, updated_at "
            "FROM processing_jobs ORDER BY file_name, updated_at DESC"
        ))
    else:
        for row in op.get_bind().execute(sa.text(
            "SELECT file_name, status, current_row, total_rows, message, updated_at "
            "FROM processing_jobs ORDER BY updated_at DESC"
        )).fetchall():
            exists = op.get_bind().execute(
                sa.text('SELECT 1 FROM job_progress WHERE file_name = :f'),
                {'f': row.file_name},
            ).first()
            if not exists:
                op.get_bind().execute(sa.text(
                    "INSERT INTO job_progress (file_name, status, current_row, total_rows, message, updated_at) "
                    "VALUES (:f, :s, :c, :t, :m, :u)"),
                    {'f': row.file_name, 's': row.status, 'c': row.current_row,
                     't': row.total_rows, 'm': row.message, 'u': row.updated_at},
                )

    op.drop_index('ix_processing_jobs_updated_at', table_name='processing_jobs')
    op.drop_index('ix_processing_jobs_status', table_name='processing_jobs')
    op.drop_index('ix_processing_jobs_run_id', table_name='processing_jobs')
    op.drop_table('processing_jobs')
    op.drop_index('ix_processing_runs_created_at', table_name='processing_runs')
    op.drop_table('processing_runs')
