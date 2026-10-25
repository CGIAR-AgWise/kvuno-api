"""
Gunicorn configuration.

Loaded via `gunicorn -c app/gunicorn_config.py` — gunicorn only auto-loads
gunicorn.conf.py from the working directory, so without -c this file is inert.

Every value comes from app.config (BIND_IP, BIND_PORT, WORKERS, THREADS, …),
where the names, defaults and descriptions live. An invalid value stops
gunicorn here, with every problem listed.

worker_class defaults to gthread rather than sync: /ui/jobs/events is an
indefinite SSE stream, and a sync worker handles one request at a time, so
open Jobs pages would exhaust the pool and stall the whole API.
"""
from app.config import get_settings

_s = get_settings()

# ── Socket ──────────────────────────────────────────────────────
bind = f"{_s.BIND_IP}:{_s.BIND_PORT}"
backlog = _s.BACKLOG

# ── Workers ─────────────────────────────────────────────────────
# gthread so one process can hold many concurrent connections. Sized for
# SSE, not CPU: raise WORKERS for CPU-bound work, THREADS for waiting I/O.
worker_class = _s.WORKER_CLASS
workers = _s.WORKERS
threads = _s.THREADS
worker_connections = _s.WORKER_CONNECTIONS

# Container /tmp is often tiny; gunicorn writes a heartbeat file here.
worker_tmp_dir = _s.WORKER_TMP_DIR

# Preloading is deliberately left off. With SQLAlchemy it shares one engine
# across forked workers, which is a well-known source of cross-process
# connection problems. Set PRELOAD_APP=true only if you know that is safe.
preload_app = _s.PRELOAD_APP

# ── Timeouts ────────────────────────────────────────────────────
# The gunicorn default is 30s; large API uploads convert RDS inline, so the
# default is too tight.
timeout = _s.TIMEOUT
graceful_timeout = _s.GRACEFUL_TIMEOUT
keepalive = _s.KEEPALIVE

# ── Recycling ───────────────────────────────────────────────────
# Bounded worker lifetime guards against slow memory growth in long-lived
# processes; the jitter stops every worker recycling at the same moment.
max_requests = _s.MAX_REQUESTS
max_requests_jitter = _s.MAX_REQUESTS_JITTER

# ── Logging ─────────────────────────────────────────────────────
# Default to stdout/stderr. No log platform collects files from inside a
# container, so writing to logs/*.log makes them invisible to Docker and
# Dokploy. Point these at a path only if something really does tail it.
loglevel = _s.LOG_LEVEL.lower()
accesslog = _s.ACCESSLOG
errorlog = _s.ERRORLOG
capture_output = True

# Do not advertise the server software in response headers.
gunicorn_version = None
