import os


APP_NAME = "KVuno API"

# Sourced from the build, not hardcoded: the Dockerfiles pass the git tag (or
# branch name) as APP_VERSION, so /health and the OpenAPI Info block report what
# was actually deployed. `version-release.yml` derives the tag from git history,
# so keeping a number here would only ever drift from it.
APP_VERSION = os.getenv('APP_VERSION') or os.getenv('GITHUB_REF_NAME') or '0.0.0-dev'
API_PREFIX = "/api"
API_VERSION = "/v1"

# ── Database ───────────────────────────────────────────────────

def build_db_url() -> str:
    """Build a database URL from individual environment variables."""
    url = os.getenv("DB_URL")
    if url:
        return url

    driver = os.getenv("DB_DRIVER", "postgresql")
    if driver == "sqlite":
        name = os.getenv("DB_NAME", "kvuno.db")
        return f"sqlite:///{name}"

    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    name = os.getenv("DB_NAME")
    host = os.getenv("DB_HOST", "127.0.0.1")
    port = os.getenv("DB_PORT", "5432")

    missing = [k for k, v in [("DB_USER", user), ("DB_PASSWORD", password), ("DB_NAME", name)] if not v]
    if missing:
        raise RuntimeError(
            f"Missing required DB environment variables: {', '.join(missing)}. "
            "Set them individually or use DB_URL for full control."
        )

    return f"{driver}://{user}:{password}@{host}:{port}/{name}"


# --- File upload------
MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE_MB', '20')) * 1024 * 1024
ALLOWED_EXTENSIONS = {'.rds', '.parquet'}

# ── Celery / Redis ─────────────────────────────────────────────

CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
CELERY_TASK_DEFAULT_QUEUE = os.getenv('CELERY_TASK_DEFAULT_QUEUE', 'kvuno')
CELERY_TASK_MAX_RETRIES = int(os.getenv('CELERY_TASK_MAX_RETRIES', '10'))
CELERY_TASK_RETRY_DELAY = int(os.getenv('CELERY_TASK_RETRY_DELAY', '60'))


def celery_broker_available() -> bool:
    """Check if the Redis/Celery broker host:port is reachable."""
    from urllib.parse import urlparse
    import socket
    parts = urlparse(CELERY_BROKER_URL)
    host = parts.hostname or 'localhost'
    port = parts.port or 6379
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        return True
    except (OSError, ValueError):
        return False


# ── Rate limiting ─────────────────────────────────────────────
#
# Each value is a string compatible with flask-limiter's limit syntax:
#   "10 per hour", "120 per minute", "1000 per day", etc.
#
RATE_LIMIT_REGISTER = os.getenv('RATE_LIMIT_REGISTER', '10 per hour')
RATE_LIMIT_LOGIN = os.getenv('RATE_LIMIT_LOGIN', '20 per hour')
RATE_LIMIT_UPLOAD = os.getenv('RATE_LIMIT_UPLOAD', '10 per hour')
RATE_LIMIT_DATA = os.getenv('RATE_LIMIT_DATA', '120 per minute')
# Backstop applied to every route without its own limit (UI pages, health).
# Deliberately generous: raising this is the fix when legitimate users start
# seeing 429s, since it is the limit that HTML page loads consume.
RATE_LIMIT_DEFAULT_HOURLY = os.getenv('RATE_LIMIT_DEFAULT_HOURLY', '600')
RATE_LIMIT_DEFAULT_DAILY = os.getenv('RATE_LIMIT_DEFAULT_DAILY', '5000')
# Storage backend for rate limit counters.
# Supports any flask-limiter storage URI:
#   memory://          — in-process (default, resets on restart)
#   redis://localhost:6379/0
#   redis+sentinel://localhost:26379
RATE_LIMIT_STORAGE = os.getenv('RATE_LIMIT_STORAGE', 'memory://')

# ── Token / auth ───────────────────────────────────────────────
#
# Tokens are opaque Sanctum-style `{id}|{secret}` values: the secret is
# generated per token and only its SHA-256 hash is stored in `user_tokens`.
# There is no signing key and no JWT — revoking a row revokes the token.
TOKEN_TTL_DAYS = int(os.getenv('TOKEN_TTL_DAYS', '0'))
# Set to 0 for no expiry, or a positive number of days.


# ── Housekeeping / ingestion ───────────────────────────────────

HOUSEKEEPING_ENABLED = os.getenv('HOUSEKEEPING_ENABLED', 'false').lower() == 'true'
HOUSEKEEPING_DATA_DIR = os.getenv('HOUSEKEEPING_DATA_DIR', os.path.join('static', 'data'))
HOUSEKEEPING_BATCH_SIZE = int(os.getenv('HOUSEKEEPING_BATCH_SIZE', '2000'))
HOUSEKEEPING_CHUNK_SIZE = int(os.getenv('HOUSEKEEPING_CHUNK_SIZE', '5000'))
HOUSEKEEPING_CHECKPOINT_INTERVAL = int(os.getenv('HOUSEKEEPING_CHECKPOINT_INTERVAL', '50'))
HOUSEKEEPING_MAX_WORKERS = int(os.getenv('HOUSEKEEPING_MAX_WORKERS', '1'))
