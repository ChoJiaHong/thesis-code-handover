import threading


class SingleFrameProcessor:
    """Processes frames one at a time using a shared worker."""

    def __init__(self, worker):
        self.worker = worker
        self._lock = threading.Lock()

    def predict(self, frame, timeout: float | None = None):
        """Run prediction for a single frame in a thread-safe manner.

        An optional ``timeout`` can be provided to limit how long the call
        waits to acquire the internal lock.  If the lock cannot be acquired
        within the timeout a ``TimeoutError`` will be raised and no GPU work
        will be scheduled.
        """
        acquired = (
            self._lock.acquire(timeout=timeout) if timeout is not None else self._lock.acquire()
        )
        if not acquired:
            raise TimeoutError("timed out waiting for the worker lock")
        try:
            return self.worker.predict(frame)
        finally:
            self._lock.release()
