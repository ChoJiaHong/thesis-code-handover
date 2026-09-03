from concurrent.futures import ThreadPoolExecutor
import threading
import time

from model.batch_worker import BatchWorker
from metrics.event_bus import event_bus
from config import settings

_thread_local = threading.local()

class PredictThreadPool:
    """Thread pool that lazily creates a BatchWorker per thread."""

    def __init__(self, num_workers: int | None = None):
        self.num_workers = num_workers or settings.num_workers
        self.executor = ThreadPoolExecutor(max_workers=self.num_workers)

    def _get_worker(self) -> BatchWorker:
        if not hasattr(_thread_local, "worker"):
            _thread_local.worker = BatchWorker()
        return _thread_local.worker

    def _predict(self, batch_images, wrappers, trigger_time):
        worker = self._get_worker()
        start = time.time()
        with wrappers[0].logger.phase("inference"):
            results = worker.predict(batch_images)
        end = time.time()
        print(
            f"[Batch] Inference: {(end - start)*1000:.2f} ms, "
            f"Total Cycle: {(time.time() - (start - trigger_time))*1000:.2f} ms"
        )
        return results

    def _on_complete(self, future, wrappers):
        results = future.result()
        for wrapper, result in zip(wrappers, results):
            wrapper.result_queue.put(result)
        event_bus.emit("batch_processed", batch_size=len(wrappers))

    def submit(self, batch_images, wrappers, trigger_time):
        future = self.executor.submit(self._predict, batch_images, wrappers, trigger_time)
        future.add_done_callback(lambda fut, w=wrappers: self._on_complete(fut, w))
        return future
