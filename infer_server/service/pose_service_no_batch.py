from datetime import datetime
from proto import pose_pb2_grpc
from proto import pose_pb2
from model.batch_worker import BatchWorker
from utils.logger import logger_context, get_logger
from processor.preprocessor import PosePreprocessor
from processor.postprocessor import PosePostprocessor
from metrics.registry import monitorRegistry
from infra.request_queue import RequestQueue
from core.droppable_processor import DroppableSingleProcessor
from core.request_dropper import RequestTimeoutError


class PoseDetectionServiceNoBatch(pose_pb2_grpc.MirrorServicer):
    """gRPC service that processes each request without batching."""

    def __init__(self, request_queue: RequestQueue):
        self.worker = BatchWorker()
        self.preprocessor = PosePreprocessor()
        self.postprocessor = PosePostprocessor()
        self.processor = DroppableSingleProcessor(self.worker)
        self.logger = get_logger(__name__)
        self.queue = request_queue

    def SkeletonFrame(self, request, context):
        rps = monitorRegistry.get("rps")
        if rps:
            rps.increment()

        # enqueue request for queue size monitoring
        self.queue.enqueue(1)
        enqueue_time = self.processor.mark()

        client_ip = context.peer().split(":")[-1].replace("ipv4/", "")
        with logger_context() as logger:
            logger.set_mark("start")
            logger.set("client_ip", client_ip)
            logger.set("receive_ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
            logger.update({
                "batch_size": 1,
                "trigger_type": "single",
                "trigger_time_ms": 0,
            })

            with logger.phase("preprocess"):
                frame = self.preprocessor.process(request.image_data)

            try:
                with logger.phase("inference"):
                    result = self.processor.predict(frame, enqueue_time)
            except RequestTimeoutError:
                processed = ""
                logger.update({"dropped": True})
                return pose_pb2.FrameResponse(skeletons=processed)
            else:
                with logger.phase("postprocess"):
                    processed = self.postprocessor.process(result)

            logger.write()

            completion = monitorRegistry.get("completion")
            if completion:
                completion.increment()

            response = pose_pb2.FrameResponse(skeletons=processed)

        if not self.queue.empty():
            try:
                self.queue.dequeue(block=False)
            except Exception:
                pass

        return response

