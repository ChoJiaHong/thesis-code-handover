import threading
import time
import os
import csv
import logging
from metrics.registry import monitorRegistry
from collections import deque
from datetime import datetime

logger = logging.getLogger(__name__)

class QueueSizeMonitor:
    def __init__(self, queue, sample_interval=0.001, report_interval=1.0,
                 csv_log_path="logs/queue_stats.csv",
                 txt_log_path="logs/queue_report.txt"):
        self.queue = queue
        self.sample_interval = sample_interval
        self.report_interval = report_interval
        self.samples = deque()
        self.lock = threading.Lock()
        self.csv_log_path = csv_log_path
        self.txt_log_path = txt_log_path
        self._init_logs()

    def _init_logs(self):
        os.makedirs(os.path.dirname(self.csv_log_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.txt_log_path), exist_ok=True)
        if not os.path.exists(self.csv_log_path):
            with open(self.csv_log_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "avg", "max", "min", "latest", "zero_count", "zero_ratio"])

    def start(self):
        threading.Thread(target=self._sample_loop, daemon=True).start()
        threading.Thread(target=self._report_loop, daemon=True).start()

    def _sample_loop(self):
        while True:
            size = self.queue.qsize()
            timestamp = time.time()
            with self.lock:
                self.samples.append((timestamp, size))
            time.sleep(self.sample_interval)

    def _report_loop(self):
        while True:
            time.sleep(self.report_interval)
            now = time.time()
            cutoff = now - self.report_interval

            with self.lock:
                recent = [size for ts, size in self.samples if ts >= cutoff]
                self.samples = deque((ts, size) for ts, size in self.samples if ts >= cutoff)

            if not recent:
                continue

            avg = sum(recent) / len(recent)
            max_val = max(recent)
            min_val = min(recent)
            latest = recent[-1]
            zero_count = sum(1 for s in recent if s == 0)
            zero_ratio = zero_count / len(recent) * 100
            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # formatted for console and txt log
            line = (f"[QueueMonitor] {timestamp_str} | "
                    f"avg={avg:>5.2f} | max={max_val:>2} | min={min_val:>2} | "
                    f"latest={latest:>2} | zero={zero_count:>3} | zero_ratio={zero_ratio:>6.2f}%")
            logger.info(line)

            # append to txt log
            with open(self.txt_log_path, "a") as f:
                f.write(line + "\n")

            # append to csv log
            with open(self.csv_log_path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([timestamp_str, avg, max_val, min_val, latest, zero_count, round(zero_ratio, 2)])

            prometheus = monitorRegistry.get("prometheus")
            if prometheus:
                prometheus.update_queue({
                    "avg": avg,
                    "max": max_val,
                    "min": min_val,
                    "latest": latest,
                    "zero_ratio": zero_ratio,
                })

    def get_recent_stats(self):
        now = time.time()
        cutoff = now - self.report_interval
        with self.lock:
            recent = [size for ts, size in self.samples if ts >= cutoff]
        if not recent:
            return None
        avg = sum(recent) / len(recent)
        max_val = max(recent)
        min_val = min(recent)
        latest = recent[-1]
        zero_count = sum(1 for s in recent if s == 0)
        zero_ratio = zero_count / len(recent) * 100
        return {
            "avg": avg,
            "max": max_val,
            "min": min_val,
            "latest": latest,
            "zero_ratio": zero_ratio,
        }
