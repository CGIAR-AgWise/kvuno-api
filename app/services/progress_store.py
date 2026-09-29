"""
Progress storage with Redis primary and DB fallback.

Layout
------
``job:{stem}``  HASH  status / current / total / mtime / message / started_at
``jobs:index``  ZSET  member=stem, score=mtime — the recent-first index
channel        ``jobs:updates`` carries the single changed job as JSON

Why a hash and an index
-----------------------
These were plain JSON strings found with ``SCAN job:*``, and every SSE client
re-read the whole keyspace on every progress write. That made the cost per
tick O(clients x jobs). A hash allows single-field updates and a ZSET turns
listing into one indexed read instead of a keyspace walk, so a client can be
sent a delta for the one job that changed.

Write cadence
-------------
Progress is published at most once per ``JOB_PROGRESS_MIN_INTERVAL`` seconds
per job, because browsers cannot use anything finer and the per-chunk cadence
was tens of pubsub messages per second. Terminal states always write
immediately. This is independent of ``HOUSEKEEPING_CHECKPOINT_INTERVAL``,
which governs *database* commits for resumability — that still needs to be
per-chunk, this does not.

Stuck jobs
----------
A worker that dies (OOM-killed, container restarted, crash) never writes a
terminal status, so its job would sit at ``processing`` forever. Celery's
``autoretry_for=(Exception,)`` hides this by retrying silently. Two guards:

- **TTL on the key** so abandoned entries expire rather than accumulating.
- **Staleness on read** (``mark_stale``): a job still claiming to be
  ``processing`` whose mtime is older than ``JOB_STALE_AFTER_SECONDS`` cannot
  genuinely be in progress, and is reported as ``stale``. This is what fixes
  the stuck UI, since the TTL alone would just make jobs vanish.
"""
import json
import os
import time

PROGRESS_CHANNEL = 'jobs:updates'
KEY_PREFIX = 'job:'
INDEX_KEY = 'jobs:index'

# Terminal states: always written through, never throttled.
TERMINAL_STATUSES = {'completed', 'error'}

# How long a job may go without reporting before we call it abandoned.
STALE_AFTER_SECONDS = int(os.getenv('JOB_STALE_AFTER_SECONDS', '1800'))

# Keys expire on their own so a crashed run cannot leak entries forever.
KEY_TTL_SECONDS = int(os.getenv('JOB_PROGRESS_TTL_SECONDS', str(7 * 24 * 3600)))

# Minimum seconds between published updates for one job in an active state.
MIN_INTERVAL_SECONDS = float(os.getenv('JOB_PROGRESS_MIN_INTERVAL', '1.0'))

# Last publish time per stem, for throttling. Per-process and advisory only:
# losing it just means a slightly higher publish rate.
_last_published: dict[str, float] = {}

_migrated = False


def _redis_client():
    try:
        from redis import Redis
        url = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
        return Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    except Exception:
        return None


def mark_stale(job: dict, now: float | None = None) -> dict:
    """Downgrade an abandoned `processing` job to `stale`.

    Terminal statuses are left alone — a completed job from last week is
    history, not a stuck job.
    """
    now = now if now is not None else time.time()
    if job.get('status') not in {'processing'}:
        return job

    last_seen = job.get('mtime') or 0
    age = now - last_seen
    if age <= STALE_AFTER_SECONDS:
        return job

    job['status'] = 'stale'
    job['stale_since'] = last_seen
    job['age_seconds'] = int(age)
    job['message'] = (
        f"No progress for {int(age // 60)} min - the worker stopped reporting. "
        f"This job did not finish; re-run it."
    )
    return job


def _to_job(stem: str, h: dict) -> dict:
    """Normalise a hash into the shape the API and UI expect."""
    def _num(key, default=0):
        raw = h.get(key)
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default

    return {
        'file': stem,
        'status': h.get('status') or 'unknown',
        'current': _num('current'),
        'total': _num('total'),
        'message': h.get('message') or '',
        'mtime': _num('mtime'),
        'started_at': _num('started_at'),
    }


def _should_publish(stem: str, status: str, now: float) -> bool:
    """Throttle intermediate updates; always let terminal states through."""
    if status in TERMINAL_STATUSES:
        return True
    last = _last_published.get(stem)
    if last is not None and (now - last) < MIN_INTERVAL_SECONDS:
        return False
    return True


def save_progress(file_path: str, status: str, current: int, total: int, message: str = ""):
    """Record progress for a file. Publishes at most once per interval."""
    stem = os.path.basename(file_path)
    now = time.time()

    if not _should_publish(stem, status, now):
        return
    _last_published[stem] = now

    r = _redis_client()
    if r is not None:
        try:
            key = f"{KEY_PREFIX}{stem}"
            exists = r.exists(key)
            r.hset(key, mapping={
                'status': status,
                'current': int(current),
                'total': int(total),
                'message': message,
                'mtime': now,
                # Preserve the original start time so the UI can show a rate and
                # an ETA across the whole job, not since the last message.
                'started_at': (r.hget(key, 'started_at') or now) if exists else now,
            })
            # Refresh expiry on every write so an active job never disappears.
            r.expire(key, KEY_TTL_SECONDS)
            r.zadd(INDEX_KEY, {stem: now})
            job = _to_job(stem, r.hgetall(key))
            r.publish(PROGRESS_CHANNEL, json.dumps(job))
        except Exception:
            pass

    # Always persist terminal states, even when Redis is healthy. Otherwise the
    # DB fallback holds no history at all, and a later Redis outage loses every
    # completed and failed job.
    if status in TERMINAL_STATUSES or r is None:
        _db_save(stem, {
            'status': status, 'current': int(current), 'total': int(total),
            'message': message, 'mtime': now,
        })


def get_job(stem: str) -> dict | None:
    """Fetch a single job. Used by the SSE delta path."""
    r = _redis_client()
    if r is not None:
        try:
            h = r.hgetall(f"{KEY_PREFIX}{stem}")
            if h:
                return mark_stale(_to_job(stem, h))
            return None
        except Exception:
            pass

    for job in _db_load_all():
        if job.get('file') == stem:
            return mark_stale(job)
    return None


def load_all_jobs(limit: int | None = None) -> list[dict]:
    """All known jobs, most recently updated first."""
    r = _redis_client()
    if r is not None:
        try:
            _migrate_legacy(r)
            stems = r.zrevrange(INDEX_KEY, 0, -1 if limit is None else limit - 1)
            jobs = []
            for stem in stems:
                stem = stem.decode() if isinstance(stem, bytes) else stem
                h = r.hgetall(f"{KEY_PREFIX}{stem}")
                if h:
                    jobs.append(mark_stale(_to_job(stem, h)))
            # The index score is mtime, but mark_stale does not change it, so
            # the ZSET order is already correct.
            return jobs
        except Exception:
            pass

    return [mark_stale(j) for j in _db_load_all()][:limit]


def delete_job(stem: str):
    r = _redis_client()
    if r is not None:
        try:
            r.delete(f"{KEY_PREFIX}{stem}")
            r.zrem(INDEX_KEY, stem)
        except Exception:
            pass
    _last_published.pop(stem, None)


def _migrate_legacy(r) -> None:
    """One-time conversion of pre-hash JSON keys into the hash + index layout.

    Jobs already recorded as plain SET strings would otherwise vanish from the
    listing, because the ZSET index is only populated on write. Runs at most
    once per process; it is a no-op once every key is a hash.
    """
    global _migrated
    if _migrated:
        return
    _migrated = True
    try:
        if r.exists(INDEX_KEY) and r.zcard(INDEX_KEY) > 0:
            return
        for raw_key in r.scan_iter(match=f"{KEY_PREFIX}*", count=200):
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            if r.type(key) != 'string':
                continue
            payload = r.get(key)
            if not payload:
                continue
            data = json.loads(payload)
            stem = key[len(KEY_PREFIX):]
            r.hset(key, mapping={
                'status': data.get('status', 'unknown'),
                'current': data.get('current', 0),
                'total': data.get('total', 0),
                'message': data.get('message', ''),
                'mtime': data.get('mtime', time.time()),
                'started_at': data.get('mtime', time.time()),
            })
            r.expire(key, KEY_TTL_SECONDS)
            r.zadd(INDEX_KEY, {stem: data.get('mtime', time.time())})
    except Exception:
        pass


# ── DB fallback ────────────────────────────────────────────────

def _db_save(stem: str, payload: dict):
    try:
        from app.models.database_conn import MyDb
        from app.models.kvuno import JobProgress

        session = MyDb.get_db().session
        existing = session.query(JobProgress).filter_by(file_name=stem).first()
        if existing:
            existing.status = payload['status']
            existing.current_row = payload['current']
            existing.total_rows = payload['total']
            existing.message = payload['message']
        else:
            session.add(JobProgress(
                file_name=stem,
                status=payload['status'],
                current_row=payload['current'],
                total_rows=payload['total'],
                message=payload['message'],
            ))
        session.commit()
    except Exception:
        pass


def _db_load_all():
    try:
        from app.models.database_conn import MyDb
        from app.models.kvuno import JobProgress

        session = MyDb.get_db().session
        rows = session.query(JobProgress).order_by(JobProgress.updated_at.desc()).all()
        return [
            {
                'file': r.file_name,
                'status': r.status,
                'current': r.current_row,
                'total': r.total_rows,
                'message': r.message or '',
                'mtime': r.updated_at.timestamp() if r.updated_at else 0,
                # The table has no started_at column, and updated_at moves on
                # every write, so reporting it as the start would inflate the
                # computed rate wildly. Zero makes the UI omit rate/ETA rather
                # than show a wrong figure.
                'started_at': 0,
            }
            for r in rows
        ]
    except Exception:
        return []
