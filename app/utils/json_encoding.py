"""
How the API writes dates and times in JSON: ISO 8601, UTC, with a `Z`.

Flask's default provider renders a `datetime` as an HTTP date
("Thu, 22 Oct 2026 07:59:49 GMT"), which is meant for headers and is
awkward for clients to parse and sort. Every JSON response now carries
"2026-10-22T07:59:49Z" instead (fractional seconds when the value has them).

Naive datetimes are treated as UTC: that is how this app stores them
(token timestamps are naive UTC by convention; Postgres `now()` defaults run
in the database's UTC session time zone). Aware ones are converted to UTC.
"""
from __future__ import annotations

import datetime
from typing import Any

from flask.json.provider import DefaultJSONProvider


def iso_utc(value: datetime.datetime) -> str:
    """'2026-10-22T07:59:49Z' — naive values are taken to be UTC."""
    if value.tzinfo is not None:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.isoformat() + 'Z'


def json_default(value: Any) -> Any:
    """`default=` hook for json.dumps, shared by Flask and the response cache."""
    if isinstance(value, datetime.datetime):
        return iso_utc(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    return DefaultJSONProvider.default(value)


class IsoJSONProvider(DefaultJSONProvider):
    """Flask JSON provider that writes dates as ISO 8601 (see module docstring)."""

    default = staticmethod(json_default)
