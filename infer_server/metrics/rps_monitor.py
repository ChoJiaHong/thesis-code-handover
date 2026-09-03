import threading
import time
import logging
import os
import csv
from metrics.registry import monitorRegistry

class RPSMonitor:
    def __init__(self, interval=1.0, csv_log_path="logs/rps_stats.csv"):
        self.interval = interval
        self.counter = 0
        self.last_rps = 0
        self.lock = threading.Lock()
        self.csv_log_path = csv_log_path
        self._init_log()

    def _init_log(self):
        os.makedirs(os.path.dirname(self.csv_log_path), exist_ok=True)
        if not os.path.exists(self.csv_log_path):
            with open(self.csv_log_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "rps"])

    def increment(self):
        with self.lock:
            self.counter += 1

    def start(self):
        def loop():
            while True:
                time.sleep(self.interval)
                with self.lock:
                    rps = self.counter
                    self.counter = 0
                    self.last_rps = rps
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                logging.info("[Monitor] Current time = %s, rps=%s", timestamp, rps)
                with open(self.csv_log_path, "a", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([timestamp, rps])
                prometheus = monitorRegistry.get("prometheus")
                if prometheus:
                    prometheus.update_rps(rps)
        threading.Thread(target=loop, daemon=True).start()

    def get_last_rps(self):
        with self.lock:
            return self.last_rps

