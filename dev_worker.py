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
    from app.utils.preflight import run_preflight

    if run_preflight("worker") != 0:
        print("Worker startup check failed — required dependencies are unreachable.",
              file=sys.stderr)
        sys.exit(1)

    argv = [
        "celery",
        "-A", "app.celery_app",
        "worker",
        "--loglevel", os.getenv("CELERY_LOGLEVEL", "info"),
    ]

    concurrency = os.getenv("CELERY_CONCURRENCY")
    if concurrency:
        argv += ["--concurrency", concurrency]

    if sys.platform.startswith("win"):
        argv += ["--pool", "solo", "--without-heartbeat", "--without-gossip", "--without-mingle"]

    sys.exit(subprocess.call(argv))


if __name__ == "__main__":
    main()
