"""Shared rate limiter instance and preset limits."""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from app.config import RATE_LIMIT_DEFAULT_DAILY, RATE_LIMIT_DEFAULT_HOURLY, RATE_LIMIT_STORAGE

limiter = Limiter(
    key_func=get_remote_address,
    # A blanket default for anything without an explicit per-endpoint limit.
    # Kept generous on purpose: it is a backstop against runaway scripts, not the
    # primary control. The endpoints that actually matter (login, register,
    # upload, data) carry their own limits. Note this previously applied to
    # static assets too, so a handful of page views exhausted it.
    default_limits=[f"{RATE_LIMIT_DEFAULT_DAILY} per day", f"{RATE_LIMIT_DEFAULT_HOURLY} per hour"],
    storage_uri=RATE_LIMIT_STORAGE,
)
