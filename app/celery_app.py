from celery import Celery

from app.config import get_settings

# Validated here: the worker stops at once, every problem listed, on an
# invalid setting.
_settings = get_settings()

celery_app = Celery('kvuno')

celery_app.config_from_object({
    'broker_url': _settings.CELERY_BROKER_URL,
    'result_backend': _settings.CELERY_RESULT_BACKEND,
    'task_default_queue': _settings.CELERY_TASK_DEFAULT_QUEUE,
    'task_serializer': 'json',
    'accept_content': ['json'],
    'result_serializer': 'json',
    'task_track_started': True,
    'task_acks_late': True,
    # With acks_late, a worker killed mid-task (OOM, container stop) would
    # otherwise have its task acked as failed and the file never finished.
    # Rejecting puts it back on the queue instead. Redelivery is safe because
    # ingest resumes from the stored offset and holds a per-file lock.
    'task_reject_on_worker_lost': True,
    # Redis redelivers an unacked task once the visibility timeout passes —
    # 1 hour by default — even if the first worker is still running it. With
    # acks_late, a long ingest therefore got a second, concurrent copy. Set
    # this above the longest expected single-file ingest.
    'broker_transport_options': {
        'visibility_timeout': _settings.CELERY_VISIBILITY_TIMEOUT,
    },
    'worker_prefetch_multiplier': 1,
    # A worker that loses Redis while running must keep trying: the consumer
    # loop catches the failed connection and restarts (capped at 5 restarts/s),
    # so the worker stays up and consumes again once the broker returns. It
    # does not exit, and it does not sit idle. max_retries=1 only keeps each
    # attempt short before that loop tries again.
    #
    # Startup is the opposite: broker_connection_retry_on_startup=False lets
    # the preflight fail fast rather than a worker wait for a broker that is
    # not there yet.
    'broker_connection_retry': True,
    'broker_connection_retry_on_startup': False,
    'broker_connection_max_retries': 1,
    # Recycle a child that has grown past WORKER_MAX_MEMORY_PER_CHILD MB. Pandas
    # and pyarrow hold large arenas that are not returned to the OS between
    # tasks, so without this a single large ingest permanently inflates the
    # worker. The child is replaced cleanly, so set it above the expected peak
    # of one file, not below it.
    # Celery counts this in KiB; the setting is MB. Passed as-is, 2048 meant
    # 2 MiB and every child was replaced after every task.
    'worker_max_memory_per_child': _settings.WORKER_MAX_MEMORY_PER_CHILD * 1024,
    'worker_max_tasks_per_child': _settings.WORKER_MAX_TASKS_PER_CHILD,
})

celery_app.autodiscover_tasks(['app'])


# ── Logging ────────────────────────────────────────────────────
# As in budget-assist: a receiver on Celery's setup_logging signal stops it
# from configuring (hijacking) the root logger, so worker and task records go
# through app.utils.logging like everything else — same format, same level
# (LOG_LEVEL), JSON in production. The worker's --loglevel no longer matters.

from celery.signals import setup_logging as _celery_setup_logging  # noqa: E402
from celery.signals import task_postrun, task_prerun  # noqa: E402


@_celery_setup_logging.connect
def _configure_logging(**_kwargs):
    from app.utils.logging import setup_logging
    setup_logging(force=True)


@task_prerun.connect
def _bind_task_context(task_id=None, task=None, **_kwargs):
    # Every line logged while a task runs carries its id and name, so one
    # file's ingest can be followed through the worker log.
    import structlog
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(task_id=task_id, task=getattr(task, 'name', None))


@task_postrun.connect
def _clear_task_context(**_kwargs):
    import structlog
    structlog.contextvars.clear_contextvars()


# ── Restarts ───────────────────────────────────────────────────
# Restarting the worker container mid-ingest used to leave the job
# "processing" for hours: Docker's SIGTERM reaches only the main process, the
# warm shutdown waited for the whole file, Docker killed it after its grace
# period, the message stayed unacked until CELERY_VISIBILITY_TIMEOUT (6 h),
# and the dead ingest lock made a manual Retry skip the file. Now:
#   - shutting down tells running ingests to stop at the next chunk boundary
#     (rows + offset committed together, job marked "Paused");
#   - starting clears this host's dead locks, then re-enqueues the jobs this
#     host left processing, in their own runs, from their stored offsets.

from celery.signals import worker_init, worker_ready, worker_shutting_down  # noqa: E402


@worker_init.connect
def _on_worker_init(**_kwargs):
    # Before the pool starts: nothing on this host can hold a lock yet.
    import socket

    from app.services.housekeeper import clear_worker_stop, release_dead_locks
    clear_worker_stop()  # a restarted container keeps its /tmp
    try:
        release_dead_locks(_settings.HOUSEKEEPING_DATA_DIR, socket.gethostname())
    except Exception:
        from app.utils.logging import get_logger
        get_logger(__name__).exception("could not release dead ingest locks")


@worker_ready.connect
def _on_worker_ready(**_kwargs):
    from app.services.housekeeper import resume_interrupted_jobs, set_app, worker_name
    from app.tasks import _get_worker_app
    from app.utils.logging import get_logger

    app = _get_worker_app()
    set_app(app)
    try:
        with app.app_context():
            resumed = resume_interrupted_jobs(worker_name())
        if resumed:
            get_logger(__name__).info("re-enqueued interrupted jobs", count=resumed)
    except Exception:
        # Never stop the worker over this: the jobs go stale and can be retried.
        get_logger(__name__).exception("could not resume interrupted jobs")


@worker_shutting_down.connect
def _on_worker_shutting_down(**_kwargs):
    from app.services.housekeeper import request_worker_stop
    request_worker_stop()
