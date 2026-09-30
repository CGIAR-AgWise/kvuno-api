from typing import Any, Optional
import datetime

from geoalchemy2.types import Geometry
from sqlalchemy import BigInteger, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, PrimaryKeyConstraint, REAL, String, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass


class ProcessingRun(Base):
    """One ingestion batch: everything submitted together.

    Progress used to be tracked per filename with no grouping, so a five-file
    upload reported five unrelated jobs and there was no way to ask how the
    batch as a whole was doing. A run is that missing grouping.
    """
    __tablename__ = 'processing_runs'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='processing_runs_pkey'),
        Index('ix_processing_runs_created_at', 'created_at'),
    )

    # with_variant: SQLite only auto-increments an INTEGER PRIMARY KEY, so a
    # plain BigInteger PK fails there. Postgres still gets BigInteger.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, 'sqlite'),
        primary_key=True, autoincrement=True)
    label: Mapped[Optional[str]] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(20), nullable=False, default='upload')
    status: Mapped[str] = mapped_column(String(20), nullable=False, default='running')
    total_files: Mapped[int] = mapped_column(Integer, default=0)
    initiated_by: Mapped[Optional[str]] = mapped_column(String(120))
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, server_default=text('now()'))
    started_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime)
    finished_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime)

    jobs: Mapped[list['ProcessingJob']] = relationship(
        back_populates='run', cascade='all, delete-orphan', lazy='selectin'
    )


class ProcessingJob(Base):
    """One file's progress within a run.

    Supersedes the old ``job_progress`` table, which had a UNIQUE constraint on
    ``file_name`` and therefore exactly one row per filename *ever* — re-running
    a file overwrote its history, and there was no attempt count or error text.
    """
    __tablename__ = 'processing_jobs'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='processing_jobs_pkey'),
        Index('ix_processing_jobs_run_id', 'run_id'),
        Index('ix_processing_jobs_status', 'status'),
        Index('ix_processing_jobs_updated_at', 'updated_at'),
    )

    # with_variant: SQLite only auto-increments an INTEGER PRIMARY KEY.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, 'sqlite'),
        primary_key=True, autoincrement=True)
    # No index=True here: ix_processing_jobs_run_id is declared explicitly in
    # __table_args__ below, and having both creates the index twice.
    run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey('processing_runs.id', ondelete='CASCADE'))
    # Basename of the stored file. NOT unique: the same file may legitimately be
    # processed in several runs, and must not collapse them into one row.
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[Optional[str]] = mapped_column(String(255))
    # The real path on disk. Differs from file_name after RDS->Parquet
    # conversion, which happens at upload time.
    stored_path: Mapped[Optional[str]] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default='unknown')
    current_row: Mapped[int] = mapped_column(BigInteger, default=0)
    total_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    message: Mapped[Optional[str]] = mapped_column(String(500))
    # Last failure detail, kept apart from `message` so progress updates cannot
    # erase the reason a job failed.
    error: Mapped[Optional[str]] = mapped_column(String(2000))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    worker: Mapped[Optional[str]] = mapped_column(String(120))
    # Both are written explicitly on every update. The previous table relied on
    # `server_default=now()`, which applies on INSERT only — so updated_at kept
    # its creation value forever, which broke mtime ordering and made healthy
    # jobs look stalled to mark_stale().
    started_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime)
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime, nullable=False, server_default=text('now()'))
    finished_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime)

    run: Mapped[Optional['ProcessingRun']] = relationship(back_populates='jobs')


class FileImport(Base):
    __tablename__ = 'file_imports'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='file_imports_pkey'),
        UniqueConstraint('check_sum', name='file_imports_check_sum_key')
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    check_sum: Mapped[str] = mapped_column(String(100), nullable=False)
    file_name: Mapped[str] = mapped_column(String(120), nullable=False)
    processed_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, server_default=text('now()'))
    offset: Mapped[Optional[int]] = mapped_column(BigInteger)
    original_filename: Mapped[Optional[str]] = mapped_column(String(255))


class ImportConflict(Base):
    __tablename__ = 'import_conflicts'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='import_conflicts_pkey'),
        Index('idx_conflict_created_at', 'created_at')
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    record_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    country: Mapped[Optional[str]] = mapped_column(String(20))
    province: Mapped[Optional[str]] = mapped_column(String(20))
    lon: Mapped[Optional[float]] = mapped_column(REAL)
    lat: Mapped[Optional[float]] = mapped_column(REAL)
    variety: Mapped[Optional[str]] = mapped_column(String(20))
    season_type: Mapped[Optional[str]] = mapped_column(String(20))
    opt_date: Mapped[Optional[str]] = mapped_column(String(8))
    check_sum: Mapped[Optional[str]] = mapped_column(String(100))
    source: Mapped[Optional[str]] = mapped_column(String(50))
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, server_default=text('now()'))


class PlantingRecommendation(Base):
    __tablename__ = 'planting_recommendations'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='planting_recommendations_pkey'),
        UniqueConstraint('country', 'province', 'lon', 'lat', 'variety', 'season_type', 'opt_date', name='uq_planting_recommendation'),
        Index('idx_check_sum', 'check_sum'),
        Index('idx_coordinates', 'coordinates', postgresql_using='gist'),
        Index('idx_country', 'country'),
        Index('idx_lat', 'lat'),
        Index('idx_lon', 'lon'),
        Index('idx_opt_date', 'opt_date'),
        Index('idx_planting_option', 'planting_option'),
        Index('idx_planting_recommendations_coordinates', 'coordinates', postgresql_using='gist'),
        Index('idx_province', 'province'),
        Index('idx_season_type', 'season_type'),
        Index('idx_variety', 'variety')
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    check_sum: Mapped[str] = mapped_column(String(100), nullable=False)
    coordinates: Mapped[Any] = mapped_column(Geometry('POINT', 4326, 2, from_text='ST_GeomFromEWKT', name='geometry', nullable=False), nullable=False)
    country: Mapped[Optional[str]] = mapped_column(String(20))
    province: Mapped[Optional[str]] = mapped_column(String(20))
    lon: Mapped[Optional[float]] = mapped_column(REAL)
    lat: Mapped[Optional[float]] = mapped_column(REAL)
    variety: Mapped[Optional[str]] = mapped_column(String(20))
    season_type: Mapped[Optional[str]] = mapped_column(String(20))
    opt_date: Mapped[Optional[str]] = mapped_column(String(8))
    planting_option: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, server_default=text('now()'))
    updated_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, server_default=text('now()'))


class User(Base):
    __tablename__ = 'users'
    __table_args__ = (
        PrimaryKeyConstraint('id', name='users_pkey'),
        UniqueConstraint('email', name='users_email_key'),
        UniqueConstraint('username', name='users_username_key')
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, server_default=text('now()'))

    user_tokens: Mapped[list['UserToken']] = relationship('UserToken', back_populates='user')


class UserToken(Base):
    __tablename__ = 'user_tokens'
    __table_args__ = (
        ForeignKeyConstraint(['user_id'], ['users.id'], name='user_tokens_user_id_fkey'),
        PrimaryKeyConstraint('id', name='user_tokens_pkey'),
        Index('idx_user_tokens_token', 'token')
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, server_default=text('now()'))
    expires_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime)
    last_used_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime)

    user: Mapped['User'] = relationship('User', back_populates='user_tokens')
