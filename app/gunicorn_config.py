"""
Gunicorn configuration.

Loaded via `gunicorn -c app/gunicorn_config.py` — gunicorn only auto-loads
gunicorn.conf.py from the working directory, so without -c this file is inert.

Every setting is environment-overridable, using UPPERCASE names.

worker_class defaults to gthread rather than sync: /ui/jobs/events is an
indefinite SSE stream, and a sync worker handles one request at a time, so
open Jobs pages would exhaust the pool and stall the whole API.
"""
import os


def _int(name, default):
    return int(os.getenv(name, default))


def _str(name, default):
    return os.getenv(name, default)


# ── Socket ──────────────────────────────────────────────────────
bind = f"{_str('BIND_IP', '0.0.0.0')}:{_str('BIND_PORT', '80')}"
backlog = _int('BACKLOG', 2048)

# ── Workers ─────────────────────────────────────────────────────
# gthread so one process can hold many concurrent connections. Sized for
# SSE, not CPU: raise WORKERS for CPU-bound work, THREADS for waiting I/O.
worker_class = _str('WORKER_CLASS', 'gthread')
workers = _int('WORKERS', 2)
threads = _int('THREADS', 4)
worker_connections = _int('WORKER_CONNECTIONS', 1000)

# Container /tmp is often tiny; gunicorn writes a heartbeat file here.
worker_tmp_dir = _str('WORKER_TMP_DIR', '/dev/shm')

# Preloading is deliberately left off. With SQLAlchemy it shares one engine
# across forked workers, which is a well-known source of cross-process
# connection problems. Set PRELOAD_APP=true only if you know that is safe.
preload_app = _str('PRELOAD_APP', 'false').lower() == 'true'

# ── Timeouts ────────────────────────────────────────────────────
# The gunicorn default is 30s. /ui/upload/complete calls pyreadr.read_r(),
# which deserialises a whole RDS file just to preview 5 rows, so the default
# is too tight for large uploads.
timeout = _int('TIMEOUT', 120)
graceful_timeout = _int('GRACEFUL_TIMEOUT', 30)
keepalive = _int('KEEPALIVE', 5)

# ── Recycling ───────────────────────────────────────────────────
# Bounded worker lifetime guards against slow memory growth in long-lived
# processes; the jitter stops every worker recycling at the same moment.
max_requests = _int('MAX_REQUESTS', 1000)
max_requests_jitter = _int('MAX_REQUESTS_JITTER', 100)

# ── Logging ─────────────────────────────────────────────────────
# Default to stdout/stderr. No log platform collects files from inside a
# container, so writing to logs/*.log makes them invisible to Docker and
# Dokploy. Point these at a path only if something really does tail it.
loglevel = _str('LOG_LEVEL', 'INFO').lower()
accesslog = _str('ACCESSLOG', '-')
errorlog = _str('ERRORLOG', '-')
capture_output = True

# Do not advertise the server software in response headers.
gunicorn_version = None
