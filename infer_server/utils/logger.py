import time
import csv
import os
import logging
from contextlib import contextmanager
from uuid import uuid4
from datetime import datetime

# basic application logging configuration
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "[%(levelname)s][%(name)s][%(asctime)s] %(message)s"
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format=LOG_FORMAT)

def get_logger(name=None):
    """Return a module-level logger."""
    return logging.getLogger(name)

LOG_PATH = "logs/request_trace.csv"
FIELDNAMES = [
    "request_id", "client_ip", "batch_size",
    "wait_ms", "trigger_type", "trigger_time_ms",
    "inference_ms", "preprocess_ms", "postprocess_ms", "total_ms",
    "receive_ts", "batch_id", "start_ts", "end_ts", "dropped"
]

class RequestLogger:
    def __init__(self):
        self.request_id = uuid4().hex[:8]
        self.start_times = {}
        self.fields = {
            "request_id": self.request_id,
            "wait_ms": 0,
            "trigger_time_ms": 0,
            "inference_ms": 0,
            "preprocess_ms": 0,
            "postprocess_ms": 0,
            "total_ms": 0,
            "receive_ts": "",
            "batch_id": None,
            "start_ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "end_ts": "",
            "dropped": False,
        }
        self.time_ref = {}
        self._start_total = time.time()

    def set(self, key, value):
        self.fields[key] = value

    def update(self, kv_dict):
        self.fields.update(kv_dict)

    def set_mark(self, label):
        self.time_ref[label] = time.time()
        
    def get_mark(self, name: str):
        return self.time_ref.get(name)


    def duration(self, label):
        if label in self.time_ref:
            return (time.time() - self.time_ref[label]) * 1000
        return None
    
    def print_summary(self):
        req_log = logging.getLogger("request")
        req_log.info("[RequestLogger Summary]")
        for k, v in self.fields.items():
            req_log.info("  %s: %s", k, v)


    @contextmanager
    def phase(self, name):
        start = time.time()
        yield
        end = time.time()
        self.fields[f"{name}_ms"] = (end - start) * 1000

    def write(self):
        self.fields["total_ms"] = (time.time() - self._start_total) * 1000
        self.fields["end_ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        is_new = not os.path.exists(LOG_PATH)
        with open(LOG_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow(self.fields)

@contextmanager
def logger_context():
    logger = RequestLogger()
    try:
        yield logger
    finally:
        pass
