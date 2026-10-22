import functools
import hashlib
import json

from redis import Redis
from redis.exceptions import RedisError

from app.config import CELERY_BROKER_URL

_client = None


def _get_client() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1)
    return _client


_VERSION = 'v2'

def _cache_key(prefix: str, query_string: str) -> str:
    return f"kvuno:{prefix}:{_VERSION}:{hashlib.md5(query_string.encode()).hexdigest()}"


def api_cache(prefix: str, ttl: int = 300):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            from flask import request
            key = _cache_key(prefix, request.query_string.decode())
            try:
                client = _get_client()
                cached = client.get(key)
                if cached is not None:
                    # noinspection PyTypeChecker
                    return json.loads(cached)
            except RedisError:
                pass

            result = fn(*args, **kwargs)

            # Flask views may return (body, status) — cache only the body.
            # Only a 2xx is cached: the body is replayed WITHOUT its status, so
            # caching a transient 500 `{'error': ...}` served that error as a
            # 200 to every caller for the rest of the TTL.
            body = result
            status = 200
            if isinstance(result, tuple):
                body = result[0]
                if len(result) > 1 and isinstance(result[1], int):
                    status = result[1]
            if not 200 <= status < 300 or not isinstance(body, (dict, list)):
                return result
            try:
                client = _get_client()
                client.setex(key, ttl, json.dumps(body, default=str))
            except RedisError:
                pass
            return result
        return wrapper
    return decorator


def invalidate_cache(prefix: str = None):
    """Delete all cache keys matching the given prefix (or all kvuno cache).

    The match is ``kvuno:{prefix}:*``, so a prefix is exact up to the colon:
    'coordinates' does NOT cover 'coordinates_bulk'. Use
    :func:`invalidate_planting_data_cache` when the dataset changes.
    """
    pattern = f"kvuno:{prefix}:*" if prefix else "kvuno:*"
    try:
        client = _get_client()
        for key in client.scan_iter(match=pattern, count=100):
            client.delete(key)
    except RedisError:
        pass


# Every api_cache prefix whose response is derived from planting_recommendations.
# Kept next to the decorator so a new cached endpoint is added here too — the
# upload endpoint used to list its own prefixes and missed 'coordinates_bulk',
# so the map kept showing the pre-upload sample for the full TTL.
PLANTING_DATA_CACHE_PREFIXES = ('filters', 'coordinates', 'coordinates_bulk', 'clusters')


def invalidate_planting_data_cache():
    """Drop every cached response derived from the planting data."""
    for prefix in PLANTING_DATA_CACHE_PREFIXES:
        invalidate_cache(prefix)
