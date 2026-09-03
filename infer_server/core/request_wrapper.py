import time
import queue
from datetime import datetime

class RequestWrapper:
    def __init__(self, frame):
        self.frame = frame
        self.result_queue = queue.Queue()
        self.enqueue_time = time.time()
        self.receive_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self.logger = None  # 在外部由 PoseService 注入
        self.batch_id = None
