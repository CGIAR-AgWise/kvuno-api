"""Shared rate limiter instance.

Its storage and default limits come from the settings (RATE_LIMIT_STORAGE,
RATE_LIMIT_DEFAULT_DAILY/HOURLY), applied by `configure_limiter()` when the
app is created rather than here at import.
"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from app.config import get_settings

limiter = Limiter(key_func=get_remote_address)


def configure_limiter(app) -> None:
    settings = get_settings()
    app.config['RATELIMIT_STORAGE_URI'] = settings.RATE_LIMIT_STORAGE
    # A blanket default for anything without an explicit per-endpoint limit.
    # Kept generous on purpose: it is a backstop against runaway scripts, not
    # the primary control. The endpoints that actually matter (login, register,
    # upload, data) carry their own limits. Note this previously applied to
    # static assets too, so a handful of page views exhausted it.
    app.config['RATELIMIT_DEFAULT'] = (f"{settings.RATE_LIMIT_DEFAULT_DAILY} per day;"
                                       f"{settings.RATE_LIMIT_DEFAULT_HOURLY} per hour")
    limiter.init_app(app)
