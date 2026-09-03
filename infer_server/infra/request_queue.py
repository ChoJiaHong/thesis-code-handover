import queue


class RequestQueue:
    """Thread-safe queue wrapper for inference requests."""

    def __init__(self):
        self._queue = queue.Queue()

    def enqueue(self, item):
        """Add an item to the queue."""
        self._queue.put(item)

    def dequeue(self, block=True, timeout=None):
        """Remove and return an item from the queue."""
        return self._queue.get(block=block, timeout=timeout)

    def size(self):
        """Return the current queue size."""
        return self._queue.qsize()

    # Compatibility helpers -----------------------------------------------
    def put(self, item):
        self.enqueue(item)

    def get(self, block=True, timeout=None):
        return self.dequeue(block=block, timeout=timeout)

    def get_nowait(self):
        return self._queue.get_nowait()

    def qsize(self):
        return self.size()

    def empty(self):
        return self._queue.empty()


# Backwards compatible global queue instance
globalRequestQueue = RequestQueue()

def get_queue_size():
    return globalRequestQueue.size()
