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
