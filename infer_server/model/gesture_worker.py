import time
import logging
import numpy as np
from vision.ssd.mobilenetv1_ssd import (
    create_mobilenetv1_ssd, create_mobilenetv1_ssd_predictor,
)
from vision.utils.misc import Timer
from config import settings

logger = logging.getLogger(__name__)


class GestureWorker:
    """Load the SSD gesture model and run inference."""

    def __init__(self):
        label_path = getattr(settings, "gesture_label_path", "voc-model-labels.txt")
        self.class_names = [name.strip() for name in open(label_path).readlines()]
        model_path = getattr(settings, "gesture_weights", "mb1-ssd-best.pth")
        self.net = create_mobilenetv1_ssd(len(self.class_names), is_test=True)
        self.net.load(model_path)
        self.predictor = create_mobilenetv1_ssd_predictor(self.net, candidate_size=200)
        self.timer = Timer()
        self._warmup()

    def _warmup(self, n_runs: int = 3):
        dummy = np.zeros((310, 540, 3), dtype=np.uint8)
        logger.info(f"[Warmup] Gesture model 暖機，跑 {n_runs} 次...")
        for i in range(n_runs):
            t0 = time.perf_counter()
            self.predictor.predict(dummy, 10, 0.8)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.info(f"[Warmup] run {i+1}/{n_runs}: {elapsed_ms:.1f} ms")
        logger.info("[Warmup] Gesture model 暖機完成")

    def predict(self, image):
        """Run inference on a single RGB image."""
        self.timer.start()
        boxes, labels, probs = self.predictor.predict(image, 10, 0.8)
        self.timer.end()
        return boxes, labels, probs
