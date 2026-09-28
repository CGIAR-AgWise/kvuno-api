import os
import sys

from loguru import logger


class SharedLogger:
    def __init__(self, log_file=None, level=None):
        self.log_file = log_file
        self.level = (level or os.getenv('LOG_LEVEL', 'INFO')).upper()
        self._configured = False

    def get_logger(self):
        if not self._configured:
            self._configured = True
            logger.remove()
            if self.log_file:
                logger.add(self.log_file, rotation='10 MB', level=self.level)
            logger.add(sys.stdout, level=self.level)
        return logger

    def __repr__(self):
        return f'SharedLogger(log_file={self.log_file}, level={self.level})'
