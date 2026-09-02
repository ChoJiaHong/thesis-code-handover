import threading
from proto import pose_pb2_grpc
from proto import pose_pb2
from model.batch_worker import BatchWorker
from processor.batch_processor import BatchProcessor
from batch_config import BatchConfig, global_batch_config
from core.request_wrapper import RequestWrapper
from infra.request_queue import RequestQueue
from utils.logger import logger_context, get_logger
from processor.preprocessor import PosePreprocessor
from processor.postprocessor import PosePostprocessor
from metrics.registry import monitorRegistry

class PoseDetectionService(pose_pb2_grpc.MirrorServicer):
    def __init__(self, request_queue: RequestQueue, config: BatchConfig = global_batch_config):
        self.worker = BatchWorker()
        self.queue = request_queue
        self.processor = BatchProcessor(
            worker=self.worker,
            queue=self.queue,
            config=config
        )
        self.preprocessor = PosePreprocessor()
        self.postprocessor = PosePostprocessor()
        self.logger = get_logger(__name__)
        threading.Thread(target=self.processor.run_forever, daemon=True).start()

    def SkeletonFrame(self, request, context):
        
        rps = monitorRegistry.get("rps")
        if rps:
            rps.increment()
            
        client_ip = context.peer().split(":")[-1].replace("ipv4/", "")
        with logger_context() as logger:
            logger.set_mark("start")
            logger.set("client_ip", client_ip)

            with logger.phase("preprocess"):
                frame = self.preprocessor.process(request.image_data)

            wrapper = RequestWrapper(frame)
            wrapper.logger = logger  # 將 logger 傳入 wrapper
            logger.set("receive_ts", wrapper.receive_ts)
            self.queue.enqueue(wrapper)
            self.logger.info(
                "Request %s enqueued from %s | size=%d",
                logger.request_id,
                client_ip,
                self.queue.size(),
            )

            try:
                result = wrapper.result_queue.get(timeout=2.0)
                with logger.phase("postprocess"):
                    processed = self.postprocessor.process(result)
            except Exception as e:
                self.logger.error("Timeout waiting for result for %s: %s", logger.request_id, e)
                processed = ""

            wrapper.logger.write()

            completion = monitorRegistry.get("completion")
            if completion:
                completion.increment()

            return pose_pb2.FrameResponse(skeletons=processed)
