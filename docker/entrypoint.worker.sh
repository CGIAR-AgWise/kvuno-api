#!/bin/sh
# Worker entrypoint: verify dependencies, then hand off to the image's CMD
# (Celery) or run housekeeping one-shot.
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

# Housekeeping one-shot mode: run python housekeeping.py with args from env
# HOUSEKEEPING_MODE=oneshot  -> run once and exit
# HOUSEKEEPING_MODE=watch    -> run with --watch until interrupted
# HOUSEKEEPING_MODE=dryrun   -> run with --dry-run and exit
# unset/empty                -> normal Celery worker (exec "$@")
if [ -n "${HOUSEKEEPING_MODE}" ]; then
    echo "[entrypoint] running housekeeping mode: ${HOUSEKEEPING_MODE}"
    ARGS=""
    case "${HOUSEKEEPING_MODE}" in
        oneshot) ARGS="" ;;
        watch) ARGS="--watch" ;;
        dryrun) ARGS="--dry-run" ;;
        *) echo "[entrypoint] unknown HOUSEKEEPING_MODE: ${HOUSEKEEPING_MODE}" >&2; exit 1 ;;
    esac
    exec python3 housekeeping.py ${ARGS}
fi

# exec so celery becomes PID 1 and receives SIGTERM directly; without this the
# shell would swallow signals and the container would only stop on SIGKILL.
exec "$@"