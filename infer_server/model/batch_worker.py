import time
import numpy as np
import logging
from ultralytics import YOLO
from config import settings
from utils.timing import timing_decorator

logger = logging.getLogger(__name__)

class BatchWorker:
    def __init__(self):
        self.model = YOLO(settings.weights)
        self._warmup()

    def _warmup(self, n_runs: int = 3):
        """
        在 server 接受真實請求之前，用假圖暖機 GPU。
        消除 CUDA kernel 編譯、cuDNN benchmark、GPU 記憶體分配造成的第一幀高延遲。
        使用與真實輸入相同的尺寸（img_resize_h × img_resize_w）。
        """
        h, w = settings.img_resize_h, settings.img_resize_w
        dummy = np.zeros((h, w, 3), dtype=np.uint8)
        logger.info(f"[Warmup] 開始暖機，圖像尺寸={w}x{h}，跑 {n_runs} 次...")
        for i in range(n_runs):
            t0 = time.perf_counter()
            self.model([dummy], device=settings.device,
                       conf=settings.conf_thres, iou=settings.iou_thres)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.info(f"[Warmup] run {i+1}/{n_runs}: {elapsed_ms:.1f} ms")
        logger.info("[Warmup] 完成，server 已就緒")

    @timing_decorator("Batch Prediction")
    def predict(self, batch_images):
        return self.model(batch_images, device=settings.device,
                          conf=settings.conf_thres, iou=settings.iou_thres)
