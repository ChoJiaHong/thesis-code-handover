import numpy as np
import cv2
import base64
from utils.timing import timing_decorator

class Preprocessor:
    def process(self, image_data): raise NotImplementedError

class PosePreprocessor(Preprocessor):
    @timing_decorator("Preprocess")
    def process(self, image_data):
        img_array = np.frombuffer(image_data, np.uint8)
        return cv2.imdecode(img_array, cv2.IMREAD_COLOR)


class GesturePreprocessor(Preprocessor):
    """Decode base64-encoded image bytes and prepare RGB frame."""

    @timing_decorator("Preprocess")
    def process(self, image_data):
        img_bytes = base64.b64decode(image_data)
        img_array = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        img = cv2.resize(img, (540, 310))
        return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
