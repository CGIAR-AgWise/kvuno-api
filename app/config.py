"""
All configuration, in one place — the same approach as the budget-assist service.

Every environment variable the application reads is a field of `Settings`
below: its name, type, default and description live here and nowhere else.
`.env.example` and the README configuration table are GENERATED from this
class (`python scripts/gen_config_docs.py`, `make config-docs`), and CI fails
when they drift (`--check`). Read configuration with `get_settings().NAME`;
never call `os.getenv` for an application setting.

Why: ~70 variables used to be read with `os.getenv` in 15+ modules, each
restating its default, and again in .env.example, two compose files and the
README — the source of drift (a default changed in one place) and of silent
errors (a typo'd name or a non-numeric value fell back to the default, or
crashed later with a traceback).

Behaviour:
- Values are validated together; every problem is reported at once, e.g.
  "SMTP_PORT: Input should be a valid integer". Empty values (compose's
  `${X:-}`) count as unset, so the default applies.
- `get_settings()` re-reads only when the environment changed (tests change
  it with monkeypatch.setenv); otherwise it returns the same object.
- Importing this module validates nothing. The preflight, `create_app()` and
  the Celery worker call `get_settings()` first, so an invalid value stops
  them before anything else happens (the preflight without retrying, and
  warning about unknown look-alike variables such as LOG_LEVLE). Logging
  falls back to defaults so the error itself is still logged.
- A missing database configuration is reported by `build_db_url()` when the
  database is first needed, so the preflight can still start and say what is
  wrong.
- `.env` is loaded once, here; real environment variables win over it.

There are deliberately no module-level copies of settings (the old
`MAX_FILE_SIZE`, `HOUSEKEEPING_ENABLED`, …): a value captured at import goes
stale, and tests had to patch each copy separately.
"""
from __future__ import annotations

import os
from typing import Literal, Optional

from dotenv import load_dotenv
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()

APP_NAME = "KVuno API"
API_PREFIX = "/api"
API_VERSION = "/v1"
ALLOWED_EXTENSIONS = {'.rds', '.parquet'}
_AGE_UNITS = {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800}

# Fields of these groups are documented together in the generated files.
# The key is the json_schema_extra "group" on each field.
GROUPS = {
    'app': 'Application',
    'logging': 'Logging',
    'database': 'Database',
    'server': 'Web server (development) and HTTP',
    'gunicorn': 'Gunicorn (production web server)',
    'auth': 'Accounts and authentication',
    'mail': 'Email (optional)',
    'uploads': 'Uploads and data files',
    'ingest': 'Ingestion (housekeeping)',
    'celery': 'Celery / Redis',
    'progress': 'Job progress',
    'remote': 'Remote RDS downloads',
    'rate_limit': 'Rate limiting',
    'preflight': 'Startup preflight',
    'tooling': 'Developer tooling',
}


def _f(default, group: str, description: str, *, example=None, secret: bool = False, active: bool = False):
    """A documented setting.

    In the generated .env.example a setting is a commented-out line showing
    `example` (else the default), or a live line when `active` — the few a
    local setup actually has to fill in.
    """
    extra = {'group': group}
    if example is not None:
        extra['example'] = example
    if secret:
        extra['secret'] = True
    if active:
        extra['active'] = True
    return Field(default, description=description, json_schema_extra=extra)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_ignore_empty=True,     # `${X:-}` from compose means "use the default"
        extra='ignore',
        validate_default=True,
    )

    # ── Application ────────────────────────────────────────────
    ENVIRONMENT: str = _f('development', 'app',
                          "production/prod → JSON log lines (for Loki etc.); anything else → readable console logs.",
                          active=True)
    APP_VERSION: Optional[str] = _f(None, 'app',
                                    "Version reported by /health and the API docs; set by the image build.")
    GITHUB_REF_NAME: Optional[str] = _f(None, 'app', "Fallback for APP_VERSION when built in GitHub Actions.")
    SPA_ROOT_URL: str = _f('', 'app',
                           "The web app's URL: target of `GET /` for API clients, linked from the landing page, and "
                           "the base of links in emails (only when an absolute http(s) URL).",
                           example='https://kvuno-ui.example.org')
    SPA_LOGIN_PATH: str = _f('/login', 'app',
                             "Where `require_auth` sends an unauthenticated browser (a client-side SPA route).")

    # ── Logging ────────────────────────────────────────────────
    LOG_LEVEL: Literal['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] = _f(
        'INFO', 'logging', "The one log level for every process (API, worker, gunicorn, migrations).",
        active=True)
    LOG_DESTINATION: Literal['stdout', 'stderr', 'file'] = _f(
        'stdout', 'logging', "Where logs go; keep stdout in containers (what docker logs / Dokploy collect).")
    LOG_ROTATION: Literal['size', 'daily'] = _f('size', 'logging', "Rotation for LOG_DESTINATION=file.")
    LOG_FILE_PATH: Optional[str] = _f(None, 'logging', "Log file for LOG_DESTINATION=file.", example='logs/kvuno.log')
    LOG_MAX_BYTES: int = _f(10 * 1024 * 1024, 'logging', "Rotate the log file past this size (LOG_ROTATION=size).")
    LOG_BACKUP_COUNT: int = _f(5, 'logging', "Rotated log files to keep.")
    DEBUG_DB: bool = _f(False, 'logging', "Log every SQL statement, whatever LOG_LEVEL says (very verbose).")

    # ── Database ───────────────────────────────────────────────
    DB_URL: Optional[str] = _f(None, 'database', "Full SQLAlchemy URL; wins over the DB_* parts below.")
    DB_DRIVER: str = _f('postgresql', 'database',
                        "postgresql, or sqlite for UI/auth work without Postgres (ingestion and spatial "
                        "queries need Postgres).", active=True)
    DB_HOST: str = _f('127.0.0.1', 'database', "Database host (from inside a container: not localhost).",
                      active=True)
    DB_PORT: int = _f(5432, 'database', "Database port.", active=True)
    DB_USER: Optional[str] = _f(None, 'database', "Database user. Required unless DB_URL is set or DB_DRIVER=sqlite.",
                                example='postgres', active=True)
    DB_PASSWORD: Optional[SecretStr] = _f(None, 'database',
                                          "Database password. Required unless DB_URL is set or DB_DRIVER=sqlite.",
                                          example='postgres', secret=True, active=True)
    DB_NAME: Optional[str] = _f(None, 'database',
                                "Database name. Required unless DB_URL is set; with DB_DRIVER=sqlite the file "
                                "name (default kvuno.db).", example='agwise_api', active=True)
    RUN_MIGRATIONS: bool = _f(True, 'database',
                              "Container entrypoint applies migrations before start (compose sets false).")

    # ── Web server and HTTP ────────────────────────────────────
    FLASK_DEBUG: bool = _f(False, 'server',
                           "Debug mode for `python run.py`; it then starts even when the preflight fails.",
                           example='true', active=True)
    SERVER_HOST: str = _f('0.0.0.0', 'server', "Bind address for `python run.py`.")
    SERVER_PORT: int = _f(80, 'server', "Port for `python run.py`.")
    SERVER_URL_PROD: str = _f('https://kvuno.agwise.org', 'server', "Server listed in the OpenAPI document.")
    SERVER_URL_PROD_2: str = _f('https://kvuno.akilimo.org', 'server', "Second server listed in the OpenAPI document.")
    CORS_ORIGINS: str = _f('http://127.0.0.1:5000,http://localhost:5173,http://127.0.0.1:5173', 'server',
                           "Comma-separated origins allowed cross-origin (only `pnpm dev`; production is same-origin).")
    PROXY_FIX_HOPS: int = _f(1, 'server',
                             "Trusted proxies in front of the app, for real client IPs. 0 when nothing proxies it, "
                             "or X-Forwarded-For can be forged to evade rate limits.")
    SORT_JSON: bool = _f(False, 'server', "Sort keys in JSON responses.")
    COORDINATES_BULK_MAX_ROWS: int = _f(5000, 'server', "Map points returned by the bulk coordinates endpoint (a random sample beyond this).")

    # ── Gunicorn ───────────────────────────────────────────────
    BIND_IP: str = _f('0.0.0.0', 'gunicorn', "Gunicorn bind address.")
    BIND_PORT: int = _f(80, 'gunicorn', "Gunicorn port.")
    BACKLOG: int = _f(2048, 'gunicorn', "Gunicorn listen backlog.")
    WORKER_CLASS: str = _f('gthread', 'gunicorn', "Gunicorn worker class (gthread: the SSE stream needs threads).")
    WORKERS: int = _f(2, 'gunicorn', "Gunicorn worker processes.")
    THREADS: int = _f(4, 'gunicorn', "Threads per gunicorn worker.")
    WORKER_CONNECTIONS: int = _f(1000, 'gunicorn', "Max simultaneous clients per worker.")
    WORKER_TMP_DIR: str = _f('/dev/shm', 'gunicorn', "Gunicorn heartbeat directory.")
    PRELOAD_APP: bool = _f(False, 'gunicorn', "Load the app before forking (unsafe with SQLAlchemy; leave off).")
    TIMEOUT: int = _f(120, 'gunicorn',
                      "Seconds before a silent worker is killed (raised from 30 for inline API upload conversion).")
    GRACEFUL_TIMEOUT: int = _f(30, 'gunicorn', "Seconds to finish requests on restart.")
    KEEPALIVE: int = _f(5, 'gunicorn', "Keep-alive seconds.")
    MAX_REQUESTS: int = _f(1000, 'gunicorn', "Recycle a worker after this many requests.")
    MAX_REQUESTS_JITTER: int = _f(100, 'gunicorn', "Random spread so workers do not recycle together.")
    ACCESSLOG: str = _f('-', 'gunicorn', "Access log target ('-' = stdout).")
    ERRORLOG: str = _f('-', 'gunicorn', "Error log target ('-' = stderr).")

    # ── Accounts ───────────────────────────────────────────────
    ADMIN_EMAILS: str = _f('', 'auth',
                           "Comma-separated owner addresses (case-insensitive): administrators who cannot be demoted, "
                           "approved at once on registration, and sent new-registration notices. Unset: nobody can "
                           "approve new accounts unless one already holds the admin role.",
                           example='admin@example.org')
    TOKEN_TTL_DAYS: int = _f(0, 'auth', "Days a sign-in token lasts; 0 = no expiry.")

    # ── Email ──────────────────────────────────────────────────
    SMTP_HOST: str = _f('', 'mail', "SMTP server; unset → no email is sent, and the log says so.",
                        example='smtp.example.org')
    SMTP_PORT: int = _f(587, 'mail', "SMTP port.")
    SMTP_USERNAME: str = _f('', 'mail', "SMTP login, skipped when empty (Mailgun: postmaster@<domain>).")
    SMTP_PASSWORD: SecretStr = _f(SecretStr(''), 'mail', "SMTP password.", secret=True)
    SMTP_SECURITY: Literal['starttls', 'ssl', 'none'] = _f('starttls', 'mail', "starttls (587), ssl (465) or none (local relay).")
    SMTP_TIMEOUT_SECONDS: int = _f(15, 'mail', "SMTP connect/send timeout.")
    MAIL_FROM: str = _f('', 'mail', "Sender address; unset = no email is sent.", example='noreply@example.org')
    MAIL_APP_NAME: str = _f('KVuno', 'mail', "Name used in the From: header and message text.")

    # ── Uploads ────────────────────────────────────────────────
    MAX_FILE_SIZE_MB: int = _f(20, 'uploads',
                               "Largest upload accepted, in MB; enforced at tus creation, before any bytes arrive.")
    UPLOAD_ABANDON_AGE: str = _f('7d', 'uploads', "Remove uploads idle this long (30m, 12h, 1d, 2w).")
    UPLOAD_INSPECT_STALE_SECONDS: int = _f(180, 'uploads',
                                           "An upload `inspecting` this long is re-claimed on the next status poll.")
    CLEANUP_AGE: str = _f('1d', 'uploads', "Remove files of jobs completed this long ago (30m, 12h, 1d, 2w).")

    # ── Ingestion ──────────────────────────────────────────────
    HOUSEKEEPING_ENABLED: bool = _f(False, 'ingest',
                                    "Hand /api/v1/data/upload and Jobs retries of plain data-folder files to the "
                                    "Celery worker (needs Redis). SPA uploads and the startup scan run in the API "
                                    "process either way.")
    HOUSEKEEPING_MODE: Optional[Literal['oneshot', 'watch', 'dryrun']] = _f(
        None, 'ingest',
        "Worker image only: run housekeeping.py instead of Celery — oneshot (scan once and exit), watch "
        "(keep importing new files) or dryrun (report only). Unset: the normal Celery worker.")
    HOUSEKEEPING_DATA_DIR: str = _f('static/data', 'ingest',
                                    "Directory holding data files; uploads go to its `uploads/` subdirectory.")
    HOUSEKEEPING_BATCH_SIZE: int = _f(2000, 'ingest', "Rows per insert batch.")
    HOUSEKEEPING_CHUNK_SIZE: int = _f(5000, 'ingest', "Rows read per chunk (sizes insert batches, not memory).")
    HOUSEKEEPING_CHECKPOINT_INTERVAL: int = _f(50, 'ingest', "Batches between offset checkpoints.")
    HOUSEKEEPING_MAX_WORKERS: int = _f(1, 'ingest', "Parallel files in a directory scan.")
    INGEST_LOCK_STALE_SECONDS: float = _f(600, 'ingest', "Take over a per-file ingest lock silent this long.")
    RDS_COLUMN_MAP: Optional[str] = _f(None, 'ingest', "JSON object overriding the default source→DB column map.",
                                       example='{"Variety": "variety"}')

    # ── Celery / Redis ─────────────────────────────────────────
    CELERY_BROKER_URL: str = _f('redis://localhost:6379/0', 'celery', "Redis URL for the Celery broker (also SSE and the cache).")
    CELERY_RESULT_BACKEND: str = _f('redis://localhost:6379/0', 'celery', "Celery result backend URL.")
    CELERY_TASK_DEFAULT_QUEUE: str = _f('kvuno', 'celery', "Celery queue name.")
    CELERY_TASK_MAX_RETRIES: int = _f(10, 'celery', "Retries for a task failing on a transient error.")
    CELERY_TASK_RETRY_DELAY: int = _f(60, 'celery', "Seconds between task retries.")
    CELERY_VISIBILITY_TIMEOUT: int = _f(21600, 'celery',
                                        "Seconds before Redis redelivers an unacked task, even a running one: must "
                                        "exceed the longest single-file ingest.")
    CELERY_CONCURRENCY: Optional[int] = _f(None, 'celery', "Worker processes for `python dev_worker.py`.")
    WORKER_MAX_MEMORY_PER_CHILD: int = _f(2048, 'celery', "MB; recycle a worker child past this (set above one file's peak).")
    WORKER_MAX_TASKS_PER_CHILD: int = _f(50, 'celery', "Recycle a worker child after this many tasks.")

    # ── Job progress ───────────────────────────────────────────
    JOB_STALE_AFTER_SECONDS: int = _f(1800, 'progress', "A processing job silent this long is shown as stale.")
    JOB_PROGRESS_TTL_SECONDS: int = _f(7 * 24 * 3600, 'progress', "Redis expiry of a job's progress entry.")
    JOB_PROGRESS_MIN_INTERVAL: float = _f(1.0, 'progress', "Minimum seconds between published progress updates.")
    JOB_DB_CHECKPOINT_SECONDS: float = _f(5.0, 'progress', "Minimum seconds between durable progress writes.")

    # ── Remote RDS downloads ───────────────────────────────────
    REMOTE_RDS_URLS: str = _f('', 'remote', "Semicolon-separated HTTPS URLs fetched before a scan.",
                              example='https://example.com/data/sample.RDS')
    REMOTE_RDS_TOKEN: SecretStr = _f(SecretStr(''), 'remote', "Bearer token for the downloads.", secret=True)
    REMOTE_RDS_COOKIES: SecretStr = _f(SecretStr(''), 'remote', "Cookies, comma-separated `name=value` pairs.",
                                       secret=True)
    REMOTE_RDS_HEADERS: SecretStr = _f(SecretStr(''), 'remote', "Extra headers, comma-separated `Name: value` pairs.",
                                       secret=True)
    REMOTE_RDS_ALLOWED_DOMAINS: str = _f('', 'remote', "Comma-separated hosts downloads may come from (exact or subdomain).")
    REMOTE_RDS_ALLOW_HTTP: bool = _f(False, 'remote', "Allow plain-HTTP download URLs.")
    REMOTE_RDS_MAX_BYTES: int = _f(1024 ** 3, 'remote', "Largest download accepted, in bytes (checked while streaming).")

    # ── Rate limiting ──────────────────────────────────────────
    RATE_LIMIT_REGISTER: str = _f('10 per hour', 'rate_limit', "Registration limit (flask-limiter syntax).")
    RATE_LIMIT_LOGIN: str = _f('20 per hour', 'rate_limit', "Sign-in limit.")
    RATE_LIMIT_UPLOAD: str = _f('10 per hour', 'rate_limit', "API upload limit.")
    RATE_LIMIT_DATA: str = _f('120 per minute', 'rate_limit', "Planting-data endpoints limit.")
    RATE_LIMIT_COORDINATES_BULK: str = _f('20 per minute', 'rate_limit', "Bulk coordinates endpoint limit.")
    RATE_LIMIT_DEFAULT_HOURLY: int = _f(600, 'rate_limit',
                                        "Requests per hour (a bare count) for any route without its own limit. If "
                                        "real users see 429s, raise this rather than the per-endpoint limits.")
    RATE_LIMIT_DEFAULT_DAILY: int = _f(5000, 'rate_limit', "Requests per day (a bare count), as above.")
    RATE_LIMIT_STORAGE: str = _f('memory://', 'rate_limit', "Counter storage; use Redis with several gunicorn workers.")

    # ── Startup preflight ──────────────────────────────────────
    PREFLIGHT_ENABLED: bool = _f(True, 'preflight',
                                 "Check the database (and Redis when required) before starting; false skips it.")
    PREFLIGHT_TIMEOUT_SECONDS: float = _f(3, 'preflight', "Timeout per check.")
    PREFLIGHT_RETRIES: int = _f(30, 'preflight', "Attempts before giving up.")
    PREFLIGHT_DELAY_SECONDS: float = _f(2, 'preflight', "Seconds between attempts (30 x 2 s = 60 s tolerance).")
    PREFLIGHT_ROLE: Literal['api', 'worker'] = _f('api', 'preflight', "Role when the preflight is run without an argument.")

    # ── Developer tooling ──────────────────────────────────────
    OUTFILE_PATH: str = _f('app/models/kvuno.py', 'tooling', "model-generator.py output file.")
    EXCLUDED_TABLES: str = _f('spatial_ref_sys,alembic_version', 'tooling', "model-generator.py tables to skip.")

    # ── Normalisation ──────────────────────────────────────────
    @field_validator('LOG_LEVEL', 'LOG_DESTINATION', 'LOG_ROTATION', 'SMTP_SECURITY', 'PREFLIGHT_ROLE', mode='before')
    @classmethod
    def _case_insensitive(cls, value):
        if not isinstance(value, str):
            return value
        value = value.strip()
        # LOG_LEVEL is upper case; the rest lower case.
        return value.upper() if value.lower() in ('debug', 'info', 'warning', 'error', 'critical') else value.lower()

    @field_validator('ENVIRONMENT', 'DB_DRIVER', mode='before')
    @classmethod
    def _lower(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator('RDS_COLUMN_MAP')
    @classmethod
    def _json_object(cls, value):
        if value is None:
            return value
        import json
        try:
            parsed = json.loads(value)
        except ValueError as e:
            raise ValueError(f'must be a JSON object: {e}') from None
        if not isinstance(parsed, dict):
            raise ValueError('must be a JSON object')
        return value

    @field_validator('UPLOAD_ABANDON_AGE', 'CLEANUP_AGE')
    @classmethod
    def _age(cls, value: str):
        value = value.strip().lower()
        if len(value) < 2 or value[-1] not in _AGE_UNITS or not value[:-1].isdigit():
            raise ValueError("must be a number followed by m, h, d or w (e.g. 30m, 12h, 1d, 2w)")
        return value

    # ── Derived values ─────────────────────────────────────────
    @staticmethod
    def age_seconds(age: str) -> int:
        """Seconds in a validated age such as '12h'."""
        return int(age[:-1]) * _AGE_UNITS[age[-1]]

    @property
    def app_version(self) -> str:
        return self.APP_VERSION or self.GITHUB_REF_NAME or '0.0.0-dev'

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT in ('production', 'prod')

    @property
    def max_file_size(self) -> int:
        return self.MAX_FILE_SIZE_MB * 1024 * 1024

    @property
    def admin_emails(self) -> frozenset[str]:
        return frozenset(e.strip().lower() for e in self.ADMIN_EMAILS.split(',') if e.strip())

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(',') if o.strip()]

    def database_url(self) -> str:
        """The SQLAlchemy URL, or RuntimeError naming what is missing."""
        if self.DB_URL:
            return self.DB_URL
        if self.DB_DRIVER == 'sqlite':
            return f"sqlite:///{self.DB_NAME or 'kvuno.db'}"
        password = self.DB_PASSWORD.get_secret_value() if self.DB_PASSWORD else ''
        missing = [k for k, v in (('DB_USER', self.DB_USER), ('DB_PASSWORD', password),
                                  ('DB_NAME', self.DB_NAME)) if not v]
        if missing:
            raise RuntimeError(
                f"Missing required DB environment variables: {', '.join(missing)}. "
                "Set them individually or use DB_URL for full control."
            )
        return f"{self.DB_DRIVER}://{self.DB_USER}:{password}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"


class ConfigurationError(RuntimeError):
    """Raised with every invalid setting listed, one per line."""


SETTING_NAMES = tuple(Settings.model_fields)
_cache: tuple[tuple, Settings] | None = None


def get_settings() -> Settings:
    """The validated settings. Rebuilt only when a setting's env var changed."""
    global _cache
    fingerprint = tuple(os.environ.get(name) for name in SETTING_NAMES)
    if _cache is not None and _cache[0] == fingerprint:
        return _cache[1]
    try:
        settings = Settings()
    except ValidationError as e:
        problems = '\n'.join(f"  - {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise ConfigurationError(f"Invalid configuration:\n{problems}") from None
    _cache = (fingerprint, settings)
    return settings


# Prefixes of variables this application owns. One that is set but is not a
# setting is almost certainly a typo (LOG_LEVLE), which used to be ignored.
_OWNED_PREFIXES = ('DB_', 'LOG_', 'SMTP_', 'MAIL_', 'CELERY_', 'HOUSEKEEPING_', 'RATE_LIMIT_', 'UPLOAD_',
                   'JOB_', 'PREFLIGHT_', 'REMOTE_RDS_', 'INGEST_', 'SPA_', 'ADMIN_', 'CORS_', 'PROXY_FIX_',
                   'TOKEN_', 'WORKER_MAX_')


def unknown_settings() -> list[str]:
    """Environment variables that look like settings but are not one."""
    known = set(SETTING_NAMES)
    return sorted(k for k in os.environ if k.startswith(_OWNED_PREFIXES) and k not in known)


# ── Helpers ────────────────────────────────────────────────────

def build_db_url() -> str:
    """The database URL (raises RuntimeError naming missing DB_* variables)."""
    return get_settings().database_url()


def admin_emails() -> frozenset[str]:
    """Owner emails (ADMIN_EMAILS): permanent administrators. See Settings.ADMIN_EMAILS."""
    return get_settings().admin_emails


def celery_broker_available() -> bool:
    """Check if the Redis/Celery broker host:port is reachable."""
    from urllib.parse import urlparse
    import socket
    parts = urlparse(get_settings().CELERY_BROKER_URL)
    host = parts.hostname or 'localhost'
    port = parts.port or 6379
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        return True
    except (OSError, ValueError):
        return False
