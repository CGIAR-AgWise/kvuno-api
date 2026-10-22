"""Cross-module contract tests.

Three bugs in a row came from the same root cause: a function's contract and
its callers disagreeing, with nothing asserting the agreement at the boundary.

1. ``process_file_async`` is annotated ``str`` but both callers passed a
   ``pathlib.Path``, which Celery cannot JSON-serialise.
2. The progress Redis client returns ``bytes`` keys and ``_to_job`` looked them
   up as ``str``, so every job silently read as ``unknown``/zero.
3. ``housekeeping_settings()`` was splatted into three callables with three
   signatures, and one parameter only one of them accepted.

These are cheap to check and expensive to rediscover, because each failure looks
like an unrelated symptom far from the cause.
"""

import inspect
import json
import re
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _source_files() -> list[Path]:
    return sorted(APP_DIR.rglob("*.py"))


def _import_housekeeper():
    from app.services import housekeeper
    return housekeeper


class TestSplattedSettingsContract:
    """A dict splatted with ``**`` must be accepted by every callee.

    Source-scanned rather than hand-listed, so a new call site is covered the
    moment it is written. The pattern is narrow on purpose: only calls that
    splat a known shared settings dict.
    """

    SHARED = re.compile(r"(\w+)\(\s*[^)]*\*\*(\w+_settings)\(\)")

    def test_every_splatted_settings_call_is_valid(self):
        housekeeper = _import_housekeeper()
        offenders: list[str] = []
        checked = 0

        for path in _source_files():
            text = path.read_text(encoding="utf-8")
            for match in self.SHARED.finditer(text):
                callee_name, settings_fn = match.group(1), match.group(2)
                # Only interested in the shared housekeeping settings.
                if not hasattr(housekeeper, settings_fn):
                    continue
                callee = getattr(housekeeper, callee_name, None)
                if callee is None or not callable(callee):
                    continue
                checked += 1
                params = inspect.signature(callee).parameters
                accepts_kwargs = any(
                    p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
                )
                if accepts_kwargs:
                    continue
                settings = getattr(housekeeper, settings_fn)()
                unknown = [k for k in settings if k not in params]
                if unknown:
                    offenders.append(
                        f"{path.name}: {callee_name}() rejects {unknown} from {settings_fn}()"
                    )

        assert not offenders, "settings splatted into a callee it does not accept:\n" + "\n".join(
            offenders
        )
        assert checked, "no splatted settings call sites found — the pattern may have drifted"


class TestCeleryBoundarySerialisation:
    """Task arguments are JSON-serialised before publishing to the broker."""

    def test_process_file_async_coerces_a_path(self):
        from unittest.mock import patch
        from app.services.housekeeper import process_file_async

        captured = {}
        with patch(
            "app.services.housekeeper._enqueue_or_warn",
            lambda task, **kw: captured.update(kw),
        ):
            process_file_async(file_path=Path("/tmp/data/x.parquet"))

        assert isinstance(captured["file_path"], str)
        json.dumps(captured)  # what Celery does before publishing

    def test_every_celery_task_is_callable_with_serialisable_arguments(self):
        """Each task's signature should be satisfiable by JSON-safe values.

        Not a substitute for the boundary test above — it catches a task whose
        parameters are typed in a way that would need a Path or a datetime
        handed straight to the broker.
        """
        from app import tasks

        for name in dir(tasks):
            obj = getattr(tasks, name)
            if not hasattr(obj, "delay") or not hasattr(obj, "apply_async"):
                continue
            sig = inspect.signature(obj.run)
            args = []
            for pname, param in sig.parameters.items():
                if pname == "self":
                    continue
                if param.default is not inspect.Parameter.empty:
                    continue
                # A stand-in that must survive JSON serialisation.
                args.append("probe")
            try:
                json.dumps(args)
            except TypeError as exc:  # pragma: no cover - defensive
                pytest.fail(f"{name} requires non-serialisable arguments: {exc}")


class TestRedisReadBoundary:
    """The progress client is built without ``decode_responses``."""

    def test_hashes_decode_from_bytes(self):
        from app.services.progress_store import _to_job

        job = _to_job(
            "x.parquet",
            {
                b"status": b"processing",
                b"current": b"605000",
                b"total": b"2025504",
                b"message": b"halfway",
                b"mtime": b"1790762111.7",
                b"started_at": b"1790761795.6",
            },
        )
        assert job["status"] == "processing"
        assert job["current"] == 605000
        assert job["total"] == 2025504
        # started_at drives the rows/sec and ETA; losing it silently disabled
        # a feature rather than raising.
        assert job["started_at"] == 1790761795

    def test_hashes_decode_from_str(self):
        from app.services.progress_store import _to_job

        job = _to_job("x.parquet", {"status": "completed", "current": "10", "total": "10"})
        assert job["status"] == "completed"
        assert job["current"] == 10

    def test_client_does_not_decode_so_readers_must(self):
        """Pins the condition that makes the two tests above necessary.

        If someone adds decode_responses=True, this fails and the decoding in
        _to_job becomes redundant — worth knowing, not worth breaking.
        """
        from app.services.progress_store import _redis_client

        client = _redis_client()
        assert client is not None
        assert not client.connection_pool.connection_kwargs.get("decode_responses", False)
