import time
import grpc
from metrics.registry import monitorRegistry
from proto import pose_pb2
from proto import pose_pb2_grpc


class MaxLoadEstimator:
    def __init__(self, target="localhost:50052", duration=5.0, step=5, max_rps=50):
        self.target = target
        self.duration = duration
        self.step = step
        self.max_rps = max_rps

    def _run_step(self, stub, rps):
        interval = 1.0 / rps
        end = time.time() + self.duration
        request = pose_pb2.FrameRequest(image_data=b"")
        while time.time() < end:
            stub.SkeletonFrame(request)
            time.sleep(interval)

    def estimate(self):
        channel = grpc.insecure_channel(self.target)
        stub = pose_pb2_grpc.MirrorStub(channel)

        queue_monitor = monitorRegistry.get("queue")
        if queue_monitor is None:
            raise RuntimeError("QueueSizeMonitor is not registered")

        best_rps = 0
        current = self.step
        while current <= self.max_rps:
            self._run_step(stub, current)
            time.sleep(queue_monitor.report_interval)
            stats = queue_monitor.get_recent_stats()
            if not stats or stats["zero_ratio"] < 80:
                break
            best_rps = current
            current += self.step

        return best_rps
