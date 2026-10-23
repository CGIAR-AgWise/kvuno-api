"""
Centralized logging — the same approach as the budget-assist service.

structlog on top of the standard `logging` module, configured once by
`setup_logging()`:

- **Format** follows ENVIRONMENT: JSON in production (`production`/`prod`), for
  log aggregators such as Loki; a readable console format everywhere else.
- **Destination** is LOG_DESTINATION: `stdout` (default — what `docker logs`
  and Dokploy collect), `stderr`, or `file` (LOG_FILE_PATH, rotated by size or
  daily per LOG_ROTATION / LOG_MAX_BYTES / LOG_BACKUP_COUNT). In a container,
  keep stdout: a file sink is invisible to the runtime.
- **Everything goes through one handler.** The root stdlib logger is
  configured with structlog's ProcessorFormatter, so Flask, Werkzeug,
  SQLAlchemy and Celery records come out in the same format as ours, with the
  same timestamp/level/logger fields (`foreign_pre_chain`). Before this,
  loguru and stdlib logged separately and stdlib INFO lines were dropped.
- **Context** bound with `structlog.contextvars` — the request id, method and
  path for an HTTP request (app/__init__.py), task id and name for a Celery
  task (app/celery_app.py) — is merged into every line logged meanwhile.

Usage:  `logger = get_logger(__name__)`, then `logger.info("message", key=value)`.
Use `logger.exception(...)` inside `except` blocks so the traceback is kept.

Settings are read from the environment directly, not from app.config, so
logging can be set up before (and without) the database configuration that
app.config validates at import.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

import structlog

_configured = False


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _handler() -> logging.Handler:
    destination = _env('LOG_DESTINATION', 'stdout').lower()
    if destination == 'stderr':
        return logging.StreamHandler(sys.stderr)
    if destination == 'file':
        path = _env('LOG_FILE_PATH', '')
        if not path:
            # Same fallback as budget-assist: never lose logs to a bad setting.
            return logging.StreamHandler(sys.stderr)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        backups = int(_env('LOG_BACKUP_COUNT', '5'))
        if _env('LOG_ROTATION', 'size').lower() == 'daily':
            return TimedRotatingFileHandler(path, when='D', interval=1, backupCount=backups)
        return RotatingFileHandler(path, maxBytes=int(_env('LOG_MAX_BYTES', str(10 * 1024 * 1024))),
                                   backupCount=backups)
    return logging.StreamHandler(sys.stdout)


def is_production() -> bool:
    return _env('ENVIRONMENT', 'development').lower() in ('production', 'prod')


def setup_logging(force: bool = False) -> None:
    """Configure global logging. Safe to call more than once (and from Celery)."""
    global _configured
    if _configured and not force:
        return
    _configured = True

    level = _env('LOG_LEVEL', 'INFO').upper()
    numeric_level = getattr(logging, level, logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt='iso', utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer = (structlog.processors.JSONRenderer() if is_production()
                else structlog.dev.ConsoleRenderer())
    formatter = structlog.stdlib.ProcessorFormatter(
        # Records from plain stdlib loggers (Flask, SQLAlchemy, Celery…) get the
        # same fields as structlog's own before rendering.
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = _handler()
    handler.setFormatter(formatter)

    logging.basicConfig(level=numeric_level, handlers=[handler], force=True)
    # werkzeug's dev-server request lines duplicate gunicorn's access log in
    # production and are noise at DEBUG elsewhere; keep them at INFO+ only.
    logging.getLogger('werkzeug').setLevel(max(numeric_level, logging.INFO))
    # DEBUG_DB=true logs every SQL statement — through this handler, in this
    # format. It used to set SQLALCHEMY_ECHO, which makes SQLAlchemy attach its
    # own stdout handler: SQL lines then bypassed LOG_LEVEL and the JSON format.
    # It is an explicit opt-in, so it shows SQL whatever LOG_LEVEL says.
    if _env('DEBUG_DB', 'false').lower() == 'true':
        logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)

    structlog.configure(
        processors=shared_processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """A named structured logger. Sets logging up on first use."""
    setup_logging()
    return structlog.get_logger(name or 'kvuno')
