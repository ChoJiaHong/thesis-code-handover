import threading
from typing import Optional
from config import settings

class BatchConfig:
    def __init__(self, batch_size: int, queue_timeout: float):
        self._batch_size = batch_size
        self._queue_timeout = queue_timeout
        self._lock = threading.Lock()

    def update(self, batch_size: Optional[int] = None, queue_timeout: Optional[float] = None):
        with self._lock:
            if batch_size is not None:
                self._batch_size = batch_size
            if queue_timeout is not None:
                self._queue_timeout = queue_timeout

    @property
    def batch_size(self) -> int:
        with self._lock:
            return self._batch_size

    @property
    def queue_timeout(self) -> float:
        with self._lock:
            return self._queue_timeout

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "batch_size": self._batch_size,
                "queue_timeout": self._queue_timeout,
            }

# Global configuration instance initialized from settings
global_batch_config = BatchConfig(settings.batch_size, settings.queue_timeout)
