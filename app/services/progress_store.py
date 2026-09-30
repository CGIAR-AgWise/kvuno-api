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
import datetime
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

# Minimum seconds between DURABLE writes for one job. Postgres is the record of
# truth, but writing it on every published tick would put one write per second
# per running job on the database. Slower than the publish throttle, terminal
# states always write through.
DB_CHECKPOINT_SECONDS = float(os.getenv('JOB_DB_CHECKPOINT_SECONDS', '5.0'))

# Postgres column widths, so a long message or error is truncated rather than
# raising a DataError mid-ingest.
MESSAGE_MAX = 500
ERROR_MAX = 2000

# Last publish time per stem, for throttling. Per-process and advisory only:
# losing it just means a slightly higher publish rate.
_last_published: dict[str, float] = {}

# Last durable write per file, for the independent DB checkpoint throttle.
_last_db_write: dict[str, float] = {}

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


def _decode_hash(h: dict) -> dict:
    """Normalise a Redis hash to str keys and values.

    The client is built without ``decode_responses``, so ``hgetall`` returns
    ``{b'status': b'processing', ...}``. Looking those up with a str key misses
    every field, which silently degraded a job to ``status='unknown'`` with
    zero rows — so the Jobs page showed every job as "Pending" while ingestion
    was running normally, and the rows/sec + ETA never appeared either because
    ``started_at`` was read the same way.
    """
    out = {}
    for key, value in h.items():
        if isinstance(key, (bytes, bytearray)):
            key = key.decode('utf-8', 'replace')
        if isinstance(value, (bytes, bytearray)):
            value = value.decode('utf-8', 'replace')
        out[key] = value
    return out


def _to_job(stem: str, h: dict) -> dict:
    """Normalise a hash into the shape the API and UI expect."""
    h = _decode_hash(h)

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


def save_progress(file_path: str, status: str, current: int, total: int, message: str = '',
                  run_id: int | None = None, error: str | None = None,
                  worker: str | None = None, original_filename: str | None = None):
    """Record progress for a file. Publishes at most once per interval.

    Two write paths with deliberately different cadences:

    - **Redis** is the hot path the SSE stream reads, throttled to
      ``JOB_PROGRESS_MIN_INTERVAL`` because browsers cannot use anything finer.
    - **Postgres** is the durable record, checkpointed on its own slower
      ``JOB_DB_CHECKPOINT_SECONDS`` interval so a long job still leaves a
      recoverable trail without a write per published tick. Terminal states
      always write through.
    """
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

    # Postgres is the durable record, not merely a fallback. Previously an
    # in-flight job existed ONLY in Redis while Redis was healthy, so a broker
    # restart or flush lost every running job with no trace. Checkpointed
    # rather than written per tick to keep the write volume sane.
    if _should_write_db(stem, status, now):
        _db_save(stem, {
            'status': status,
            'current': int(current),
            'total': int(total),
            'message': message,
            'error': error,
            'worker': worker,
            'original_filename': original_filename or stem,
            'stored_path': file_path,
            'run_id': run_id,
            'mtime': now,
        })


def _should_write_db(stem: str, status: str, now: float) -> bool:
    """Throttle durable writes independently of the publish throttle.

    Terminal states always write. Otherwise a job is checkpointed at most once
    per DB_CHECKPOINT_SECONDS, and only when the same file has not just been
    written.
    """
    if status in TERMINAL_STATUSES:
        _last_db_write.pop(stem, None)
        return True
    last = _last_db_write.get(stem)
    if last is not None and (now - last) < DB_CHECKPOINT_SECONDS:
        return False
    _last_db_write[stem] = now
    return True


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


# ── Durable record (Postgres) ──────────────────────────────────

def _db_session():
    from app.models.database_conn import MyDb
    return MyDb.get_db().session


def _clip(value, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + '…'


def _db_save(stem: str, payload: dict):
    """Upsert one job row.

    Matching is on ``(run_id, file_name)``, NOT on ``file_name`` alone. The
    table this replaces had a UNIQUE constraint on file_name, which capped
    history at one row per filename forever and silently overwrote a previous
    run whenever a file was reprocessed.

    ``updated_at`` is assigned explicitly on every write. Relying on
    ``server_default=now()`` was the original bug: a DEFAULT applies on INSERT
    only, so for any existing row it kept the creation timestamp — which made
    mtime ordering wrong and let ``mark_stale`` report healthy jobs as stalled.
    """
    from app.models.kvuno import ProcessingJob

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    session = _db_session()

    query = session.query(ProcessingJob).filter_by(file_name=stem)
    if payload.get('run_id') is not None:
        query = query.filter_by(run_id=payload['run_id'])

    row = query.order_by(ProcessingJob.id.desc()).first()
    if row is None:
        row = ProcessingJob(
            file_name=stem,
            run_id=payload.get('run_id'),
            # started_at is set on creation and then preserved, so the rate and
            # ETA the UI computes cover the whole job rather than resuming from
            # whatever this attempt happens to have reached.
            started_at=now,
            attempts=1,
        )
        session.add(row)

    row.status = payload['status']
    row.current_row = int(payload.get('current') or 0)
    row.total_rows = int(payload.get('total') or 0)
    row.message = _clip(payload.get('message'), MESSAGE_MAX)
    row.error = _clip(payload.get('error'), ERROR_MAX)
    row.updated_at = now
    if payload.get('worker'):
        row.worker = str(payload['worker'])[:120]
    if payload.get('original_filename'):
        row.original_filename = _clip(payload['original_filename'], 255)
    if payload.get('stored_path'):
        row.stored_path = _clip(payload['stored_path'], 512)
    if payload['status'] in TERMINAL_STATUSES:
        row.finished_at = now

    session.commit()
    return row


def _row_to_job(row) -> dict:
    """Normalise a ProcessingJob row into the shape the API and UI expect."""
    return {
        'file': row.file_name,
        'status': row.status,
        'current': row.current_row or 0,
        'total': row.total_rows or 0,
        'message': row.message or '',
        'error': row.error or '',
        'run_id': row.run_id,
        'attempts': row.attempts or 0,
        'worker': row.worker or '',
        'original_name': row.original_filename or row.file_name,
        'mtime': row.updated_at.timestamp() if row.updated_at else 0,
        'started_at': row.started_at.timestamp() if row.started_at else 0,
    }


def _db_load_all(limit: int | None = None) -> list[dict]:
    from app.models.kvuno import ProcessingJob

    session = _db_session()
    query = session.query(ProcessingJob)
    if limit is not None:
        query = query.limit(limit)
    rows = query.order_by(ProcessingJob.updated_at.desc()).all()
    return [_row_to_job(r) for r in rows]


# ── Runs ───────────────────────────────────────────────────────

def start_run(label: str | None = None, source: str = 'upload',
              total_files: int = 0, initiated_by: str | None = None) -> int | None:
    """Open a run so a batch can be tracked as a unit. Returns its id."""
    from app.models.kvuno import ProcessingRun

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    run = ProcessingRun(
        label=_clip(label, 255),
        source=source,
        status='running',
        total_files=int(total_files or 0),
        initiated_by=_clip(initiated_by, 120),
        created_at=now,
        started_at=now,
    )
    session = _db_session()
    session.add(run)
    session.commit()
    return run.id


def finish_run(run_id: int, status: str = 'completed'):
    """Close a run once all its jobs have settled."""
    from app.models.kvuno import ProcessingRun

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    session = _db_session()
    run = session.get(ProcessingRun, run_id)
    if run is None:
        return
    run.status = status
    run.finished_at = now
    session.commit()


def _derive_run_status(run, counts: dict) -> str:
    """A run is finished once none of its jobs are still in flight.

    'pending' means the run exists but no job has reported yet, so a
    freshly-created run does not briefly claim to have failed.
    """
    if counts['processing']:
        return 'running'
    if counts['completed'] and not counts['error']:
        return 'completed'
    if counts['error'] and not counts['completed']:
        return 'error'
    if counts['completed'] and counts['error']:
        return 'partial'
    if counts['stale']:
        return 'stale'
    return 'pending'


def list_runs(limit: int = 20) -> list[dict]:
    """Recent runs with aggregate progress, newest first."""
    from app.models.kvuno import ProcessingRun

    session = _db_session()
    runs = session.query(ProcessingRun).order_by(ProcessingRun.created_at.desc()).limit(limit).all()
    out = []
    for run in runs:
        jobs = [j for j in run.jobs]
        counts = {
            'total': len(jobs),
            'processing': sum(1 for j in jobs if j.status == 'processing'),
            'completed': sum(1 for j in jobs if j.status == 'completed'),
            'error': sum(1 for j in jobs if j.status == 'error'),
            'stale': sum(1 for j in jobs if j.status == 'stale'),
        }
        out.append({
            'id': run.id,
            'label': run.label or f'Run {run.id}',
            'source': run.source,
            # Derived from the jobs, not read from run.status. A run is only
            # finished when every one of its jobs has settled, and nothing
            # reliably calls finish_run() — deriving it here means a run can
            # never be left claiming to be running after its work finished.
            'status': _derive_run_status(run, counts),
            'total_files': run.total_files or len(jobs),
            'initiated_by': run.initiated_by or '',
            'created_at': run.created_at.timestamp() if run.created_at else 0,
            'finished_at': run.finished_at.timestamp() if run.finished_at else 0,
            'counts': counts,
            'rows': {
                'current': sum(j.current_row or 0 for j in jobs),
                'total': sum(j.total_rows or 0 for j in jobs),
            },
        })
    return out
