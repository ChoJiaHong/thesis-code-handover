import os
import csv
import threading
import time
import logging
from datetime import datetime


class SystemMetricsWriter:
    """Background writer for global system metrics."""

    def __init__(self, queue, rps_monitor=None, batch_processor=None, interval=1.0,
                 log_path="logs/system_metrics.csv"):
        self.queue = queue
        self.rps_monitor = rps_monitor
        self.batch_processor = batch_processor
        self.interval = interval
        self.log_path = log_path
        self.logger = logging.getLogger(__name__)
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        self.fieldnames = [
            "timestamp",
            "queue_size",
            "rps",
            "batch_size",
            "batch_efficiency",
            "gpu_util"
        ]

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _get_gpu_util(self):
        try:
            import torch
            if torch.cuda.is_available():
                util_fn = getattr(torch.cuda, "utilization", None)
                if callable(util_fn):
                    return util_fn()
        except Exception:
            pass
        return None

    def _run(self):
        is_new = not os.path.exists(self.log_path)
        while True:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            queue_size = self.queue.qsize() if self.queue else 0
            rps = self.rps_monitor.get_rps() if self.rps_monitor else 0
            last_batch = (
                self.batch_processor.get_last_batch_size()
                if self.batch_processor else 0
            )
            batch_eff = None
            if self.batch_processor:
                total_size = getattr(self.batch_processor, "batch_size", None)
                if total_size:
                    batch_eff = last_batch / total_size
            gpu_util = self._get_gpu_util()
            row = {
                "timestamp": timestamp,
                "queue_size": queue_size,
                "rps": rps,
                "batch_size": last_batch,
                "batch_efficiency": batch_eff,
                "gpu_util": gpu_util,
            }
            try:
                with open(self.log_path, "a", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=self.fieldnames)
                    if is_new:
                        writer.writeheader()
                        is_new = False
                    writer.writerow(row)
            except Exception as e:
                self.logger.error("Failed to write system metrics: %s", e)
            time.sleep(self.interval)
