"""
Start the Celery worker in development mode, auto-detecting the platform
to select a compatible pool implementation.

- Windows → ``--pool=solo`` (prefork unsupported)
- Linux/macOS → ``--pool=prefork`` (default, omitted)
"""
import os
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv()


def main():
    # Fail fast if the broker or database is down. A Celery worker that starts
    # without Redis does not crash — it sits there retrying forever, which
    # looks identical to "no work is arriving". Exiting non-zero instead lets
    # the container runtime restart it.
    from app.utils.logging import get_logger
    from app.utils.preflight import run_preflight

    if run_preflight("worker") != 0:
        get_logger('dev_worker').fatal("Worker startup check failed — required dependencies are unreachable.")
        sys.exit(1)

    argv = [
        "celery",
        "-A", "app.celery_app",
        "worker",
        # The one log level everywhere (LOG_LEVEL); there is no Celery-specific
        # setting. app/celery_app.py routes the worker's logging through
        # app/utils/logging.py, which reads the same variable.
        "--loglevel", os.getenv("LOG_LEVEL", "INFO").lower(),
    ]

    concurrency = os.getenv("CELERY_CONCURRENCY")
    if concurrency:
        argv += ["--concurrency", concurrency]

    if sys.platform.startswith("win"):
        argv += ["--pool", "solo", "--without-heartbeat", "--without-gossip", "--without-mingle"]

    sys.exit(subprocess.call(argv))


if __name__ == "__main__":
    main()
