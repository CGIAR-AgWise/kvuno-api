"""Fail fast on missing dependencies at process start.

A container that boots without its database or broker does not crash — it
accepts traffic and then fails every request, which is far harder to diagnose
than a container that refuses to start. The API in particular was
*deliberately* tolerant: ``run_migrations()`` is a no-op when the database is
unreachable so a slow-starting database cannot block boot. That is the right
trade for the app process, but the wrong one for a deployment — the symptom
should be a restart, not a stream of 500s.

This module draws the line: wait for dependencies for a bounded time, then exit
non-zero and let the container runtime restart the container.

Redis is only *required* when ``HOUSEKEEPING_ENABLED=true``. With housekeeping
off, progress is stored in PostgreSQL and no Celery worker is involved, so
requiring Redis would break a documented, supported configuration.
"""

import os
import sys
import time
from dataclasses import dataclass

from app.utils.logging import SharedLogger

logger = SharedLogger().get_logger()


@dataclass
class CheckResult:
    name: str
    ok: bool
    required: bool
    detail: str

    @property
    def blocking(self) -> bool:
        """A required dependency that is not available must stop the process."""
        return self.required and not self.ok


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _sanitize(url: str) -> str:
    """Strip credentials from a DSN so it is safe to log."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"


def check_database(timeout: float) -> CheckResult:
    """Connect directly, without an app context.

    Deliberately does not reuse ``MyDb``/``check_db_connection()``: that needs
    a Flask app context, and the point of a preflight is to run *before* the
    app is built.
    """
    name = "database"
    try:
        from sqlalchemy import create_engine, text

        from app.config import build_db_url
        url = build_db_url()
    except Exception as exc:  # config raises at import time when misconfigured
        return CheckResult(name, False, True, f"configuration error: {exc}")

    # The connect-timeout option name is driver-specific, and passing the wrong
    # one is a hard error rather than a timeout: psycopg2 uses `timeout`,
    # psycopg3 (what this app uses, via PGDialect_psycopg) and MySQL use
    # `connect_timeout`, and SQLite has no such option. Getting this wrong made
    # the check fail unconditionally against a healthy Postgres.
    backend = url.split("://", 1)[0]
    if backend.startswith("sqlite"):
        connect_args: dict = {}
    elif backend.startswith("postgresql"):
        connect_args = {"connect_timeout": max(1, int(timeout))}
    else:
        connect_args = {"connect_timeout": max(1, int(timeout))}

    engine = None
    try:
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return CheckResult(name, True, True, "connected")
    except Exception as exc:
        return CheckResult(name, False, True, f"unreachable ({_sanitize(url)}): {exc}")
    finally:
        if engine is not None:
            engine.dispose()


def check_redis(timeout: float, role: str = "api") -> CheckResult:
    """Ping the broker.

    Required for the worker unconditionally — a Celery worker with no broker is
    useless, whatever else is configured. For the API it is required only when
    ``HOUSEKEEPING_ENABLED=true``: with housekeeping off, progress is stored in
    PostgreSQL and nothing is enqueued, so requiring Redis would break a
    documented, supported configuration.
    """
    name = "redis"
    try:
        from app.config import CELERY_BROKER_URL
    except Exception as exc:
        return CheckResult(name, False, role == "worker", f"configuration error: {exc}")

    required = role == "worker" or _as_bool(os.getenv("HOUSEKEEPING_ENABLED"), False)
    client = None
    try:
        from redis import Redis

        client = Redis.from_url(
            CELERY_BROKER_URL,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
        )
        client.ping()
        return CheckResult(
            name, True, required, f"connected ({_sanitize(CELERY_BROKER_URL)})"
        )
    except Exception as exc:
        if not required:
            # Not an error: the app stores progress in PostgreSQL and enqueues
            # no work when housekeeping is off.
            return CheckResult(name, False, False, f"not reachable, not required: {exc}")
        return CheckResult(name, False, True, f"unreachable ({_sanitize(CELERY_BROKER_URL)}): {exc}")
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def run_preflight(role: str = "api") -> int:
    """Wait for required dependencies, then exit 0 or 1.

    ``role`` is informational today but keeps the door open for the worker to
    require Redis unconditionally. Returns a process exit code rather than
    calling ``sys.exit`` so it is testable.
    """
    if not _as_bool(os.getenv("PREFLIGHT_ENABLED"), True):
        logger.info(f"[preflight] PREFLIGHT_ENABLED=false — skipping ({role})")
        return 0

    timeout = float(os.getenv("PREFLIGHT_TIMEOUT_SECONDS", "3"))
    retries = int(os.getenv("PREFLIGHT_RETRIES", "30"))
    delay = float(os.getenv("PREFLIGHT_DELAY_SECONDS", "2"))

    def database_probe() -> CheckResult:
        return check_database(timeout)

    def redis_probe() -> CheckResult:
        return check_redis(timeout, role=role)

    probes = (("database", database_probe), ("redis", redis_probe))

    attempt = 0
    while True:
        attempt += 1
        results = [probe() for _, probe in probes]
        blocking = [r for r in results if r.blocking]

        if not blocking:
            # Optional dependencies may still be down; say so without failing.
            degraded = [r for r in results if not r.ok and not r.required]
            for result in results:
                level = logger.info if result.ok or not result.required else logger.warning
                level(f"[preflight] {result.name}: {result.detail}")
            for result in degraded:
                logger.warning(
                    f"[preflight] {result.name} is unavailable but not required: {result.detail}"
                )
            logger.info(f"[preflight] all required dependencies ready ({role})")
            return 0

        remaining = retries - attempt
        for result in blocking:
            logger.warning(
                f"[preflight] {result.name} unavailable "
                f"(attempt {attempt}/{retries}, {remaining} left): {result.detail}"
            )
        if remaining <= 0:
            logger.error(
                f"[preflight] giving up after {retries} attempts: "
                + "; ".join(f"{r.name}: {r.detail}" for r in blocking)
            )
            return 1
        time.sleep(delay)


def main() -> None:
    role = sys.argv[1] if len(sys.argv) > 1 else os.getenv("PREFLIGHT_ROLE", "api")
    sys.exit(run_preflight(role))


if __name__ == "__main__":
    main()
