import threading
import time


class LatencyMonitor:
    """Tracks per-request total_ms and exposes average latency over a sliding window."""

    def __init__(self, window_seconds: float = 5.0):
        self._lock = threading.Lock()
        self._window = window_seconds
        self._samples: list = []  # list of (timestamp, ms)

    def record(self, ms: float):
        now = time.time()
        with self._lock:
            self._samples.append((now, ms))
            cutoff = now - self._window
            self._samples = [(t, v) for t, v in self._samples if t >= cutoff]

    def get_avg(self) -> float:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            recent = [v for t, v in self._samples if t >= cutoff]
        return round(sum(recent) / len(recent), 2) if recent else 0.0

    def get_stats(self) -> dict:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            recent = [v for t, v in self._samples if t >= cutoff]
        if not recent:
            return {"avg_ms": 0.0, "count": 0}
        return {
            "avg_ms": round(sum(recent) / len(recent), 2),
            "count": len(recent),
        }

    def start(self):
        pass  # no background thread needed
