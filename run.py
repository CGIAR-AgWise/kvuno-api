"""
This script initializes and runs a Flask web application.

The script performs the following steps:
1. Loads environment variables from a `.env` file using `python-dotenv`.
2. Creates an instance of the Flask application using a factory method `create_app()`.
3. Verifies that required dependencies (database, and Redis when housekeeping is
   enabled) are reachable, exiting non-zero if not.
4. Retrieves the Flask debug mode, server host, and port settings from environment variables.
5. Runs the Flask application with the specified host, port, and debug mode.

Environment Variables:
- `FLASK_DEBUG`: If set to '1', the application will run in debug mode.
- `SERVER_HOST`: Specifies the host address on which the Flask app will run. Defaults to '0.0.0.0'.
- `SERVER_PORT`: Specifies the port on which the Flask app will run. Defaults to 80.
- `PREFLIGHT_ENABLED`: Set to 'false' to skip the dependency check.

Usage:
- Run this script directly to start the Flask application.

Example:
    $ python run.py
"""

import os
import sys

from dotenv import load_dotenv

from app import create_app

load_dotenv()

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
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'

    from app.utils.preflight import run_preflight

    if run_preflight("api") != 0:
        if debug:
            print(
                "[preflight] Required dependencies are unreachable, but "
                "FLASK_DEBUG is on so the dev server is starting anyway. "
                "Requests that touch the database will fail until it is "
                "available.",
                file=sys.stderr,
            )
        else:
            print(
                "Refusing to start: required dependencies are unreachable. "
                "See the [preflight] lines above. Set PREFLIGHT_ENABLED=false "
                "to bypass.",
                file=sys.stderr,
            )
            sys.exit(1)

    host = os.getenv('SERVER_HOST', default='0.0.0.0')
    port = os.getenv('SERVER_PORT', default=80)

    app.run(host=host, port=port, debug=debug, use_reloader=True)


if __name__ == '__main__':
    main()
