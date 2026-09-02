import numpy as np
import cv2
import json
from utils.timing import timing_decorator
from utils.landmarks import yolo_result2landmarks

@timing_decorator("Preprocess")
def preprocess_image(image_data):
    img_array = np.frombuffer(image_data, np.uint8)
    return cv2.imdecode(img_array, cv2.IMREAD_COLOR)

@timing_decorator("Postprocess")
def postprocess_result(result):
    skeletons = []
    for i, det in enumerate(result):
        landmarks = yolo_result2landmarks(det.keypoints)
        skeletons.append({"id": i, "keypoints": landmarks})
    return json.dumps(skeletons)

