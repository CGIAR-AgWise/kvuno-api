#!/bin/sh
# Container entrypoint: apply pending Alembic migrations, then hand off to the
# image's CMD (Flask dev server, Gunicorn, ...).
#
# This replaces the separate one-shot `migrate` compose service, which ran the
# same image with a different command.
#
# Set RUN_MIGRATIONS=false to skip (for workers, or when another component owns
# schema changes). Note run_migrations() is a no-op when the database is
# unreachable, so an unavailable DB will not block the app from starting.
set -e

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    echo "[entrypoint] applying database migrations..."
    python3 scripts/run_migrations.py
    echo "[entrypoint] migrations complete"
else
    echo "[entrypoint] RUN_MIGRATIONS=${RUN_MIGRATIONS:-} — skipping migrations"
fi

# exec so the app becomes PID 1 and receives SIGTERM directly; without this the
# shell would swallow signals and the container would only stop on SIGKILL.
exec "$@"
