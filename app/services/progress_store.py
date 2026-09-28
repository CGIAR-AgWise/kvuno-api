"""
Progress storage with Redis primary and DB fallback.

Redis keys: ``job:{stem}`` → JSON with status/current/total/message/mtime
Pub/sub channel: ``jobs:updates`` → stem published on each change
"""
import json
import os
import time

PROGRESS_CHANNEL = 'jobs:updates'


def _redis_client():
    try:
        from redis import Redis
        url = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
        return Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    except Exception:
        return None


def save_progress(file_path: str, status: str, current: int, total: int, message: str = ""):
    stem = os.path.basename(file_path)
    payload = {"status": status, "current": current, "total": total, "message": message, "mtime": time.time()}

    r = _redis_client()
    if r is not None:
        try:
            r.set(f"job:{stem}", json.dumps(payload))
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
                    jobs.append(job)
            jobs.sort(key=lambda j: j.get('mtime', 0), reverse=True)
            return jobs
        except Exception:
            pass

    return _db_load_all()


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
