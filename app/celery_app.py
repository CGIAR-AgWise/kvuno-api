import os

from celery import Celery

from dotenv import load_dotenv

from app.config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND, CELERY_TASK_DEFAULT_QUEUE

load_dotenv()

celery_app = Celery('kvuno')

celery_app.config_from_object({
    'broker_url': CELERY_BROKER_URL,
    'result_backend': CELERY_RESULT_BACKEND,
    'task_default_queue': CELERY_TASK_DEFAULT_QUEUE,
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
        'visibility_timeout': int(os.getenv('CELERY_VISIBILITY_TIMEOUT', '21600')),
    },
    'worker_prefetch_multiplier': 1,
    'broker_connection_retry_on_startup': False,
    'broker_connection_max_retries': 1,
    # Recycle a child that has grown past WORKER_MAX_MEMORY_PER_CHILD MB. Pandas
    # and pyarrow hold large arenas that are not returned to the OS between
    # tasks, so without this a single large ingest permanently inflates the
    # worker. The child is replaced cleanly, so set it above the expected peak
    # of one file, not below it.
    'worker_max_memory_per_child': int(os.getenv('WORKER_MAX_MEMORY_PER_CHILD', '2048')),
    'worker_max_tasks_per_child': int(os.getenv('WORKER_MAX_TASKS_PER_CHILD', '50')),
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
