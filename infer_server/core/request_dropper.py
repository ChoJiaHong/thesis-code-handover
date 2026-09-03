import time
from typing import Callable, Any

from config import settings
from batch_config import global_batch_config


class RequestTimeoutError(Exception):
    """Raised when a request has waited in queue too long."""


class RequestDropper:
    """Encapsulates logic for dropping requests that exceed a wait timeout.

    The dropper is intended to be used by higher level services.  They only
    need to ``mark`` the arrival time of a request and then execute the
    desired function through ``run``.  Any waiting logic is handled here so
    that the request pipeline remains clean.
    """

    def __init__(self, max_wait: float | None = None, use_dynamic_timeout: bool = True):
        self._static_max_wait = max_wait if max_wait is not None else settings.queue_timeout
        self._use_dynamic_timeout = use_dynamic_timeout
    
    @property
    def max_wait(self) -> float:
        """Get the current max wait time, either from dynamic config or static value."""
        if self._use_dynamic_timeout:
            return global_batch_config.queue_timeout
        return self._static_max_wait

    def mark(self) -> float:
        """Return a timestamp marking the enqueue time of a request."""
        return time.time()

    def run(self, func: Callable[..., Any], enqueue_time: float, *args, **kwargs):
        """Execute ``func`` if the request has not timed out.

        ``func`` should accept a ``timeout`` keyword argument controlling how
        long it is willing to wait for required resources.  This allows the
        dropper to ensure GPU resources are not used for overdue requests.
        """
        elapsed = time.time() - enqueue_time
        remaining = self.max_wait - elapsed
        if remaining <= 0:
            raise RequestTimeoutError("request exceeded max wait time")

        kwargs.setdefault("timeout", remaining)
        try:
            return func(*args, **kwargs)
        except TimeoutError as e:  # Propagate as request-level timeout
            raise RequestTimeoutError("request exceeded max wait time") from e
