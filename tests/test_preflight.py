"""Tests for the startup dependency check.

The behaviour that matters is not "does it connect" but "does it refuse to
start when it must, and start when it need not". A regression that makes the
check silently permissive is worse than no check at all, so each case asserts
the exit code for both roles.
"""

import importlib
import sys

import pytest


@pytest.fixture(autouse=True)
def _reload_env(monkeypatch):
    """Force app.config to re-read the environment for each scenario.

    app.config binds module-level constants at import time, so without
    dropping it from sys.modules a monkeypatched CELERY_BROKER_URL would be
    ignored and the test would silently measure the previous scenario.
    """
    for name in ("app.config", "app.utils.preflight"):
        sys.modules.pop(name, None)
    yield
    for name in ("app.config", "app.utils.preflight"):
        sys.modules.pop(name, None)


def _preflight(monkeypatch, **env):
    values = {
        "PREFLIGHT_RETRIES": "1",
        "PREFLIGHT_DELAY_SECONDS": "0.01",
        "PREFLIGHT_TIMEOUT_SECONDS": "1",
    }
    values.update(env)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    import app.utils.preflight as pf
    importlib.reload(pf)
    return pf


OK_DB = {"DB_URL": "sqlite:///:memory:", "DB_DRIVER": "sqlite", "DB_NAME": ":memory:"}
DEAD_DB = {"DB_URL": "postgresql://u:p@127.0.0.1:5499/nope", "DB_DRIVER": "postgresql"}
DEAD_REDIS = "redis://127.0.0.1:6399/0"  # nothing is listening on this port


class TestDatabaseRequired:
    def test_api_fails_when_database_is_down(self, monkeypatch):
        pf = _preflight(monkeypatch, **DEAD_DB, HOUSEKEEPING_ENABLED="false")
        assert pf.run_preflight("api") == 1

    def test_worker_fails_when_database_is_down(self, monkeypatch):
        pf = _preflight(monkeypatch, **DEAD_DB, HOUSEKEEPING_ENABLED="false")
        assert pf.run_preflight("worker") == 1


class TestRedisRequirementDependsOnRoleAndConfig:
    """Redis is optional for the API when housekeeping is off, never for the worker."""

    def test_api_starts_with_redis_down_when_housekeeping_off(self, monkeypatch):
        pf = _preflight(monkeypatch, **OK_DB,
                        CELERY_BROKER_URL=DEAD_REDIS, HOUSEKEEPING_ENABLED="false")
        assert pf.run_preflight("api") == 0

    def test_api_fails_with_redis_down_when_housekeeping_on(self, monkeypatch):
        pf = _preflight(monkeypatch, **OK_DB,
                        CELERY_BROKER_URL=DEAD_REDIS, HOUSEKEEPING_ENABLED="true")
        assert pf.run_preflight("api") == 1

    def test_worker_always_requires_redis(self, monkeypatch):
        """A Celery worker with no broker is useless whatever else is configured."""
        pf = _preflight(monkeypatch, **OK_DB,
                        CELERY_BROKER_URL=DEAD_REDIS, HOUSEKEEPING_ENABLED="false")
        assert pf.run_preflight("worker") == 1


class TestOptOut:
    def test_preflight_can_be_disabled(self, monkeypatch):
        """Escape hatch: a broken dependency must not block a deliberate boot."""
        pf = _preflight(monkeypatch, **DEAD_DB, PREFLIGHT_ENABLED="false")
        assert pf.run_preflight("api") == 0
        assert pf.run_preflight("worker") == 0

    @pytest.mark.parametrize("value,expected", [
        ("false", False), ("0", False), ("no", False), ("off", False),
        ("true", True), ("1", True), ("yes", True), ("on", True),
    ])
    def test_truthy_parsing(self, monkeypatch, value, expected):
        pf = _preflight(monkeypatch, PREFLIGHT_ENABLED=value)
        assert pf._as_bool(value, True) is expected


class TestCredentialRedaction:
    def test_password_is_not_logged(self, monkeypatch):
        pf = _preflight(monkeypatch)
        redacted = pf._sanitize("postgresql://user:hunter2@db.internal:5432/kvuno")
        assert "hunter2" not in redacted
        assert "db.internal:5432/kvuno" in redacted

    def test_url_without_credentials_is_unchanged(self, monkeypatch):
        pf = _preflight(monkeypatch)
        assert pf._sanitize("redis://localhost:6379/0") == "redis://localhost:6379/0"


class TestCheckResult:
    def test_blocking_only_when_required_and_unavailable(self, monkeypatch):
        pf = _preflight(monkeypatch)
        assert pf.CheckResult("x", True, True, "").blocking is False
        assert pf.CheckResult("x", False, True, "").blocking is True
        # An optional dependency being down must never block.
        assert pf.CheckResult("x", False, False, "").blocking is False


class TestDatabaseConnectTimeoutOption:
    """The connect-timeout option name is driver-specific.

    Passing psycopg2's `timeout` to psycopg3 is a hard ProgrammingError, not a
    timeout — which made the check fail against a *healthy* Postgres and would
    have stopped the API from ever starting. Guard the mapping explicitly.
    """

    def _connect_args(self, monkeypatch, url):
        captured = {}
        import sqlalchemy

        real_create_engine = sqlalchemy.create_engine

        def spy_create_engine(u, **kwargs):
            captured["url"] = u
            captured["connect_args"] = kwargs.get("connect_args")
            # Do not actually build an engine: the point is the arguments.
            raise RuntimeError("stop-before-connect")

        monkeypatch.setenv("DB_URL", url)
        monkeypatch.setenv("DB_DRIVER", url.split("://", 1)[0])
        import app.utils.preflight as pf
        importlib.reload(pf)
        monkeypatch.setattr(sqlalchemy, "create_engine", spy_create_engine)
        try:
            pf.check_database(3)
        except Exception:
            pass
        finally:
            monkeypatch.setattr(sqlalchemy, "create_engine", real_create_engine)
        return captured.get("connect_args")

    def test_postgres_uses_connect_timeout(self, monkeypatch):
        args = self._connect_args(monkeypatch, "postgresql://u:p@host:5432/db")
        assert args == {"connect_timeout": 3}
        # The psycopg2 spelling must never appear.
        assert "timeout" not in args

    def test_sqlite_gets_no_connect_args(self, monkeypatch):
        assert self._connect_args(monkeypatch, "sqlite:///:memory:") == {}
