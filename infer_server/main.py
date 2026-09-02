import grpc
from concurrent import futures
import time
from utils.logger import get_logger
import threading
import uvicorn
from admin_api import app as admin_app

from service.pose_service_no_batch import PoseDetectionServiceNoBatch
from service.gesture_service import GestureDetectionService
from batch_config import global_batch_config
from grpc_health.v1 import health_pb2_grpc, health_pb2
from proto import pose_pb2_grpc
from proto import gesture_pb2_grpc
from config import settings


#----------導入註冊服務----------------
from metrics import registry
from infra.request_queue import RequestQueue
from metrics.registry import monitorRegistry
from metrics.rps_monitor import RPSMonitor
from metrics.completion_monitor import CompletionMonitor
from metrics.queue_monitor import QueueSizeMonitor
from metrics.prometheus_exporter import PrometheusExporter
from metrics.latency_monitor import LatencyMonitor

# Shared queue for all incoming requests
request_queue = RequestQueue()

#--------------------------------

class HealthServicer(health_pb2_grpc.HealthServicer):
    def Check(self, request, context):
        return health_pb2.HealthCheckResponse(status=health_pb2.HealthCheckResponse.SERVING)

def start_admin_api():
    def run_api():
        uvicorn.run(admin_app, host="0.0.0.0", port=8000)
    threading.Thread(target=run_api, daemon=True).start()

def serve():
    logger = get_logger(__name__)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=300))
    health_pb2_grpc.add_HealthServicer_to_server(HealthServicer(), server)
    if settings.service == "pose":
        pose_pb2_grpc.add_MirrorServicer_to_server(
            # PoseDetectionService(request_queue, config=global_batch_config),
            PoseDetectionServiceNoBatch(request_queue),
            server
        )
    elif settings.service == "gesture":
        gesture_pb2_grpc.add_GestureRecognitionServicer_to_server(
            GestureDetectionService(request_queue),
            server
        )
    else:
        raise ValueError(f"Unknown service type: {settings.service}")
    server.add_insecure_port('[::]:' + settings.gRPC_port)
    start_admin_api()
    server.start()
    logger.info("gRPC server running on port %s", settings.gRPC_port)
    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        logger.info("Shutting down server")
        server.stop(0)

if __name__ == "__main__":
    #----------服務註冊位置-------------------
    # metrics/registry.py（繼續）
    monitorRegistry.register(name="rps", instance=RPSMonitor(interval=1.0))
    monitorRegistry.register(name="completion", instance=CompletionMonitor(interval=1.0))
    monitorRegistry.register(name="queue", instance=QueueSizeMonitor(request_queue, sample_interval=0.001, report_interval=1.0))
    monitorRegistry.register(name="latency", instance=LatencyMonitor(window_seconds=5.0))
    monitorRegistry.register(name="prometheus", instance=PrometheusExporter(port=8001))
    monitorRegistry.start_all()
    #----------------------------------------
    serve()
