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

Settings come from app.config (LOG_*, ENVIRONMENT, DEBUG_DB). When the
configuration itself is invalid, logging still starts with the defaults, so
the configuration error can be logged rather than lost.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

import structlog

_configured = False


def _settings():
    from app.config import ConfigurationError, Settings, get_settings
    try:
        return get_settings()
    except ConfigurationError:
        # Defaults only: never lose logs (or the error itself) to a bad setting.
        return Settings.model_construct()


def _handler(settings) -> logging.Handler:
    if settings.LOG_DESTINATION == 'stderr':
        return logging.StreamHandler(sys.stderr)
    if settings.LOG_DESTINATION == 'file':
        path = settings.LOG_FILE_PATH
        if not path:
            # Same fallback as budget-assist: never lose logs to a bad setting.
            return logging.StreamHandler(sys.stderr)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if settings.LOG_ROTATION == 'daily':
            return TimedRotatingFileHandler(path, when='D', interval=1, backupCount=settings.LOG_BACKUP_COUNT)
        return RotatingFileHandler(path, maxBytes=settings.LOG_MAX_BYTES, backupCount=settings.LOG_BACKUP_COUNT)
    return logging.StreamHandler(sys.stdout)


def is_production() -> bool:
    return _settings().is_production


def setup_logging(force: bool = False) -> None:
    """Configure global logging. Safe to call more than once (and from Celery)."""
    global _configured
    if _configured and not force:
        return
    _configured = True

    settings = _settings()
    numeric_level = getattr(logging, settings.LOG_LEVEL, logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt='iso', utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer = (structlog.processors.JSONRenderer() if settings.is_production
                else structlog.dev.ConsoleRenderer())
    formatter = structlog.stdlib.ProcessorFormatter(
        # Records from plain stdlib loggers (Flask, SQLAlchemy, Celery…) get the
        # same fields as structlog's own before rendering.
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = _handler(settings)
    handler.setFormatter(formatter)

    logging.basicConfig(level=numeric_level, handlers=[handler], force=True)
    # werkzeug's dev-server request lines duplicate gunicorn's access log in
    # production and are noise at DEBUG elsewhere; keep them at INFO+ only.
    logging.getLogger('werkzeug').setLevel(max(numeric_level, logging.INFO))
    # DEBUG_DB=true logs every SQL statement — through this handler, in this
    # format. It used to set SQLALCHEMY_ECHO, which makes SQLAlchemy attach its
    # own stdout handler: SQL lines then bypassed LOG_LEVEL and the JSON format.
    # It is an explicit opt-in, so it shows SQL whatever LOG_LEVEL says.
    if settings.DEBUG_DB:
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
