import time
import queue
from utils.logger import logger_context, get_logger
from core.request_wrapper import RequestWrapper
from batch_config import global_batch_config

class BatchProcessor:
    def __init__(self, worker, queue, config=global_batch_config):
        self.worker = worker
        self.queue = queue
        self.config = config
        self.logger = get_logger(__name__)
        self.batch_counter = 0

    def run_forever(self):
        while True:
            wrappers = self._collect_batch()
            if wrappers:
                self._run_inference(wrappers)

    def _collect_batch(self):
        batch_images = []
        wrappers = []
        start_time = time.time()

        batch_size = self.config.batch_size
        timeout = self.config.queue_timeout
        #print(f"[BatchProcessor] Using batch_size={batch_size}, timeout={timeout}")
        while len(batch_images) < batch_size and (time.time() - start_time) < timeout:
            try:
                wrapper = self.queue.get(timeout=0.01)
                batch_images.append(wrapper.frame)
                wrappers.append(wrapper)
            except queue.Empty:
                continue

        self.trigger_type = "full batch" if len(wrappers) == batch_size else "timeout"
        self.trigger_time = time.time() - start_time
        if wrappers:
            self.logger.info(
                "Batch collected size=%d trigger=%s wait=%.3f", len(wrappers),
                self.trigger_type, self.trigger_time
            )
        return wrappers

    def _run_inference(self, wrappers):
        self.batch_counter += 1
        current_batch_id = self.batch_counter
        for wrapper in wrappers:
            wrapper.batch_id = current_batch_id
            wrapper.logger.set_mark("infer_start")
            wrapper.logger.update({
                "batch_size": len(wrappers),
                "trigger_type": self.trigger_type,
                "trigger_time_ms": self.trigger_time * 1000,
                "batch_id": current_batch_id,
            })

        batch_images = [w.frame for w in wrappers]
        infer_start = time.time()

        self.logger.info("Inference start batch_size=%d", len(wrappers))
        with wrappers[0].logger.phase("inference"):
            results = self.worker.predict(batch_images)

        infer_end = time.time()
        self.logger.info(
            "Inference done %.2f ms, total cycle %.2f ms",
            (infer_end - infer_start)*1000,
            (time.time() - (infer_start - self.trigger_time))*1000,
        )

        self._dispatch_results(wrappers, results)

    def _dispatch_results(self, wrappers, results):
        self.logger.debug("Dispatching results to %d wrappers", len(wrappers))
        for wrapper, result in zip(wrappers, results):
            wrapper.result_queue.put(result)
