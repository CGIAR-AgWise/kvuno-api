"""
Progress storage with Redis primary and DB fallback.

Redis keys: ``job:{stem}`` → JSON with status/current/total/message/mtime
Pub/sub channel: ``jobs:updates`` → stem published on each change

Stuck jobs
----------
A worker that dies (OOM-killed, container restarted, unhandled crash) never
writes a terminal status, so its job entry would sit at ``processing``
forever. Celery's ``autoretry_for=(Exception,)`` makes this worse: the task
is retried silently up to 10 times, and if every attempt dies the entry is
just abandoned.

Two guards, since neither alone is sufficient:

- **TTL on the key** so abandoned entries eventually expire instead of
  accumulating in Redis indefinitely.
- **Staleness detection on read** (``mark_stale``), using the ``mtime`` that
  every write already records. A job still claiming to be ``processing``
  whose mtime is older than ``JOB_STALE_AFTER_SECONDS`` is reported as
  ``stale`` — it cannot be genuinely in progress if nothing has written to it
  in that long. This is the part that actually fixes the stuck UI, since the
  TTL alone would just make jobs vanish.
"""
import json
import os
import time

PROGRESS_CHANNEL = 'jobs:updates'

# How long a job may go without reporting before we call it abandoned. Long
# enough to cover a slow batch insert, short enough that a dead worker is
# noticed while someone is still looking at the page.
STALE_AFTER_SECONDS = int(os.getenv('JOB_STALE_AFTER_SECONDS', '1800'))

# Keys expire on their own so a crashed run cannot leak entries forever.
# Well beyond any legitimate job, since jobs are also listed in the UI.
KEY_TTL_SECONDS = int(os.getenv('JOB_PROGRESS_TTL_SECONDS', str(7 * 24 * 3600)))

# Statuses that mean "a worker is or should be working on this".
ACTIVE_STATUSES = {'processing'}


def _redis_client():
    try:
        from redis import Redis
        url = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
        return Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    except Exception:
        return None


def mark_stale(job: dict, now: float | None = None) -> dict:
    """Downgrade an abandoned `processing` job to `stale`.

    Returns the job, modified in place. Terminal statuses are left alone —
    a completed job from last week is history, not a stuck job.
    """
    now = now if now is not None else time.time()
    if job.get('status') not in ACTIVE_STATUSES:
        return job

    last_seen = job.get('mtime') or 0
    age = now - last_seen
    if age <= STALE_AFTER_SECONDS:
        return job

    job['status'] = 'stale'
    job['stale_since'] = last_seen
    job['age_seconds'] = int(age)
    job['message'] = (
        f"No progress for {int(age // 60)} min — the worker stopped reporting. "
        f"This job did not finish; re-run it."
    )
    return job


def save_progress(file_path: str, status: str, current: int, total: int, message: str = ""):
    stem = os.path.basename(file_path)
    payload = {"status": status, "current": current, "total": total, "message": message, "mtime": time.time()}

    r = _redis_client()
    if r is not None:
        try:
            key = f"job:{stem}"
            r.set(key, json.dumps(payload))
            # Refresh the expiry on every write, so an active job never
            # disappears mid-run.
            r.expire(key, KEY_TTL_SECONDS)
            r.publish(PROGRESS_CHANNEL, stem)
            return
        except Exception:
            pass

    _db_save(stem, payload)


def load_all_jobs():
    r = _redis_client()
    if r is not None:
        try:
            jobs = []
            for key in r.scan_iter("job:*", count=100):
                data = r.get(key)
                if data:
                    job = json.loads(data)
                    job['file'] = key.decode() if isinstance(key, bytes) else key.split(':', 1)[1]
                    jobs.append(mark_stale(job))
            jobs.sort(key=lambda j: j.get('mtime', 0), reverse=True)
            return jobs
        except Exception:
            pass

    return [mark_stale(j) for j in _db_load_all()]


def delete_job(stem: str):
    r = _redis_client()
    if r is not None:
        try:
            r.delete(f"job:{stem}")
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
                "file": r.file_name,
                "status": r.status,
                "current": r.current_row,
                "total": r.total_rows,
                "message": r.message or "",
                "mtime": r.updated_at.timestamp() if r.updated_at else 0,
            }
            for r in rows
        ]
    except Exception:
        return []
