"""Shared rate limiter instance and preset limits."""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from app.config import RATE_LIMIT_STORAGE

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri=RATE_LIMIT_STORAGE,
)
