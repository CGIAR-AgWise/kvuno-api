#!/bin/sh
# Worker entrypoint: verify dependencies, then hand off to the image's CMD
# (Celery).
#
# The worker is the case where failing fast matters most: a Celery worker that
# starts without its broker does not crash, it sits there retrying forever,
# which looks exactly like "no work is arriving". Exiting non-zero makes the
# container runtime restart it instead.
#
# The worker never applies migrations — it only consumes work produced by the
# API, which owns the schema.
set -e

echo "[entrypoint] checking dependencies..."
python3 scripts/preflight.py worker

# exec so celery becomes PID 1 and receives SIGTERM directly; without this the
# shell would swallow signals and the container would only stop on SIGKILL.
exec "$@"
