"""
This script initializes and runs a Flask web application.

The script performs the following steps:
1. Creates an instance of the Flask application using a factory method `create_app()`.
2. Verifies that required dependencies (database, and Redis when housekeeping is
   enabled) are reachable, exiting non-zero if not.
3. Runs the Flask application with the specified host, port, and debug mode.

Settings (app/config.py): FLASK_DEBUG, SERVER_HOST, SERVER_PORT, and
PREFLIGHT_ENABLED=false to skip the dependency check.

Usage:
- Run this script directly to start the Flask application.

Example:
    $ python run.py
"""

import sys

from app import create_app
from app.config import get_settings

app = create_app()
app.app_context().push()


def main():
    # Fail fast if a required dependency is unreachable. Without this the
    # server binds its port and then 500s on every request, which is far
    # harder to diagnose than refusing to start.
    #
    # This lives in main() rather than at module level on purpose: this module
    # pushes an app context at import time, and anything that imports `run`
    # gets that context. A blocking 60-second connectivity check at import
    # would break that contract for every importer.
    #
    # In debug mode a failure is a warning, not fatal: this is the local
    # development server, and being unable to start it at all (to work on the
    # UI, or against a SQLite file) is a worse outcome than requests failing
    # until the database comes up. Containers are unaffected — the entrypoint
    # hard-fails there, so a deployed container still refuses to start.
    settings = get_settings()
    debug = settings.FLASK_DEBUG

    from app.utils.logging import get_logger
    from app.utils.preflight import run_preflight

    logger = get_logger('run')
    if run_preflight("api") != 0:
        if debug:
            logger.warning(
                "[preflight] Required dependencies are unreachable, but "
                "FLASK_DEBUG is on so the dev server is starting anyway. "
                "Requests that touch the database will fail until it is "
                "available."
            )
        else:
            logger.error(
                "Refusing to start: invalid configuration or an unreachable dependency. "
                "See the [preflight] lines above. Set PREFLIGHT_ENABLED=false "
                "to bypass."
            )
            sys.exit(1)

    app.run(host=settings.SERVER_HOST, port=settings.SERVER_PORT, debug=debug, use_reloader=True)


if __name__ == '__main__':
    main()
