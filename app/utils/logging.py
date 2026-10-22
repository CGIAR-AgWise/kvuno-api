"""
Application logging.

Everything goes to a stream — stdout by default. There is deliberately no
file sink: no log platform collects files from inside a container, so writing
to disk makes logs invisible to `docker logs`, Dokploy, and any other runtime.
If you need persistence, redirect the stream at the container level.

Gunicorn is configured the same way; see `app/gunicorn_config.py`
(ACCESSLOG/ERRORLOG default to `-`, i.e. stdout/stderr).
"""
import os
import sys

from loguru import logger


class SharedLogger:
    def __init__(self, level=None):
        self.level = (level or os.getenv('LOG_LEVEL', 'INFO')).upper()
        self._configured = False

    def get_logger(self):
        if not self._configured:
            self._configured = True
            # Drop loguru's default stderr handler so level changes are honoured
            # and nothing is emitted twice.
            logger.remove()
            logger.add(sys.stdout, level=self.level)
        return logger

    def __repr__(self):
        return f'SharedLogger(level={self.level})'
