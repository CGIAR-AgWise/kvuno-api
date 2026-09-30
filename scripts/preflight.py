"""Standalone startup connectivity check.

Run before migrations and before the app or worker starts:
    python scripts/preflight.py api
    python scripts/preflight.py worker

Exits 0 when every *required* dependency is reachable, 1 otherwise. A non-zero
exit under a container runtime with `restart: unless-stopped` produces a
restart, which is a far clearer signal than an app that boots and then fails
every request.

Redis is required only when HOUSEKEEPING_ENABLED=true — with housekeeping off
the app stores progress in PostgreSQL and no worker is involved.
"""
import os
import sys

from dotenv import load_dotenv

# Ensure the project root is on sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    # noinspection PyTypeChecker
    sys.path.insert(0, _project_root)

load_dotenv()

from app.utils.preflight import run_preflight  # noqa: E402


def main() -> int:
    role = sys.argv[1] if len(sys.argv) > 1 else "api"
    code = run_preflight(role)
    if code != 0:
        print(
            f"Startup check failed for {role} — required dependencies are "
            "unreachable. See the [preflight] log lines above.",
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
