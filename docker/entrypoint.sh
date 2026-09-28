#!/bin/sh
# Container entrypoint: apply pending Alembic migrations, then start the server.
#
# Server selection is by APP_MODE, baked at build time (see docker/Dockerfile)
# and overridable at run time with -e APP_MODE=prod|dev:
#   prod -> gunicorn   (WEB_CONCURRENCY workers, configurable timeout)
#   dev  -> the image CMD (python3 run.py, Flask dev server with reloader)
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
if [ "${APP_MODE:-dev}" = "prod" ]; then
    echo "[entrypoint] starting gunicorn (APP_MODE=prod)"
    exec gunicorn \
        --bind "0.0.0.0:${SERVER_PORT:-5000}" \
        --workers "${WEB_CONCURRENCY:-4}" \
        --timeout "${GUNICORN_TIMEOUT:-60}" \
        wsgi:app
fi

echo "[entrypoint] starting dev server (APP_MODE=${APP_MODE:-dev})"
exec "$@"
