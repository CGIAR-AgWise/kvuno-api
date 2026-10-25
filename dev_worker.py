"""
Start the Celery worker in development mode, auto-detecting the platform
to select a compatible pool implementation.

- Windows → ``--pool=solo`` (prefork unsupported)
- Linux/macOS → ``--pool=prefork`` (default, omitted)
"""
import subprocess
import sys


def main():
    # Fail fast if the broker or database is down. A Celery worker that starts
    # without Redis does not crash — it sits there retrying forever, which
    # looks identical to "no work is arriving". Exiting non-zero instead lets
    # the container runtime restart it.
    from app.config import get_settings
    from app.utils.logging import get_logger
    from app.utils.preflight import run_preflight

    if run_preflight("worker") != 0:
        get_logger('dev_worker').fatal("Worker startup check failed — invalid configuration or an unreachable dependency.")
        sys.exit(1)

    settings = get_settings()
    argv = [
        "celery",
        "-A", "app.celery_app",
        "worker",
        # The one log level everywhere (LOG_LEVEL); there is no Celery-specific
        # setting. app/celery_app.py routes the worker's logging through
        # app/utils/logging.py, which reads the same variable.
        "--loglevel", settings.LOG_LEVEL.lower(),
    ]

    if settings.CELERY_CONCURRENCY:
        argv += ["--concurrency", str(settings.CELERY_CONCURRENCY)]

    if sys.platform.startswith("win"):
        argv += ["--pool", "solo", "--without-heartbeat", "--without-gossip", "--without-mingle"]

    sys.exit(subprocess.call(argv))


if __name__ == "__main__":
    main()
